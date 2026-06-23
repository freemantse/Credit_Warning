"""
Stage 2 — assemble the rating-migration model's feature matrix.

Grain: one row per (cik, period_end, agency), to join 1:1 with rating_labels (the
targets). Every FEATURE is known AS OF period_end — no lookahead; only the TARGETS
(label_*m, default_12m) look forward, which is intentional. The financial features
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
    "liquidity", "cash_flow_to_debt", "debt_to_assets", "current_ratio",
]

ID_COLUMNS = ["cik", "period_end", "agency"]
TARGET_COLUMNS = ["label_3m", "label_6m", "label_12m", "notch_change_12m", "default_12m"]

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
    ]
)

# Monotone direction of each feature w.r.t. DOWNGRADE probability, for the
# auditability-first LightGBM constraints in Stage 3 (+1 = increasing the feature
# can only raise modeled downgrade risk, -1 = only lower it, 0 = unconstrained).
# Higher rating_index / score / leverage = worse credit → +1; coverage/margins/
# liquidity = better credit → -1. Deltas mirror their level's sign.
FEATURE_DIRECTIONS: dict[str, int] = {
    "leverage": 1, "interest_coverage": -1, "free_cash_flow": -1, "fcf_margin": -1,
    "ebitda_margin": -1, "liquidity": -1, "cash_flow_to_debt": -1, "debt_to_assets": 1,
    "current_ratio": -1,
    "implied_rating_index": 1, "financial_risk_index": 1, "stress_score": 1,
    "maturity_near_term_pct": 1, "covenant_near_limit_count": 1,
    "material_provision_count": 1, "outlook_trend_pressure": 1,
    "implied_vs_agency_gap": 1,
    # The starting agency rating and time-in-rating are non-monotone → unconstrained.
    "agency_rating_index": 0, "time_in_rating_months": 0,
}
# Deltas inherit their level's monotone direction.
for _r in RATIO_FEATURES:
    FEATURE_DIRECTIONS[f"{_r}_yoy"] = FEATURE_DIRECTIONS[_r]
FEATURE_DIRECTIONS["implied_rating_index_yoy"] = 1
FEATURE_DIRECTIONS["stress_score_yoy"] = 1


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


def load_training_matrix(config: dict | None = None):
    """
    DB orchestrator: pull the grouped store reads and assemble the full training
    matrix as a DataFrame. Thin glue over build_issuer_features + merge_labels.
    """
    from src.store import (
        get_ratios_grouped, get_implied_ratings_grouped, get_findings_grouped,
        get_maturities_grouped, get_covenants_grouped, get_loss_provisions_grouped,
        get_rating_labels_grouped, get_agency_ratings_grouped, get_score_config,
    )

    cfg = ScoreConfig.from_dict(config or get_score_config())
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
    return to_dataframe(rows)
