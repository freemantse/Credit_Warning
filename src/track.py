"""
CLI tool: add an issuer to the tracked portfolio and display its credit ratios.

This module is the command-line equivalent of the POST /api/track endpoint.
Use it for quick manual checks or when running outside a web server context.

What it does:
  1. Resolves the ticker to a CIK via EDGAR.
  2. Fetches the full XBRL companyfacts JSON (cached to disk).
  3. Finds all available annual periods (fiscal year-end dates).
  4. For each of the N most recent periods:
     a. Extracts all five credit ratios from the XBRL data.
     b. Saves the ratios to Supabase.
     c. Optionally fetches the 10-K text and runs an LLM qualitative review.
     d. Computes the stress score.
  5. Prints a formatted table of ratios and scores across all periods.
  6. Prints a source audit for the most recent leverage figure, showing
     exactly which XBRL tags were used and what their raw values were.

Usage:
    python -m src.track AAPL
    python -m src.track AAPL --no-llm        # skip the LLM qualitative pass
    python -m src.track AAPL --periods 8     # show 8 most recent annual periods
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from src.ingest import (
    get_company_facts,
    get_company_info,
    get_filings,
    get_filing_text,
    resolve_identifier,
)
from src.extract import (
    extract_all,
    RatioResult,
    _get_available_periods,
    debt_maturity_schedule,
)
from src.store import (
    save_company,
    save_ratios_bulk,
    save_findings,
    save_maturities_bulk,
    save_covenants,
    save_loss_provisions,
    get_periods,
)
from src.score import compute_score, STRESS_THRESHOLD
from src.concepts import MissingDataError


# ── Display helpers ──────────────────────────────────────────────────────────

def _fmt(val: float | None, decimals: int = 2) -> str:
    """
    Format a float for fixed-width table display.

    Uses right-aligned formatting with the given number of decimal places.
    Returns '  N/A  ' (same character width) when the value is None so that
    table columns stay aligned even when some ratios are missing.
    """
    if val is None:
        return "  N/A  "
    return f"{val:>7.{decimals}f}"


def _display_table(
    ticker: str,
    periods: list[str],
    all_results: list[dict],
    all_scores: list,
) -> None:
    """
    Print a fixed-width ASCII table summarising ratios and scores across periods.

    Layout: one row per fiscal period. Columns are Leverage, Coverage, FCF (in $M),
    Liquidity, Score, and triggered Alerts.

    Periods are shown in the order passed — _get_available_periods() returns
    newest-first, so the most recent year appears at the top.

    Rows that breach STRESS_THRESHOLD are marked with '*** STRESS ***' at the end.
    FCF is converted from raw dollars to millions for human readability.
    """
    header = (
        f"{'Period':<14} {'Leverage':>9} {'Coverage':>9} "
        f"{'FCF ($M)':>10} {'Liquidity':>10} {'Score':>6}  Alerts"
    )
    print(f"\n{'='*90}")
    print(f"  {ticker} — Credit Ratio Summary")
    print(f"{'='*90}")
    print(header)
    print("-" * 90)

    for period, results, score_result in zip(periods, all_results, all_scores):

        # Helper to extract a ratio value from the results dict.
        # Returns None if the ratio was missing (MissingDataError).
        def _v(name: str) -> float | None:
            r = results.get(name)
            return r.value if isinstance(r, RatioResult) else None

        lev = _v("leverage")
        cov = _v("interest_coverage")
        fcf = _v("free_cash_flow")
        liq = _v("liquidity")

        # Convert raw dollar FCF to millions for display (e.g. 5000000000 → 5000.0).
        fcf_m = f"{fcf/1e6:>8.1f}" if fcf is not None else "     N/A"

        score_str = f"{score_result.score:>5.0f}"

        # Join alert messages with "; " or show a dash if there are no alerts.
        alert_str = "; ".join(score_result.alerts) if score_result.alerts else "—"

        # Append a visual warning flag for stressed periods.
        flag = " *** STRESS ***" if score_result.score >= STRESS_THRESHOLD else ""

        print(
            f"{period:<14} {_fmt(lev):>9} {_fmt(cov):>9} {fcf_m:>10} "
            f"{_fmt(liq):>10} {score_str:>6}  {alert_str}{flag}"
        )
    print("=" * 90)


# ── Main tracking function ───────────────────────────────────────────────────

def track(ticker: str, n_periods: int | None = None, include_llm: bool = True) -> None:
    """
    Fetch, extract, store, and display credit ratios for a ticker.

    Args:
        ticker:      Stock ticker symbol (case-insensitive).
        n_periods:   Number of most recent annual periods to process.
                     None (the default) processes the full available history
                     (~15 years — XBRL data only goes back to ~2009).
        include_llm: If True, attempt an LLM qualitative review for each period.
                     Set False (--no-llm) to skip and run faster.
    """
    ticker = ticker.upper()
    print(f"Tracking {ticker}...")

    # Step 1: Resolve the input (ticker OR CIK) → canonical CIK. Raises ValueError if not found.
    cik = resolve_identifier(ticker)
    print(f"  CIK: {cik}")

    # Step 2: Persist the company identity snapshot (name, current tickers, former names),
    # keyed on the permanent CIK, so reads can map CIK ↔ ticker without an EDGAR call.
    save_company(get_company_info(cik))

    # Step 3: Fetch the full XBRL companyfacts JSON from EDGAR (cached after first fetch).
    facts = get_company_facts(cik)

    # Step 3: Find available annual periods. Process the full history by default
    # (n_periods=None); a caller may still cap it to the N most recent.
    available = _get_available_periods(facts)
    periods = available if n_periods is None else available[:n_periods]

    if not periods:
        print("  No annual periods found in XBRL data.")
        return

    print(f"  Extracting ratios for {len(periods)} periods...")

    all_results = []   # one dict of {ratio_name: RatioResult} per period
    all_scores = []    # one ScoreResult per period

    # Step 4a: Extract every period (pure compute) and persist them in ONE bulk
    # upsert. Writing per-period was ~18 DB round-trips — the slowest part of a
    # track. Findings (below) still save per-period since each needs its own LLM call.
    results_by_period = {period: extract_all(facts, period) for period in periods}
    save_ratios_bulk(cik, results_by_period)

    # Step 4b: Debt maturity schedules are pure XBRL compute (no LLM, no filing
    # fetch), so extract them for every period and bulk-save alongside the ratios.
    maturities_by_period = {
        period: debt_maturity_schedule(facts, period) for period in periods
    }
    save_maturities_bulk(cik, maturities_by_period)

    for period in periods:

        results = results_by_period[period]
        maturity = maturities_by_period[period]

        findings = []
        covenants = []
        provisions = []
        if include_llm:
            try:
                # Step 4c: Locate and review the period's 10-K. The MD&A pass uses
                # the first 12 000 chars; the footnote pass locates the debt and
                # contingencies sections deep in the document (which the 12k slice
                # never reaches) and extracts covenants + loss provisions.
                filings = get_filings(cik, ["10-K"])
                matching = [f for f in filings if period[:4] in f["filingDate"]]

                if matching:
                    filing = matching[0]  # use the first (most recent) match for this year
                    text = get_filing_text(
                        cik, filing["accessionNumber"], filing["primaryDocument"]
                    )

                    from src.llm_review import review_text
                    findings = review_text(text[:12000], f"10-K {period}")
                    save_findings(cik, period, findings)

                    from src.footnote_review import review_filing_footnotes
                    covenants, provisions = review_filing_footnotes(cik, period, filings)
                    save_covenants(cik, period, covenants)
                    save_loss_provisions(cik, period, provisions)

            except Exception as e:
                # LLM review is best-effort — ratio data is already saved.
                # Print the reason so the user can see what went wrong.
                print(f"  [LLM review skipped for {period}: {e}]")

        # Step 4d: Compute the stress score from ratios, findings, and footnotes.
        score_result = compute_score(results, findings, maturity, covenants, provisions)
        all_results.append(results)
        all_scores.append(score_result)

    # Step 5: Print the summary table.
    _display_table(ticker, periods, all_results, all_scores)

    # Step 6: Print a source audit for the most recent leverage calculation.
    # This shows the analyst exactly which XBRL tags were used and what the
    # raw dollar values were, making it easy to spot data quality issues.
    latest_period = periods[0]   # periods is newest-first
    latest = all_results[0].get("leverage")
    if isinstance(latest, RatioResult):
        print(f"\n  Leverage audit ({latest_period}):")
        for k, v in latest.inputs.items():
            tag = latest.source_tags.get(k, "?")
            # Format large numbers with thousands separators for readability.
            print(f"    {k}: {v:,.0f}  [{tag}]")


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Track credit ratios for an issuer")
    parser.add_argument("ticker", help="Stock ticker symbol, e.g. AAPL")
    parser.add_argument(
        "--no-llm", action="store_true",
        help="Skip LLM qualitative review (faster)"
    )
    parser.add_argument(
        "--periods", type=int, default=None,
        help="Number of most recent annual periods to show (default: full history)"
    )
    args = parser.parse_args()

    track(args.ticker, n_periods=args.periods, include_llm=not args.no_llm)
