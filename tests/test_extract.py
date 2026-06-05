"""Tests for src/extract.py — pure, no network. Uses fixture companyfacts."""

import pytest
from src.extract import (
    leverage, interest_coverage, free_cash_flow, fcf_margin, liquidity,
    extract_all, RatioResult,
)
from src.concepts import MissingDataError

# All values in USD, designed so ratios have clean expected results:
#   net_debt = 4_000_000 - 1_000_000 = 3_000_000
#   EBITDA = 1_500_000 + 500_000 = 2_000_000
#   leverage = 3_000_000 / 2_000_000 = 1.5
#   interest_coverage = 2_000_000 / 400_000 = 5.0
#   FCF = 1_800_000 - 300_000 = 1_500_000
#   FCF margin = 1_500_000 / 10_000_000 = 0.15
#   liquidity = 1_000_000 / 500_000 = 2.0
PERIOD = "2023-12-31"

FACTS = {
    "facts": {
        "us-gaap": {
            "OperatingIncomeLoss":                            {"units": {"USD": [{"end": PERIOD, "val": 1_500_000, "filed": "2024-02-01", "form": "10-K"}]}},
            "DepreciationDepletionAndAmortization":           {"units": {"USD": [{"end": PERIOD, "val": 500_000,   "filed": "2024-02-01", "form": "10-K"}]}},
            "LongTermDebt":                                   {"units": {"USD": [{"end": PERIOD, "val": 4_000_000, "filed": "2024-02-01", "form": "10-K"}]}},
            "CashAndCashEquivalentsAtCarryingValue":          {"units": {"USD": [{"end": PERIOD, "val": 1_000_000, "filed": "2024-02-01", "form": "10-K"}]}},
            "InterestExpense":                                {"units": {"USD": [{"end": PERIOD, "val": 400_000,   "filed": "2024-02-01", "form": "10-K"}]}},
            "NetCashProvidedByUsedInOperatingActivities":     {"units": {"USD": [{"end": PERIOD, "val": 1_800_000, "filed": "2024-02-01", "form": "10-K"}]}},
            "PaymentsToAcquirePropertyPlantAndEquipment":     {"units": {"USD": [{"end": PERIOD, "val": 300_000,   "filed": "2024-02-01", "form": "10-K"}]}},
            "Revenues":                                       {"units": {"USD": [{"end": PERIOD, "val": 10_000_000,"filed": "2024-02-01", "form": "10-K"}]}},
            "ShortTermBorrowings":                            {"units": {"USD": [{"end": PERIOD, "val": 500_000,   "filed": "2024-02-01", "form": "10-K"}]}},
        }
    }
}


def test_leverage():
    result = leverage(FACTS, PERIOD)
    assert isinstance(result, RatioResult)
    assert abs(result.value - 1.5) < 1e-9
    assert result.inputs["total_debt"] == 4_000_000
    assert result.inputs["cash"] == 1_000_000
    assert result.inputs["operating_income"] == 1_500_000
    assert result.inputs["depreciation"] == 500_000


def test_interest_coverage():
    result = interest_coverage(FACTS, PERIOD)
    assert abs(result.value - 5.0) < 1e-9
    assert result.inputs["interest_expense"] == 400_000


def test_free_cash_flow():
    result = free_cash_flow(FACTS, PERIOD)
    assert abs(result.value - 1_500_000) < 1e-9
    assert result.inputs["operating_cashflow"] == 1_800_000
    assert result.inputs["capex"] == 300_000


def test_fcf_margin():
    result = fcf_margin(FACTS, PERIOD)
    assert abs(result.value - 0.15) < 1e-9


def test_liquidity():
    result = liquidity(FACTS, PERIOD)
    assert abs(result.value - 2.0) < 1e-9


def test_source_tags_populated():
    result = leverage(FACTS, PERIOD)
    assert "total_debt" in result.source_tags
    assert "LongTermDebt" in result.source_tags["total_debt"]


def test_extract_all_returns_all_ratios():
    results = extract_all(FACTS, PERIOD)
    assert "leverage" in results
    assert "interest_coverage" in results
    assert "free_cash_flow" in results
    assert isinstance(results["leverage"], RatioResult)


def test_extract_all_records_missing_not_crashes():
    # Remove cash concept to force a missing error on leverage/liquidity
    import copy
    facts_no_cash = copy.deepcopy(FACTS)
    del facts_no_cash["facts"]["us-gaap"]["CashAndCashEquivalentsAtCarryingValue"]
    results = extract_all(facts_no_cash, PERIOD)
    # leverage requires cash → should be a MissingDataError
    assert isinstance(results["leverage"], MissingDataError)
    # free_cash_flow doesn't need cash → should still resolve
    assert isinstance(results["free_cash_flow"], RatioResult)


def test_missing_period_raises():
    with pytest.raises(MissingDataError):
        leverage(FACTS, "2021-12-31")
