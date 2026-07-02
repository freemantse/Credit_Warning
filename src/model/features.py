"""
Stage 2 — assemble the rating-migration model's feature matrix.

Grain: one row per (cik, period_end, agency), to join 1:1 with rating_labels (the
targets). Every FEATURE is known AS OF period_end — no lookahead; only the TARGETS
(label_*m, distress_12m) look forward, which is intentional. The financial features
come from data already stored point-in-time at ingest (ratios extracted with
filed_before, the per-period implied rating); the only derived-over-time features
(YoY deltas, the Stage-0 outlook) are computed causally from periods ≤ period_end.

Layered so the core is pure and unit-testable without a DB:
  ratio_features()        — the 9 ratios + their YoY deltas for one period.
  build_issuer_features() — per-period feature dict for one issuer (ratios, score,
                            implied rating, outlook), strictly causal.
  merge_labels()          — cross with rating_labels per agency; add the agency
                            rating, the implied-vs-agency gap, time-in-rating, targets.
  load_training_matrix()  — thin DB orchestrator pulling the grouped store reads.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from src.extract import RatioResult
from src.score import compute_score, ScoreConfig, DEFAULT_CONFIG
from src.rating import rating_outlook, OUTLOOK_DEFAULT, OutlookConfig


# The nine deterministic ratios, in canonical order. Each contributes a level and a
# YoY-delta feature.
RATIO_FEATURES = [
    "leverage", "interest_coverage", "free_cash_flow", "fcf_margin", "ebitda_margin",
    "liquidity", "cash_flow_to_debt", "debt_to_assets",
]

ID_COLUMNS = ["cik", "period_end", "agency"]
TARGET_COLUMNS = ["label_3m", "label_6m", "label_12m", "notch_change_12m", "distress_12m"]

# Rating agency → integer code for the `agency_code` feature. The model trains pooled
# over all agencies (one row per (cik, period, agency)); this code lets the booster
# condition on WHICH agency a row/label came from — a real signal, since e.g. Egan-Jones
# (EJR) downgrades at roughly twice Moody's (MDY) rate and reverses faster. Mirrors the
# agencies in src.ratings.labels (SPI carried for forward-compat).
#
# It is fed as an ORDINAL integer, UNCONSTRAINED (FEATURE_DIRECTIONS["agency_code"] = 0),
# NOT declared categorical: sklearn 1.4's HistGradientBoosting forbids mixing monotonic
# constraints with categorical_features, and we keep the credit-coherent monotone
# directions on the other features. With only 3–4 agencies the booster still isolates
# any single one via threshold splits (EJR at the top of the code range is separated by
# a single split). Codes must stay STABLE — they are baked into persisted model vintages.
AGENCY_CODE: dict[str, int] = {"MDY": 0, "FTC": 1, "EJR": 2, "SPI": 3}

# Forward-looking MARKET features (Merton distance-to-default + equity momentum /
# volatility / market leverage), joined per (cik, period_end) by add_market_features from
# data/market_features.csv (precomputed point-in-time by scripts.build_market_features).
# They lift the downgrade + upgrade heads; the DISTRESS head EXCLUDES them
# (dataset.make_xy masks them to NaN) because they regressed its precision — see the
# market-features experiment. Missing issuers get NaN (booster-tolerant).
MARKET_FEATURES = ["distance_to_default", "equity_vol", "equity_ret_12m", "market_leverage"]

# Full ordered feature list (the model's X). Built once so train/predict agree.
FEATURE_COLUMNS = (
    RATIO_FEATURES
    + [f"{r}_yoy" for r in RATIO_FEATURES]
    + [
        "implied_rating_index", "implied_rating_index_yoy", "financial_risk_index",
        "stress_score", "stress_score_yoy",
        "maturity_near_term_pct", "covenant_near_limit_count", "material_provision_count",
        "outlook_trend_pressure",
        # added by merge_labels (depend on the agency):
        "agency_rating_index", "implied_vs_agency_gap", "time_in_rating_months",
        "agency_code",   # categorical: which agency this row/label comes from
    ]
    + MARKET_FEATURES
)

# Monotone direction of each feature w.r.t. DOWNGRADE probability, for the
# auditability-first LightGBM constraints in Stage 3 (+1 = increasing the feature
# can only raise modeled downgrade risk, -1 = only lower it, 0 = unconstrained).
# Higher rating_index / score / leverage = worse credit → +1; coverage/margins/
# liquidity = better credit → -1. Deltas mirror their level's sign.
FEATURE_DIRECTIONS: dict[str, int] = {
    "leverage": 1, "interest_coverage": -1, "free_cash_flow": -1, "fcf_margin": -1,
    "ebitda_margin": -1, "liquidity": -1, "cash_flow_to_debt": -1, "debt_to_assets": 1,
    "implied_rating_index": 1, "financial_risk_index": 1, "stress_score": 1,
    "maturity_near_term_pct": 1, "covenant_near_limit_count": 1,
    "material_provision_count": 1, "outlook_trend_pressure": 1,
    "implied_vs_agency_gap": 1,
    # The starting agency rating and time-in-rating are non-monotone → unconstrained.
    "agency_rating_index": 0, "time_in_rating_months": 0,
    # agency identity is an unordered categorical → must be unconstrained (0).
    "agency_code": 0,
}
# Deltas inherit their level's monotone direction.
for _r in RATIO_FEATURES:
    FEATURE_DIRECTIONS[f"{_r}_yoy"] = FEATURE_DIRECTIONS[_r]
FEATURE_DIRECTIONS["implied_rating_index_yoy"] = 1
FEATURE_DIRECTIONS["stress_score_yoy"] = 1
# Market features: higher distance-to-default / equity return = safer (−1); higher
# equity volatility / market leverage = riskier (+1). Credit-coherent, so constrained.
FEATURE_DIRECTIONS["distance_to_default"] = -1
FEATURE_DIRECTIONS["equity_ret_12m"] = -1
FEATURE_DIRECTIONS["equity_vol"] = 1
FEATURE_DIRECTIONS["market_leverage"] = 1

# The fundamentals whose credit-coherent monotone direction is ALWAYS enforced for
# auditability. The Lever-4 relax sweep (dataset.monotone_constraints(relax_secondary=))
# may unconstrain everything else (agency rating, gap, time-in-rating, maturity,
# covenants, provisions, outlook) to let the booster learn interactions there.
CORE_CONSTRAINED_FEATURES = frozenset(
    set(RATIO_FEATURES)
    | {f"{r}_yoy" for r in RATIO_FEATURES}
    | {"implied_rating_index", "implied_rating_index_yoy", "financial_risk_index",
       "stress_score", "stress_score_yoy"}
)


def _ratio_value(period_ratios: dict[str, Any], name: str) -> float | None:
    """Pull a ratio's value from a stored grouped dict ({name: {value, ...}}), or None."""
    d = period_ratios.get(name)
    return d.get("value") if isinstance(d, dict) else None


def _delta(now: float | None, prev: float | None) -> float | None:
    return (now - prev) if (now is not None and prev is not None) else None


def ratio_features(period_ratios: dict[str, Any], prev_ratios: dict[str, Any] | None) -> dict[str, float | None]:
    """The 9 ratio levels + their YoY deltas (delta None when either side missing)."""
    prev_ratios = prev_ratios or {}
    out: dict[str, float | None] = {}
    for name in RATIO_FEATURES:
        now = _ratio_value(period_ratios, name)
        out[name] = now
        out[f"{name}_yoy"] = _delta(now, _ratio_value(prev_ratios, name))
    return out


def _ratio_results_from_stored(period_ratios: dict[str, Any], period_end: str) -> dict[str, RatioResult]:
    """
    Reconstruct RatioResult objects from stored grouped ratio dicts so compute_score
    can consume them. Skips missing ratios (value None) — same contract as the API's
    _to_ratio_results: an absent ratio contributes no stress points.
    """
    out: dict[str, RatioResult] = {}
    for name, data in period_ratios.items():
        if not isinstance(data, dict) or data.get("value") is None:
            continue
        out[name] = RatioResult(
            name=name,
            value=data["value"],
            inputs=data.get("inputs", {}) or {},
            source_tags=data.get("source_tags", {}) or {},
            period_end=period_end,
        )
    return out


def build_issuer_features(
    periods: list[str],
    *,
    ratios_by_period: dict[str, dict],
    implied_by_period: dict[str, dict] | None = None,
    findings_by_period: dict[str, list] | None = None,
    maturities_by_period: dict[str, dict] | None = None,
    covenants_by_period: dict[str, list] | None = None,
    provisions_by_period: dict[str, list] | None = None,
    config: ScoreConfig | None = None,
    outlook_config: OutlookConfig = OUTLOOK_DEFAULT,
) -> dict[str, dict[str, Any]]:
    """
    Build the as-of-period feature dict for ONE issuer, for every period.

    Strictly causal: each period's features use only that period and earlier ones
    (the prior period for YoY deltas; periods ≤ period_end for the outlook trend).
    Returns {period_end: feature_dict}. The financial side only — agency rating, the
    implied-vs-agency gap, and targets are attached later by merge_labels.
    """
    config = config or ScoreConfig.from_dict(DEFAULT_CONFIG)
    implied_by_period = implied_by_period or {}
    findings_by_period = findings_by_period or {}
    maturities_by_period = maturities_by_period or {}
    covenants_by_period = covenants_by_period or {}
    provisions_by_period = provisions_by_period or {}

    ordered = sorted(periods)            # oldest → newest
    out: dict[str, dict[str, Any]] = {}
    outlook_series: list[dict[str, Any]] = []

    for i, period in enumerate(ordered):
        period_ratios = ratios_by_period.get(period, {})
        prev_ratios = ratios_by_period.get(ordered[i - 1], {}) if i > 0 else {}
        implied = implied_by_period.get(period) or {}
        prev_implied = implied_by_period.get(ordered[i - 1]) if i > 0 else None

        # Stress score for this period (pure compute over already-extracted ratios).
        score_result = compute_score(
            _ratio_results_from_stored(period_ratios, period),
            findings_by_period.get(period, []),
            maturities_by_period.get(period),
            covenants_by_period.get(period, []),
            provisions_by_period.get(period, []),
            config=config,
        )
        score = score_result.score
        prev_score = out[ordered[i - 1]]["stress_score"] if i > 0 else None

        # Causal outlook: extend the series with this period, then derive from ≤ now.
        outlook_series.append({
            "period_end": period,
            "rating_index": implied.get("rating_index"),
            "score": score,
        })
        outlook = rating_outlook(outlook_series, config=outlook_config)

        maturity = maturities_by_period.get(period) or {}
        covenants = covenants_by_period.get(period, [])
        provisions = provisions_by_period.get(period, [])

        row: dict[str, Any] = {
            **ratio_features(period_ratios, prev_ratios),
            "implied_rating_index": implied.get("rating_index"),
            "implied_rating_index_yoy": _delta(
                implied.get("rating_index"),
                (prev_implied or {}).get("rating_index") if prev_implied else None,
            ),
            "financial_risk_index": implied.get("financial_risk_index"),
            "stress_score": score,
            "stress_score_yoy": _delta(score, prev_score),
            "maturity_near_term_pct": maturity.get("near_term_pct"),
            "covenant_near_limit_count": sum(1 for c in covenants if (c or {}).get("near_limit")),
            "material_provision_count": sum(1 for p in provisions if (p or {}).get("is_material")),
            "outlook_trend_pressure": outlook.trend_pressure if outlook else 0,
        }
        out[period] = row

    return out


def _months_between(d_from: str, d_to: str) -> int:
    """Whole-month difference d_to − d_from (negative if d_to precedes d_from)."""
    a = date.fromisoformat(d_from)
    b = date.fromisoformat(d_to)
    return (b.year - a.year) * 12 + (b.month - a.month)


def _time_in_rating(events: list[dict] | None, period_end: str) -> int | None:
    """Months since the last agency action on/before period_end, or None if unknown."""
    if not events:
        return None
    last = None
    for e in events:                      # ascending by effective_date
        if e["effective_date"] <= period_end:
            last = e
        else:
            break
    if last is None:
        return None
    return max(0, _months_between(last["effective_date"], period_end))


def agency_features_asof(
    timeline: list[dict] | None, period_end: str, implied_rating_index: float | None
) -> dict[str, Any]:
    """
    The three agency-conditioning features as of `period_end`, derived from a
    forward-filled agency-event `timeline` (one agency's events, ascending by
    effective_date). This is the SINGLE source of truth so the SCORING path
    (build_scoring_matrix / the migration backtest) computes them identically to the
    TRAINING path (merge_labels) — without it those columns are NaN at serve time and
    the model collapses (train/serve skew):

      agency_rating_index   — the agency rating in effect (rating_asof), or None
      implied_vs_agency_gap — implied_rating_index − agency_rating_index (None if either side missing)
      time_in_rating_months — months since the last agency action ≤ period_end

    Point-in-time and lookahead-free: only events on/before period_end are read.
    """
    from src.ratings.labels import rating_asof

    if not timeline:
        return {"agency_rating_index": None, "implied_vs_agency_gap": None,
                "time_in_rating_months": None}
    idx, _status = rating_asof(timeline, period_end)
    gap = (implied_rating_index - idx) if (
        implied_rating_index is not None and idx is not None
        and not (isinstance(implied_rating_index, float) and implied_rating_index != implied_rating_index)
    ) else None
    return {
        "agency_rating_index": idx,
        "implied_vs_agency_gap": gap,
        "time_in_rating_months": _time_in_rating(timeline, period_end),
    }


def merge_labels(
    features_by_cik: dict[str, dict[str, dict]],
    labels_grouped: dict[str, dict[str, dict[str, dict]]],
    *,
    agency_events_by_cik: dict[str, dict[str, list]] | None = None,
) -> list[dict[str, Any]]:
    """
    Cross the per-issuer financial features with rating_labels, one row per
    (cik, period_end, agency). Adds the agency rating as of period_end, the
    implied-vs-agency gap, time-in-rating, and the targets. Rows are emitted only
    where both a feature row and a label row exist for that (cik, period_end).
    """
    agency_events_by_cik = agency_events_by_cik or {}
    rows: list[dict[str, Any]] = []

    for cik, by_period in labels_grouped.items():
        feats = features_by_cik.get(cik, {})
        for period_end, by_agency in by_period.items():
            feat = feats.get(period_end)
            if feat is None:
                continue
            for agency, label in by_agency.items():
                agency_idx = label.get("rating_index")
                implied_idx = feat.get("implied_rating_index")
                gap = (implied_idx - agency_idx) if (implied_idx is not None and agency_idx is not None) else None
                events = (agency_events_by_cik.get(cik, {}) or {}).get(agency)
                row = {
                    "cik": cik,
                    "period_end": period_end,
                    "agency": agency,
                    **feat,
                    "agency_rating_index": agency_idx,
                    "implied_vs_agency_gap": gap,
                    "time_in_rating_months": _time_in_rating(events, period_end),
                    "agency_code": AGENCY_CODE.get(agency),
                    **{t: label.get(t) for t in TARGET_COLUMNS},
                }
                rows.append(row)
    return rows


def to_dataframe(rows: list[dict[str, Any]]):
    """Build the ordered feature DataFrame (id + features + targets) from merged rows."""
    import pandas as pd

    cols = ID_COLUMNS + FEATURE_COLUMNS + TARGET_COLUMNS
    df = pd.DataFrame(rows)
    # Keep only known columns, in canonical order; tolerate an empty matrix.
    present = [c for c in cols if c in df.columns]
    return df.reindex(columns=present)


_MARKET_FEATURES_CACHE = None


def add_market_features(df, path: str = "data/market_features.csv"):
    """
    Left-join the precomputed point-in-time MARKET_FEATURES onto an assembled matrix by
    (cik, period_end) — the same file feeds train (load_training_matrix) and serve
    (build_scoring_matrix), so both agree. Columns are created (NaN) even when the file
    is absent, so the feature set stays stable. `df` must carry 'cik' and 'period_end'.
    """
    global _MARKET_FEATURES_CACHE
    import pandas as pd
    from pathlib import Path

    if _MARKET_FEATURES_CACHE is None:
        p = Path(path)
        _MARKET_FEATURES_CACHE = (
            pd.read_csv(p, dtype={"cik": str, "period_end": str}) if p.exists()
            else pd.DataFrame(columns=["cik", "period_end", *MARKET_FEATURES])
        )
    mf = _MARKET_FEATURES_CACHE
    if df.empty:
        for c in MARKET_FEATURES:
            if c not in df.columns:
                df[c] = pd.NA
        return df
    keep = ["cik", "period_end", *[c for c in MARKET_FEATURES if c in mf.columns]]
    out = df.merge(mf[keep], on=["cik", "period_end"], how="left")
    for c in MARKET_FEATURES:            # ensure every market column exists even if absent
        if c not in out.columns:
            out[c] = pd.NA
    return out


def load_training_matrix(config: dict | None = None):
    """
    DB orchestrator: pull the grouped store reads and assemble the full training
    matrix as a DataFrame. Thin glue over build_issuer_features + merge_labels.

    The `stress_score` FEATURE is computed with DEFAULT_CONFIG (not the active,
    possibly model-learned config) on purpose: the learned score weights are derived
    FROM this model, so feeding the learned-weight score back in as a feature would
    be circular. Display paths use the learned config; training stays on DEFAULT.
    """
    from src.store import (
        get_ratios_grouped, get_implied_ratings_grouped, get_findings_grouped,
        get_maturities_grouped, get_covenants_grouped, get_loss_provisions_grouped,
        get_rating_labels_grouped, get_agency_ratings_grouped,
    )

    cfg = ScoreConfig.from_dict(config) if config else ScoreConfig.from_dict(DEFAULT_CONFIG)
    ratios = get_ratios_grouped()
    implied = get_implied_ratings_grouped()
    findings = get_findings_grouped()
    maturities = get_maturities_grouped()
    covenants = get_covenants_grouped()
    provisions = get_loss_provisions_grouped()
    labels = get_rating_labels_grouped()
    agency_events = get_agency_ratings_grouped()

    features_by_cik: dict[str, dict[str, dict]] = {}
    for cik in set(ratios) | set(implied):
        features_by_cik[cik] = build_issuer_features(
            sorted(ratios.get(cik, {})),
            ratios_by_period=ratios.get(cik, {}),
            implied_by_period=implied.get(cik, {}),
            findings_by_period=findings.get(cik, {}),
            maturities_by_period=maturities.get(cik, {}),
            covenants_by_period=covenants.get(cik, {}),
            provisions_by_period=provisions.get(cik, {}),
            config=cfg,
        )

    rows = merge_labels(features_by_cik, labels, agency_events_by_cik=agency_events)
    return add_market_features(to_dataframe(rows))


def build_scoring_matrix(agency_events: dict[str, dict[str, list[dict]]] | None = None,
                         *, fill_agency_features: bool = True, per_agency: bool = False):
    """
    Assemble feature rows for every (cik, period_end) WITHOUT requiring labels — for
    live prediction and the migration event backtest.

    The agency-conditioning columns (agency_rating_index, implied_vs_agency_gap,
    time_in_rating_months) plus `agency_code` are populated POINT-IN-TIME from the
    issuer's agency timeline via `agency_features_asof`, matching how merge_labels
    derives them at training time. This closes the train/serve skew that otherwise
    leaves them NaN at scoring (the model was trained with them present, so all-NaN at
    serve collapses its resolution). `agency_events` defaults to a fresh
    get_agency_ratings_grouped() read; pass `fill_agency_features=False` (or empty
    agency_events) to keep them NaN.

    Grain:
      per_agency=False (default) — ONE row per (cik, period_end), conditioned on the
        issuer's best-covered ("primary") agency. Used by the event backtest (which
        re-derives the agency per snapshot) and case-eligibility scans.
      per_agency=True — ONE row per (cik, period_end, covering-agency), each carrying
        that agency's rating features + agency_code. The live-prediction path uses this
        so predict._iter_predict_rows can combine the per-agency probabilities into the
        issuer-level "any-agency deterioration" probability (noisy-OR). Issuers with no
        agency coverage still emit a single row (agency columns NaN).

    Returns a DataFrame with columns [cik, period_end] + FEATURE_COLUMNS, computed
    with DEFAULT_CONFIG (the stress_score feature stays non-circular).
    """
    import pandas as pd
    from src.store import (
        get_ratios_grouped, get_implied_ratings_grouped, get_findings_grouped,
        get_maturities_grouped, get_covenants_grouped, get_loss_provisions_grouped,
    )

    cfg = ScoreConfig.from_dict(DEFAULT_CONFIG)
    ratios = get_ratios_grouped()
    implied = get_implied_ratings_grouped()
    findings = get_findings_grouped()
    maturities = get_maturities_grouped()
    covenants = get_covenants_grouped()
    provisions = get_loss_provisions_grouped()
    if fill_agency_features and agency_events is None:
        from src.store import get_agency_ratings_grouped
        agency_events = get_agency_ratings_grouped()
    agency_events = agency_events or {}

    rows: list[dict[str, Any]] = []
    for cik in set(ratios) | set(implied):
        feats = build_issuer_features(
            sorted(ratios.get(cik, {})),
            ratios_by_period=ratios.get(cik, {}),
            implied_by_period=implied.get(cik, {}),
            findings_by_period=findings.get(cik, {}),
            maturities_by_period=maturities.get(cik, {}),
            covenants_by_period=covenants.get(cik, {}),
            provisions_by_period=provisions.get(cik, {}),
            config=cfg,
        )
        by_agency = (agency_events.get(cik) or {}) if fill_agency_features else {}
        # agencies with actual events, code order (MDY, FTC, EJR, …) for determinism
        covering = [a for a in sorted(by_agency, key=lambda a: AGENCY_CODE.get(a, 99))
                    if by_agency.get(a)]
        for period_end, feat in feats.items():
            base = {"cik": cik, "period_end": period_end, **feat}
            if per_agency and covering:
                for agency in covering:
                    row = dict(base)
                    row.update(agency_features_asof(
                        by_agency[agency], period_end, feat.get("implied_rating_index")))
                    row["agency_code"] = AGENCY_CODE.get(agency)
                    rows.append(row)
            else:
                # Single row: condition on the primary (best-covered) agency, if any.
                primary = max(covering, key=lambda a: len(by_agency[a])) if covering else None
                if primary is not None:
                    base.update(agency_features_asof(
                        by_agency[primary], period_end, feat.get("implied_rating_index")))
                    base["agency_code"] = AGENCY_CODE.get(primary)
                rows.append(base)

    cols = ["cik", "period_end"] + FEATURE_COLUMNS
    return add_market_features(pd.DataFrame(rows).reindex(columns=cols))
