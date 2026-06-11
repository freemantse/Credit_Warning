"""
Point-in-time backtest harness.

For each case in data/cases.csv (case_id, company_name, ticker, cik, label,
event_date, notes — CIK is the authoritative identifier so delisted companies
resolve too):

  Distressed issuers:
    Score at the event date itself plus 12 backward 90-day steps (~3 years).
    At each step, score the issuer using only data available at that moment.
    Outcomes:
      caught    — the score crossed the threshold at some snapshot; lead time
                  is measured from the EARLIEST such snapshot to the event.
      missed    — data existed but the score never crossed the threshold.
      data_gap  — no filings were available at ANY snapshot (counted
                  separately from "missed" — the model never had a chance).
      error     — resolution/fetch failed.

  Healthy controls:
    Score 12 quarterly snapshots backward from the case's pinned event_date
    (pinning the anchor keeps runs reproducible for baseline comparison).
    Any stressed snapshot is a false positive.

Outputs:
  data/backtest_report.txt    — human-readable scorecard
  data/backtest_results.json  — machine-readable per-case detail incl. the full
                                score trajectory (for threshold tuning / jq)
  data/backtest_baseline.json — frozen reference run (via --save-baseline);
                                later runs diff against it and exit 1 on
                                regression, so this can gate CI.

Usage:
    python -m src.backtest                      # run + compare to baseline
    python -m src.backtest --save-baseline      # freeze current results
    python -m src.backtest --threshold 40       # experiment with the threshold

Exit codes: 0 = ok, 1 = regression vs baseline, 2 = harness failure.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from src.ingest import resolve_identifier, get_company_facts
from src.extract import extract_all, debt_maturity_schedule, RatioResult
from src.concepts import TAGS
from src.score import compute_score, STRESS_THRESHOLD, ScoreConfig, DEFAULT


# ── File paths ───────────────────────────────────────────────────────────────
DATA_DIR = pathlib.Path(__file__).parent.parent / "data"
CASES_PATH = DATA_DIR / "cases.csv"
REPORT_PATH = DATA_DIR / "backtest_report.txt"
RESULTS_PATH = DATA_DIR / "backtest_results.json"
BASELINE_PATH = DATA_DIR / "backtest_baseline.json"

# A case is an "early warning" only if flagged at least this many months
# before the event. Catching a bankruptcy 2 weeks out is technically a catch,
# but far too late to act on — this stricter bar is the metric to improve.
DEFAULT_EARLY_MONTHS = 6.0

# Lead time can wobble between runs by one snapshot step (~3 months) without
# the model genuinely regressing, so the baseline diff tolerates this much.
DEFAULT_LEAD_TOLERANCE_MONTHS = 3.0

# Number of point-in-time snapshots per case (anchor + steps-1 backward 90-day
# steps). 40 × 90 days ≈ 9.9 years of roughly-quarterly history. Configurable
# via run_backtest(steps=…), the --steps CLI flag, and the API request body.
DEFAULT_STEPS = 40


# ── Utility helpers ──────────────────────────────────────────────────────────

def _parse_date(s: str) -> date | None:
    """
    Parse an ISO date string "YYYY-MM-DD" into a date object.
    Returns None for empty strings (event_date is optional in cases.csv).
    """
    if not s:
        return None
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def _months_between(d1: date, d2: date) -> float:
    """
    Approximate the number of months between two dates.
    Uses a 30.44-day average month (365.25 / 12) for simplicity.
    """
    return (d2 - d1).days / 30.44


# ── Point-in-time filtering ──────────────────────────────────────────────────

def _filter_periods_point_in_time(facts: dict, eval_date: date) -> list[str]:
    """
    Return fiscal year-end dates for which the 10-K had been filed by eval_date.

    The key invariant: we only return period_end dates for filings whose
    "filed" date is on or before eval_date. This mirrors what a real investor
    would have had access to at that moment in time.

    Why this matters:
      A company's 2022 fiscal year 10-K might be filed in February 2023.
      If eval_date is January 2023, that filing wasn't yet public — so we
      must exclude it even though the period (Dec 2022) has already ended.

    Args:
        facts:     Full EDGAR companyfacts JSON for the company.
        eval_date: The simulated "today" — we pretend we're scoring as of this date.

    Returns:
        List of "YYYY-MM-DD" period-end strings, newest-available-first.
    """
    # Convert date to ISO string for direct string comparison with EDGAR "filed" dates.
    # ISO format ("YYYY-MM-DD") supports lexicographic comparison for date ordering.
    eval_str = eval_date.isoformat()

    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    def _valid(entry: dict) -> bool:
        return (
            entry.get("form") == "10-K"
            and bool(entry.get("end"))      # has a period-end date
            and bool(entry.get("filed"))    # has a filing date
            and entry["filed"] <= eval_str  # was filed on or before eval_date
            # A real fiscal period must have ENDED before it was filed. 10-Ks
            # also carry facts about FUTURE periods (e.g. expected debt
            # maturities tagged with end dates years ahead) and stray
            # subsequent-event dates — without this guard the "most recent
            # period" can be a date with no actual financials, scoring 0.
            and entry["end"] <= entry["filed"]
        )

    # ── Primary path: anchor on total_assets ─────────────────────────────────
    # Same approach as extract._get_available_periods: total assets is reported
    # exactly once per fiscal year-end in a 10-K, so its 'end' dates ARE the
    # fiscal year-ends. Scanning all concepts instead would pick up quarterly
    # comparatives, cover-page dates and subsequent-event facts.
    anchor_tags = [t.split("/", 1)[1] for t in TAGS["total_assets"]]
    valid_periods: set[str] = set()

    for tag in anchor_tags:
        concept_data = us_gaap.get(tag)
        if not concept_data:
            continue
        for entries in concept_data.get("units", {}).values():
            for entry in entries:
                if _valid(entry):
                    valid_periods.add(entry["end"])

    if valid_periods:
        # Return newest-first so callers can take periods[0] as the most recent available.
        return sorted(valid_periods, reverse=True)

    # ── Fallback path (issuers that don't tag total assets): scan everything,
    # but require duration entries to span ≥ 350 days so quarterly comparative
    # periods (~90 days) inside the 10-K are excluded.
    for concept_data in us_gaap.values():
        for entries in concept_data.get("units", {}).values():
            for entry in entries:
                if not _valid(entry):
                    continue
                start = entry.get("start")
                if start:
                    try:
                        span = date.fromisoformat(entry["end"]) - date.fromisoformat(start)
                    except ValueError:
                        continue  # malformed date string — skip this entry
                    if span.days < 350:
                        continue
                valid_periods.add(entry["end"])

    return sorted(valid_periods, reverse=True)


# ── Point-in-time scoring ────────────────────────────────────────────────────

@dataclass
class Snapshot:
    """
    The result of scoring one issuer at one simulated date.

    has_data distinguishes "the model saw the filings and scored 0" from
    "no filings existed yet at this date" — conflating the two would let a
    company with no data count as a healthy-looking miss.
    """
    eval_date: date
    score: float
    stressed: bool
    has_data: bool
    period_end: str | None   # fiscal period the score was computed against
    missing_ratios: int      # how many of the core ratios could not be computed
    # Ratio name → value as seen at this point in time (None = not computable).
    # Captured so the backtest output can show each metric's history per
    # company, not just the composite score.
    ratios: dict = field(default_factory=dict)


def score_issuer_at_date(
    facts: dict,
    eval_date: date,
    threshold: int,
    *,
    config: ScoreConfig = DEFAULT,
) -> Snapshot:
    """
    Score an issuer using ONLY data that was publicly available at eval_date.

    This is the core point-in-time scoring function used by the backtest.
    It differs from the normal scoring path (extract_all without filed_before)
    because it passes filed_before=eval_str to every XBRL tag lookup, preventing
    any data from after eval_date from influencing the score.

    Args:
        facts:     Full EDGAR companyfacts JSON for the company.
        eval_date: The simulated scoring date.
        threshold: The stress threshold (usually STRESS_THRESHOLD = 50).

    Returns:
        A Snapshot. When no filings were available at eval_date, the Snapshot
        has has_data=False (and score 0.0 — but callers must check has_data,
        not the score, to detect that situation).
    """
    eval_str = eval_date.isoformat()

    # Get the list of fiscal periods that had been filed by eval_date.
    periods = _filter_periods_point_in_time(facts, eval_date)
    if not periods:
        # No filings available at this eval_date — cannot assess stress.
        return Snapshot(eval_date, 0.0, False, has_data=False,
                        period_end=None, missing_ratios=0)

    # Score against the most recently filed fiscal year-end available at eval_date.
    latest_period = periods[0]

    # Pass filed_before so extract_all only uses XBRL values from filings
    # that existed at eval_date — enforcing the no-look-ahead rule.
    results = extract_all(facts, latest_period, filed_before=eval_str)
    missing = sum(1 for r in results.values() if not isinstance(r, RatioResult))

    # The maturity wall is XBRL-derived and point-in-time safe (filed_before),
    # so include it. LLM findings and footnote covenants/provisions are NOT used
    # in the backtest (too slow, non-deterministic, and not cleanly point-in-time
    # bounded from filing text).
    maturity = debt_maturity_schedule(facts, latest_period, filed_before=eval_str)
    score_result = compute_score(results, [], maturity, config=config)

    # Preserve the individual metric values, not just the composite score, so
    # the eval output can show how each ratio evolved per fiscal year.
    ratio_values = {
        name: (r.value if isinstance(r, RatioResult) else None)
        for name, r in results.items()
    }
    ratio_values["maturity_near_term_pct"] = (
        getattr(maturity, "near_term_pct", None) if maturity else None
    )

    return Snapshot(eval_date, score_result.score, score_result.score >= threshold,
                    has_data=True, period_end=latest_period, missing_ratios=missing,
                    ratios=ratio_values)


# ── Case library ─────────────────────────────────────────────────────────────

def load_cases(cases_path: pathlib.Path | str = CASES_PATH) -> list[dict]:
    """
    Load the case-library rows.

    When called with the DEFAULT path, the primary source is the Supabase `cases`
    table (so the roster is editable from the UI), falling back to the CSV when
    Supabase is unavailable (no credentials, a connection error, or an empty /
    not-yet-seeded table) — this keeps the CLI backtest runnable offline/in CI.

    When called with an EXPLICIT path (e.g. the `--cases somefile.csv` flag, or a
    test fixture), that CSV is read directly and Supabase is bypassed.

    Keys: case_id, company_name, ticker, cik, label, event_date, notes (all
    strings). Shared by run_backtest and the /api/backtest/cases endpoint.

    Raises:
        FileNotFoundError — when the chosen CSV is missing (fail loud, same as
        before): always for an explicit path, or for the default path only when
        Supabase is also unavailable.
    """
    cases_path = pathlib.Path(cases_path)
    explicit = cases_path != CASES_PATH

    if not explicit:
        try:
            from src.store import list_cases as _list_cases  # lazy: don't require Supabase at import
            cases = _list_cases()
            if cases:
                return cases
            # Empty table (fresh DB, pre-seed) → fall through to the CSV.
        except Exception:
            # Supabase not configured / unreachable → fall back to the CSV.
            pass

    if not cases_path.exists():
        raise FileNotFoundError(f"Case library not found: {cases_path}")
    with open(cases_path, newline="") as f:
        return list(csv.DictReader(f))


# ── Case resolution ──────────────────────────────────────────────────────────

def _resolve_case_cik(case: dict) -> str:
    """
    Resolve a cases.csv row to a zero-padded CIK.

    Prefers the explicit "cik" column — the authoritative identifier, required
    for delisted/bankrupt companies whose tickers vanished from SEC's current
    tickers list. Falls back to ticker resolution for rows without a CIK
    (including rows from the old 4-column schema).
    """
    cik = (case.get("cik") or "").strip()
    if cik:
        return resolve_identifier(cik)
    return resolve_identifier(case["ticker"].strip())


# ── Per-case evaluation ──────────────────────────────────────────────────────

def _take_snapshots(
    facts: dict,
    anchor: date,
    threshold: int,
    steps: int,
    *,
    config: ScoreConfig = DEFAULT,
) -> list[Snapshot]:
    """
    Score at the anchor date and then backward in 90-day increments.

    Returns `steps` snapshots, newest-first. 40 steps from an event date is the
    anchor plus ~10 years of roughly-quarterly history; the expanded case card
    collapses this to one column per fiscal year.
    """
    snapshots: list[Snapshot] = []
    scan = anchor
    for i in range(steps):
        if i > 0:
            # Step back 90 days using ordinal arithmetic (avoids month boundary issues).
            scan = date.fromordinal(scan.toordinal() - 90)
        snapshots.append(score_issuer_at_date(facts, scan, threshold, config=config))
    return snapshots


def evaluate_distressed_case(
    facts: dict, event_date: date, threshold: int, steps: int = DEFAULT_STEPS,
    *, config: ScoreConfig = DEFAULT,
) -> dict:
    """
    Walk a distressed issuer's history and decide caught / missed / data_gap.

    Evaluates at the event date itself (T-0) plus steps-1 backward 90-day steps,
    so the trajectory spans ~(steps × 90 days) before the bankruptcy.
    """
    snapshots = _take_snapshots(facts, event_date, threshold, steps=steps, config=config)

    flagged = [s for s in snapshots if s.has_data and s.stressed]
    had_any_data = any(s.has_data for s in snapshots)

    if flagged:
        earliest = min(s.eval_date for s in flagged)
        return {
            "status": "caught",
            "earliest_flag_date": earliest,
            "lead_months": _months_between(earliest, event_date),
            "trajectory": snapshots,
        }
    return {
        "status": "missed" if had_any_data else "data_gap",
        "earliest_flag_date": None,
        "lead_months": None,
        "trajectory": snapshots,
    }


def evaluate_healthy_case(
    facts: dict, anchor_date: date, threshold: int, steps: int = DEFAULT_STEPS,
    *, config: ScoreConfig = DEFAULT,
) -> dict:
    """
    Score a healthy control at `steps` quarterly snapshots backward from anchor_date.

    Any stressed snapshot is a false positive. The FP-rate denominator is the
    number of snapshots that actually had data (periods_evaluated), not the
    snapshot count — otherwise sparse data would dilute the rate.
    """
    snapshots = _take_snapshots(facts, anchor_date, threshold, steps=steps, config=config)

    evaluated = [s for s in snapshots if s.has_data]
    fp_count = sum(1 for s in evaluated if s.stressed)

    if not evaluated:
        status = "data_gap"
    elif fp_count:
        status = "false_positive"
    else:
        status = "clean"
    return {
        "status": status,
        "fp_count": fp_count,
        "periods_evaluated": len(evaluated),
        "trajectory": snapshots,
    }


# ── Scorecard ────────────────────────────────────────────────────────────────

def build_scorecard(case_results: list[dict], threshold: int, early_months: float) -> dict:
    """
    Aggregate per-case results into the summary scorecard.

    Denominator rules:
      - catch_rate is over caught + missed. data_gap and error cases are
        counted separately — the model never saw data for them, so including
        them would punish (or flatter) changes that have nothing to do with
        scoring quality.
      - fp_rate is over healthy snapshots that actually had data.

    Legacy keys (catch_rate, caught, total_distressed, median_lead_months,
    fp_rate) are kept intact — api/main.py and the frontend read them.
    """
    distressed = [c for c in case_results if c["label"] == "distressed"]
    healthy = [c for c in case_results if c["label"] == "healthy"]

    caught_cases = [c for c in distressed if c.get("status") == "caught"]
    missed = sum(1 for c in distressed if c.get("status") == "missed")
    data_gaps = sum(1 for c in case_results if c.get("status") == "data_gap")
    errors = sum(1 for c in case_results if c.get("status") == "error")

    caught = len(caught_cases)
    total_d = caught + missed
    catch_rate = (caught / total_d * 100) if total_d else 0.0

    lead_times = [c["lead_months"] for c in caught_cases]
    median_lead = statistics.median(lead_times) if lead_times else 0.0
    mean_lead = statistics.mean(lead_times) if lead_times else 0.0

    # The stricter bar: caught AND with enough lead time to act on.
    early_caught = sum(1 for c in caught_cases if c["lead_months"] >= early_months)
    early_rate = (early_caught / total_d * 100) if total_d else 0.0

    fp_periods = sum(c.get("fp_count", 0) for c in healthy)
    healthy_periods = sum(c.get("periods_evaluated", 0) for c in healthy)
    fp_rate = (fp_periods / healthy_periods * 100) if healthy_periods else 0.0

    return {
        # Legacy keys — consumed by api/main.py and app/backtest/page.tsx.
        "catch_rate": round(catch_rate, 1),
        "caught": caught,
        "total_distressed": total_d,
        "median_lead_months": round(median_lead, 1),
        "fp_rate": round(fp_rate, 1),
        # Additive keys.
        "missed": missed,
        "data_gaps": data_gaps,
        "errors": errors,
        "mean_lead_months": round(mean_lead, 1),
        "early_warning_caught": early_caught,
        "early_warning_rate": round(early_rate, 1),
        "early_months": early_months,
        "fp_periods": fp_periods,
        "healthy_periods_evaluated": healthy_periods,
        "threshold": threshold,
    }


# ── Baseline comparison ──────────────────────────────────────────────────────

def compare_to_baseline(
    current: dict,
    baseline: dict,
    lead_tolerance_months: float = DEFAULT_LEAD_TOLERANCE_MONTHS,
) -> dict:
    """
    Diff a backtest run against a frozen baseline, case by case.

    Regressions (any one makes the run "regressed", which exits 1 — the CI
    gate for future model changes):
      - a case caught in the baseline is now missed / data_gap / error
      - a caught case's lead time fell by more than lead_tolerance_months
      - a healthy case produced more false positives than before
      - a baseline case is missing from the current run

    Improvements (newly caught, longer lead, fewer FPs) are reported but never
    fail the run. Cases new to the library are listed as neutral.
    """
    def _index(results: dict) -> dict[str, dict]:
        # Old baselines may predate case_id — fall back to ticker as the key.
        return {(c.get("case_id") or c.get("ticker")): c for c in results["cases"]}

    cur_by_id = _index(current)
    base_by_id = _index(baseline)

    regressions: list[str] = []
    improvements: list[str] = []
    new_cases: list[str] = []

    for case_id, base in base_by_id.items():
        cur = cur_by_id.get(case_id)
        if cur is None:
            regressions.append(f"{case_id}: present in baseline but missing from this run")
            continue

        if base["label"] == "distressed":
            base_caught = base.get("status") == "caught" or base.get("caught") is True
            cur_caught = cur.get("status") == "caught" or cur.get("caught") is True
            if base_caught and not cur_caught:
                regressions.append(
                    f"{case_id}: was caught in baseline, now {cur.get('status', 'missed')}"
                )
            elif not base_caught and cur_caught:
                improvements.append(
                    f"{case_id}: newly caught ({cur.get('lead_months', 0):.1f} months early)"
                )
            elif base_caught and cur_caught:
                base_lead = base.get("lead_months") or 0
                cur_lead = cur.get("lead_months") or 0
                if cur_lead < base_lead - lead_tolerance_months:
                    regressions.append(
                        f"{case_id}: lead time fell {base_lead:.1f} → {cur_lead:.1f} months"
                    )
                elif cur_lead > base_lead + lead_tolerance_months:
                    improvements.append(
                        f"{case_id}: lead time rose {base_lead:.1f} → {cur_lead:.1f} months"
                    )
        else:  # healthy
            base_fp = base.get("fp_count", 0) or 0
            cur_fp = cur.get("fp_count", 0) or 0
            if cur_fp > base_fp:
                regressions.append(f"{case_id}: false positives rose {base_fp} → {cur_fp}")
            elif cur_fp < base_fp:
                improvements.append(f"{case_id}: false positives fell {base_fp} → {cur_fp}")

    for case_id in cur_by_id:
        if case_id not in base_by_id:
            new_cases.append(f"{case_id}: new case, no baseline yet")

    return {
        "regressed": bool(regressions),
        "regressions": regressions,
        "improvements": improvements,
        "new_cases": new_cases,
    }


# ── Serialization helpers ────────────────────────────────────────────────────

def _trajectory_dicts(snapshots: list[Snapshot], reference: date) -> list[dict]:
    """JSON-friendly trajectory, with each point's distance from the event/anchor."""
    return [
        {
            "eval_date": s.eval_date.isoformat(),
            "months_before_event": round(_months_between(s.eval_date, reference), 1),
            "score": round(s.score, 1),
            "stressed": s.stressed,
            "has_data": s.has_data,
            "period_end": s.period_end,
            "missing_ratios": s.missing_ratios,
            "ratios": s.ratios,
        }
        for s in snapshots
    ]


def _trajectory_line(snapshots: list[Snapshot], reference: date) -> str:
    """Compact one-line trajectory for the text report, oldest-first."""
    parts = []
    for s in reversed(snapshots):  # snapshots are newest-first
        months = _months_between(s.eval_date, reference)
        score = f"{s.score:.0f}" if s.has_data else "—"
        parts.append(f"T-{months:.0f}:{score}")
    return " ".join(parts)


# ── Main backtest loop ───────────────────────────────────────────────────────

def run_backtest(
    threshold: int | None = None,
    early_months: float = DEFAULT_EARLY_MONTHS,
    cases_path: pathlib.Path | str = CASES_PATH,
    steps: int = DEFAULT_STEPS,
    *,
    config: ScoreConfig = DEFAULT,
) -> dict:
    """
    Run the full backtest over the case library.

    `steps` is the number of point-in-time snapshots per case (anchor + steps-1
    backward 90-day steps), so the trajectory spans ~(steps × 90 days).

    `config` is the stress-score parameter set (weights, ramp thresholds, caps).
    `threshold` defaults to config.threshold; pass it explicitly (e.g. the CLI
    --threshold flag) to override just the stressed cutoff.

    Writes data/backtest_report.txt (human) and data/backtest_results.json
    (machine), and returns the same structured dict the JSON contains. The
    API serves this dict to the backtest page.
    """
    if threshold is None:
        threshold = config.threshold
    cases = load_cases(cases_path)

    lines = []
    lines.append(
        f"Credit Warning Backtest — threshold={threshold}, "
        f"early-warning ≥ {early_months:g} months, "
        f"steps={steps} (~{steps * 90 / 365.25:.1f}y)"
    )
    lines.append("=" * 100)

    cases_output: list[dict] = []

    for case in cases:
        ticker = (case.get("ticker") or "").strip()
        label = (case.get("label") or "").strip()  # "distressed" or "healthy"
        case_id = (case.get("case_id") or "").strip() or ticker
        company_name = (case.get("company_name") or "").strip()
        event_date = _parse_date(case.get("event_date", ""))

        print(f"  Processing {case_id} ({label})...", end=" ", flush=True)

        # Identity fields shared by every outcome below.
        out: dict[str, Any] = {
            "case_id": case_id,
            "company_name": company_name,
            "ticker": ticker,
            "cik": (case.get("cik") or "").strip() or None,
            "label": label,
            "event_date": str(event_date) if event_date else None,
            "error": None,
        }

        # Fetch EDGAR data. If this fails, record the error and skip to next case.
        try:
            cik = _resolve_case_cik(case)
            out["cik"] = cik
            facts = get_company_facts(cik)
        except Exception as e:
            out.update({"status": "error", "error": str(e)})
            cases_output.append(out)
            line = f"{case_id:<18} {ticker:<6} {label:<11} ERROR: {e}"
            print(f"ERROR: {e}")
            lines.append(line)
            continue

        # ── Distressed case ──────────────────────────────────────────────────
        if label == "distressed" and event_date:
            result = evaluate_distressed_case(facts, event_date, threshold, steps, config=config)
            caught = result["status"] == "caught"
            lead = result["lead_months"]
            early = bool(caught and lead >= early_months)
            out.update({
                "status": result["status"],
                "caught": caught,
                "lead_months": round(lead, 1) if lead is not None else 0,
                "earliest_flag_date": (
                    result["earliest_flag_date"].isoformat()
                    if result["earliest_flag_date"] else None
                ),
                "early_warning": early,
                "trajectory": _trajectory_dicts(result["trajectory"], event_date),
            })
            if caught:
                marker = "✓ EARLY" if early else "✓ (late)"
                line = (f"{case_id:<18} {ticker:<6} {label:<11} "
                        f"CAUGHT {lead:.1f} months early {marker}  (event: {event_date})")
            elif result["status"] == "missed":
                line = (f"{case_id:<18} {ticker:<6} {label:<11} "
                        f"MISSED — never reached threshold ✗  (event: {event_date})")
            else:
                line = (f"{case_id:<18} {ticker:<6} {label:<11} "
                        f"DATA-GAP — no filings in window  (event: {event_date})")
            lines.append(line)
            lines.append(f"{'':<18} score: {_trajectory_line(result['trajectory'], event_date)}")

        # ── Healthy control case ─────────────────────────────────────────────
        elif label == "healthy":
            # Pinned anchor (from cases.csv) keeps runs reproducible; without
            # one we fall back to today, which makes baselines drift over time.
            anchor = event_date or date.today()
            if not event_date:
                print("(no pinned anchor date — results not baseline-stable)", end=" ")
            result = evaluate_healthy_case(facts, anchor, threshold, steps, config=config)
            out.update({
                "status": result["status"],
                "fp_count": result["fp_count"],
                "periods_evaluated": result["periods_evaluated"],
                "trajectory": _trajectory_dicts(result["trajectory"], anchor),
            })
            line = (f"{case_id:<18} {ticker:<6} {label:<11} "
                    f"{result['fp_count']} false-positive periods "
                    f"({result['periods_evaluated']} evaluated)")
            lines.append(line)

        else:
            # Distressed case with no event_date — can't measure lead time.
            out.update({"status": "error", "error": "no event_date"})
            line = f"{case_id:<18} {ticker:<6} {label:<11} SKIPPED (no event_date for distressed)"
            lines.append(line)

        cases_output.append(out)
        print(line.split(maxsplit=3)[-1] if len(line.split()) > 3 else "done")

    lines.append("-" * 100)

    # ── Aggregate scorecard ──────────────────────────────────────────────────
    summary = build_scorecard(cases_output, threshold, early_months)

    summary_lines = [
        (f"Caught {summary['caught']}/{summary['total_distressed']} "
         f"({summary['catch_rate']:.0f}%)  |  "
         f"early-warning (≥{early_months:g} mo): {summary['early_warning_caught']}"
         f"/{summary['total_distressed']} ({summary['early_warning_rate']:.0f}%)  |  "
         f"median lead {summary['median_lead_months']:.1f} mo  |  "
         f"mean lead {summary['mean_lead_months']:.1f} mo"),
        (f"False positives: {summary['fp_periods']}/{summary['healthy_periods_evaluated']} "
         f"healthy periods ({summary['fp_rate']:.1f}%)  |  "
         f"data gaps: {summary['data_gaps']}  |  errors: {summary['errors']}"),
    ]
    lines.extend(summary_lines)

    results = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "threshold": threshold,
        "early_months": early_months,
        "steps": steps,
        # Provenance: which scoring parameters produced this run.
        "config": config.to_dict(),
        "cases": cases_output,
        "summary": summary,
    }

    # Write both outputs. The JSON is the canonical artifact (baseline diffs,
    # jq analysis); the text report is for humans.
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines))
    RESULTS_PATH.write_text(json.dumps(results, indent=2))

    print()
    for s in summary_lines:
        print(s)
    print(f"Report written to {REPORT_PATH}")
    print(f"Results JSON written to {RESULTS_PATH}")

    return results


# ── CLI entry point ──────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the credit warning backtest")
    parser.add_argument(
        "--threshold", type=int, default=STRESS_THRESHOLD,
        help=f"Stress score threshold (default: {STRESS_THRESHOLD})")
    parser.add_argument(
        "--early-months", type=float, default=DEFAULT_EARLY_MONTHS,
        help=f"Lead time required to count as an early warning "
             f"(default: {DEFAULT_EARLY_MONTHS:g} months)")
    parser.add_argument(
        "--steps", type=int, default=DEFAULT_STEPS,
        help=f"Point-in-time snapshots per case, ~90 days apart "
             f"(default: {DEFAULT_STEPS}, ≈ {DEFAULT_STEPS * 90 / 365.25:.1f} years)")
    parser.add_argument(
        "--cases", type=pathlib.Path, default=CASES_PATH,
        help=f"Case library CSV (default: {CASES_PATH})")
    parser.add_argument(
        "--save-baseline", action="store_true",
        help=f"Save this run as the baseline ({BASELINE_PATH}) instead of comparing")
    parser.add_argument(
        "--baseline", type=pathlib.Path, default=BASELINE_PATH,
        help=f"Baseline JSON to compare against (default: {BASELINE_PATH})")
    parser.add_argument(
        "--no-compare", action="store_true",
        help="Skip the baseline comparison")
    parser.add_argument(
        "--lead-tolerance", type=float, default=DEFAULT_LEAD_TOLERANCE_MONTHS,
        help=f"Months of lead-time slack before a drop counts as a regression "
             f"(default: {DEFAULT_LEAD_TOLERANCE_MONTHS:g})")
    args = parser.parse_args(argv)

    results = run_backtest(
        threshold=args.threshold,
        early_months=args.early_months,
        cases_path=args.cases,
        steps=args.steps,
    )

    if args.save_baseline:
        args.baseline.write_text(json.dumps(results, indent=2))
        print(f"Baseline saved to {args.baseline}")
        return 0

    if args.no_compare:
        return 0

    if not args.baseline.exists():
        print(f"No baseline at {args.baseline} — run with --save-baseline to create one.")
        return 0

    baseline = json.loads(args.baseline.read_text())
    diff = compare_to_baseline(results, baseline, args.lead_tolerance)

    diff_lines = ["", f"Baseline comparison (vs {args.baseline.name}):"]
    for r in diff["regressions"]:
        diff_lines.append(f"  REGRESSION  {r}")
    for i in diff["improvements"]:
        diff_lines.append(f"  improvement {i}")
    for n in diff["new_cases"]:
        diff_lines.append(f"  new         {n}")
    if not (diff["regressions"] or diff["improvements"] or diff["new_cases"]):
        diff_lines.append("  no changes vs baseline")

    diff_text = "\n".join(diff_lines)
    print(diff_text)
    # Append the diff to the text report so it's part of the artifact.
    REPORT_PATH.write_text(REPORT_PATH.read_text() + "\n" + diff_text + "\n")

    return 1 if diff["regressed"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # harness failure (missing cases file, etc.)
        print(f"backtest harness failure: {e}", file=sys.stderr)
        sys.exit(2)
