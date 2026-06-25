"""
Unified rating-EVENT backtest: does the trained migration model flag an issuer's
upgrade / downgrade / default EARLY?

For each case (issuer + event_type + event_date) it walks the issuer's filing
periods backward from the event and, at each snapshot date T, scores the model
VINTAGE trained before T (no leakage — see src.model.train.train_vintages /
select_vintage) on that period's point-in-time features. The head matching the
event is read (downgrade→p_downgrade, upgrade→p_upgrade, default→p_default); a
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

# event_type → the model head (probability key) that should fire for it.
EVENT_HEAD = {"downgrade": "downgrade", "upgrade": "upgrade", "default": "default"}

# How many ~quarterly snapshots to walk back from the event (≈ this/4 years).
DEFAULT_STEPS = 12
# A probability at/above this flags the event.
DEFAULT_THRESHOLD = 0.5
# An early warning is a catch with at least this much lead time.
EARLY_MONTHS = 6


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


def _case_snapshots(rows, event_date: str, *, steps: int, controls: bool):
    """The (period_end, feature-row) snapshots to score for a case, newest-first.

    For an event case: periods strictly before the event (the model must catch it
    ahead of time). For a control: the most recent `steps` periods (any flag is a FP).
    """
    rows = sorted(rows, key=lambda r: r["period_end"])
    usable = rows if controls else [r for r in rows if r["period_end"] < event_date]
    return list(reversed(usable[-steps:]))


def run_migration_backtest(
    cases: list[dict[str, Any]],
    scoring_by_cik: dict[str, list[dict[str, Any]]],
    vintages: list[dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    steps: int = DEFAULT_STEPS,
    early_months: int = EARLY_MONTHS,
    head_prob_fn: Callable[[str, Any, str], float | None] | None = None,
    feature_columns: list[str] | None = None,
) -> dict[str, Any]:
    """
    Run the event backtest. `scoring_by_cik` maps cik → list of feature-row dicts
    (period_end + the model's feature columns). `vintages` is the walk-forward panel
    from train_vintages. `head_prob_fn(path, X, head)` is injectable for testing;
    by default it loads the joblib vintage and runs the calibrated model.

    Returns {threshold, by_event_type, cases}.
    """
    import pandas as pd
    from src.model.features import FEATURE_COLUMNS, RATIO_FEATURES

    feats = feature_columns or FEATURE_COLUMNS
    head_prob = head_prob_fn or _default_loader()
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

        snaps = _case_snapshots(rows, event_date, steps=steps, controls=controls)
        if not snaps:
            results.append({**base, "status": "data_gap", "trajectory": []})
            continue

        # Controls have no event; a false positive is the model wrongly flagging a
        # downgrade, so they are scored against the downgrade head.
        head = EVENT_HEAD.get(et) or ("downgrade" if controls else None)
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
                X = pd.DataFrame([{c: row.get(c) for c in feats}])
                for c in feats:
                    X[c] = pd.to_numeric(X[c], errors="coerce")
                prob = head_prob(path, X, head)
            flagged = prob is not None and prob >= threshold
            if flagged:
                flag_count += 1
                earliest_flag = period  # snaps are newest-first → ends on the oldest flag
            trajectory.append({
                "eval_date": period,
                "months_before_event": round(_months_between(period, event_date), 1) if event_date else None,
                "prob": round(prob, 4) if prob is not None else None,
                "flagged": flagged,
                # Point-in-time stress score + ratio levels the model saw at this snapshot
                # (lookahead-free), for the expanded per-period view on the backtest page.
                "score": _num(row.get("stress_score")),
                "ratios": {k: _num(row.get(k)) for k in RATIO_FEATURES},
            })

        if controls:
            results.append({**base, "status": "clean" if flag_count == 0 else "false_positive",
                            "fp_count": flag_count, "trajectory": trajectory})
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
            "trajectory": trajectory,
        })

    return {
        "threshold": threshold,
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
