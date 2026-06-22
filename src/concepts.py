from __future__ import annotations

class MissingDataError(Exception):
    """
    Raised when a required financial concept cannot be resolved from XBRL facts.

    Callers (extract_all in extract.py) catch this and record it in the results dict rather than letting it propagate — so one missing ratio doesn't block the others from being computed.
    """

# ── Prioritised fallback tag lists per financial concept ────────────────────
#
# Each key is the concept name used throughout this codebase ("revenue", "cash").
# Each value is an ordered list of XBRL tag paths in "taxonomy/TagName" format.
# resolve_tag iterates this list top-to-bottom and returns the first match found in the company's EDGAR data for the requested period_end date.
#
# Ordering rule: most-specific / most-commonly-used tags come first.
# Less-specific fallbacks (e.g. pre-taxonomy tags) come last.
TAGS: dict[str, list[str]] = {

    # ── Revenue ──────────────────────────────────────────────────────────────
    # Preferred: ASC 606 tag introduced in 2018 (excludes collected sales tax).
    # Fallback chain covers older taxonomy names used before ASC 606 adoption.
    "revenue": [
        "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax",  # post-2018 standard
        "us-gaap/Revenues",                                              # broad, widely used
        "us-gaap/SalesRevenueNet",                                       # older net-revenue tag
        "us-gaap/RevenueFromContractWithCustomerIncludingAssessedTax",   # includes sales tax
        "us-gaap/SalesRevenueGoodsNet",                                  # goods-only legacy tag
        "us-gaap/RevenuesNetOfInterestExpense",                          # banks / broker-dealers
        "us-gaap/RegulatedAndUnregulatedOperatingRevenue",              # utilities
        "us-gaap/SalesRevenueServicesNet",                               # services-only legacy tag
    ],

    # ── Operating Income ─────────────────────────────────────────────────────
    # OperatingIncomeLoss is the standard tag. The second tag catches companies
    # that report pre-tax income as their top-line income figure but don't
    # separately break out operating income on the XBRL tagging.
    "operating_income": [
        "us-gaap/OperatingIncomeLoss",
        "us-gaap/IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "us-gaap/IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],

    # ── Interest Expense ─────────────────────────────────────────────────────
    # Multiple equivalent tags exist across debt instruments and lease obligations.
    "interest_expense": [
        "us-gaap/InterestExpense",           # most common
        "us-gaap/InterestAndDebtExpense",    # combined debt + lease interest
        "us-gaap/InterestExpenseDebt",       # debt-only interest
        "us-gaap/FinanceLeaseInterestExpense", # finance lease interest (post-ASC 842)
        "us-gaap/InterestExpenseBorrowings",  # interest on borrowings
        "us-gaap/InterestExpenseDebtExcludingAmortization", # debt interest ex-amortisation
        "us-gaap/InterestPaidNet",            # last resort: cash interest paid (cash flow stmt)
    ],

    # ── Depreciation & Amortisation ──────────────────────────────────────────
    # Added back to operating income to derive EBITDA (Earnings Before Interest,
    # Taxes, Depreciation & Amortisation). D&A is a non-cash charge so EBITDA
    # is a better proxy for cash generation than operating income alone.
    "depreciation": [
        "us-gaap/DepreciationDepletionAndAmortization",  # most inclusive
        "us-gaap/DepreciationAndAmortization",
        "us-gaap/DepreciationAmortizationAndAccretionNet", # incl. accretion (extractives, ARO)
        "us-gaap/CostOfGoodsAndServicesDepreciationAndAmortization", # D&A embedded in COGS
        "us-gaap/Depreciation",                          # depreciation only
        "us-gaap/AmortizationOfIntangibleAssets",        # last resort: intangibles only
    ],

    # ── Total (Long-Term) Debt ───────────────────────────────────────────────
    # Used as the numerator in net_debt = total_debt - cash.
    # Prefers LongTermDebt which excludes current maturities; broader tags
    # (DebtAndCapitalLeaseObligations) are used if the narrower tag is absent.
    "total_debt": [
        "us-gaap/LongTermDebt",
        "us-gaap/LongTermDebtNoncurrent",
        "us-gaap/DebtAndCapitalLeaseObligations",         # includes lease liabilities
        "us-gaap/LongTermDebtAndCapitalLeaseObligations",
        "us-gaap/LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities", # gross incl. current
        "us-gaap/DebtLongtermAndShorttermCombinedAmount", # combined long + short term debt
        "us-gaap/NotesAndLoansPayable",
    ],

    # ── Short-Term Debt ──────────────────────────────────────────────────────
    # Current-portion debt: used in the liquidity ratio (cash / short_term_debt).
    # This measures whether the company can cover imminent debt maturities.
    "short_term_debt": [
        "us-gaap/ShortTermBorrowings",
        "us-gaap/LongTermDebtCurrent",          # current portion of long-term debt
        "us-gaap/DebtCurrent",                   # broadest current-debt tag
        "us-gaap/NotesAndLoansPayableCurrent",
        "us-gaap/CommercialPaper",               # commercial paper outstanding
        "us-gaap/LinesOfCreditCurrent",          # drawn revolving credit (current)
        "us-gaap/OtherShortTermBorrowings",      # residual short-term borrowings
        "us-gaap/SecuredDebtCurrent",            # current portion of secured debt
    ],

    # ── Debt-waterfall components (Phase 1.5) ──────────────────────────────────
    # gross_debt() builds total debt additively from these components instead of
    # adding total_debt + short_term_debt (which double-counted the current
    # portion and undercounted multi-instrument short-term debt). The old
    # total_debt / short_term_debt keys above are RETAINED for backward
    # compatibility (diagnose_ratio / RATIO_INPUTS still reference them).
    #
    # Component A — genuinely-additive short-term instruments, SUMMED (not
    # first-only) via _resolve_sum. Deliberately EXCLUDES LongTermDebtCurrent
    # (that is the current portion of LTD = Component B, not a separate
    # instrument). Deduplicated — each tag is summed at most once.
    "st_debt_components": [
        "us-gaap/ShortTermBorrowings",
        "us-gaap/CommercialPaper",
        "us-gaap/NotesPayableCurrent",
        "us-gaap/LinesOfCreditCurrent",
        "us-gaap/ShortTermBankLoansAndNotesPayable",
    ],
    # Component B — current portion of long-term debt (first-match).
    "current_ltd": [
        "us-gaap/LongTermDebtCurrent",
        "us-gaap/LongTermDebtAndCapitalLeaseObligationsCurrent",
    ],
    # Component B fallback — used only when current_ltd is absent.
    "debt_current_fallback": [
        "us-gaap/DebtCurrent",
    ],
    # Component C — non-current long-term debt (first-match). LongTermDebt is
    # included but frequently bundles the current maturities, so gross_debt()
    # applies a double-count guard when C resolves via exactly us-gaap/LongTermDebt.
    "lt_debt_noncurrent": [
        "us-gaap/LongTermDebtNoncurrent",
        "us-gaap/LongTermDebt",
        "us-gaap/LongTermDebtAndCapitalLeaseObligationsNoncurrent",
    ],
    # Aggregate combined-debt tags (first-match) — the Level-2 fallback / override.
    # The last two are NOT in the Phase-1.5 spec list but ARE in the legacy
    # total_debt chain this waterfall replaces; without them, capital-lease
    # filers that report ONLY an "including current maturities" aggregate (e.g.
    # GM → us-gaap/LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities,
    # ~$131.6B) would resolve no components and regress to data_gap. Added to
    # preserve the legacy coverage exactly while keeping the double-count fix.
    # FLAGGED for review (Step 7) — revert if strict spec adherence is preferred.
    "debt_aggregate": [
        "us-gaap/DebtLongtermAndShorttermCombinedAmount",
        "us-gaap/DebtAndCapitalLeaseObligations",
        "us-gaap/LongTermDebtAndCapitalLeaseObligations",
        "us-gaap/LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",  # legacy-parity
        "us-gaap/NotesAndLoansPayable",                                              # legacy-parity (last resort)
    ],

    # ── Cash ─────────────────────────────────────────────────────────────────
    # Used in: net_debt = total_debt - cash, and liquidity = cash / short_term_debt.
    # Prefer the balance-sheet carrying value; the period-increase tag is a last
    # resort (it measures change, not level, and is only used if nothing else matches).
    "cash": [
        "us-gaap/CashAndCashEquivalentsAtCarryingValue",      # standard balance-sheet tag
        "us-gaap/CashCashEquivalentsAndShortTermInvestments", # includes short-term investments
        "us-gaap/CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations", # incl. disc. ops
        "us-gaap/CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", # post-ASU 2016-18 total
        "us-gaap/Cash",                                        # narrower: cash only
        "us-gaap/CashAndCashEquivalentsPeriodIncreaseDecrease", # last resort: flow tag
    ],

    # ── Operating Cash Flow ──────────────────────────────────────────────────
    # From the cash flow statement (not the income statement). Only one tag exists
    # in practice — this is one of the most consistently tagged XBRL concepts.
    "operating_cashflow": [
        "us-gaap/NetCashProvidedByUsedInOperatingActivities",
        "us-gaap/NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", # continuing ops only
    ],

    # ── Capital Expenditures (CapEx) ─────────────────────────────────────────
    # Subtracted from operating cash flow to compute free cash flow.
    # CapEx is reported as a cash outflow, so EDGAR stores it as a positive number.
    # We subtract it: FCF = OCF - capex.
    "capex": [
        "us-gaap/PaymentsToAcquirePropertyPlantAndEquipment",  # most common PP&E capex tag
        "us-gaap/CapitalExpendituresIncurredButNotYetPaid",    # accrued but unpaid capex
        "us-gaap/PaymentsToAcquireProductiveAssets",           # broader productive asset tag
        "us-gaap/PaymentsForCapitalImprovements",              # capital improvements (e.g. REITs)
        "us-gaap/PaymentsToAcquireOtherPropertyPlantAndEquipment", # other PP&E purchases
        "us-gaap/PaymentsToAcquireMachineryAndEquipment",      # machinery & equipment only
    ],

    # ── Net Income ───────────────────────────────────────────────────────────
    # Stored in the database for completeness and potential future use.
    # Not directly used in any stress-score calculation today.
    "net_income": [
        "us-gaap/NetIncomeLoss",
        "us-gaap/ProfitLoss",                               # IFRS-influenced filers
        "us-gaap/IncomeLossFromContinuingOperations",
        "us-gaap/NetIncomeLossAvailableToCommonStockholdersBasic", # net of preferred dividends
    ],

    # ── Total Assets ─────────────────────────────────────────────────────────
    # Used as the "anchor" concept in _get_available_periods() (extract.py).
    # Total assets (us-gaap/Assets) is reported exactly once per fiscal year in a
    # 10-K — making its 'end' dates a reliable list of fiscal year-end dates.
    "total_assets": [
        "us-gaap/Assets",
    ],

    # ── Total Liabilities ────────────────────────────────────────────────────
    # Numerator of the liabilities-to-assets solvency ratio (total_liabilities /
    # total_assets): a value > 1 implies negative book equity.
    "total_liabilities": [
        "us-gaap/Liabilities",
    ],

    # ── Current Assets ─────────────────────────────────────────────────────────
    # Numerator of the current ratio (current_assets / current_liabilities), the
    # classic working-capital liquidity measure. Reported once per year-end on the
    # balance sheet by non-financial filers (banks/insurers don't classify).
    "current_assets": [
        "us-gaap/AssetsCurrent",
    ],

    # ── Current Liabilities ──────────────────────────────────────────────────
    # Denominator of the current ratio. Obligations due within one year.
    "current_liabilities": [
        "us-gaap/LiabilitiesCurrent",
    ],

    # ── Debt maturity schedule (the "maturity wall") ───────────────────────────
    # Companies disclose how much long-term-debt principal comes due in each of
    # the next five fiscal years, plus a "thereafter" bucket. These tags drive
    # the deterministic maturity-wall extraction (debt_maturity_schedule in
    # extract.py): a heavy near-term concentration signals refinancing risk.
    # Per-bucket misses are tolerated — many filers omit some buckets.
    "debt_maturity_y1": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalRemainderOfFiscalYear",
    ],
    "debt_maturity_y2": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo",
    ],
    "debt_maturity_y3": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree",
    ],
    "debt_maturity_y4": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour",
    ],
    "debt_maturity_y5": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive",
    ],
    "debt_maturity_thereafter": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive",
    ],

    # ── Loss contingency accrual ───────────────────────────────────────────────
    # The amount accrued on the balance sheet for probable losses (litigation,
    # environmental, etc.). Sparsely tagged, so treated as best-effort context
    # alongside the LLM-extracted loss provisions.
    "loss_contingency_accrual": [
        "us-gaap/LossContingencyAccrualAtCarryingValue",
        "us-gaap/LossContingencyAccrualCarryingValueCurrent",
    ],

    # ── Shareholders' Equity (for debt_to_equity) ──────────────────────────────
    "stockholders_equity": [
        "us-gaap/StockholdersEquity",
        "us-gaap/StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],

    # ── Goodwill (for tangible_asset_coverage) ─────────────────────────────────
    "goodwill": [
        "us-gaap/Goodwill",
        "us-gaap/GoodwillNet",
    ],

    # ── Intangible Assets net of goodwill (for tangible_asset_coverage) ────────
    "intangible_assets": [
        "us-gaap/IntangibleAssetsNetExcludingGoodwill",
        "us-gaap/FiniteLivedIntangibleAssetsNet",
        "us-gaap/IndefiniteLivedIntangibleAssetsExcludingGoodwill",
    ],

    # ── Deferred Tax Asset (for tangible_asset_coverage) ───────────────────────
    "deferred_tax_asset": [
        "us-gaap/DeferredTaxAssetsLiabilitiesNet",
        "us-gaap/DeferredTaxAssetsNet",
    ],

    # ── Inventory (for quick_ratio and liquidation_asset_coverage) ─────────────
    "inventory": [
        "us-gaap/InventoryNet",
        "us-gaap/InventoryGross",
        "us-gaap/FIFOInventoryAmount",
    ],

    # ── Accounts Receivable (for quick_ratio and liquidation_asset_coverage) ───
    "accounts_receivable": [
        "us-gaap/AccountsReceivableNetCurrent",
        "us-gaap/ReceivablesNetCurrent",
        "us-gaap/AccountsAndNotesReceivableNet",
    ],

    # ── PP&E net (for liquidation_asset_coverage) ──────────────────────────────
    "ppe_net": [
        "us-gaap/PropertyPlantAndEquipmentNet",
        "us-gaap/PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
    ],

    # ── Prepaid expenses (for quick_ratio — excluded from numerator) ───────────
    "prepaid_expenses": [
        "us-gaap/PrepaidExpenseAndOtherAssetsCurrent",
        "us-gaap/PrepaidExpenseCurrent",
    ],

    # ── Dividends paid (for moody_adjusted_fcf) ────────────────────────────────
    "dividends_paid": [
        "us-gaap/PaymentsOfDividendsCommonStock",
        "us-gaap/PaymentsOfDividends",
        "us-gaap/PaymentsOfDividendsAndDividendEquivalentsOnCommonStockAndRestrictedStockUnits",
    ],

    # Note: "total_liabilities" (supplementary for debt_to_equity) is already
    # defined above (us-gaap/Liabilities) — not re-added here to avoid a dup key.
}


def resolve_tag(
    facts: dict,
    concept: str,
    period_end: str,
    filed_before: str | None = None,
) -> tuple[float, str]:
    """
    Search the EDGAR companyfacts JSON for a financial concept and return its value.

    How it works:
      1. Look up the fallback tag list for `concept` in TAGS.
      2. For each tag, navigate into facts["facts"]["us-gaap"][tag_name].
      3. Inside each tag's "units" dict (e.g. {"USD": [...entries...]}), scan
         each entry for one whose "end" date matches `period_end`.
      4. Apply the point-in-time filter: skip entries filed after `filed_before`.
      5. Return (value, tag_path) for the first entry that passes all checks.
      6. If no tag resolves, raise MissingDataError listing exactly what was tried.

    Args:
        facts:        Full EDGAR companyfacts JSON for one company.
        concept:      Key from the TAGS dict above, e.g. "revenue" or "cash".
        period_end:   ISO date "YYYY-MM-DD" for the fiscal year-end to look up.
        filed_before: If provided, only consider XBRL entries whose "filed" date is on or before this date. Used in the backtest to prevent look-ahead bias (we only use data that existed at eval_date).

    Returns:
        (value, winning_tag) — e.g. (394328000000.0, "us-gaap/Revenues")

    Raises:
        MissingDataError — if concept is unknown or no tag resolves for period_end.
    """
    if concept not in TAGS:
        raise MissingDataError(f"Unknown concept: {concept!r}")

    tag_list = TAGS[concept]

    # Walk the fallback list and return the first tag that resolves. The per-tag
    # scan lives in resolve_one_tag so the additive _resolve_sum path (Phase 1.5)
    # can reuse the exact same matching rules.
    for tag_path in tag_list:
        value = resolve_one_tag(facts, tag_path, period_end, filed_before)
        if value is not None:
            return value, tag_path

    # We exhausted every fallback tag without finding a matching entry.
    # Build a helpful error message listing exactly which tags were tried.
    attempted = ", ".join(tag_list)
    raise MissingDataError(
        f"Concept {concept!r} not found for period_end={period_end!r}. "
        f"Tried: {attempted}"
    )


def resolve_one_tag(
    facts: dict,
    tag_path: str,
    period_end: str,
    filed_before: str | None = None,
) -> float | None:
    """
    Resolve a SINGLE explicit XBRL tag path (e.g. "us-gaap/CommercialPaper") for
    one fiscal period. Returns the float value, or None when the tag is absent or
    has no matching entry (never raises — callers decide what a miss means).

    Applies the same per-entry rules as resolve_tag: match period_end, honour the
    filed_before point-in-time filter, and require a numeric "val". This is the
    shared building block for both resolve_tag (first-match) and extract._resolve_sum
    (sum-all-matches across an additive component list).
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    tag_name = tag_path.split("/", 1)[1]  # "us-gaap/Revenues" → "Revenues"

    concept_data = us_gaap.get(tag_name)
    if not concept_data:
        return None

    # EDGAR groups values by unit of measurement; iterate all units rather than
    # hard-coding "USD" (some concepts also carry "USD/shares" etc.).
    for unit_key, entries in concept_data.get("units", {}).items():
        for entry in entries:
            if entry.get("end") != period_end:
                continue
            # Point-in-time filter: skip filings not yet public at filed_before.
            # ISO dates compare lexicographically.
            if filed_before and entry.get("filed", "") > filed_before:
                continue
            # Context-only entries (segment labels etc.) have no "val".
            if "val" not in entry:
                continue
            return float(entry["val"])

    return None
