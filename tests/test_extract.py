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
#   gross_debt = 500_000 (ShortTermBorrowings, component A) + 4_000_000
#                (LongTermDebt, component C) = 4_500_000   (component waterfall)
#   net_debt = gross_debt 4_500_000 - cash 1_000_000 = 3_500_000
#   EBITDA = 1_500_000 + 500_000 = 2_000_000
#   leverage = 3_500_000 / 2_000_000 = 1.75
#   interest_coverage = 2_000_000 / 400_000 = 5.0
#   FCF = 1_800_000 - 300_000 = 1_500_000
#   FCF margin = 1_500_000 / 10_000_000 = 0.15
#   liquidity = 1_000_000 / 500_000 = 2.0
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
    # net_debt uses the component-waterfall gross_debt (4.5M incl. short-term), not
    # LongTermDebt alone: (4.5M - 1M) / 2M = 1.75.
    assert abs(result.value - 1.75) < 1e-9
    assert result.inputs["total_debt"] == 4_500_000        # waterfall total (A+B+C)
    assert result.inputs["long_term_noncurrent"] == 4_000_000
    assert result.inputs["short_term_components"] == 500_000
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
    # gross_debt exposes per-component tags; the long-term tranche resolved via
    # us-gaap/LongTermDebt is recorded under the "long_term_noncurrent" component.
    assert "long_term_noncurrent" in result.source_tags
    assert "LongTermDebt" in result.source_tags["long_term_noncurrent"]


def test_extract_all_returns_all_ratios():
    results = extract_all(FACTS, PERIOD)
    assert "leverage" in results
    assert "interest_coverage" in results
    assert "free_cash_flow" in results
    assert isinstance(results["leverage"], RatioResult)


def test_extract_all_records_missing_not_crashes():
    # Remove EVERY debt component to force gross_debt (and thus leverage) to fail.
    # The component waterfall derives debt from short-term borrowings too, so deleting
    # only LongTermDebt would still leave ShortTermBorrowings (component A) and
    # leverage would compute — the missing case requires no debt component at all.
    facts_no_debt = copy.deepcopy(FACTS)
    del facts_no_debt["facts"]["us-gaap"]["LongTermDebt"]
    del facts_no_debt["facts"]["us-gaap"]["ShortTermBorrowings"]
    results = extract_all(facts_no_debt, PERIOD)
    # leverage requires total_debt → should be a MissingRatio
    assert isinstance(results["leverage"], MissingRatio)
    # free_cash_flow doesn't need total_debt → should still resolve
    assert isinstance(results["free_cash_flow"], RatioResult)


def test_missing_ratio_pinpoints_missing_input():
    # Removing all debt components leaves the other three leverage inputs resolved
    # and flags exactly total_debt as missing, with the tags that were tried.
    facts_no_debt = copy.deepcopy(FACTS)
    del facts_no_debt["facts"]["us-gaap"]["LongTermDebt"]
    del facts_no_debt["facts"]["us-gaap"]["ShortTermBorrowings"]
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
    # Component waterfall: A (short-term, summed) + B (current LTD) + C (non-current).
    value, inputs, tags = gross_debt(FACTS, PERIOD)
    assert value == 4_500_000                          # A 500K + C 4M
    assert inputs["total_debt"] == 4_500_000           # waterfall TOTAL (not LTD alone)
    assert inputs["short_term_components"] == 500_000  # component A (ShortTermBorrowings)
    assert inputs["long_term_noncurrent"] == 4_000_000 # component C
    assert inputs["current_portion_ltd"] == 0.0        # component B absent
    assert "short_term_components" in tags
    assert "LongTermDebt" in tags["long_term_noncurrent"]


def test_gross_debt_treats_missing_short_term_as_zero():
    # No short-term-debt tag → component A is 0, gross debt == non-current LTD.
    facts = copy.deepcopy(FACTS)
    del facts["facts"]["us-gaap"]["ShortTermBorrowings"]
    value, inputs, tags = gross_debt(facts, PERIOD)
    assert value == 4_000_000
    assert inputs["short_term_components"] == 0.0
    # No source tag recorded for the absent short-term components.
    assert "short_term_components" not in tags


def test_cash_flow_to_debt():
    result = cash_flow_to_debt(FACTS, PERIOD)
    assert isinstance(result, RatioResult)
    assert abs(result.value - 0.4) < 1e-9               # 1.8M / gross_debt 4.5M
    assert result.inputs["operating_cashflow"] == 1_800_000
    assert result.inputs["total_debt"] == 4_500_000     # waterfall total


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
