"""
Point-in-time backtest harness.

For each case in data/cases.csv:
- Distressed: score the issuer using ONLY data available before the event_date.
  Measure whether it was flagged (score >= threshold) and by how many months.
- Healthy: count false-positive periods (score >= threshold).

Critical invariant: no look-ahead. The filed_before filter in extract/concepts
ensures only filings with filed_date <= eval_date are used.

Usage:
    python -m src.backtest
    python -m src.backtest --threshold 40   # adjust stress threshold
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys
from datetime import date, datetime
from typing import Any

from src.ingest import get_cik, get_company_facts, get_filings
from src.extract import extract_all, _get_available_periods
from src.score import compute_score, STRESS_THRESHOLD
from src.concepts import MissingDataError

CASES_PATH = pathlib.Path(__file__).parent.parent / "data" / "cases.csv"
REPORT_PATH = pathlib.Path(__file__).parent.parent / "data" / "backtest_report.txt"


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def _months_between(d1: date, d2: date) -> float:
    """Approximate months between two dates."""
    return (d2 - d1).days / 30.44


def _filter_periods_point_in_time(facts: dict, eval_date: date) -> list[str]:
    """
    Return annual period-end dates where the filing was available on or before eval_date.
    This enforces the no-look-ahead invariant.
    """
    eval_str = eval_date.isoformat()
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    valid_periods: set[str] = set()

    for concept_data in us_gaap.values():
        for entries in concept_data.get("units", {}).values():
            for entry in entries:
                if entry.get("form") == "10-K" and entry.get("end") and entry.get("filed"):
                    if entry["filed"] <= eval_str:
                        valid_periods.add(entry["end"])

    return sorted(valid_periods, reverse=True)


def score_issuer_at_date(
    facts: dict,
    eval_date: date,
    threshold: int,
) -> tuple[float, bool]:
    """
    Score an issuer using only data available at eval_date.
    Returns (score, is_stressed).
    """
    eval_str = eval_date.isoformat()
    periods = _filter_periods_point_in_time(facts, eval_date)
    if not periods:
        return 0.0, False

    # Use the most recent available period
    latest_period = periods[0]
    results = extract_all(facts, latest_period, filed_before=eval_str)
    score_result = compute_score(results, [])
    return score_result.score, score_result.score >= threshold


def run_backtest(threshold: int = STRESS_THRESHOLD) -> dict:
    """
    Run the full backtest and return a results dict.
    Also writes a report to data/backtest_report.txt.
    """
    if not CASES_PATH.exists():
        raise FileNotFoundError(f"Case library not found: {CASES_PATH}")

    with open(CASES_PATH, newline="") as f:
        cases = list(csv.DictReader(f))

    lines = []
    lines.append(f"Credit Warning Backtest — threshold={threshold}")
    lines.append("=" * 70)

    cases_output: list[dict] = []
    distressed_results: list[dict] = []
    healthy_fp_counts: list[int] = []

    for case in cases:
        ticker = case["ticker"].strip()
        label = case["label"].strip()
        event_date = _parse_date(case.get("event_date", ""))

        print(f"  Processing {ticker} ({label})...", end=" ", flush=True)
        try:
            cik = get_cik(ticker)
            facts = get_company_facts(cik)
        except Exception as e:
            err = f"ERROR: {e}"
            print(err)
            lines.append(f"{ticker:<8} {label:<12} {err}")
            cases_output.append({"ticker": ticker, "label": label, "error": str(e)})
            continue

        if label == "distressed" and event_date:
            first_flag_date: date | None = None
            scan_date = event_date
            for _ in range(12):
                scan_date = date.fromordinal(scan_date.toordinal() - 90)
                if scan_date < date(event_date.year - 3, event_date.month, event_date.day):
                    break
                score, stressed = score_issuer_at_date(facts, scan_date, threshold)
                if stressed:
                    first_flag_date = scan_date

            if first_flag_date:
                lead = _months_between(first_flag_date, event_date)
                line = f"{ticker:<8} {label:<12} FLAGGED {lead:.0f} months early ✓  (event: {event_date})"
                distressed_results.append({"caught": True, "lead_months": lead})
                cases_output.append({"ticker": ticker, "label": label,
                                     "event_date": str(event_date), "caught": True,
                                     "lead_months": round(lead, 1), "error": None})
            else:
                line = f"{ticker:<8} {label:<12} MISSED — never reached threshold ✗  (event: {event_date})"
                distressed_results.append({"caught": False, "lead_months": 0})
                cases_output.append({"ticker": ticker, "label": label,
                                     "event_date": str(event_date), "caught": False,
                                     "lead_months": 0, "error": None})

        elif label == "healthy":
            fp_count = 0
            eval_date_h = date.today()
            for _ in range(12):
                score, stressed = score_issuer_at_date(facts, eval_date_h, threshold)
                if stressed:
                    fp_count += 1
                eval_date_h = date.fromordinal(eval_date_h.toordinal() - 90)
            line = f"{ticker:<8} {label:<12} {fp_count} false-positive periods"
            healthy_fp_counts.append(fp_count)
            cases_output.append({"ticker": ticker, "label": label,
                                  "fp_count": fp_count, "error": None})
        else:
            line = f"{ticker:<8} {label:<12} SKIPPED (no event_date for distressed)"
            cases_output.append({"ticker": ticker, "label": label, "error": "no event_date"})

        print(line.split("  ")[-1] if "  " in line else "done")
        lines.append(line)

    lines.append("-" * 70)

    caught = sum(1 for r in distressed_results if r["caught"])
    total_d = len(distressed_results)
    catch_rate = (caught / total_d * 100) if total_d else 0

    lead_times = [r["lead_months"] for r in distressed_results if r["caught"]]
    median_lead = sorted(lead_times)[len(lead_times) // 2] if lead_times else 0

    total_fp = sum(healthy_fp_counts)
    total_healthy_periods = len(healthy_fp_counts) * 12
    fp_rate = (total_fp / total_healthy_periods * 100) if total_healthy_periods else 0

    summary = (
        f"Catch rate: {catch_rate:.0f}% ({caught}/{total_d})  |  "
        f"Median lead: {median_lead:.0f} months  |  "
        f"False-positive rate: {fp_rate:.1f}%"
    )
    lines.append(summary)

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the credit warning backtest")
    parser.add_argument("--threshold", type=int, default=STRESS_THRESHOLD,
                        help=f"Stress score threshold (default: {STRESS_THRESHOLD})")
    args = parser.parse_args()

    run_backtest(threshold=args.threshold)
