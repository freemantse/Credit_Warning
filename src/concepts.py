"""
XBRL tag fallback maps and concept resolution.

Each concept maps to a prioritised list of XBRL tags (taxonomy/ConceptName).
resolve_tag tries them in order and returns (value, winning_tag).
It raises MissingDataError if none resolve — never returns 0 or a default.
"""

from __future__ import annotations


class MissingDataError(Exception):
    """Raised when a required financial concept cannot be resolved from XBRL facts."""


# Prioritised fallback lists per concept.
# Format: "taxonomy/ConceptName" matching the EDGAR companyfacts JSON structure.
TAGS: dict[str, list[str]] = {
    "revenue": [
        "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap/Revenues",
        "us-gaap/SalesRevenueNet",
        "us-gaap/RevenueFromContractWithCustomerIncludingAssessedTax",
        "us-gaap/SalesRevenueGoodsNet",
    ],
    "operating_income": [
        "us-gaap/OperatingIncomeLoss",
        "us-gaap/IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    ],
    "interest_expense": [
        "us-gaap/InterestExpense",
        "us-gaap/InterestAndDebtExpense",
        "us-gaap/InterestExpenseDebt",
        "us-gaap/FinanceLeaseInterestExpense",
    ],
    "depreciation": [
        "us-gaap/DepreciationDepletionAndAmortization",
        "us-gaap/DepreciationAndAmortization",
        "us-gaap/Depreciation",
        "us-gaap/AmortizationOfIntangibleAssets",
    ],
    "total_debt": [
        "us-gaap/LongTermDebt",
        "us-gaap/LongTermDebtNoncurrent",
        "us-gaap/DebtAndCapitalLeaseObligations",
        "us-gaap/LongTermDebtAndCapitalLeaseObligations",
        "us-gaap/NotesAndLoansPayable",
    ],
    "short_term_debt": [
        "us-gaap/ShortTermBorrowings",
        "us-gaap/LongTermDebtCurrent",
        "us-gaap/DebtCurrent",
        "us-gaap/NotesAndLoansPayableCurrent",
    ],
    "cash": [
        "us-gaap/CashAndCashEquivalentsAtCarryingValue",
        "us-gaap/CashCashEquivalentsAndShortTermInvestments",
        "us-gaap/Cash",
        "us-gaap/CashAndCashEquivalentsPeriodIncreaseDecrease",
    ],
    "operating_cashflow": [
        "us-gaap/NetCashProvidedByUsedInOperatingActivities",
    ],
    "capex": [
        "us-gaap/PaymentsToAcquirePropertyPlantAndEquipment",
        "us-gaap/CapitalExpendituresIncurredButNotYetPaid",
        "us-gaap/PaymentsToAcquireProductiveAssets",
    ],
    "net_income": [
        "us-gaap/NetIncomeLoss",
        "us-gaap/ProfitLoss",
        "us-gaap/IncomeLossFromContinuingOperations",
    ],
    "total_assets": [
        "us-gaap/Assets",
    ],
    "total_liabilities": [
        "us-gaap/Liabilities",
    ],
}


def resolve_tag(
    facts: dict,
    concept: str,
    period_end: str,
    filed_before: str | None = None,
) -> tuple[float, str]:
    """
    Try each tag for `concept` in priority order.

    Args:
        facts: EDGAR companyfacts JSON (the full dict from CIK{cik}.json).
        concept: key from TAGS, e.g. "revenue".
        period_end: ISO date string "YYYY-MM-DD"; match the 'end' field of a unit entry.
        filed_before: if set, only consider entries whose 'filed' <= this date (point-in-time).

    Returns:
        (value, winning_tag) — the first tag that resolves.

    Raises:
        MissingDataError: if concept unknown or no tag resolves for this period.
    """
    if concept not in TAGS:
        raise MissingDataError(f"Unknown concept: {concept!r}")

    tag_list = TAGS[concept]
    us_gaap = facts.get("facts", {}).get("us-gaap", {})

    for tag_path in tag_list:
        # tag_path is "us-gaap/ConceptName" — strip the taxonomy prefix
        tag_name = tag_path.split("/", 1)[1]
        concept_data = us_gaap.get(tag_name)
        if not concept_data:
            continue

        # EDGAR units: usually "USD" for financials
        for unit_key, entries in concept_data.get("units", {}).items():
            for entry in entries:
                if entry.get("end") != period_end:
                    continue
                # Only annual (10-K) or quarterly (10-Q) forms; skip 10-K/A amendments
                # that might skew point-in-time if filed_before is set
                if filed_before and entry.get("filed", "") > filed_before:
                    continue
                # Prefer instantaneous or duration entries that match period exactly
                if "val" not in entry:
                    continue
                return float(entry["val"]), tag_path

    attempted = ", ".join(tag_list)
    raise MissingDataError(
        f"Concept {concept!r} not found for period_end={period_end!r}. "
        f"Tried: {attempted}"
    )
