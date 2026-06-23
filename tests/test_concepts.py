"""Tests for src/concepts.py — pure, no network."""

import pytest
from src.concepts import resolve_tag, MissingDataError

# Minimal companyfacts fixture
FIXTURE_FACTS = {
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {"end": "2023-12-31", "val": 1_000_000, "filed": "2024-02-15", "form": "10-K"},
                        {"end": "2022-12-31", "val": 900_000,   "filed": "2023-02-10", "form": "10-K"},
                    ]
                }
            },
            "OperatingIncomeLoss": {
                "units": {
                    "USD": [
                        {"end": "2023-12-31", "val": 200_000, "filed": "2024-02-15", "form": "10-K"},
                    ]
                }
            },
        }
    }
}

# Fixture where the FIRST priority tag is missing, second resolves
FIXTURE_FALLBACK = {
    "facts": {
        "us-gaap": {
            # "RevenueFromContractWithCustomerExcludingAssessedTax" absent
            "Revenues": {
                "units": {
                    "USD": [
                        {"end": "2023-12-31", "val": 500_000, "filed": "2024-01-01", "form": "10-K"},
                    ]
                }
            }
        }
    }
}


def test_resolve_primary_tag():
    val, tag = resolve_tag(FIXTURE_FACTS, "revenue", "2023-12-31")
    assert val == 1_000_000
    assert "Revenues" in tag


def test_resolve_fallback_tag():
    # First tag (RevenueFromContract...) missing, falls back to Revenues
    val, tag = resolve_tag(FIXTURE_FALLBACK, "revenue", "2023-12-31")
    assert val == 500_000
    assert "Revenues" in tag


def test_resolve_operating_income():
    val, tag = resolve_tag(FIXTURE_FACTS, "operating_income", "2023-12-31")
    assert val == 200_000


def test_missing_period_raises():
    with pytest.raises(MissingDataError):
        resolve_tag(FIXTURE_FACTS, "revenue", "2021-12-31")


def test_missing_concept_raises():
    with pytest.raises(MissingDataError, match="Unknown concept"):
        resolve_tag(FIXTURE_FACTS, "nonexistent_concept", "2023-12-31")


def test_point_in_time_filtering():
    # filed_before set to before the filing date → should be excluded
    with pytest.raises(MissingDataError):
        resolve_tag(FIXTURE_FACTS, "revenue", "2023-12-31", filed_before="2024-01-01")

    # filed_before set to on or after filing date → should resolve
    val, _ = resolve_tag(FIXTURE_FACTS, "revenue", "2023-12-31", filed_before="2024-02-15")
    assert val == 1_000_000


def test_empty_facts_raises():
    with pytest.raises(MissingDataError):
        resolve_tag({}, "revenue", "2023-12-31")


# ── Newly-added fallback tags resolve when primaries are absent ─────────────

def test_revenue_falls_back_to_bank_tag():
    # Only the bank-specific revenue tag is present — a newly-added fallback.
    facts = {
        "facts": {"us-gaap": {
            "RevenuesNetOfInterestExpense": {
                "units": {"USD": [{"end": "2023-12-31", "val": 250_000, "filed": "2024-02-15", "form": "10-K"}]}
            }
        }}
    }
    val, tag = resolve_tag(facts, "revenue", "2023-12-31")
    assert val == 250_000
    assert tag == "us-gaap/RevenuesNetOfInterestExpense"


def test_short_term_debt_falls_back_to_commercial_paper():
    # Only CommercialPaper present — a newly-added short-term-debt fallback.
    facts = {
        "facts": {"us-gaap": {
            "CommercialPaper": {
                "units": {"USD": [{"end": "2023-12-31", "val": 75_000, "filed": "2024-02-15", "form": "10-K"}]}
            }
        }}
    }
    val, tag = resolve_tag(facts, "short_term_debt", "2023-12-31")
    assert val == 75_000
    assert tag == "us-gaap/CommercialPaper"


def test_revenue_falls_back_to_bank_interest_income():
    # Bank with no Revenues tag — resolves via the newly-added net-interest-income tag.
    facts = {
        "facts": {"us-gaap": {
            "InterestAndDividendIncomeOperating": {
                "units": {"USD": [{"end": "2023-12-31", "val": 410_000, "filed": "2024-02-15", "form": "10-K"}]}
            }
        }}
    }
    val, tag = resolve_tag(facts, "revenue", "2023-12-31")
    assert val == 410_000
    assert tag == "us-gaap/InterestAndDividendIncomeOperating"


def test_debt_maturity_falls_back_to_rolling_variant():
    # Filer using the rolling-window maturity taxonomy — newly-added fallback.
    facts = {
        "facts": {"us-gaap": {
            "LongTermDebtMaturitiesRepaymentsOfPrincipalInRollingYearTwo": {
                "units": {"USD": [{"end": "2023-12-31", "val": 50_000, "filed": "2024-02-15", "form": "10-K"}]}
            }
        }}
    }
    val, tag = resolve_tag(facts, "debt_maturity_y2", "2023-12-31")
    assert val == 50_000
    assert tag == "us-gaap/LongTermDebtMaturitiesRepaymentsOfPrincipalInRollingYearTwo"
