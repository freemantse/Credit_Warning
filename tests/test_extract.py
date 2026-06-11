"""Tests for src/extract.py — pure, no network. Uses fixture companyfacts."""

import pytest
import copy

from src.extract import (
    leverage, interest_coverage, free_cash_flow, fcf_margin, liquidity,
    cash_flow_to_debt, debt_to_assets, current_ratio,
    gross_debt, extract_all, RatioResult, MissingRatio,
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
#   gross_debt = 4_000_000 + 500_000 = 4_500_000
#   cash_flow_to_debt = 1_800_000 / 4_500_000 = 0.4
#   debt_to_assets = 4_500_000 / 20_000_000 = 0.225
#   current_ratio = 6_000_000 / 4_000_000 = 1.5
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
            "Assets":                                         {"units": {"USD": [{"end": PERIOD, "val": 20_000_000,"filed": "2024-02-01", "form": "10-K"}]}},
            "Liabilities":                                    {"units": {"USD": [{"end": PERIOD, "val": 12_000_000,"filed": "2024-02-01", "form": "10-K"}]}},
            "AssetsCurrent":                                  {"units": {"USD": [{"end": PERIOD, "val": 6_000_000, "filed": "2024-02-01", "form": "10-K"}]}},
            "LiabilitiesCurrent":                             {"units": {"USD": [{"end": PERIOD, "val": 4_000_000, "filed": "2024-02-01", "form": "10-K"}]}},
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
    # Remove total_debt concept to force a missing error on leverage only.
    facts_no_debt = copy.deepcopy(FACTS)
    del facts_no_debt["facts"]["us-gaap"]["LongTermDebt"]
    results = extract_all(facts_no_debt, PERIOD)
    # leverage requires total_debt → should be a MissingRatio
    assert isinstance(results["leverage"], MissingRatio)
    # free_cash_flow doesn't need total_debt → should still resolve
    assert isinstance(results["free_cash_flow"], RatioResult)


def test_missing_ratio_pinpoints_missing_input():
    # Removing total_debt should leave the other three leverage inputs resolved and
    # flag exactly total_debt as missing, with the tags that were tried.
    facts_no_debt = copy.deepcopy(FACTS)
    del facts_no_debt["facts"]["us-gaap"]["LongTermDebt"]
    miss = extract_all(facts_no_debt, PERIOD)["leverage"]
    assert isinstance(miss, MissingRatio)

    missing_fields = [m["field"] for m in miss.missing_inputs]
    assert missing_fields == ["total_debt"]
    # The tags that were searched are surfaced for the audit trail.
    assert any("LongTermDebt" in t for t in miss.missing_inputs[0]["tags_tried"])
    # Inputs that DID resolve are still carried so the card stays informative.
    assert miss.inputs["cash"] == 1_000_000
    assert "cash" in miss.source_tags


def test_missing_ratio_guard_failure_sets_reason():
    # Zero EBITDA: every leverage input resolves, but the ratio is undefined.
    # missing_inputs should be empty and reason should explain why.
    facts_zero_ebitda = copy.deepcopy(FACTS)
    g = facts_zero_ebitda["facts"]["us-gaap"]
    g["OperatingIncomeLoss"]["units"]["USD"][0]["val"] = 0
    g["DepreciationDepletionAndAmortization"]["units"]["USD"][0]["val"] = 0
    miss = extract_all(facts_zero_ebitda, PERIOD)["leverage"]
    assert isinstance(miss, MissingRatio)
    assert miss.missing_inputs == []
    assert "EBITDA" in miss.reason


def test_missing_period_raises():
    with pytest.raises(MissingDataError):
        leverage(FACTS, "2021-12-31")


# ── New ratios: cash_flow_to_debt, debt_to_assets, current_ratio ──

def test_gross_debt_includes_short_term():
    value, inputs, tags = gross_debt(FACTS, PERIOD)
    assert value == 4_500_000
    assert inputs["total_debt"] == 4_000_000
    assert inputs["short_term_debt"] == 500_000
    assert "short_term_debt" in tags


def test_gross_debt_treats_missing_short_term_as_zero():
    # No short-term-debt tag at all → gross debt == long-term debt, ST recorded as 0.
    facts = copy.deepcopy(FACTS)
    del facts["facts"]["us-gaap"]["ShortTermBorrowings"]
    value, inputs, tags = gross_debt(facts, PERIOD)
    assert value == 4_000_000
    assert inputs["short_term_debt"] == 0.0
    # No source tag recorded for the absent short-term debt.
    assert "short_term_debt" not in tags


def test_cash_flow_to_debt():
    result = cash_flow_to_debt(FACTS, PERIOD)
    assert isinstance(result, RatioResult)
    assert abs(result.value - 0.4) < 1e-9
    assert result.inputs["operating_cashflow"] == 1_800_000
    assert result.inputs["total_debt"] == 4_000_000


def test_debt_to_assets():
    result = debt_to_assets(FACTS, PERIOD)
    assert abs(result.value - 0.225) < 1e-9
    assert result.inputs["total_assets"] == 20_000_000


def test_current_ratio():
    result = current_ratio(FACTS, PERIOD)
    assert abs(result.value - 1.5) < 1e-9
    assert result.inputs["current_assets"] == 6_000_000
    assert result.inputs["current_liabilities"] == 4_000_000


def test_cash_flow_to_debt_zero_debt_raises():
    facts = copy.deepcopy(FACTS)
    facts["facts"]["us-gaap"]["LongTermDebt"]["units"]["USD"][0]["val"] = 0
    del facts["facts"]["us-gaap"]["ShortTermBorrowings"]
    with pytest.raises(MissingDataError):
        cash_flow_to_debt(facts, PERIOD)


def test_current_ratio_zero_liabilities_raises():
    facts = copy.deepcopy(FACTS)
    facts["facts"]["us-gaap"]["LiabilitiesCurrent"]["units"]["USD"][0]["val"] = 0
    with pytest.raises(MissingDataError):
        current_ratio(facts, PERIOD)


def test_new_ratios_in_extract_all():
    results = extract_all(FACTS, PERIOD)
    for name in ("cash_flow_to_debt", "debt_to_assets", "current_ratio"):
        assert isinstance(results[name], RatioResult), name


def test_current_ratio_missing_input_pinpointed():
    # Drop current assets → current_ratio becomes a MissingRatio naming that input.
    facts = copy.deepcopy(FACTS)
    del facts["facts"]["us-gaap"]["AssetsCurrent"]
    miss = extract_all(facts, PERIOD)["current_ratio"]
    assert isinstance(miss, MissingRatio)
    assert [m["field"] for m in miss.missing_inputs] == ["current_assets"]
