"""
CLI: add an issuer to track and display its key ratios + stress score over time.

Usage:
    python -m src.track AAPL
    python -m src.track AAPL --no-llm        # skip qualitative LLM pass
    python -m src.track AAPL --periods 8     # how many annual periods to show
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from src.ingest import get_cik, get_company_facts, get_filings, get_filing_text
from src.extract import extract_all, RatioResult, _get_available_periods
from src.store import save_ratios, save_findings, get_ratio_history, get_periods
from src.score import compute_score, STRESS_THRESHOLD
from src.concepts import MissingDataError


def _fmt(val: float | None, decimals: int = 2) -> str:
    if val is None:
        return "  N/A  "
    return f"{val:>7.{decimals}f}"


def _display_table(ticker: str, periods: list[str], all_results: list[dict], all_scores: list) -> None:
    header = f"{'Period':<14} {'Leverage':>9} {'Coverage':>9} {'FCF ($M)':>10} {'Liquidity':>10} {'Score':>6}  Alerts"
    print(f"\n{'='*90}")
    print(f"  {ticker} — Credit Ratio Summary")
    print(f"{'='*90}")
    print(header)
    print("-" * 90)

    for period, results, score_result in zip(periods, all_results, all_scores):
        def _v(name: str) -> float | None:
            r = results.get(name)
            return r.value if isinstance(r, RatioResult) else None

        lev = _v("leverage")
        cov = _v("interest_coverage")
        fcf = _v("free_cash_flow")
        liq = _v("liquidity")

        fcf_m = f"{fcf/1e6:>8.1f}" if fcf is not None else "     N/A"
        score_str = f"{score_result.score:>5.0f}"
        alert_str = "; ".join(score_result.alerts) if score_result.alerts else "—"
        flag = " *** STRESS ***" if score_result.score >= STRESS_THRESHOLD else ""

        print(
            f"{period:<14} {_fmt(lev):>9} {_fmt(cov):>9} {fcf_m:>10} {_fmt(liq):>10} "
            f"{score_str:>6}  {alert_str}{flag}"
        )
    print("=" * 90)


def track(ticker: str, n_periods: int = 8, include_llm: bool = True) -> None:
    ticker = ticker.upper()
    print(f"Tracking {ticker}...")

    cik = get_cik(ticker)
    print(f"  CIK: {cik}")

    facts = get_company_facts(cik)
    available = _get_available_periods(facts)
    periods = available[:n_periods]

    if not periods:
        print("  No annual periods found in XBRL data.")
        return

    print(f"  Extracting ratios for {len(periods)} periods...")

    all_results = []
    all_scores = []

    for period in periods:
        results = extract_all(facts, period)
        save_ratios(ticker, period, results)

        findings = []
        if include_llm:
            # Try to fetch the most recent 10-K text for this period
            try:
                filings = get_filings(cik, ["10-K"])
                # Find the 10-K filing closest to this period_end
                matching = [f for f in filings if period[:4] in f["filingDate"]]
                if matching:
                    filing = matching[0]
                    text = get_filing_text(cik, filing["accessionNumber"], filing["primaryDocument"])
                    from src.llm_review import review_text
                    label = f"10-K {period}"
                    findings = review_text(text[:12000], label)
                    save_findings(ticker, period, findings)
            except Exception as e:
                print(f"  [LLM review skipped for {period}: {e}]")

        score_result = compute_score(results, findings)
        all_results.append(results)
        all_scores.append(score_result)

    _display_table(ticker, periods, all_results, all_scores)

    # Show source audit for leverage (most recent period)
    latest_period = periods[0]
    latest = all_results[0].get("leverage")
    if isinstance(latest, RatioResult):
        print(f"\n  Leverage audit ({latest_period}):")
        for k, v in latest.inputs.items():
            tag = latest.source_tags.get(k, "?")
            print(f"    {k}: {v:,.0f}  [{tag}]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Track credit ratios for an issuer")
    parser.add_argument("ticker", help="Stock ticker symbol, e.g. AAPL")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM qualitative review")
    parser.add_argument("--periods", type=int, default=8, help="Number of annual periods to show")
    args = parser.parse_args()

    track(args.ticker, n_periods=args.periods, include_llm=not args.no_llm)
