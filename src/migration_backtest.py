"""
Unified rating-EVENT backtest: does the trained migration model flag an issuer's
upgrade / downgrade / default EARLY?

For each case (issuer + event_type + event_date) it walks the issuer's filing
periods backward from the event and, at each snapshot date T, scores the model
VINTAGE trained before T (no leakage — see src.model.train.train_vintages /
select_vintage) on that period's point-in-time features. The head matching the
event is read (downgrade→downgrade, upgrade→upgrade, default→distress: a default IS a
distress transition, so default cases score against the broadened distress head); a
probability ≥ threshold is a flag. The earliest flag before the event is the catch,
and the months from it to the event are the lead time. `control` cases have no
event — any flag is a false positive.

This mirrors src/backtest.py's point-in-time discipline, but the signal is the ML
model's calibrated probability rather than the deterministic stress score, and the
features come from the lookahead-free scoring matrix (build_scoring_matrix).

Inert until the model has been trained (vintages exist) and agency-rating labels
have been ingested; until then the harness simply reports data_gap.
"""

from __future__ import annotations

from typing import Any, Callable

from src.model.train import select_vintage
from src.ratings.labels import add_months
from src.rating import RATING_SCALE

# event_type → the model head (probability key) that should fire for it. A "default"
# case routes to the broadened "distress" head (D ≥ CCC+ is a distress transition);
# the case event_type string stays "default" — this is just the head it scores against.
EVENT_HEAD = {"downgrade": "downgrade", "upgrade": "upgrade", "default": "distress"}

# How many ~quarterly snapshots to walk back from the event (≈ this/4 years).
DEFAULT_STEPS = 12
# A probability at/above this flags the event. Used only as a fallback when a
# per-head tuned threshold (from data/migration_eval.json) isn't supplied — the
# calibrated heads cluster well below 0.5, so the tuned cutoffs are far lower.
DEFAULT_THRESHOLD = 0.5
# An early warning is a catch with at least this much lead time.
EARLY_MONTHS = 6
# A flag only counts as a catch if it lands within this many months of the event.
# The model's training horizon is 12 months, so a flag 3+ years out isn't a useful
# early warning — it just inflates lead time. Snapshots older than this are dropped.
DEFAULT_MAX_LEAD_MONTHS = 24


def _months_between(d_from: str, d_to: str) -> float:
    from datetime import date
    a, b = date.fromisoformat(d_from), date.fromisoformat(d_to)
    return (b.year - a.year) * 12 + (b.month - a.month) + (b.day - a.day) / 30.44


def _num(v: Any) -> float | None:
    """Coerce a feature value to a JSON-safe float, or None for missing/NaN/inf.

    Scoring-matrix rows come from a pandas DataFrame, so a missing ratio is NaN
    (not None); NaN/inf would serialize to invalid JSON the browser can't parse.
    """
    import math
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _default_loader():
    from src.model.predict import load_model, predict_proba_all
    cache: dict[str, Any] = {}

    def head_prob(path: str, X, head: str) -> float | None:
        bundle = cache.get(path)
        if bundle is None:
            bundle = cache[path] = load_model(path)
        probs = predict_proba_all(bundle, X)
        return float(probs[head][0]) if head in probs else None

    return head_prob


def _default_rating_loader():
    """Return events_for(cik) → {agency: [events asc by date]}, cached per cik.

    Used to forward-fill the issuer's real agency rating onto each snapshot for the
    expanded per-period view. Injectable so tests don't need a live store.
    """
    from src.store import get_agency_ratings_grouped
    cache: dict[str, dict[str, list[dict]]] = {}

    def events_for(cik: str) -> dict[str, list[dict]]:
        key = cik.zfill(10)
        if key not in cache:
            cache[key] = get_agency_ratings_grouped(key).get(key, {})
        return cache[key]

    return events_for


def _rating_timeline(events_by_agency: dict[str, list[dict]], agency: str) -> list[dict]:
    """The rating-event timeline to read for a case: its own agency if present, else
    the agency with the most events (the issuer's best-covered timeline)."""
    if not events_by_agency:
        return []
    if agency and events_by_agency.get(agency):
        return events_by_agency[agency]
    return max(events_by_agency.values(), key=len)


def _case_snapshots(rows, event_date: str, *, steps: int, controls: bool, max_lead_months: int):
    """The (period_end, feature-row) snapshots to score for a case, newest-first.

    Both event cases AND controls use the SAME trailing `max_lead_months` window
    anchored at `event_date` (a control's `event_date` is its pinned anchor) — so a
    control is scored over the same handful of recent snapshots an event gets, not its
    whole history. The old asymmetry (controls = full history ≈12 snaps, events ≈2)
    gave controls ~6x more chances to trip the single-snapshot flag, inflating the FP
    rate. Events use a STRICT cutoff (before the event, no lookahead); a control has no
    event so its anchor period is included.
    """
    rows = sorted(rows, key=lambda r: r["period_end"])
    if not event_date:
        usable = rows
    else:
        earliest = add_months(event_date, -max_lead_months)
        usable = [r for r in rows
                  if (r["period_end"] <= event_date if controls else r["period_end"] < event_date)
                  and r["period_end"] >= earliest]
    return list(reversed(usable[-steps:]))


def run_migration_backtest(
    cases: list[dict[str, Any]],
    scoring_by_cik: dict[str, list[dict[str, Any]]],
    vintages: list[dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    thresholds: dict[str, float] | None = None,
    steps: int = DEFAULT_STEPS,
    early_months: int = EARLY_MONTHS,
    max_lead_months: int = DEFAULT_MAX_LEAD_MONTHS,
    head_prob_fn: Callable[[str, Any, str], float | None] | None = None,
    agency_events_fn: Callable[[str], dict[str, list[dict]]] | None = None,
    feature_columns: list[str] | None = None,
) -> dict[str, Any]:
    """
    Run the event backtest. `scoring_by_cik` maps cik → list of feature-row dicts
    (period_end + the model's feature columns). `vintages` is the walk-forward panel
    from train_vintages. `head_prob_fn(path, X, head)` is injectable for testing;
    by default it loads the joblib vintage and runs the calibrated model.

    `thresholds` maps a model head (downgrade/upgrade/distress) → its tuned flag
    cutoff; heads not listed fall back to `threshold`. A flag only counts when it
    lands within `max_lead_months` of the event (older snapshots are dropped).

    Returns {threshold, thresholds, by_event_type, cases}.
    """
    import pandas as pd
    from src.model.features import FEATURE_COLUMNS, RATIO_FEATURES, agency_features_asof, AGENCY_CODE

    feats = feature_columns or FEATURE_COLUMNS

    def _score_issuer_any(path: str, head: str, row: dict, period: str,
                          by_agency: dict[str, list[dict]], score_agencies: list[str]) -> float | None:
        """
        Issuer-level "any-agency" probability for one snapshot — the shipped signal.
        Scores the model under EACH covering agency (its point-in-time rating features
        + agency_code) and combines them by noisy-OR (1 − ∏(1 − pₐ)), matching
        predict._iter_predict_rows. Issuers with no rating coverage fall back to a
        single agency-less row (agency features NaN) — the pre-agency behavior.
        """
        def _one(agency_feats: dict, agency_code) -> float | None:
            feat_row = {c: row.get(c) for c in feats}
            feat_row.update({k: v for k, v in agency_feats.items() if k in feats})
            if "agency_code" in feats:
                feat_row["agency_code"] = agency_code
            X = pd.DataFrame([feat_row])
            for c in feats:
                X[c] = pd.to_numeric(X[c], errors="coerce")
            return head_prob(path, X, head)

        if not score_agencies:
            return _one({"agency_rating_index": None, "implied_vs_agency_gap": None,
                         "time_in_rating_months": None}, None)
        ps: list[float] = []
        for a in score_agencies:
            af = agency_features_asof(by_agency[a], period, row.get("implied_rating_index"))
            p = _one(af, AGENCY_CODE.get(a))
            if p is not None:
                ps.append(max(0.0, min(1.0, float(p))))
        if not ps:
            return None
        prod = 1.0
        for p in ps:
            prod *= (1.0 - p)
        return 1.0 - prod
    head_prob = head_prob_fn or _default_loader()
    events_for = agency_events_fn or _default_rating_loader()
    head_threshold = thresholds or {}
    results: list[dict[str, Any]] = []

    for case in cases:
        cik = str(case.get("cik", "")).zfill(10)
        et = (case.get("event_type") or "").lower()
        event_date = case.get("event_date") or ""
        base = {
            "case_id": case.get("case_id"), "ticker": case.get("ticker", ""),
            "company_name": case.get("company_name", ""), "event_type": et,
            "event_date": event_date,
        }
        rows = scoring_by_cik.get(cik) or []
        controls = et == "control"
        if not rows or (not controls and not event_date):
            results.append({**base, "status": "data_gap", "trajectory": []})
            continue

        snaps = _case_snapshots(rows, event_date, steps=steps, controls=controls,
                                max_lead_months=max_lead_months)
        if not snaps:
            results.append({**base, "status": "data_gap", "trajectory": []})
            continue

        # Controls have no event; a false positive is the model wrongly flagging a
        # downgrade, so they are scored against the downgrade head.
        head = EVENT_HEAD.get(et) or ("downgrade" if controls else None)
        # The flag cutoff for this case's head (tuned per head; falls back to the
        # legacy single threshold for any head without a tuned value).
        flag_threshold = head_threshold.get(head, threshold) if head else threshold
        # The issuer's agency-rating coverage. `by_agency` drives the issuer-level
        # "any-agency" scoring (all covering agencies, noisy-OR — the shipped signal);
        # `display_timeline` (the case's own agency, else best-covered) drives the rating
        # shown in the trajectory, so the case still reads against the rating its event
        # moved. Empty coverage → agency-less fallback (fundamentals-only issuers/tests).
        by_agency = events_for(cik) or {}
        display_timeline = _rating_timeline(by_agency, (case.get("agency") or "").strip())
        score_agencies = [a for a in sorted(by_agency, key=lambda a: AGENCY_CODE.get(a, 99))
                          if by_agency.get(a)]
        trajectory: list[dict[str, Any]] = []
        earliest_flag: str | None = None
        flag_count = 0
        scored_any = False
        for row in snaps:
            period = row["period_end"]
            path = select_vintage(vintages, period)   # vintage trained strictly before T
            prob = None
            if path is not None and head is not None:
                scored_any = True
                prob = _score_issuer_any(path, head, row, period, by_agency, score_agencies)
            flagged = prob is not None and prob >= flag_threshold
            if flagged:
                flag_count += 1
                earliest_flag = period  # snaps are newest-first → ends on the oldest flag
            disp = agency_features_asof(display_timeline, period, row.get("implied_rating_index"))
            rating_index = disp["agency_rating_index"]
            rating = RATING_SCALE[int(rating_index)] if rating_index is not None else None
            trajectory.append({
                "eval_date": period,
                "months_before_event": round(_months_between(period, event_date), 1) if event_date else None,
                "prob": round(prob, 4) if prob is not None else None,
                "flagged": flagged,
                # The issuer's real agency rating in effect at this snapshot (point-in-time).
                "rating": rating,
                "rating_index": rating_index,
                # Point-in-time stress score + ratio levels the model saw at this snapshot
                # (lookahead-free), for the expanded per-period view on the backtest page.
                "score": _num(row.get("stress_score")),
                "ratios": {k: _num(row.get(k)) for k in RATIO_FEATURES},
            })

        if controls:
            results.append({**base, "status": "clean" if flag_count == 0 else "false_positive",
                            "fp_count": flag_count, "flag_threshold": flag_threshold,
                            "trajectory": trajectory})
            continue

        if not scored_any:
            # NO snapshot had a vintage trained before it → can't score without leakage.
            # (Gate on "any snapshot scored", NOT the oldest one: deep-history issuers
            # have early snapshots that predate the first vintage, but their later
            # snapshots score fine — those must not be discarded as a data_gap.)
            results.append({**base, "status": "data_gap", "trajectory": trajectory})
            continue

        caught = earliest_flag is not None
        lead = _months_between(earliest_flag, event_date) if caught else None
        results.append({
            **base,
            "status": "caught" if caught else "missed",
            "caught": caught,
            "lead_months": round(lead, 1) if lead is not None else None,
            "early_warning": bool(caught and lead is not None and lead >= early_months),
            "flag_threshold": flag_threshold,
            "trajectory": trajectory,
        })

    return {
        # Legacy single threshold (the downgrade head's cutoff if tuned) for older
        # readers; `thresholds` carries the full per-head map.
        "threshold": head_threshold.get("downgrade", threshold),
        "thresholds": head_threshold,
        "max_lead_months": max_lead_months,
        "by_event_type": _scorecard(results, early_months),
        "cases": results,
    }


def _scorecard(results: list[dict[str, Any]], early_months: int) -> dict[str, dict]:
    """Per-event-type catch-rate / lead time, plus the control false-positive rate."""
    import statistics

    out: dict[str, dict] = {}
    for et in ("downgrade", "upgrade", "default"):
        cases = [r for r in results if r["event_type"] == et and r["status"] in ("caught", "missed")]
        if not cases:
            continue
        caught = [r for r in cases if r.get("caught")]
        leads = [r["lead_months"] for r in caught if r.get("lead_months") is not None]
        early = [r for r in caught if r.get("early_warning")]
        out[et] = {
            "total": len(cases),
            "caught": len(caught),
            "catch_rate": round(100 * len(caught) / len(cases), 1),
            "median_lead_months": round(statistics.median(leads), 1) if leads else 0.0,
            "early_warning_rate": round(100 * len(early) / len(cases), 1),
        }
    controls = [r for r in results if r["event_type"] == "control" and r["status"] in ("clean", "false_positive")]
    if controls:
        fp = sum(1 for r in controls if r["status"] == "false_positive")
        out["control"] = {"total": len(controls), "false_positive": fp,
                          "fp_rate": round(100 * fp / len(controls), 1)}
    return out
