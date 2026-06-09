"""
Point-in-time backtest harness.

What is a point-in-time backtest?
  A naive backtest would use all available data to score an issuer, then check
  whether it would have been flagged before a known credit event. This suffers
  from "look-ahead bias" — using information that wasn't available at the time.

  A point-in-time backtest only uses data that was publicly available at each
  evaluation date. For EDGAR filings, that means: only use XBRL entries whose
  "filed" date is on or before the evaluation date.

For each case in data/cases.csv:
  Distressed issuers:
    Step backward in 90-day increments from the event_date, up to 3 years.
    At each step, score the issuer using only data available at that moment.
    Record whether the score ever crossed the threshold, and how early.

  Healthy controls:
    Step backward in 90-day increments from today, up to 3 years.
    Count how many of those 12 snapshots produced a "stressed" score (false positives).

Key output metrics:
  Catch rate         — % of distressed cases that were flagged before the event
  Median lead time   — how many months early the model flagged caught cases
  False-positive rate— % of healthy-issuer quarterly snapshots that were flagged

Usage:
    python -m src.backtest
    python -m src.backtest --threshold 40   # experiment with a different threshold
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys
from datetime import date, datetime
from typing import Any

from src.ingest import get_cik, get_company_facts, get_filings
from src.extract import extract_all, _get_available_periods, debt_maturity_schedule
from src.score import compute_score, STRESS_THRESHOLD
from src.concepts import MissingDataError


# ── File paths ───────────────────────────────────────────────────────────────
# cases.csv columns: ticker, label ("distressed" or "healthy"), event_date (YYYY-MM-DD)
CASES_PATH = pathlib.Path(__file__).parent.parent / "data" / "cases.csv"
REPORT_PATH = pathlib.Path(__file__).parent.parent / "data" / "backtest_report.txt"


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
    valid_periods: set[str] = set()

    # Scan all concepts and all their entries to find 10-K periods that were
    # already filed by eval_date. We use a set to avoid counting the same period
    # multiple times (many concepts repeat the same period_end date).
    for concept_data in us_gaap.values():
        for entries in concept_data.get("units", {}).values():
            for entry in entries:
                if (
                    entry.get("form") == "10-K"
                    and entry.get("end")       # has a period-end date
                    and entry.get("filed")     # has a filing date
                    and entry["filed"] <= eval_str  # was filed on or before eval_date
                ):
                    valid_periods.add(entry["end"])

    # Return newest-first so callers can take periods[0] as the most recent available.
    return sorted(valid_periods, reverse=True)


def score_issuer_at_date(
    facts: dict,
    eval_date: date,
    threshold: int,
) -> tuple[float, bool]:
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
        (score, is_stressed) — e.g. (75.0, True) or (20.0, False)
        Returns (0.0, False) if no filings were available at eval_date.
    """
    eval_str = eval_date.isoformat()

    # Get the list of fiscal periods that had been filed by eval_date.
    periods = _filter_periods_point_in_time(facts, eval_date)
    if not periods:
        # No filings available at this eval_date — cannot assess stress.
        return 0.0, False

    # Score against the most recently filed fiscal year-end available at eval_date.
    latest_period = periods[0]

    # Pass filed_before so extract_all only uses XBRL values from filings
    # that existed at eval_date — enforcing the no-look-ahead rule.
    results = extract_all(facts, latest_period, filed_before=eval_str)

    # The maturity wall is XBRL-derived and point-in-time safe (filed_before),
    # so include it. LLM findings and footnote covenants/provisions are NOT used
    # in the backtest (too slow, non-deterministic, and not cleanly point-in-time
    # bounded from filing text).
    maturity = debt_maturity_schedule(facts, latest_period, filed_before=eval_str)
    score_result = compute_score(results, [], maturity)
    return score_result.score, score_result.score >= threshold


# ── Main backtest loop ───────────────────────────────────────────────────────

def run_backtest(threshold: int = STRESS_THRESHOLD) -> dict:
    """
    Run the full backtest over all cases in data/cases.csv.

    For each case:
      - distressed: walk backward 90 days at a time up to 3 years before the
        event_date and record whether the stress flag was ever triggered.
      - healthy: walk backward 90 days at a time for the past 3 years from today
        and count how many snapshots were falsely flagged as stressed.

    Writes a text report to data/backtest_report.txt and returns a structured
    dict that the API serves to the backtest page.
    """
    if not CASES_PATH.exists():
        raise FileNotFoundError(f"Case library not found: {CASES_PATH}")

    with open(CASES_PATH, newline="") as f:
        cases = list(csv.DictReader(f))

    # Accumulate report lines for the text file.
    lines = []
    lines.append(f"Credit Warning Backtest — threshold={threshold}")
    lines.append("=" * 70)

    # Structured results for the API response.
    cases_output: list[dict] = []
    # Track per-issuer results for aggregate statistics.
    distressed_results: list[dict] = []   # {"caught": bool, "lead_months": float}
    healthy_fp_counts: list[int] = []     # one int per healthy issuer

    for case in cases:
        ticker = case["ticker"].strip()
        label = case["label"].strip()  # "distressed" or "healthy"
        event_date = _parse_date(case.get("event_date", ""))

        print(f"  Processing {ticker} ({label})...", end=" ", flush=True)

        # Fetch EDGAR data. If this fails, record the error and skip to next case.
        try:
            cik = get_cik(ticker)
            facts = get_company_facts(cik)
        except Exception as e:
            err = f"ERROR: {e}"
            print(err)
            lines.append(f"{ticker:<8} {label:<12} {err}")
            cases_output.append({"ticker": ticker, "label": label, "error": str(e)})
            continue

        # ── Distressed case ──────────────────────────────────────────────────
        if label == "distressed" and event_date:
            first_flag_date: date | None = None
            scan_date = event_date

            # Walk backward in 90-day steps (roughly quarterly snapshots).
            # 12 steps × 90 days ≈ 3 years of history before the event date.
            # The explicit date boundary check guards against edge cases where
            # a leap year or different month causes the 12th step to overshoot.
            for _ in range(12):
                # Step back 90 days using ordinal arithmetic (avoids month boundary issues).
                scan_date = date.fromordinal(scan_date.toordinal() - 90)

                # Stop if we've gone back more than 3 years before the event.
                if scan_date < date(event_date.year - 3, event_date.month, event_date.day):
                    break

                score, stressed = score_issuer_at_date(facts, scan_date, threshold)

                if stressed:
                    # Keep overwriting first_flag_date so that at the end of the loop
                    # it holds the EARLIEST date the flag was triggered.
                    # (We scan newest-to-oldest, so the last assignment = earliest date.)
                    first_flag_date = scan_date

            if first_flag_date:
                # The model flagged this issuer before the event. Compute lead time.
                lead = _months_between(first_flag_date, event_date)
                line = f"{ticker:<8} {label:<12} FLAGGED {lead:.0f} months early ✓  (event: {event_date})"
                distressed_results.append({"caught": True, "lead_months": lead})
                cases_output.append({
                    "ticker": ticker, "label": label,
                    "event_date": str(event_date), "caught": True,
                    "lead_months": round(lead, 1), "error": None,
                })
            else:
                # The model never crossed the threshold in the 3 years before the event.
                line = f"{ticker:<8} {label:<12} MISSED — never reached threshold ✗  (event: {event_date})"
                distressed_results.append({"caught": False, "lead_months": 0})
                cases_output.append({
                    "ticker": ticker, "label": label,
                    "event_date": str(event_date), "caught": False,
                    "lead_months": 0, "error": None,
                })

        # ── Healthy control case ─────────────────────────────────────────────
        elif label == "healthy":
            fp_count = 0  # count of false-positive quarters for this issuer
            eval_date_h = date.today()

            # Score the healthy issuer at 12 quarterly snapshots going back 3 years.
            # Any snapshot that produces a "stressed" score is a false positive.
            for _ in range(12):
                score, stressed = score_issuer_at_date(facts, eval_date_h, threshold)
                if stressed:
                    fp_count += 1
                # Step back 90 days for the next snapshot.
                eval_date_h = date.fromordinal(eval_date_h.toordinal() - 90)

            line = f"{ticker:<8} {label:<12} {fp_count} false-positive periods"
            healthy_fp_counts.append(fp_count)
            cases_output.append({"ticker": ticker, "label": label, "fp_count": fp_count, "error": None})

        else:
            # Distressed case with no event_date — can't measure lead time.
            line = f"{ticker:<8} {label:<12} SKIPPED (no event_date for distressed)"
            cases_output.append({"ticker": ticker, "label": label, "error": "no event_date"})

        print(line.split("  ")[-1] if "  " in line else "done")
        lines.append(line)

    lines.append("-" * 70)

    # ── Aggregate statistics ─────────────────────────────────────────────────

    caught = sum(1 for r in distressed_results if r["caught"])
    total_d = len(distressed_results)
    # Avoid ZeroDivisionError if no distressed cases were processed.
    catch_rate = (caught / total_d * 100) if total_d else 0

    # Median lead time: sort all caught lead times and take the middle value.
    lead_times = [r["lead_months"] for r in distressed_results if r["caught"]]
    median_lead = sorted(lead_times)[len(lead_times) // 2] if lead_times else 0

    total_fp = sum(healthy_fp_counts)
    # Each healthy issuer contributes 12 snapshots to the denominator.
    total_healthy_periods = len(healthy_fp_counts) * 12
    fp_rate = (total_fp / total_healthy_periods * 100) if total_healthy_periods else 0

    summary = (
        f"Catch rate: {catch_rate:.0f}% ({caught}/{total_d})  |  "
        f"Median lead: {median_lead:.0f} months  |  "
        f"False-positive rate: {fp_rate:.1f}%"
    )
    lines.append(summary)

    # Write the full human-readable report to disk for offline review.
    report_text = "\n".join(lines)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report_text)

    print(f"\n{summary}")
    print(f"Report written to {REPORT_PATH}")

    return {
        "cases": cases_output,
        "summary": {
            "catch_rate": round(catch_rate, 1),
            "caught": caught,
            "total_distressed": total_d,
            "median_lead_months": round(median_lead, 1),
            "fp_rate": round(fp_rate, 1),
        },
    }


# ── CLI entry point ──────────────────────────────────────────────────────────
# Usage:  python -m src.backtest
#         python -m src.backtest --threshold 40

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the credit warning backtest")
    parser.add_argument(
        "--threshold", type=int, default=STRESS_THRESHOLD,
        help=f"Stress score threshold (default: {STRESS_THRESHOLD})"
    )
    args = parser.parse_args()
    run_backtest(threshold=args.threshold)
