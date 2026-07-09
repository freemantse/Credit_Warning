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
  leverage              = net_debt / EBITDA
  interest_coverage     = EBITDA / interest_expense
  free_cash_flow        = operating_cashflow - capex
  fcf_margin            = free_cash_flow / revenue
  ebitda_margin         = EBITDA / revenue
  liquidity             = cash / short_term_debt
  cash_flow_to_debt     = operating_cashflow / gross_debt   (FFO/Debt proxy)
  debt_to_assets        = gross_debt / total_assets         (gearing)
  current_ratio         = current_assets / current_liabilities
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from src.concepts import resolve_tag, resolve_one_tag, MissingDataError, TAGS


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
    # Provenance marker for Moody's-adjustment ratios (leverage_adjusted). Optional
    # and defaults to None, so every existing ratio is byte-for-byte unchanged.
    # "xbrl"  → the adjustment inputs came entirely from XBRL tags (full trust).
    # "llm"   → reserved for a later pass where a footnote-extracted input is used.
    # (When the adjustment can't be computed the ratio is a MissingRatio, not a
    # RatioResult with lease_source="none" — absence itself is the "none" signal.)
    lease_source: str | None = None
    # Independent provenance marker for the Moody's PENSION adjustment
    # (pension_debt_burden). Kept SEPARATE from lease_source so lease and pension
    # provenance never collide — a ratio can be lease-xbrl and pension-none. Same
    # values ("xbrl" now; "llm" reserved; absence = unavailable, no stored ratio).
    pension_source: str | None = None


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
        near_term_pct:   (y1 + y2 + y3) / total_scheduled, or None when total is 0
                         (so the maturity-wall score rule is suppressed, not
                         mis-computed, on unreliable data).
        wall_year:       Bucket with the largest principal due, or None if empty.
        schedule_confidence:
                         Reconciliation guard against under-tagging. "high" when
                         sum(buckets) reconciles with XBRL total debt (within
                         MATURITY_RECONCILE_TOLERANCE); "degraded" when it does
                         NOT (e.g. a filer dropped the y5/thereafter buckets, so
                         near_term_pct reads artificially high off a truncated
                         total); "unknown" when total debt is unavailable/zero or
                         no buckets were tagged. The scorer suppresses the
                         maturity-wall rule when this is "degraded" (see score.py)
                         so a wrong near_term_pct never scores at full weight.
        total_debt_reconcile:
                         The XBRL total-debt figure the reconciliation compared
                         against (gross_debt waterfall total), or None when
                         unavailable. Kept for audit — explains the confidence.
    """
    period_end: str
    buckets: dict[str, float]
    source_tags: dict[str, str]
    total_scheduled: float
    near_term_pct: float | None
    wall_year: str | None
    schedule_confidence: str
    total_debt_reconcile: float | None


# Reconciliation tolerance for the maturity schedule. The sum of the tagged
# maturity buckets should reconcile with XBRL total debt; a gap wider than this
# fraction means the filer under-tagged the schedule (e.g. dropped y5/thereafter),
# so near_term_pct is computed off a truncated total and must NOT score at full
# weight. 15% (not 10%) deliberately absorbs legitimate discrepancies —
# unamortized discount/premium, capital-lease treatment, and short-term
# instruments (commercial paper/revolver) that are in gross_debt but not in the
# long-term-debt maturity schedule — without letting a heavy under-tag through.
MATURITY_RECONCILE_TOLERANCE = 0.15


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
    Returns (value, winning_tag_path) — the FIRST non-null tag in the fallback list.
    """
    return resolve_tag(facts, concept, period_end, filed_before=filed_before)


def _resolve_sum(facts: dict, concept: str, period_end: str, filed_before: str | None = None) -> tuple[float, list[str]]:
    """
    SUM-capable sibling of _resolve. For the given `concept` (a TAGS key), resolve
    EVERY tag in its list and SUM all non-null matches — not first-only.

    This is the additive counterpart to _resolve, needed for short-term debt where
    a filer can carry several distinct instruments simultaneously (commercial paper
    + revolver + short-term notes); first-match would undercount them.

    Tags are de-duplicated so the same tag path is never summed twice. Returns
    (summed_value, [matched_tag_paths]); (0.0, []) when nothing in the list matches.
    """
    total = 0.0
    matched: list[str] = []
    seen: set[str] = set()
    for tag_path in TAGS.get(concept, []):
        if tag_path in seen:
            continue
        seen.add(tag_path)
        value = resolve_one_tag(facts, tag_path, period_end, filed_before)
        if value is not None:
            total += value
            matched.append(tag_path)
    return total, matched


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


def _resolve_first_opt(facts: dict, concept: str, period_end: str, filed_before: str | None):
    """First-match resolve that returns (None, None) instead of raising on a miss."""
    try:
        return _resolve(facts, concept, period_end, filed_before)
    except MissingDataError:
        return None, None


def net_debt(facts: dict, period_end: str, filed_before: str | None = None) -> tuple[float, dict, dict]:
    """
    Compute net debt = gross_debt − cash for a given period.

    Net debt is the leverage numerator. As of Phase 1.5 the debt figure is the
    full component-based waterfall total from gross_debt() (interest-bearing
    short-term + current + non-current debt), NOT a single LongTermDebt tag — so
    leverage now reflects the complete obligation.

    A negative RESULT means the company holds more cash than debt (net cash) —
    preserved here as financial strength (leverage goes negative). A negative
    gross-debt TOTAL, by contrast, is a tagging error and is surfaced (raised) by
    gross_debt() before it ever reaches this function.

    Returns:
        (net_debt_value, inputs_dict, source_tags_dict)
    """
    total, gd_inputs, gd_tags = gross_debt(facts, period_end, filed_before)
    cash, cash_tag = _resolve(facts, "cash", period_end, filed_before)
    value = total - cash  # positive = more debt than cash (typical); negative = net cash
    inputs = {**gd_inputs, "cash": cash}
    tags = {**gd_tags, "cash": cash_tag}
    return value, inputs, tags


def gross_debt(facts: dict, period_end: str, filed_before: str | None = None) -> tuple[float, dict, dict]:
    """
    Compute gross (total interest-bearing) debt via a component-based waterfall.

    Ported from Yuetong's validated _total_debt. Replaces the old
    total_debt + short_term_debt addition, which (a) double-counted the current
    portion of long-term debt for filers who tagged both LongTermDebt (incl.
    current) and LongTermDebtCurrent, and (b) undercounted short-term debt by
    taking only the first non-null instrument.

    Components:
      A = sum of additive short-term instruments (st_debt_components, summed).
      B = current portion of long-term debt (current_ltd; else debt_current_fallback).
      C = non-current long-term debt (lt_debt_noncurrent, first-match).

    Double-count guard: when C resolved via exactly us-gaap/LongTermDebt (which
    typically already includes the current maturities), subtract B from C before
    summing, so the current portion isn't counted in both B and C.

    Waterfall:
      Level 1 — C present: total = A + B + C_adjusted.
                If debt_aggregate resolves ABOVE the Level-1 sum, the components
                missed something → prefer the aggregate (level "2-override").
      Level 2 — C absent but an aggregate tag exists: total = aggregate.
      All-current — C and aggregate absent but A/B exist: total = A + B.
      Failure — nothing resolves: raise MissingDataError (never return 0).

    A computed negative total is a tagging inconsistency (e.g. current portion
    larger than the LongTermDebt it was subtracted from) and is raised, not returned.

    Returns:
        (gross_debt_value, inputs_dict, source_tags_dict)
        inputs holds the numeric component breakdown (+ double_count_adjustment);
        source_tags holds the per-component tags plus the qualitative audit flags
        (_waterfall_level, _double_count_guard) as strings.
    """
    # Component A — additive short-term instruments (SUMMED, not first-only).
    a_short_term, a_tags = _resolve_sum(facts, "st_debt_components", period_end, filed_before)

    # Component B — current portion of LTD; fall back to the broad DebtCurrent tag.
    b_current, b_tag = _resolve_first_opt(facts, "current_ltd", period_end, filed_before)
    if b_current is None:
        b_current, b_tag = _resolve_first_opt(facts, "debt_current_fallback", period_end, filed_before)
    b_val = b_current if b_current is not None else 0.0

    # Component C — non-current long-term debt (first-match).
    c_noncurrent, c_tag = _resolve_first_opt(facts, "lt_debt_noncurrent", period_end, filed_before)

    # Aggregate combined-debt tag — Level-2 fallback / override.
    aggregate, agg_tag = _resolve_first_opt(facts, "debt_aggregate", period_end, filed_before)

    # Double-count guard: LongTermDebt often bundles the current maturities, so when
    # C resolved via exactly us-gaap/LongTermDebt, remove B (already inside C).
    double_count_adj = 0.0
    c_used = c_noncurrent
    if c_noncurrent is not None and c_tag == "us-gaap/LongTermDebt" and b_current is not None:
        double_count_adj = -b_val
        c_used = c_noncurrent - b_val

    inputs: dict[str, float] = {
        "short_term_components": a_short_term,
        "current_portion_ltd": b_val,
        "long_term_noncurrent": (c_used if c_used is not None else 0.0),
        "double_count_adjustment": double_count_adj,
    }
    tags: dict[str, Any] = {}
    if a_tags:
        tags["short_term_components"] = a_tags          # list of summed tags
    if b_tag:
        tags["current_portion_ltd"] = b_tag
    if c_tag:
        tags["long_term_noncurrent"] = c_tag

    # ── Waterfall ─────────────────────────────────────────────────────────────
    if c_noncurrent is not None:
        level1 = a_short_term + b_val + (c_used if c_used is not None else 0.0)
        total = level1
        level = "1"
        # Level-2 override: a combined-debt aggregate above the component sum means
        # the components missed something — trust the aggregate instead.
        if aggregate is not None and aggregate > level1:
            total = aggregate
            level = "2-override"
            tags["debt_aggregate"] = agg_tag
    elif aggregate is not None:
        total = aggregate
        level = "2"
        tags["debt_aggregate"] = agg_tag
    elif a_short_term != 0.0 or b_current is not None:
        # No non-current tranche and no aggregate, but the firm does carry
        # short-term / current debt (e.g. all debt is current) — don't fail.
        total = a_short_term + b_val
        level = "1"
    else:
        raise MissingDataError(
            f"No debt components resolved for period_end={period_end!r}. Tried: "
            f"st_debt_components, current_ltd, debt_current_fallback, "
            f"lt_debt_noncurrent, debt_aggregate"
        )

    if total < 0:
        # Negative gross debt is impossible for a real balance sheet — surface the
        # tagging inconsistency / double-count over-subtraction rather than hide it.
        raise MissingDataError(
            f"Computed negative gross debt ({total:,.0f}) for period_end={period_end!r} "
            f"— tagging inconsistency or double-count over-subtraction; surfacing instead "
            f"of returning a bad value"
        )

    inputs["total_debt"] = total  # waterfall result (kept under the legacy key, numeric)
    tags["_waterfall_level"] = level
    tags["_double_count_guard"] = (
        "applied — C resolved via us-gaap/LongTermDebt (incl. current portion); subtracted B"
        if double_count_adj
        else "not applied"
    )
    return total, inputs, tags


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
      > 5×  — stress; the leverage rule ramps toward its full penalty (see score.py)

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


# ── Operating-lease capitalization (Moody's Formula 2, post-2019 deterministic) ─

# Moody's reclassifies annual operating-lease expense as 1/3 interest + 2/3
# depreciation. For the leverage denominator (EBITDA), Formula 2 adds back the
# depreciation component (2/3 of annual lease cost) — see spec/LEVERAGE.md Formula 2.
_LEASE_EBITDA_DEPRECIATION_FRACTION = 2.0 / 3.0
# The interest component (1/3 of annual lease cost) is added to the interest-expense
# denominator for adjusted coverage — see spec/INTEREST_COVERAGE.md:173. The two
# fractions are the same OperatingLeaseCost split (1/3 interest + 2/3 depreciation).
_LEASE_INTEREST_FRACTION = 1.0 / 3.0


def operating_lease_debt(
    facts: dict, period_end: str, filed_before: str | None = None
) -> tuple[float, dict, dict] | None:
    """
    Capitalized operating-lease obligation for the Moody's Formula-2 debt add:
    the balance-sheet ROU LIABILITY = OperatingLeaseLiabilityCurrent +
    OperatingLeaseLiabilityNoncurrent (post-ASC 842, FY2019+).

    Deterministic-only: returns None when NEITHER tag resolves (pre-2019 filings, or
    a filer that doesn't tag the ROU liability) — the caller then marks the whole
    adjustment unavailable rather than guessing a rent×multiple (no LLM this pass).
    A partially-tagged filer (only one of the two) still sums what is present.

    Returns (rou_liability, inputs_dict, source_tags_dict) or None.
    """
    cur, cur_tag = _resolve_first_opt(facts, "operating_lease_liability_current", period_end, filed_before)
    non, non_tag = _resolve_first_opt(facts, "operating_lease_liability_noncurrent", period_end, filed_before)
    if cur is None and non is None:
        return None
    cur_val = cur if cur is not None else 0.0
    non_val = non if non is not None else 0.0
    inputs = {
        "operating_lease_liability_current": cur_val,
        "operating_lease_liability_noncurrent": non_val,
    }
    tags: dict[str, str] = {}
    if cur_tag:
        tags["operating_lease_liability_current"] = cur_tag
    if non_tag:
        tags["operating_lease_liability_noncurrent"] = non_tag
    return cur_val + non_val, inputs, tags


def leverage_adjusted(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Moody's-adjusted leverage (Formula 2, spec/LEVERAGE.md) — SUPPLEMENTS the raw
    Formula-1 `leverage` ratio; it never replaces it.

        Adjusted Net Debt = net_debt (Formula 1) + capitalized operating leases
        Adjusted EBITDA    = EBITDA (Formula 1) + 2/3 × annual operating-lease cost
        leverage_adjusted  = Adjusted Net Debt / Adjusted EBITDA

    Deterministic-only this pass: the lease obligation is the XBRL ROU liability
    (operating_lease_debt). Pension and non-recurring-gain terms of Formula 2 are
    deferred. If the ROU liability isn't tagged (pre-2019 / untagged), the whole
    adjustment is UNAVAILABLE — this raises MissingDataError so extract_all records
    a MissingRatio and scoring falls back to Formula 1 only (the "none" provenance
    state; no rent×multiple guess). When computable, lease_source="xbrl".

    Raises MissingDataError if the ROU liability is untagged or Adjusted EBITDA is 0.
    """
    lease = operating_lease_debt(facts, period_end, filed_before)
    if lease is None:
        raise MissingDataError(
            f"No operating-lease ROU liability tagged for {period_end} — Moody's "
            f"Formula-2 lease adjustment unavailable (deterministic pass; no LLM fallback)"
        )
    lease_debt, lease_inputs, lease_tags = lease

    nd, nd_inputs, nd_tags = net_debt(facts, period_end, filed_before)
    ebit, ebit_inputs, ebit_tags = ebitda(facts, period_end, filed_before)

    # EBITDA add-back = 2/3 of annual operating-lease cost (the depreciation
    # component). Optional: if OperatingLeaseCost isn't tagged, add 0 rather than
    # guess — the ROU-liability debt add (the dominant term) still applies.
    lease_cost, cost_tag = _resolve_first_opt(facts, "operating_lease_cost", period_end, filed_before)
    lease_cost_val = lease_cost if lease_cost is not None else 0.0
    ebitda_lease_addback = _LEASE_EBITDA_DEPRECIATION_FRACTION * lease_cost_val

    adj_net_debt = nd + lease_debt
    adj_ebitda = ebit + ebitda_lease_addback
    if adj_ebitda == 0:
        raise MissingDataError(f"Adjusted EBITDA is zero for {period_end}, cannot compute leverage_adjusted")

    inputs = {
        **nd_inputs, **ebit_inputs, **lease_inputs,
        "operating_lease_cost": lease_cost_val,
        "capitalized_lease_debt_added": lease_debt,
        "ebitda_lease_depreciation_addback": ebitda_lease_addback,
        "adjusted_net_debt": adj_net_debt,
        "adjusted_ebitda": adj_ebitda,
    }
    tags = {**nd_tags, **ebit_tags, **lease_tags}
    if cost_tag:
        tags["operating_lease_cost"] = cost_tag
    return RatioResult(
        name="leverage_adjusted",
        value=adj_net_debt / adj_ebitda,
        inputs=inputs,
        source_tags=tags,
        period_end=period_end,
        lease_source="xbrl",
    )


def interest_coverage_adjusted(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Moody's-adjusted interest coverage (Formula 2, lease-interest leg ONLY) —
    SUPPLEMENTS the raw Formula-1 `interest_coverage`; it never replaces it.

        Adjusted Interest = interest_expense (Formula 1) + 1/3 × annual operating-lease cost
        interest_coverage_adjusted = EBITDA / Adjusted Interest

    Mirrors the code's Formula-1 base (EBITDA / interest, not the spec's EBIT
    numerator): only the DENOMINATOR is adjusted this pass. The spec's EBIT switch
    and the pension-interest / capitalized-interest legs are deferred.

    The 1/3 lease-interest term is the counterpart to leverage_adjusted's 2/3
    depreciation add-back — same OperatingLeaseCost tag, so 1/3 + 2/3 = the full
    Moody's rent split.

    UNLIKE leverage_adjusted (whose gate is the ROU liability, with lease cost
    optional), here the lease-interest term IS the entire adjustment — so the gate
    is `operating_lease_cost` ITSELF. If it isn't tagged the adjustment is
    UNAVAILABLE and this raises MissingDataError → extract_all records a
    MissingRatio and scoring falls back to Formula 1. It deliberately does NOT
    default the lease cost to 0, which would silently return unadjusted coverage
    mislabeled as "adjusted". When computable, lease_source="xbrl".

    Raises MissingDataError if operating_lease_cost is untagged, or if adjusted
    interest expense is 0.
    """
    lease_cost, cost_tag = _resolve_first_opt(facts, "operating_lease_cost", period_end, filed_before)
    if lease_cost is None:
        raise MissingDataError(
            f"No operating-lease cost tagged for {period_end} — Moody's Formula-2 "
            f"lease-interest adjustment unavailable (deterministic pass; no LLM fallback)"
        )

    ebit, ebit_inputs, ebit_tags = ebitda(facts, period_end, filed_before)
    int_exp, int_tag = _resolve(facts, "interest_expense", period_end, filed_before)

    lease_interest = _LEASE_INTEREST_FRACTION * lease_cost
    adj_interest = int_exp + lease_interest
    if adj_interest == 0:
        raise MissingDataError(f"Adjusted interest expense is zero for {period_end}, cannot compute interest_coverage_adjusted")

    inputs = {
        **ebit_inputs,
        "interest_expense": int_exp,
        "operating_lease_cost": lease_cost,
        "lease_interest_component": lease_interest,
        "adjusted_interest_expense": adj_interest,
    }
    tags = {**ebit_tags, "interest_expense": int_tag}
    if cost_tag:
        tags["operating_lease_cost"] = cost_tag
    return RatioResult(
        name="interest_coverage_adjusted",
        value=ebit / adj_interest,
        inputs=inputs,
        source_tags=tags,
        period_end=period_end,
        lease_source="xbrl",
    )


# ── Pension Formula-2 legs (deterministic flags, PARALLEL to the lease legs) ────
#
# All three are flag-first (weight 0 in score.py) and independent ratios with their
# own pension_source provenance — kept SEPARATE from the lease legs (lease_source)
# because one ratio can't cleanly carry two provenance markers, mirroring the
# pension_debt-vs-leverage_adjusted parallel decision.

def interest_coverage_pension_adjusted(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Moody's Formula-2 pension-interest leg (INTEREST_COVERAGE.md:171) — SUPPLEMENTS
    Formula-1 `interest_coverage`; parallel to `interest_coverage_adjusted` (leases).

        interest_coverage_pension_adjusted = EBITDA / (interest + pension interest cost)

    Mirrors the code's EBITDA base (not the spec's EBIT numerator); only the
    DENOMINATOR gets the pension-interest reclass this leg. Gated on
    `pension_interest_cost` — untagged → MissingDataError → MissingRatio (no guess).
    pension_source="xbrl". Raises if adjusted interest is 0.
    """
    pint_cost, pint_tag = _resolve_first_opt(facts, "pension_interest_cost", period_end, filed_before)
    if pint_cost is None:
        raise MissingDataError(
            f"No pension interest cost tagged for {period_end} — Moody's Formula-2 "
            f"pension-interest adjustment unavailable (deterministic pass; no guess)"
        )
    ebit, ebit_inputs, ebit_tags = ebitda(facts, period_end, filed_before)
    int_exp, int_tag = _resolve(facts, "interest_expense", period_end, filed_before)
    adj_interest = int_exp + pint_cost
    if adj_interest == 0:
        raise MissingDataError(f"Adjusted interest expense is zero for {period_end}, cannot compute interest_coverage_pension_adjusted")
    inputs = {
        **ebit_inputs,
        "interest_expense": int_exp,
        "pension_interest_cost": pint_cost,
        "adjusted_interest_expense": adj_interest,
    }
    tags = {**ebit_tags, "interest_expense": int_tag, "pension_interest_cost": pint_tag}
    return RatioResult(
        name="interest_coverage_pension_adjusted",
        value=ebit / adj_interest,
        inputs=inputs,
        source_tags=tags,
        period_end=period_end,
        pension_source="xbrl",
    )


def pension_ebitda_reclass(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Moody's Formula-2 pension-EBITDA reclass (LEVERAGE.md:111) — the FULL periodic-
    cost reclass, NOT a partial approximation.

        reclass addback = net periodic benefit cost − service cost
        value = (EBITDA + addback) / EBITDA          (the EBITDA-uplift multiple)

    Moody's reclassifies the excess of total pension expense over service cost out
    of operating expense, lifting EBITDA. Computing that excess REQUIRES the total
    (DefinedBenefitPlanNetPeriodicBenefitCost) AND service cost — this leg requires
    BOTH tags; if either is missing it raises MissingDataError → MissingRatio (no
    guess). A service-cost-only figure cannot yield the excess, so it is never
    faked. Deterministic where both tag (~19% of filers). pension_source="xbrl".

    Raises if either pension tag is missing, or if base EBITDA ≤ 0 (the uplift
    multiple is degenerate — reported unavailable rather than misleading).
    """
    total, tot_tag = _resolve_first_opt(facts, "pension_net_periodic_cost", period_end, filed_before)
    service, svc_tag = _resolve_first_opt(facts, "pension_service_cost", period_end, filed_before)
    if total is None or service is None:
        raise MissingDataError(
            f"Pension net-periodic-cost and/or service-cost untagged for {period_end} — "
            f"full reclass (total − service) not computable (deterministic pass; no guess)"
        )
    ebit, ebit_inputs, ebit_tags = ebitda(facts, period_end, filed_before)
    if ebit <= 0:
        raise MissingDataError(
            f"Base EBITDA ≤ 0 for {period_end}, pension-EBITDA-reclass uplift multiple is degenerate"
        )
    addback = total - service          # the non-service "excess" reclassified out of opex
    adj_ebitda = ebit + addback
    inputs = {
        **ebit_inputs,
        "pension_net_periodic_cost": total,
        "pension_service_cost": service,
        "pension_ebitda_reclass_addback": addback,
        "adjusted_ebitda": adj_ebitda,
    }
    tags = {**ebit_tags, "pension_net_periodic_cost": tot_tag, "pension_service_cost": svc_tag}
    return RatioResult(
        name="pension_ebitda_reclass",
        value=adj_ebitda / ebit,       # uplift multiple; >1 when the reclass adds to EBITDA
        inputs=inputs,
        source_tags=tags,
        period_end=period_end,
        pension_source="xbrl",
    )


def moody_adjusted_fcf_pension(facts, period_end, filed_before=None) -> RatioResult:
    """
    Moody's Formula-2 pension-FCF leg (FREE_CASH_FLOW.md:229) — PARALLEL FLAG.

        moody_adjusted_fcf_pension = OCF + pension cash contributions − D&A(maint capex proxy) − dividends

    Built as an INDEPENDENT flag rather than editing the live `moody_adjusted_fcf`
    ON PURPOSE: `moody_adjusted_fcf` is a scored ratio (moody_adjusted_fcf_negative,
    weight 8) AND a co-condition in the lease_debt_burden Option-C gate, so adding
    the pension addback in place would CHANGE scores for the ~14% of filers that tag
    contributions (score-affecting, not flag-first). That in-place change is deferred
    as a separate, deliberate, A/B-gated deliverable. This parallel flag leaves
    `moody_adjusted_fcf` and its omit-shortcut untouched.

    Gated on `pension_contributions` — untagged → MissingDataError → MissingRatio
    (no guess; that is the whole point of this leg). pension_source="xbrl".
    """
    contrib, con_tag = _resolve_first_opt(facts, "pension_contributions", period_end, filed_before)
    if contrib is None:
        raise MissingDataError(
            f"No pension cash contributions tagged for {period_end} — Moody's Formula-2 "
            f"pension-FCF addback unavailable (deterministic pass; no guess)"
        )
    ocf, ocf_tag = _resolve(facts, "operating_cashflow", period_end, filed_before)
    dep, dep_tag = _resolve(facts, "depreciation", period_end, filed_before)
    inputs = {
        "operating_cashflow": ocf,
        "pension_contributions_addback": contrib,
        "depreciation_proxy_for_maintenance_capex": dep,
    }
    tags = {"operating_cashflow": ocf_tag, "pension_contributions": con_tag, "depreciation": dep_tag}
    try:
        div, div_tag = _resolve(facts, "dividends_paid", period_end, filed_before)
        inputs["dividends_paid"] = div
        tags["dividends_paid"] = div_tag
    except MissingDataError:
        div = 0.0
    value = ocf + contrib - dep - div
    inputs["adjusted_fcf_pension"] = value
    return RatioResult(
        name="moody_adjusted_fcf_pension",
        value=value,
        inputs=inputs,
        source_tags=tags,
        period_end=period_end,
        pension_source="xbrl",
    )


def lease_debt_burden(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Lease-inflation multiple = adjusted_net_debt / raw_net_debt, where
    adjusted_net_debt = raw net_debt + capitalized operating leases (ROU liability).

    Unlike leverage_adjusted this has NO EBITDA in the denominator, so it is
    well-defined at ANY profitability — it answers "how much do operating leases
    inflate the debt obligation?" independent of earnings. A retailer whose leases
    equal its funded debt reads ~2.0×. This is the always-available companion to
    leverage_adjusted for lease-heavy names whose adjusted-leverage RATIO is
    degenerate because EBITDA ≤ 0 (e.g. RAD).

    Deterministic-only (XBRL ROU liability) → lease_source="xbrl".

    Raises MissingDataError when the ROU liability isn't tagged (adjustment
    unavailable), or when raw net_debt ≤ 0 (a net-cash issuer — the multiple would
    be negative/undefined and is not a meaningful debt-inflation figure).
    """
    lease = operating_lease_debt(facts, period_end, filed_before)
    if lease is None:
        raise MissingDataError(
            f"No operating-lease ROU liability tagged for {period_end} — "
            f"lease_debt_burden unavailable (deterministic pass; no LLM fallback)"
        )
    lease_debt, lease_inputs, lease_tags = lease

    nd, nd_inputs, nd_tags = net_debt(facts, period_end, filed_before)
    if nd <= 0:
        raise MissingDataError(
            f"Raw net debt is ≤ 0 (net-cash position) for {period_end} — "
            f"lease_debt_burden multiple is not meaningful"
        )
    adj_net_debt = nd + lease_debt
    inputs = {
        **nd_inputs, **lease_inputs,
        "raw_net_debt": nd,
        "capitalized_lease_debt_added": lease_debt,
        "adjusted_net_debt": adj_net_debt,
    }
    return RatioResult(
        name="lease_debt_burden",
        value=adj_net_debt / nd,
        inputs=inputs,
        source_tags={**nd_tags, **lease_tags},
        period_end=period_end,
        lease_source="xbrl",
    )


# ── Pension capitalization (Moody's Formula 2, deterministic layer — PARALLEL to leases) ─
#
# Deliberately NOT folded into leverage_adjusted: that ratio's availability gate
# requires leases, which would kill the pension add for pension-heavy/lease-light
# filers, and its single lease_source can't carry pension provenance. So pensions
# is its own helper + ratio + pension_source marker, additive and independent.
#
# This pass builds only the DEBT ADD (unfunded pension → adjusted debt), the clean
# deterministic win (~26% of filers). The Moody's EBITDA/interest reclass needs
# TOTAL net periodic benefit cost (not a single clean tag), so it is treated as
# partial and deferred to the LLM layer — NOT computed here.

def pension_debt(
    facts: dict, period_end: str, filed_before: str | None = None
) -> tuple[float, dict, dict] | None:
    """
    Unfunded defined-benefit pension deficit for the Moody's Formula-2 debt add.

    Funded-status waterfall (spec/LEVERAGE.md Formula 2):
      1. Prefer the direct tag `pension_funded_status` (DefinedBenefitPlanFundedStatusOfPlan);
         it is reported as (assets − PBO), so the unfunded deficit is −min(0, funded_status).
      2. Else derive from PBO and plan assets: deficit = PBO − plan_assets.
      3. Else return None — the adjustment is UNAVAILABLE (no guess; a later LLM
         footnote layer fills these ~70-80% of filers).

    The overfunded floor is applied in every branch: an overfunded plan (assets >
    PBO) adds 0 to debt, never a negative (spec/LEVERAGE.md:589). PBO and plan
    assets are instant year-end balances resolved at the same period_end, and
    companyfacts carries only the undimensioned plan total (not a segment), so the
    subtraction is valid.

    Returns (unfunded_deficit, inputs_dict, source_tags_dict) or None.
    """
    inputs: dict[str, float] = {}
    tags: dict[str, str] = {}

    # 1. Direct funded-status tag (assets − PBO; negative = underfunded).
    funded, funded_tag = _resolve_first_opt(facts, "pension_funded_status", period_end, filed_before)
    if funded is not None:
        deficit = max(0.0, -funded)  # overfunded (funded>0) → 0
        inputs["pension_funded_status"] = funded
        if funded_tag:
            tags["pension_funded_status"] = funded_tag
        inputs["unfunded_pension"] = deficit
        return deficit, inputs, tags

    # 2. Derive from PBO − plan assets (needs both).
    pbo, pbo_tag = _resolve_first_opt(facts, "pension_pbo", period_end, filed_before)
    assets, assets_tag = _resolve_first_opt(facts, "pension_plan_assets", period_end, filed_before)
    if pbo is not None and assets is not None:
        deficit = max(0.0, pbo - assets)  # overfunded → 0
        inputs["pension_pbo"] = pbo
        inputs["pension_plan_assets"] = assets
        inputs["unfunded_pension"] = deficit
        if pbo_tag:
            tags["pension_pbo"] = pbo_tag
        if assets_tag:
            tags["pension_plan_assets"] = assets_tag
        return deficit, inputs, tags

    # 3. Unavailable — no direct tag and can't derive.
    return None


def pension_debt_burden(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Pension-inflation multiple = (raw net_debt + unfunded pension) / raw net_debt —
    the PARALLEL companion to lease_debt_burden, for the Moody's pension debt add.

    Always defined at any profitability (no EBITDA in the denominator). Answers
    "how much does the unfunded pension deficit inflate the debt obligation?"
    Deterministic-only (XBRL funded status / PBO − assets) → pension_source="xbrl".

    Raises MissingDataError when the pension adjustment is unavailable (no funded
    status and can't derive — the ~70-80% incl. RAD/AAPL, deferred to the LLM
    layer), or when raw net_debt ≤ 0 (net-cash issuer — multiple not meaningful).
    """
    pension = pension_debt(facts, period_end, filed_before)
    if pension is None:
        raise MissingDataError(
            f"No defined-benefit pension funded status tagged for {period_end} — "
            f"pension_debt_burden unavailable (deterministic pass; no LLM fallback)"
        )
    deficit, pension_inputs, pension_tags = pension

    nd, nd_inputs, nd_tags = net_debt(facts, period_end, filed_before)
    if nd <= 0:
        raise MissingDataError(
            f"Raw net debt is ≤ 0 (net-cash position) for {period_end} — "
            f"pension_debt_burden multiple is not meaningful"
        )
    adj_net_debt = nd + deficit
    inputs = {
        **nd_inputs, **pension_inputs,
        "raw_net_debt": nd,
        "unfunded_pension_added": deficit,
        "adjusted_net_debt": adj_net_debt,
    }
    return RatioResult(
        name="pension_debt_burden",
        value=adj_net_debt / nd,
        inputs=inputs,
        source_tags={**nd_tags, **pension_tags},
        period_end=period_end,
        pension_source="xbrl",
    )


def interest_coverage(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Interest coverage = EBITDA / interest_expense.

    Interpretation:
      > 4×  — comfortable — EBITDA covers interest many times over
      2–4×  — watch territory
      < 2×  — stress; the coverage rule ramps toward its full penalty (see score.py)

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


def capex_total(
    facts: dict, period_end: str, filed_before: str | None = None
) -> tuple[float, dict, dict] | None:
    """
    Total capital expenditure as a COMPONENT SUM, mirroring the gross_debt waterfall.

    Components:
      P = own-use capex — first-match over the "capex" tag list (the primary line;
          its members are instead-of ALTERNATIVES — PP&E, or ProductiveAssets, or
          CapitalImprovements, … — so first-match, not sum, avoids double-counting
          a filer that tags several equivalents).
      E = equipment acquired to lease to others (capex_equipment_on_lease) — a
          genuinely DISJOINT cash-flow line for rental-model filers, so it is ADDED.

    total = P + E, with an absent component treated as 0 (no such spend). The
    first-match within P is the "instead-of" guard; E is the only "in-addition-to"
    component, kept in its own concept precisely so it is summed rather than
    treated as an alternative.

    Returns (total, inputs, tags) exposing the components (capex_ppe,
    capex_equipment_on_lease) alongside the summed "capex", the way gross_debt
    surfaces its A/B/C. Returns None when NEITHER component resolves, so the caller
    records a MissingRatio (never a fabricated 0).
    """
    primary, p_tag = _resolve_first_opt(facts, "capex", period_end, filed_before)
    lease, l_tag = _resolve_first_opt(facts, "capex_equipment_on_lease", period_end, filed_before)
    if primary is None and lease is None:
        return None
    p_val = primary if primary is not None else 0.0
    l_val = lease if lease is not None else 0.0
    total = p_val + l_val
    inputs = {
        "capex_ppe": p_val,                       # own-use capex component (P)
        "capex_equipment_on_lease": l_val,        # equipment-leased-to-others component (E)
        "capex": total,                            # summed total (legacy key retained)
    }
    tags: dict = {}
    if p_tag:
        tags["capex_ppe"] = p_tag
    if l_tag:
        tags["capex_equipment_on_lease"] = l_tag
    return total, inputs, tags


def free_cash_flow(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Free cash flow = operating_cashflow - capex.

    FCF measures how much real cash the company generates after maintaining/expanding
    its asset base. Negative FCF means the company spent more cash on operations
    and investment than it brought in — a stress signal (scored via FCF margin in score.py).

    Note: EDGAR reports capex (PaymentsToAcquirePropertyPlantAndEquipment) as a
    POSITIVE outflow number, so we subtract it from OCF. capex is the COMPONENT
    SUM (own-use PP&E + equipment-on-lease) via capex_total(), not a single tag.
    """
    ocf, ocf_tag = _resolve(facts, "operating_cashflow", period_end, filed_before)
    cx = capex_total(facts, period_end, filed_before)
    if cx is None:
        # Preserve the pre-waterfall behavior: nothing tagged → MissingRatio, never 0.
        raise MissingDataError(
            f"No capex components resolved for period_end={period_end!r}. Tried: "
            f"capex (own-use PP&E), capex_equipment_on_lease"
        )
    capex, cx_inputs, cx_tags = cx

    return RatioResult(
        name="free_cash_flow",
        value=ocf - capex,
        inputs={"operating_cashflow": ocf, **cx_inputs},
        source_tags={"operating_cashflow": ocf_tag, **cx_tags},
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


def ebitda_margin(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    EBITDA margin = EBITDA / revenue.

    Measures core operating profitability per revenue dollar. A negative EBITDA
    margin means the company loses money at the operating line before interest,
    taxes, and capex — the most fundamental credit-distress signal. Unlike the
    leverage ratio (whose sign flips when EBITDA goes negative), this metric
    moves the right way: lower is always worse.

    Interpretation:
      >= 10% — healthy operating profitability
      0–10%  — thin
      < 0%   — operating losses; stress rule triggers (full profitability penalty)

    Raises MissingDataError if revenue is zero (e.g. pre-revenue companies).
    """
    ebit, ebit_inputs, ebit_tags = ebitda(facts, period_end, filed_before)
    rev, rev_tag = _resolve(facts, "revenue", period_end, filed_before)

    if rev == 0:
        raise MissingDataError(f"Revenue is zero for {period_end}, cannot compute EBITDA margin")

    return RatioResult(
        name="ebitda_margin",
        value=ebit / rev,
        inputs={**ebit_inputs, "revenue": rev},
        source_tags={**ebit_tags, "revenue": rev_tag},
        period_end=period_end,
    )


def liquidity(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Liquidity = cash / short_term_debt.

    Measures near-term solvency: can the company cover its maturing obligations
    using cash on its balance sheet today?

    Interpretation:
      > 1×  — cash exceeds near-term debt (healthy)
      < 1×  — stress; the liquidity rule ramps toward its full penalty (see score.py);
               company may need to refinance or draw on credit lines to meet maturities

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


def cash_flow_to_debt(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Cash flow to debt = operating_cashflow / gross_debt.

    A proxy for the rating agencies' FFO/Debt — the single most predictive distress
    ratio in the academic literature (Beaver 1966, Jooste 2007). Measures how much
    of the company's debt it could repay from one year of operating cash flow.

    Interpretation (anchored to S&P's FFO/Debt financial-risk bands):
      >= 30% — investment-grade cash-flow adequacy (Intermediate or better)
      20–30% — speculative (~BB)
      < 10%  — highly leveraged / distress (stress rule maxes out)

    Higher is healthier. Raises MissingDataError if gross debt is zero (no debt to
    measure against — the ratio is undefined, not a stress signal).
    """
    ocf, ocf_tag = _resolve(facts, "operating_cashflow", period_end, filed_before)
    gd, gd_inputs, gd_tags = gross_debt(facts, period_end, filed_before)

    if gd == 0:
        raise MissingDataError(f"Gross debt is zero for {period_end}, cannot compute cash flow to debt")

    return RatioResult(
        name="cash_flow_to_debt",
        value=ocf / gd,
        inputs={"operating_cashflow": ocf, **gd_inputs},
        source_tags={"operating_cashflow": ocf_tag, **gd_tags},
        period_end=period_end,
    )


def debt_to_assets(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Debt to assets = gross_debt / total_assets.

    Capital-structure leverage (gearing): the share of the asset base funded by
    interest-bearing debt. Complements net-debt/EBITDA by anchoring leverage to the
    balance sheet rather than earnings (so it stays meaningful when EBITDA is volatile).

    Interpretation:
      <= 40% — conservatively geared
      40–65% — rising leverage
      >= 65% — heavily debt-funded; distress models flag the 0.6–0.7 band (rule maxes out)

    Higher is worse. Raises MissingDataError if total assets is zero.
    """
    gd, gd_inputs, gd_tags = gross_debt(facts, period_end, filed_before)
    assets, assets_tag = _resolve(facts, "total_assets", period_end, filed_before)

    if assets == 0:
        raise MissingDataError(f"Total assets is zero for {period_end}, cannot compute debt to assets")

    return RatioResult(
        name="debt_to_assets",
        value=gd / assets,
        inputs={**gd_inputs, "total_assets": assets},
        source_tags={**gd_tags, "total_assets": assets_tag},
        period_end=period_end,
    )


def current_ratio(facts: dict, period_end: str, filed_before: str | None = None) -> RatioResult:
    """
    Current ratio = current_assets / current_liabilities.

    The classic working-capital liquidity test: can the company cover obligations
    due within a year from assets convertible to cash within a year?

    Interpretation:
      >= 1.5× — comfortable working-capital cushion
      ~1.0×  — can just cover current liabilities (already meaningful stress)
      < 0.75× — acute working-capital deficit; <0.6× is used in distress classification

    Lower is worse. Raises MissingDataError if current liabilities is zero.
    """
    ca, ca_tag = _resolve(facts, "current_assets", period_end, filed_before)
    cl, cl_tag = _resolve(facts, "current_liabilities", period_end, filed_before)

    if cl == 0:
        raise MissingDataError(f"Current liabilities is zero for {period_end}, current ratio undefined")

    return RatioResult(
        name="current_ratio",
        value=ca / cl,
        inputs={"current_assets": ca, "current_liabilities": cl},
        source_tags={"current_assets": ca_tag, "current_liabilities": cl_tag},
        period_end=period_end,
    )


def debt_to_equity(facts, period_end, filed_before=None):
    """
    Debt-to-Equity = Total Debt / Shareholders' Equity.
    If equity is zero: raise MissingDataError.
    If equity is negative: compute and return — negative D/E is itself the stress signal.
    Total Debt reuses the same tags as leverage() — see gross_debt().
    """
    gd, gd_inputs, gd_tags = gross_debt(facts, period_end, filed_before)
    equity, eq_tag = _resolve(facts, "stockholders_equity", period_end, filed_before)
    if equity == 0:
        raise MissingDataError(f"Shareholders equity is zero for {period_end}")
    return RatioResult(
        name="debt_to_equity",
        value=gd / equity,
        inputs={**gd_inputs, "stockholders_equity": equity},
        source_tags={**gd_tags, "stockholders_equity": eq_tag},
        period_end=period_end,
    )


def revenue_yoy_growth(facts, period_end, filed_before=None):
    """
    Revenue YoY Growth = (Current Revenue − Prior Year Revenue) / Prior Year Revenue.
    Prior year period_end = subtract exactly 365 days from period_end.
    If prior year revenue not found: raise MissingDataError.
    Returns growth as a decimal fraction (e.g. -0.05 = -5% decline).
    """
    from datetime import date, timedelta
    rev, rev_tag = _resolve(facts, "revenue", period_end, filed_before)
    prior_date = (date.fromisoformat(period_end) - timedelta(days=365)).isoformat()
    try:
        prior_rev, prior_tag = _resolve(facts, "revenue", prior_date, filed_before)
    except MissingDataError:
        # Try ±15 days around the prior year date to handle fiscal year shifts
        found = False
        for delta in range(1, 16):
            for sign in (1, -1):
                try_date = (date.fromisoformat(prior_date) + timedelta(days=sign*delta)).isoformat()
                try:
                    prior_rev, prior_tag = _resolve(facts, "revenue", try_date, filed_before)
                    found = True
                    break
                except MissingDataError:
                    continue
            if found:
                break
        if not found:
            raise MissingDataError(f"Prior year revenue not found for {period_end}")
    if prior_rev == 0:
        raise MissingDataError(f"Prior year revenue is zero for {period_end}")
    return RatioResult(
        name="revenue_yoy_growth",
        value=(rev - prior_rev) / prior_rev,
        inputs={"revenue": rev, "prior_year_revenue": prior_rev},
        source_tags={"revenue": rev_tag, "prior_year_revenue": prior_tag},
        period_end=period_end,
    )


def asset_coverage(facts, period_end, filed_before=None):
    """
    Asset Coverage = Total Assets / Total Debt.
    MissingDataError if Total Debt is zero (no debt = ratio undefined, not stress).
    """
    assets, assets_tag = _resolve(facts, "total_assets", period_end, filed_before)
    gd, gd_inputs, gd_tags = gross_debt(facts, period_end, filed_before)
    if gd == 0:
        raise MissingDataError(f"Total debt is zero for {period_end}")
    return RatioResult(
        name="asset_coverage",
        value=assets / gd,
        inputs={"total_assets": assets, **gd_inputs},
        source_tags={"total_assets": assets_tag, **gd_tags},
        period_end=period_end,
    )


def tangible_asset_coverage(facts, period_end, filed_before=None):
    """
    Tangible Asset Coverage = (Total Assets − Goodwill − Intangibles − DTA) / Total Debt.
    Goodwill, intangibles, DTA are optional — if missing treat as zero (no flag needed
    for industrial companies; flag for tech/pharma is handled in alerts not here).
    MissingDataError if Total Assets or Total Debt is missing.
    """
    assets, assets_tag = _resolve(facts, "total_assets", period_end, filed_before)
    gd, gd_inputs, gd_tags = gross_debt(facts, period_end, filed_before)
    if gd == 0:
        raise MissingDataError(f"Total debt is zero for {period_end}")

    deductions = {}
    deduction_tags = {}
    for concept in ("goodwill", "intangible_assets", "deferred_tax_asset"):
        try:
            val, tag = _resolve(facts, concept, period_end, filed_before)
            deductions[concept] = max(val, 0)  # DTA can be negative (net liability) → treat as 0
            deduction_tags[concept] = tag
        except MissingDataError:
            deductions[concept] = 0.0

    tangible = assets - sum(deductions.values())
    return RatioResult(
        name="tangible_asset_coverage",
        value=tangible / gd,
        inputs={"total_assets": assets, **deductions, **gd_inputs},
        source_tags={"total_assets": assets_tag, **deduction_tags, **gd_tags},
        period_end=period_end,
    )


def liquidation_asset_coverage(facts, period_end, filed_before=None):
    """
    Liquidation Asset Coverage = Liquidation Value / Total Debt.
    Applies standard distressed-lending haircuts per asset class:
      Cash: 100%, Receivables: 75% (midpoint 70-80%), Inventory: 50% (midpoint 40-60%),
      PP&E: 45% (midpoint 30-60%), Intangibles/Goodwill: 0%.
    Uses gross_debt() as denominator (consistent with asset_coverage).
    MissingDataError if Total Debt is zero or if Cash and all asset components are missing.
    """
    gd, gd_inputs, gd_tags = gross_debt(facts, period_end, filed_before)
    if gd == 0:
        raise MissingDataError(f"Total debt is zero for {period_end}")

    cash, cash_tag = _resolve(facts, "cash", period_end, filed_before)

    inputs = {"cash": cash, **gd_inputs}
    tags = {"cash": cash_tag, **gd_tags}
    liquidation_value = cash * 1.0  # 100% haircut

    for concept, haircut in [("accounts_receivable", 0.75), ("inventory", 0.50), ("ppe_net", 0.45)]:
        try:
            val, tag = _resolve(facts, concept, period_end, filed_before)
            inputs[concept] = val
            tags[concept] = tag
            liquidation_value += val * haircut
        except MissingDataError:
            pass  # asset class absent — skip, do not raise

    return RatioResult(
        name="liquidation_asset_coverage",
        value=liquidation_value / gd,
        inputs=inputs,
        source_tags=tags,
        period_end=period_end,
    )


def quick_ratio(facts, period_end, filed_before=None):
    """
    Quick Ratio = (Current Assets − Inventory − Prepaid Expenses) / Current Liabilities.
    Inventory and prepaid are optional — if missing treat as zero (conservative: assumes
    all current assets are liquid, which is the safer direction for credit analysis).
    MissingDataError if Current Liabilities is zero.
    """
    ca, ca_tag = _resolve(facts, "current_assets", period_end, filed_before)
    cl, cl_tag = _resolve(facts, "current_liabilities", period_end, filed_before)
    if cl == 0:
        raise MissingDataError(f"Current liabilities is zero for {period_end}")

    inputs = {"current_assets": ca, "current_liabilities": cl}
    tags = {"current_assets": ca_tag, "current_liabilities": cl_tag}
    deductions = 0.0

    for concept in ("inventory", "prepaid_expenses"):
        try:
            val, tag = _resolve(facts, concept, period_end, filed_before)
            inputs[concept] = val
            tags[concept] = tag
            deductions += val
        except MissingDataError:
            pass

    return RatioResult(
        name="quick_ratio",
        value=(ca - deductions) / cl,
        inputs=inputs,
        source_tags=tags,
        period_end=period_end,
    )


def ocf_ebitda_conversion(facts, period_end, filed_before=None):
    """
    OCF/EBITDA Conversion = Operating Cash Flow / EBITDA.
    MissingDataError if EBITDA is zero (same guard as interest_coverage).
    High conversion (>0.9x) = clean earnings; low conversion (<0.6x) = working capital drag.
    """
    ocf, ocf_tag = _resolve(facts, "operating_cashflow", period_end, filed_before)
    ebit, ebit_inputs, ebit_tags = ebitda(facts, period_end, filed_before)
    if ebit == 0:
        raise MissingDataError(f"EBITDA is zero for {period_end}")
    return RatioResult(
        name="ocf_ebitda_conversion",
        value=ocf / ebit,
        inputs={"operating_cashflow": ocf, **ebit_inputs},
        source_tags={"operating_cashflow": ocf_tag, **ebit_tags},
        period_end=period_end,
    )


def moody_adjusted_fcf(facts, period_end, filed_before=None):
    """
    Moody's Adjusted FCF = Operating Cash Flow − Dividends Paid − Maintenance Capex.
    Since maintenance capex is rarely disclosed, use D&A as proxy for maintenance capex
    (standard analyst convention when split not available).
    Dividends paid is optional — if not tagged, use OCF − D&A (excludes dividend adjustment).
    This is the Moody's RCF approximation: OCF + pension addback − dividends.
    Pension addback requires LLM (Phase 3) — omit here; use OCF directly.
    """
    ocf, ocf_tag = _resolve(facts, "operating_cashflow", period_end, filed_before)
    dep, dep_tag = _resolve(facts, "depreciation", period_end, filed_before)

    inputs = {"operating_cashflow": ocf, "depreciation_proxy_for_maintenance_capex": dep}
    tags = {"operating_cashflow": ocf_tag, "depreciation": dep_tag}

    # Dividends paid — optional, from financing activities on cash flow statement
    try:
        div, div_tag = _resolve(facts, "dividends_paid", period_end, filed_before)
        inputs["dividends_paid"] = div
        tags["dividends_paid"] = div_tag
    except MissingDataError:
        div = 0.0

    # Moody's Adjusted FCF = OCF − maintenance capex (proxy: D&A) − dividends
    value = ocf - dep - div
    return RatioResult(
        name="moody_adjusted_fcf",
        value=value,
        inputs=inputs,
        source_tags=tags,
        period_end=period_end,
    )


def rcf_net_debt(facts, period_end, filed_before=None):
    """
    RCF/Net Debt = Retained Cash Flow / Net Debt.
    Retained Cash Flow (Moody's RCF) = OCF − Dividends Paid.
    Net Debt reuses net_debt() helper (same as leverage numerator).
    MissingDataError if Net Debt is zero or negative (net cash = ratio undefined as stress signal).
    RCF/Net Debt is a companion to Debt/EBITDA — measures cash generation vs debt burden.
    """
    ocf, ocf_tag = _resolve(facts, "operating_cashflow", period_end, filed_before)

    try:
        div, div_tag = _resolve(facts, "dividends_paid", period_end, filed_before)
    except MissingDataError:
        div, div_tag = 0.0, None

    rcf = ocf - div
    nd, nd_inputs, nd_tags = net_debt(facts, period_end, filed_before)

    if nd <= 0:
        raise MissingDataError(f"Net debt is zero or negative for {period_end} — ratio undefined for net-cash companies")

    inputs = {"operating_cashflow": ocf, "dividends_paid": div, **nd_inputs}
    tags = {"operating_cashflow": ocf_tag, **nd_tags}
    if div_tag:
        tags["dividends_paid"] = div_tag

    return RatioResult(
        name="rcf_net_debt",
        value=rcf / nd,
        inputs=inputs,
        source_tags=tags,
        period_end=period_end,
    )


def maturity_coverage_near_term(facts, period_end, filed_before=None):
    """
    Maturity Coverage (Near-Term) = Cash / Year-1 Debt Maturities.
    Uses the same XBRL maturity tags already in concepts.py (debt_maturity_y1).
    MissingDataError if Year-1 maturities not tagged (common — many filers omit).
    MissingDataError if Year-1 maturities is zero (no near-term maturities = not a stress signal).
    A ratio below 1.0x means cash cannot cover the next 12 months of maturities alone.
    """
    cash, cash_tag = _resolve(facts, "cash", period_end, filed_before)
    y1, y1_tag = _resolve(facts, "debt_maturity_y1", period_end, filed_before)
    if y1 == 0:
        raise MissingDataError(f"Year-1 debt maturities is zero for {period_end}")
    return RatioResult(
        name="maturity_coverage_near_term",
        value=cash / y1,
        inputs={"cash": cash, "debt_maturity_y1": y1},
        source_tags={"cash": cash_tag, "debt_maturity_y1": y1_tag},
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
      near_term_pct   = (y1 + y2 + y3) / total_scheduled   (None if total is 0)
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

    # near_term = principal due within the next three fiscal years.
    near_term = buckets.get("y1", 0.0) + buckets.get("y2", 0.0) + buckets.get("y3", 0.0)
    near_term_pct = (near_term / total_scheduled) if total_scheduled else None

    # wall_year = the single bucket carrying the most principal.
    wall_year = max(buckets, key=buckets.get) if buckets else None

    # ── Reconciliation guard (deterministic, no LLM) ─────────────────────────
    # Compare the tagged bucket sum against XBRL total debt (the validated
    # gross_debt waterfall on the SAME facts). When they don't reconcile the
    # filer under-tagged the schedule, so near_term_pct is off a truncated total
    # and the maturity-wall rule must be suppressed (see score.py), not scored.
    #   "high"     — buckets reconcile with total debt (healthy, fully-tagged).
    #   "degraded" — they don't (under-tagging, e.g. RAD dropping y5/thereafter).
    #   "unknown"  — total debt unavailable/zero, or no buckets tagged; falls
    #                through to today's behavior (near_term_pct already None/0).
    total_debt_reconcile: float | None = None
    if total_scheduled and buckets:
        try:
            total_debt_reconcile, _, _ = gross_debt(facts, period_end, filed_before)
        except MissingDataError:
            total_debt_reconcile = None

    if not total_scheduled or not buckets or not total_debt_reconcile:
        schedule_confidence = "unknown"
    else:
        reconciled = (
            abs(total_scheduled - total_debt_reconcile) / total_debt_reconcile
            <= MATURITY_RECONCILE_TOLERANCE
        )
        schedule_confidence = "high" if reconciled else "degraded"

    return MaturitySchedule(
        period_end=period_end,
        buckets=buckets,
        source_tags=source_tags,
        total_scheduled=total_scheduled,
        near_term_pct=near_term_pct,
        wall_year=wall_year,
        schedule_confidence=schedule_confidence,
        total_debt_reconcile=total_debt_reconcile,
    )


# ── Batch extraction ─────────────────────────────────────────────────────────

# This list drives extract_all(). Adding a new ratio function here is all
# that's needed to include it in every batch extraction run.
_RATIO_FUNCTIONS = [
    leverage, interest_coverage, free_cash_flow, fcf_margin, ebitda_margin, liquidity,
    cash_flow_to_debt, debt_to_assets, current_ratio,
    # 10 additional metrics
    debt_to_equity, revenue_yoy_growth, asset_coverage, tangible_asset_coverage,
    liquidation_asset_coverage, quick_ratio, ocf_ebitda_conversion, moody_adjusted_fcf,
    rcf_net_debt, maturity_coverage_near_term,
    # Moody's Formula-2 adjustment (post-2019 deterministic): SUPPLEMENTS `leverage`,
    # a MissingRatio when the ROU liability isn't tagged (falls back to Formula 1).
    leverage_adjusted,
    # Moody's Formula-2 adjusted coverage (lease-interest leg): SUPPLEMENTS
    # `interest_coverage`; MissingRatio when operating_lease_cost is untagged.
    interest_coverage_adjusted,
    # Moody's Formula-2 pension legs (deterministic flags, parallel to lease legs):
    # MissingRatio when the pension tags are absent (falls back to Formula 1).
    interest_coverage_pension_adjusted, pension_ebitda_reclass, moody_adjusted_fcf_pension,
    # Always-defined lease-inflation multiple (no EBITDA denominator) — the
    # companion signal that works even when leverage_adjusted's ratio is degenerate.
    lease_debt_burden,
    # Moody's pension debt add (deterministic layer, PARALLEL to leases): a
    # MissingRatio when funded status is untagged (~70-80%, deferred to LLM layer).
    pension_debt_burden,
]


# Maps each ratio name → the ordered list of input concept keys (keys into
# concepts.TAGS) its formula consumes. Used by diagnose_ratio() to pinpoint which
# raw input is missing when a ratio can't be computed. Field names equal the concept
# keys, matching the input names the ratio functions put in RatioResult.inputs.
RATIO_INPUTS: dict[str, list[str]] = {
    "leverage":              ["total_debt", "cash", "operating_income", "depreciation"],
    "interest_coverage":     ["operating_income", "depreciation", "interest_expense"],
    "free_cash_flow":        ["operating_cashflow", "capex"],
    "fcf_margin":            ["operating_cashflow", "capex", "revenue"],
    "ebitda_margin":         ["operating_income", "depreciation", "revenue"],
    "liquidity":             ["cash", "short_term_debt"],
    "cash_flow_to_debt":     ["operating_cashflow", "total_debt", "short_term_debt"],
    "debt_to_assets":        ["total_debt", "short_term_debt", "total_assets"],
    "current_ratio":         ["current_assets", "current_liabilities"],
    "debt_to_equity":             ["total_debt", "short_term_debt", "stockholders_equity"],
    "revenue_yoy_growth":         ["revenue"],
    "asset_coverage":             ["total_assets", "total_debt", "short_term_debt"],
    "tangible_asset_coverage":    ["total_assets", "goodwill", "intangible_assets", "deferred_tax_asset", "total_debt", "short_term_debt"],
    "liquidation_asset_coverage": ["cash", "accounts_receivable", "inventory", "ppe_net", "total_debt", "short_term_debt"],
    "quick_ratio":                ["current_assets", "current_liabilities", "inventory", "prepaid_expenses"],
    "ocf_ebitda_conversion":      ["operating_cashflow", "operating_income", "depreciation"],
    "moody_adjusted_fcf":         ["operating_cashflow", "depreciation", "dividends_paid"],
    "rcf_net_debt":               ["operating_cashflow", "dividends_paid", "total_debt", "cash"],
    "maturity_coverage_near_term":["cash", "debt_maturity_y1"],
    "leverage_adjusted":          ["total_debt", "cash", "operating_income", "depreciation",
                                   "operating_lease_liability_current",
                                   "operating_lease_liability_noncurrent", "operating_lease_cost"],
    "interest_coverage_adjusted": ["operating_income", "depreciation", "interest_expense",
                                   "operating_lease_cost"],
    "interest_coverage_pension_adjusted": ["operating_income", "depreciation", "interest_expense",
                                           "pension_interest_cost"],
    "pension_ebitda_reclass":     ["operating_income", "depreciation",
                                   "pension_net_periodic_cost", "pension_service_cost"],
    "moody_adjusted_fcf_pension": ["operating_cashflow", "pension_contributions",
                                   "depreciation", "dividends_paid"],
    "lease_debt_burden":          ["total_debt", "cash",
                                   "operating_lease_liability_current",
                                   "operating_lease_liability_noncurrent"],
    "pension_debt_burden":        ["total_debt", "cash", "pension_funded_status",
                                   "pension_pbo", "pension_plan_assets"],
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
    Run every ratio function in _RATIO_FUNCTIONS for one (company, period) combination.

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
