"""
Deterministic ratio extraction from EDGAR companyfacts JSON.

Every public function returns a RatioResult carrying the computed value,
the raw XBRL inputs used, and the winning XBRL source tags — fully auditable.
Missing data raises MissingDataError (never silently returns 0 or a guess).

How the module fits into the system:
  ingest.py  →  get_company_facts(cik)  →  facts dict
  extract.py →  extract_all(facts, period_end)  →  dict of RatioResult objects
  store.py   →  save_ratios(ticker, period, results)  →  Supabase
  score.py   →  compute_score(results, findings)  →  ScoreResult

Ratios computed:
  leverage          = net_debt / EBITDA
  interest_coverage = EBITDA / interest_expense
  free_cash_flow    = operating_cashflow - capex
  fcf_margin        = free_cash_flow / revenue
  liquidity         = cash / short_term_debt
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from src.concepts import resolve_tag, MissingDataError, TAGS


# ── Data container ───────────────────────────────────────────────────────────

@dataclass
class RatioResult:
    """
    Immutable container for one computed ratio value plus its full audit trail.

    Every ratio function returns one of these so downstream code (score.py,
    store.py, the API) gets not just the number but also exactly which XBRL tags
    were used and what the raw input values were.

    Example for leverage:
        name        = "leverage"
        value       = 3.2          # net_debt / EBITDA = 3.2×
        inputs      = {"total_debt": 8e9, "cash": 2e9,
                        "operating_income": 2e9, "depreciation": 0.5e9}
        source_tags = {"total_debt": "us-gaap/LongTermDebt",
                        "cash": "us-gaap/CashAndCashEquivalentsAtCarryingValue",
                        "operating_income": "us-gaap/OperatingIncomeLoss",
                        "depreciation": "us-gaap/DepreciationDepletionAndAmortization"}
        period_end  = "2023-09-30"
    """
    name: str                       # ratio identifier, e.g. "leverage"
    value: float                    # the computed ratio
    inputs: dict[str, float]        # raw dollar values used in the formula
    source_tags: dict[str, str]     # maps each input name to its winning XBRL tag
    period_end: str                 # fiscal year-end date, e.g. "2023-09-30"


@dataclass
class MissingRatio:
    """
    Audit record for a ratio that could NOT be computed for one period.

    Parallels RatioResult so the source-audit panel can still render a card for the
    ratio and show the analyst exactly which raw XBRL input is missing.

    Two flavours of "missing":
      1. An input concept didn't resolve from XBRL — `missing_inputs` lists each
         such field with the tags that were tried. Any inputs that DID resolve are
         carried in `inputs`/`source_tags` so the card stays informative.
      2. All inputs resolved but a guard made the ratio undefined (e.g. zero EBITDA).
         Then `missing_inputs` is empty and `reason` explains why.

    Example (missing total_debt for leverage):
        name           = "leverage"
        inputs         = {"cash": 2e9, "operating_income": 2e9, "depreciation": 0.5e9}
        source_tags    = {"cash": "us-gaap/CashAndCashEquivalentsAtCarryingValue", ...}
        missing_inputs = [{"field": "total_debt", "tags_tried": ["us-gaap/LongTermDebt", ...]}]
        reason         = "Concept 'total_debt' not found for period_end='2023-09-30'. Tried: ..."
    """
    name: str                            # ratio identifier, e.g. "leverage"
    period_end: str                      # fiscal year-end date
    inputs: dict[str, float]             # the subset of inputs that DID resolve
    source_tags: dict[str, str]          # winning XBRL tag per resolved input
    missing_inputs: list[dict]           # [{"field": str, "tags_tried": [str, ...]}]
    reason: str                          # the original MissingDataError message


@dataclass
class MaturitySchedule:
    """
    Deterministic long-term-debt maturity schedule for one fiscal period.

    Built entirely from XBRL companyfacts tags (no LLM, no HTML parsing), so it
    carries the same audit guarantee as RatioResult: every bucket value maps to
    the exact us-gaap tag that supplied it.

    The "maturity wall" is the near-term concentration of principal coming due:
    a high near_term_pct means a large share of debt must be refinanced soon.

    Attributes:
        period_end:      Fiscal year-end the schedule is reported as of.
        buckets:         {bucket_name: principal_due} for whichever buckets the
                         filer tagged. Keys are "y1".."y5" and "thereafter".
        source_tags:     {bucket_name: winning_xbrl_tag} audit trail.
        total_scheduled: Sum of all resolved buckets.
        near_term_pct:   (y1 + y2) / total_scheduled, or None when total is 0
                         (so the maturity-wall score rule is suppressed, not
                         mis-computed, on unreliable data).
        wall_year:       Bucket with the largest principal due, or None if empty.
    """
    period_end: str
    buckets: dict[str, float]
    source_tags: dict[str, str]
    total_scheduled: float
    near_term_pct: float | None
    wall_year: str | None


# Ordered maturity buckets: concept name (in TAGS) → display/short key.
_MATURITY_BUCKETS = [
    ("debt_maturity_y1", "y1"),
    ("debt_maturity_y2", "y2"),
    ("debt_maturity_y3", "y3"),
    ("debt_maturity_y4", "y4"),
    ("debt_maturity_y5", "y5"),
    ("debt_maturity_thereafter", "thereafter"),
]


# ── Private helper ───────────────────────────────────────────────────────────

def _resolve(facts: dict, concept: str, period_end: str, filed_before: str | None = None) -> tuple[float, str]:
    """
    Thin wrapper around concepts.resolve_tag.
    Kept private because only this module calls it directly.
    Returns (value, winning_tag_path).
    """
    return resolve_tag(facts, concept, period_end, filed_before=filed_before)


# ── Intermediate building-block helpers ─────────────────────────────────────
#
# ebitda() and net_debt() are NOT public ratio functions — they are shared
# building blocks used internally by leverage() and interest_coverage().
# They return raw (value, inputs_dict, tags_dict) tuples instead of RatioResult
# so that the ratio functions can merge their inputs/tags into one flat dict
# for the final RatioResult audit trail.

def ebitda(facts: dict, period_end: str, filed_before: str | None = None) -> tuple[float, dict, dict]:
    """
    Compute EBITDA = operating_income + depreciation for a given period.

    EDGAR doesn't provide an EBITDA tag directly, so we derive it by adding
    back the depreciation & amortisation charge to operating income.
    This is the standard EBITDA derivation used in credit analysis.

    Returns:
        (ebitda_value, inputs_dict, source_tags_dict)
        e.g. (2.5e9, {"operating_income": 2e9, "depreciation": 0.5e9}, {...})
    """
    # Fetch operating income and the XBRL tag that supplied it.
    op_inc, op_tag = _resolve(facts, "operating_income", period_end, filed_before)
    # Fetch D&A and the XBRL tag that supplied it.
    dep, dep_tag = _resolve(facts, "depreciation", period_end, filed_before)
    value = op_inc + dep
    # Return the merged inputs and tags dicts so callers can include them in
    # the final RatioResult.inputs and RatioResult.source_tags.
    return (
        value,
        {"operating_income": op_inc, "depreciation": dep},
        {"operating_income": op_tag, "depreciation": dep_tag},
    )


def net_debt(facts: dict, period_end: str, filed_before: str | None = None) -> tuple[float, dict, dict]:
    """
    Compute net debt = total_debt - cash for a given period.

    Net debt is the leverage numerator. A negative result means the company
    holds more cash than debt — which makes leverage negative (financial strength).

    Returns:
        (net_debt_value, inputs_dict, source_tags_dict)
    """
    debt, debt_tag = _resolve(facts, "total_debt", period_end, filed_before)
    cash, cash_tag = _resolve(facts, "cash", period_end, filed_before)
    value = debt - cash  # positive = more debt than cash (typical); negative = net cash
    return (
        value,
        {"total_debt": debt, "cash": cash},
        {"total_debt": debt_tag, "cash": cash_tag},
    )


# ── Public ratio functions ───────────────────────────────────────────────────
#
# Each function takes the same three parameters (facts, period_end, filed_before)
# and returns a RatioResult. They are collected into _RATIO_FUNCTIONS below so
# extract_all() can iterate them without listing them by name.

def leverage(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Leverage = net_debt / EBITDA.

    Interpretation:
      < 3×  — typical investment-grade issuer
      3–5×  — watch territory (elevated but manageable)
      > 5×  — stress rule triggers (+25 pts to the stress score)

    The dict-merge pattern {**nd_inputs, **ebit_inputs} combines the raw inputs
    from both net_debt and ebitda into a single flat dict for the audit trail.

    Raises MissingDataError if EBITDA is zero (division undefined).
    """
    nd, nd_inputs, nd_tags = net_debt(facts, period_end, filed_before)
    ebit, ebit_inputs, ebit_tags = ebitda(facts, period_end, filed_before)

    if ebit == 0:
        # Zero EBITDA makes the ratio undefined. Raise rather than return inf/nan.
        raise MissingDataError(f"EBITDA is zero for {period_end}, cannot compute leverage")

    return RatioResult(
        name="leverage",
        value=nd / ebit,
        # Merge the four input values (total_debt, cash, operating_income, depreciation)
        # into one flat dict for display in the source audit panel.
        inputs={**nd_inputs, **ebit_inputs},
        source_tags={**nd_tags, **ebit_tags},
        period_end=period_end,
    )


def interest_coverage(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Interest coverage = EBITDA / interest_expense.

    Interpretation:
      > 4×  — comfortable — EBITDA covers interest many times over
      2–4×  — watch territory
      < 2×  — stress rule triggers (+25 pts to the stress score)

    Raises MissingDataError if interest expense is zero (company has no
    interest-bearing debt, making the ratio undefined/meaningless).
    """
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
    """
    Free cash flow = operating_cashflow - capex.

    FCF measures how much real cash the company generates after maintaining/expanding
    its asset base. Negative FCF means the company spent more cash on operations
    and investment than it brought in — a stress signal (+20 pts).

    Note: EDGAR reports capex (PaymentsToAcquirePropertyPlantAndEquipment) as a
    POSITIVE outflow number, so we subtract it from OCF.
    """
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
    """
    FCF margin = free_cash_flow / revenue.

    Normalises FCF by revenue so it is size-agnostic and comparable across
    issuers. A 10% FCF margin means the company converts 10 cents of every
    revenue dollar into free cash.

    Not directly used in the stress score, but stored for trend analysis.
    Raises MissingDataError if revenue is zero (e.g. pre-revenue companies).
    """
    # Re-use the free_cash_flow function — avoids fetching OCF and capex twice.
    fcf_result = free_cash_flow(facts, period_end, filed_before)
    rev, rev_tag = _resolve(facts, "revenue", period_end, filed_before)

    if rev == 0:
        raise MissingDataError(f"Revenue is zero for {period_end}, cannot compute FCF margin")

    return RatioResult(
        name="fcf_margin",
        # Divide FCF by revenue to get the margin as a decimal fraction (e.g. 0.12 = 12%).
        value=fcf_result.value / rev,
        # Merge FCF's inputs (OCF + capex) with revenue into one flat audit dict.
        inputs={**fcf_result.inputs, "revenue": rev},
        source_tags={**fcf_result.source_tags, "revenue": rev_tag},
        period_end=period_end,
    )


def liquidity(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Liquidity = cash / short_term_debt.

    Measures near-term solvency: can the company cover its maturing obligations
    using cash on its balance sheet today?

    Interpretation:
      > 1×  — cash exceeds near-term debt (healthy)
      < 1×  — stress rule triggers (+20 pts); company may need to refinance
               or draw on credit lines to meet maturities

    Raises MissingDataError if short-term debt is zero — not necessarily bad,
    it just means the ratio is undefined (no short-term debt to cover).
    """
    cash, cash_tag = _resolve(facts, "cash", period_end, filed_before)
    st_debt, st_tag = _resolve(facts, "short_term_debt", period_end, filed_before)

    if st_debt == 0:
        # Undefined, not a stress signal. Raise so the score treats it as missing.
        raise MissingDataError(f"Short-term debt is zero for {period_end}, liquidity ratio undefined")

    return RatioResult(
        name="liquidity",
        value=cash / st_debt,
        inputs={"cash": cash, "short_term_debt": st_debt},
        source_tags={"cash": cash_tag, "short_term_debt": st_tag},
        period_end=period_end,
    )


def debt_maturity_schedule(
    facts: dict,
    period_end: str,
    filed_before: str | None = None,
) -> MaturitySchedule:
    """
    Build the long-term-debt maturity schedule from XBRL for one period.

    Unlike the ratio functions, this NEVER raises on missing data: a filer may
    tag only some buckets (or none). Each bucket is resolved independently and a
    per-bucket MissingDataError is simply skipped. The returned schedule reflects
    whatever buckets were available — possibly empty.

    Derived metrics are pure arithmetic over the resolved buckets:
      total_scheduled = sum of all buckets
      near_term_pct   = (y1 + y2) / total_scheduled   (None if total is 0)
      wall_year       = bucket with the largest principal due (None if empty)
    """
    buckets: dict[str, float] = {}
    source_tags: dict[str, str] = {}

    for concept, key in _MATURITY_BUCKETS:
        try:
            value, tag = _resolve(facts, concept, period_end, filed_before)
        except MissingDataError:
            # Filer didn't tag this bucket for this period — skip it.
            continue
        buckets[key] = value
        source_tags[key] = tag

    total_scheduled = sum(buckets.values())

    # near_term = principal due within the next two fiscal years.
    near_term = buckets.get("y1", 0.0) + buckets.get("y2", 0.0)
    near_term_pct = (near_term / total_scheduled) if total_scheduled else None

    # wall_year = the single bucket carrying the most principal.
    wall_year = max(buckets, key=buckets.get) if buckets else None

    return MaturitySchedule(
        period_end=period_end,
        buckets=buckets,
        source_tags=source_tags,
        total_scheduled=total_scheduled,
        near_term_pct=near_term_pct,
        wall_year=wall_year,
    )


# ── Batch extraction ─────────────────────────────────────────────────────────

# This list drives extract_all(). Adding a new ratio function here is all
# that's needed to include it in every batch extraction run.
_RATIO_FUNCTIONS = [leverage, interest_coverage, free_cash_flow, fcf_margin, liquidity]


# Maps each ratio name → the ordered list of input concept keys (keys into
# concepts.TAGS) its formula consumes. Used by diagnose_ratio() to pinpoint which
# raw input is missing when a ratio can't be computed. Field names equal the concept
# keys, matching the input names the ratio functions put in RatioResult.inputs.
RATIO_INPUTS: dict[str, list[str]] = {
    "leverage":          ["total_debt", "cash", "operating_income", "depreciation"],
    "interest_coverage": ["operating_income", "depreciation", "interest_expense"],
    "free_cash_flow":    ["operating_cashflow", "capex"],
    "fcf_margin":        ["operating_cashflow", "capex", "revenue"],
    "liquidity":         ["cash", "short_term_debt"],
}


def diagnose_ratio(
    name: str,
    facts: dict,
    period_end: str,
    reason: str,
    filed_before: str | None = None,
) -> MissingRatio:
    """
    Build a MissingRatio audit record for a ratio that failed to compute.

    Re-resolves each of the ratio's input concepts independently (via resolve_tag)
    so we can tell which raw inputs are present and which are genuinely missing:
      - resolved inputs go into `inputs`/`source_tags`
      - unresolved inputs are recorded in `missing_inputs` with the tags that were
        tried (TAGS[concept]) so the analyst can see exactly what was searched for

    `reason` is the original MissingDataError message — important for guard failures
    (e.g. zero EBITDA) where every input resolves but the ratio is still undefined.
    """
    inputs: dict[str, float] = {}
    source_tags: dict[str, str] = {}
    missing_inputs: list[dict] = []

    for concept in RATIO_INPUTS.get(name, []):
        try:
            value, tag = _resolve(facts, concept, period_end, filed_before)
        except MissingDataError:
            missing_inputs.append({"field": concept, "tags_tried": TAGS.get(concept, [])})
            continue
        inputs[concept] = value
        source_tags[concept] = tag

    return MissingRatio(
        name=name,
        period_end=period_end,
        inputs=inputs,
        source_tags=source_tags,
        missing_inputs=missing_inputs,
        reason=reason,
    )


def extract_all(
    facts: dict,
    period_end: str,
    filed_before: str | None = None,
) -> dict[str, RatioResult | MissingRatio]:
    """
    Run all five ratio functions for one (company, period) combination.

    Design decision — never raises, records misses:
      If one ratio can't be computed (e.g. missing depreciation tag), the others
      still run. The failed ratio is stored in the dict as a MissingRatio object
      that records which raw inputs are missing (see diagnose_ratio). Callers check
      with isinstance(result, RatioResult) to skip misses for scoring.

    Returns:
        Dict keyed by ratio name. Values are either a RatioResult (success)
        or a MissingRatio (explains which inputs resolved and which are missing).
        Example: {"leverage": RatioResult(...), "liquidity": MissingRatio(...)}
    """
    results: dict[str, RatioResult | MissingRatio] = {}

    for fn in _RATIO_FUNCTIONS:
        try:
            result = fn(facts, period_end, filed_before)
            # Key by result.name (e.g. "leverage") — the canonical ratio name.
            results[result.name] = result
        except MissingDataError as e:
            # fn.__name__ (e.g. "leverage") matches the canonical ratio name. Build a
            # per-input diagnostic so the source-audit panel can show what's missing.
            results[fn.__name__] = diagnose_ratio(
                fn.__name__, facts, period_end, str(e), filed_before
            )

    return results


def _get_available_periods(facts: dict) -> list[str]:
    """
    Find all annual fiscal-year-end dates in the EDGAR XBRL data.

    The problem:
      Many different XBRL tags in a 10-K carry form=="10-K" metadata, but most
      of them are NOT fiscal-year-end dates. For example:
        - Quarterly revenue breakdowns (reported in the 10-K for comparison)
        - Segment data at acquisition dates
        - Cover-page share counts at arbitrary dates
      Scanning all tags naively produces dozens of spurious dates.

    The solution — anchor on total_assets (us-gaap/Assets):
      Total assets is a balance-sheet item reported EXACTLY ONCE per year-end
      in a 10-K. Its 'end' date IS the fiscal year-end. Using it as the anchor
      gives a clean list with no spurious dates.

    Fallback (for rare issuers that don't tag total assets):
      Scan all concepts but require the reporting span to be ≥ 350 days,
      which filters out quarterly periods (≈ 90 days) while keeping annual ones.

    Returns:
        List of "YYYY-MM-DD" fiscal year-end strings, newest first.
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    # ── Primary path: anchor on total_assets ──────────────────────────────
    # Strip the "us-gaap/" prefix to get the bare tag name used as a dict key.
    anchor_tags = [t.split("/", 1)[1] for t in TAGS["total_assets"]]
    periods: set[str] = set()

    for tag in anchor_tags:
        concept_data = us_gaap.get(tag)
        if not concept_data:
            continue
        for entries in concept_data.get("units", {}).values():
            for entry in entries:
                # Only collect dates from annual (10-K) filings with an 'end' date.
                if entry.get("form") == "10-K" and entry.get("end"):
                    periods.add(entry["end"])

    if periods:
        # Return newest-first so callers can slice [:N] to get the most recent N.
        return sorted(periods, reverse=True)

    # ── Fallback path: scan all concepts, filter by duration ──────────────
    # Deferred import — only needed in the fallback, and `date` is a stdlib type
    # that we don't want to import at module level for a rarely-hit code path.
    from datetime import date

    for concept_data in us_gaap.values():
        for entries in concept_data.get("units", {}).values():
            for entry in entries:
                if entry.get("form") != "10-K" or not entry.get("end"):
                    continue
                start = entry.get("start")
                if start:
                    try:
                        # Compute the number of days the reporting span covers.
                        span = date.fromisoformat(entry["end"]) - date.fromisoformat(start)
                    except ValueError:
                        # Malformed date string — skip this entry.
                        continue
                    # Annual reports span ~365 days; quarterly span ~90 days.
                    # 350 days is the threshold to distinguish annual from quarterly.
                    if span.days < 350:
                        continue
                periods.add(entry["end"])

    return sorted(periods, reverse=True)


# ── CLI convenience ──────────────────────────────────────────────────────────
# Run this file directly to test ratio extraction for a given ticker.
# Usage:  python -m src.extract AAPL

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
                # MissingRatio — print which inputs are missing (or the guard reason).
                missing = [m["field"] for m in result.missing_inputs]
                detail = f"missing inputs: {missing}" if missing else result.reason
                print(f"  {name:20s} = MISSING: {detail}")
        print()
