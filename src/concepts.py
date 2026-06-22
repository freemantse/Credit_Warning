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
        "us-gaap/InterestAndDividendIncomeOperating",                    # banks: net interest income line
        "us-gaap/SalesRevenueServicesGross",                             # gross services revenue (pre-606)
        "us-gaap/OilAndGasRevenue",                                      # oil & gas operators
        "us-gaap/ContractsRevenue",                                      # construction / long-term contracts
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
        "us-gaap/InterestExpenseOperating",   # operating interest expense (finance/leasing cos)
        "us-gaap/InterestCostsIncurred",      # gross interest incurred (before capitalisation)
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
        "us-gaap/CostOfServicesDepreciation",            # D&A embedded in cost of services
        "us-gaap/DepreciationNonproduction",             # non-production (SG&A) depreciation
        "us-gaap/AmortizationOfDeferredCharges",         # amortisation of deferred charges
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
        "us-gaap/NotesPayable",                           # total notes payable (e.g. insurers)
        "us-gaap/SeniorNotes",                            # senior notes outstanding
        "us-gaap/SubordinatedDebt",                       # subordinated debt outstanding
        "us-gaap/SecuredDebt",                            # total secured debt
        "us-gaap/UnsecuredDebt",                          # total unsecured debt
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
        "us-gaap/LoansPayableCurrent",           # current loans payable
        "us-gaap/OtherLoansPayableCurrent",      # other current loans payable
        "us-gaap/ConvertibleNotesPayableCurrent",# current convertible notes
        "us-gaap/ShortTermBankLoansAndNotesPayable", # short-term bank loans & notes
        "us-gaap/BankOverdrafts",                # bank overdrafts
        "us-gaap/BridgeLoan",                    # bridge financing
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
        "us-gaap/RestrictedCashAndCashEquivalentsAtCarryingValue", # restricted cash (last-resort level)
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
        "us-gaap/PaymentsToAcquireBuildings",                  # buildings
        "us-gaap/PaymentsToAcquireEquipmentOnLease",           # equipment leased to others
        "us-gaap/PaymentsToAcquireRealEstate",                 # REITs / real estate
        "us-gaap/PaymentsToDevelopRealEstateAssets",           # REIT development spend
        "us-gaap/PaymentsToAcquireOilAndGasProperty",          # oil & gas property
    ],

    # ── Net Income ───────────────────────────────────────────────────────────
    # Stored in the database for completeness and potential future use.
    # Not directly used in any stress-score calculation today.
    "net_income": [
        "us-gaap/NetIncomeLoss",
        "us-gaap/ProfitLoss",                               # IFRS-influenced filers
        "us-gaap/IncomeLossFromContinuingOperations",
        "us-gaap/IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest", # incl. NCI
        "us-gaap/NetIncomeLossAvailableToCommonStockholdersBasic", # net of preferred dividends
        "us-gaap/NetIncomeLossAllocatedToLimitedPartners",  # MLPs / partnerships
    ],

    # ── Total Assets ─────────────────────────────────────────────────────────
    # Used as the "anchor" concept in _get_available_periods() (extract.py).
    # Total assets (us-gaap/Assets) is reported exactly once per fiscal year in a
    # 10-K — making its 'end' dates a reliable list of fiscal year-end dates.
    # Intentionally SINGLE-TAG: us-gaap/Assets is the only clean balance-sheet
    # total, and this list anchors the fiscal-year-end period scan. Adding
    # "equivalents" here would pollute that period list, so do not expand it.
    "total_assets": [
        "us-gaap/Assets",
    ],

    # ── Total Liabilities ────────────────────────────────────────────────────
    # Numerator of the liabilities-to-assets solvency ratio (total_liabilities /
    # total_assets): a value > 1 implies negative book equity.
    # Intentionally SINGLE-TAG: us-gaap/Liabilities is the only unambiguous total;
    # there is no standard alternative tag for total liabilities.
    "total_liabilities": [
        "us-gaap/Liabilities",
    ],

    # ── Current Assets ─────────────────────────────────────────────────────────
    # Numerator of the current ratio (current_assets / current_liabilities), the
    # classic working-capital liquidity measure.
    # Intentionally SINGLE-TAG: us-gaap/AssetsCurrent is the ONLY standard US-GAAP
    # tag for total current assets — there is no fallback to add. Filers with an
    # UNCLASSIFIED balance sheet (banks, insurers such as AFLAC, some REITs) do not
    # split assets into current/non-current, so this concept is simply absent from
    # their filings. A missing current ratio for such issuers is expected, not a
    # tagging gap — no additional tag can recover it.
    "current_assets": [
        "us-gaap/AssetsCurrent",
    ],

    # ── Current Liabilities ──────────────────────────────────────────────────
    # Denominator of the current ratio. Obligations due within one year.
    # Intentionally SINGLE-TAG: us-gaap/LiabilitiesCurrent is the ONLY standard
    # total-current-liabilities tag. Same unclassified-balance-sheet caveat as
    # current_assets above applies (banks/insurers do not report it).
    "current_liabilities": [
        "us-gaap/LiabilitiesCurrent",
    ],

    # ── Debt maturity schedule (the "maturity wall") ───────────────────────────
    # Companies disclose how much long-term-debt principal comes due in each of
    # the next five fiscal years, plus a "thereafter" bucket. These tags drive
    # the deterministic maturity-wall extraction (debt_maturity_schedule in
    # extract.py): a heavy near-term concentration signals refinancing risk.
    # Per-bucket misses are tolerated — many filers omit some buckets.
    # Each bucket also accepts the "Rolling" taxonomy variant used by filers that
    # disclose maturities on a rolling-12-month basis rather than fiscal years.
    "debt_maturity_y1": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalRemainderOfFiscalYear",
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInNextRollingTwelveMonths",
    ],
    "debt_maturity_y2": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo",
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInRollingYearTwo",
    ],
    "debt_maturity_y3": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree",
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInRollingYearThree",
    ],
    "debt_maturity_y4": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour",
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInRollingYearFour",
    ],
    "debt_maturity_y5": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive",
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInRollingYearFive",
    ],
    "debt_maturity_thereafter": [
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive",
        "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInRollingAfterYearFive",
    ],

    # ── Loss contingency accrual ───────────────────────────────────────────────
    # The amount accrued on the balance sheet for probable losses (litigation,
    # environmental, etc.). Sparsely tagged, so treated as best-effort context
    # alongside the LLM-extracted loss provisions.
    "loss_contingency_accrual": [
        "us-gaap/LossContingencyAccrualAtCarryingValue",
        "us-gaap/LossContingencyAccrualCarryingValueCurrent",
        "us-gaap/LossContingencyAccrualCarryingValueNoncurrent",
    ],
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

    # Navigate to the us-gaap section of the EDGAR facts JSON.
    # Use .get() with defaults so we don't crash on malformed/empty data.
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    for tag_path in tag_list:
        # tag_path format: "us-gaap/ConceptName"
        # The EDGAR JSON uses just "ConceptName" as the dict key (no taxonomy prefix).
        tag_name = tag_path.split("/", 1)[1]  # e.g. "us-gaap/Revenues" → "Revenues"

        concept_data = us_gaap.get(tag_name)
        if not concept_data:
            # This company doesn't use this tag at all — try the next fallback.
            continue

        # EDGAR groups values by their unit of measurement.
        # Financial dollar amounts use "USD". Some concepts also have a "USD/shares"
        # unit (e.g. EPS). We iterate all units to be safe rather than hard-coding "USD".
        for unit_key, entries in concept_data.get("units", {}).items():
            for entry in entries:

                # Each entry represents one reported value. We only want entries
                # whose fiscal period-end date matches what we're looking for.
                if entry.get("end") != period_end:
                    continue

                # Point-in-time filter for the backtest: if filed_before is set,
                # skip entries from filings that weren't yet public at that date.
                # String comparison works here because dates are ISO format "YYYY-MM-DD".
                if filed_before and entry.get("filed", "") > filed_before:
                    continue

                # Some XBRL entries describe context (e.g. segment labels) without
                # a numeric value. The "val" key only exists for numeric entries.
                if "val" not in entry:
                    continue

                # This entry matches — return the value and the tag that supplied it.
                # Converting to float handles both int and float values from the JSON.
                return float(entry["val"]), tag_path

    # We exhausted every fallback tag without finding a matching entry.
    # Build a helpful error message listing exactly which tags were tried.
    attempted = ", ".join(tag_list)
    raise MissingDataError(
        f"Concept {concept!r} not found for period_end={period_end!r}. "
        f"Tried: {attempted}"
    )
