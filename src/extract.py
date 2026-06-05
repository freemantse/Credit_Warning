"""
Deterministic ratio extraction from EDGAR companyfacts JSON.

Every function returns a RatioResult carrying the computed value,
the raw inputs used, and the winning XBRL source tags — fully auditable.
Missing data raises MissingDataError (never silently returns 0 or a guess).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from src.concepts import resolve_tag, MissingDataError, TAGS


@dataclass
class RatioResult:
    name: str
    value: float
    inputs: dict[str, float]
    source_tags: dict[str, str]  # input_name → winning XBRL tag
    period_end: str


def _resolve(facts: dict, concept: str, period_end: str, filed_before: str | None = None) -> tuple[float, str]:
    return resolve_tag(facts, concept, period_end, filed_before=filed_before)


def ebitda(facts: dict, period_end: str, filed_before: str | None = None) -> tuple[float, dict, dict]:
    """Derived EBITDA = operating_income + depreciation. Returns (value, inputs, tags)."""
    op_inc, op_tag = _resolve(facts, "operating_income", period_end, filed_before)
    dep, dep_tag = _resolve(facts, "depreciation", period_end, filed_before)
    value = op_inc + dep
    return value, {"operating_income": op_inc, "depreciation": dep}, \
           {"operating_income": op_tag, "depreciation": dep_tag}


def net_debt(facts: dict, period_end: str, filed_before: str | None = None) -> tuple[float, dict, dict]:
    """Net debt = total_debt - cash."""
    debt, debt_tag = _resolve(facts, "total_debt", period_end, filed_before)
    cash, cash_tag = _resolve(facts, "cash", period_end, filed_before)
    value = debt - cash
    return value, {"total_debt": debt, "cash": cash}, \
           {"total_debt": debt_tag, "cash": cash_tag}


def leverage(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """Leverage = net_debt / EBITDA."""
    nd, nd_inputs, nd_tags = net_debt(facts, period_end, filed_before)
    ebit, ebit_inputs, ebit_tags = ebitda(facts, period_end, filed_before)
    if ebit == 0:
        raise MissingDataError(f"EBITDA is zero for {period_end}, cannot compute leverage")
    return RatioResult(
        name="leverage",
        value=nd / ebit,
        inputs={**nd_inputs, **ebit_inputs},
        source_tags={**nd_tags, **ebit_tags},
        period_end=period_end,
    )


def interest_coverage(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """Interest coverage = EBITDA / interest_expense."""
    ebit, ebit_inputs, ebit_tags = ebitda(facts, period_end, filed_before)
    int_exp, int_tag = _resolve(facts, "interest_expense", period_end, filed_before)
    if int_exp == 0:
        raise MissingDataError(f"Interest expense is zero for {period_end}")
    return RatioResult(
        name="interest_coverage",
        value=ebit / int_exp,
        inputs={**ebit_inputs, "interest_expense": int_exp},
        source_tags={**ebit_tags, "interest_expense": int_tag},
        period_end=period_end,
    )


def free_cash_flow(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """Free cash flow = operating_cashflow - capex."""
    ocf, ocf_tag = _resolve(facts, "operating_cashflow", period_end, filed_before)
    capex, capex_tag = _resolve(facts, "capex", period_end, filed_before)
    return RatioResult(
        name="free_cash_flow",
        value=ocf - capex,
        inputs={"operating_cashflow": ocf, "capex": capex},
        source_tags={"operating_cashflow": ocf_tag, "capex": capex_tag},
        period_end=period_end,
    )


def fcf_margin(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """FCF margin = free_cash_flow / revenue."""
    fcf_result = free_cash_flow(facts, period_end, filed_before)
    rev, rev_tag = _resolve(facts, "revenue", period_end, filed_before)
    if rev == 0:
        raise MissingDataError(f"Revenue is zero for {period_end}, cannot compute FCF margin")
    return RatioResult(
        name="fcf_margin",
        value=fcf_result.value / rev,
        inputs={**fcf_result.inputs, "revenue": rev},
        source_tags={**fcf_result.source_tags, "revenue": rev_tag},
        period_end=period_end,
    )


def liquidity(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """Liquidity = cash / short_term_debt. Signals near-term coverage."""
    cash, cash_tag = _resolve(facts, "cash", period_end, filed_before)
    st_debt, st_tag = _resolve(facts, "short_term_debt", period_end, filed_before)
    if st_debt == 0:
        raise MissingDataError(f"Short-term debt is zero for {period_end}, liquidity ratio undefined")
    return RatioResult(
        name="liquidity",
        value=cash / st_debt,
        inputs={"cash": cash, "short_term_debt": st_debt},
        source_tags={"cash": cash_tag, "short_term_debt": st_tag},
        period_end=period_end,
    )


_RATIO_FUNCTIONS = [leverage, interest_coverage, free_cash_flow, fcf_margin, liquidity]


def extract_all(
    facts: dict,
    period_end: str,
    filed_before: str | None = None,
) -> dict[str, RatioResult | MissingDataError]:
    """
    Run all ratio functions for a given period.
    Returns a dict of ratio_name → RatioResult (or MissingDataError if data absent).
    Never raises — missing data is recorded, not propagated.
    """
    results: dict[str, RatioResult | MissingDataError] = {}
    for fn in _RATIO_FUNCTIONS:
        try:
            result = fn(facts, period_end, filed_before)
            results[result.name] = result
        except MissingDataError as e:
            results[fn.__name__] = e
    return results


def _get_available_periods(facts: dict) -> list[str]:
    """Return annual fiscal-year-end dates, newest first.

    Anchored on the balance-sheet date (total assets), which every 10-K reports
    exactly once per fiscal year-end (current year + one comparative). Its 'end'
    dates therefore *are* the fiscal year ends. Scanning every concept instead
    (the naive approach) pulls in spurious dates: quarterly revenue
    disaggregations, acquisition-date fair values, and cover-page share counts
    all carry form=="10-K" but are not annual period ends.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    anchor_tags = [t.split("/", 1)[1] for t in TAGS["total_assets"]]
    periods: set[str] = set()
    for tag in anchor_tags:
        concept_data = us_gaap.get(tag)
        if not concept_data:
            continue
        for entries in concept_data.get("units", {}).values():
            for entry in entries:
                if entry.get("form") == "10-K" and entry.get("end"):
                    periods.add(entry["end"])
    if periods:
        return sorted(periods, reverse=True)

    # Fallback: issuer doesn't tag total assets — scan all concepts but keep
    # only full-year durations (~350+ days), dropping quarterly periods.
    from datetime import date

    for concept_data in us_gaap.values():
        for entries in concept_data.get("units", {}).values():
            for entry in entries:
                if entry.get("form") != "10-K" or not entry.get("end"):
                    continue
                start = entry.get("start")
                if start:
                    try:
                        span = date.fromisoformat(entry["end"]) - date.fromisoformat(start)
                    except ValueError:
                        continue
                    if span.days < 350:
                        continue
                periods.add(entry["end"])
    return sorted(periods, reverse=True)


if __name__ == "__main__":
    from src.ingest import get_cik, get_company_facts

    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(f"Extracting ratios for {ticker}...")
    cik = get_cik(ticker)
    facts = get_company_facts(cik)

    periods = _get_available_periods(facts)[:8]
    print(f"  Found {len(periods)} annual periods: {periods[:4]}...\n")

    for period in periods[:4]:
        print(f"Period: {period}")
        results = extract_all(facts, period)
        for name, result in results.items():
            if isinstance(result, RatioResult):
                print(f"  {name:20s} = {result.value:>10.2f}  (tags: {list(result.source_tags.values())})")
            else:
                print(f"  {name:20s} = MISSING: {result}")
        print()
