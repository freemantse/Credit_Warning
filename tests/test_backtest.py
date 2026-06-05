"""Tests for src/backtest.py — point-in-time correctness is the key invariant."""

import pytest
from datetime import date
from src.backtest import _filter_periods_point_in_time, score_issuer_at_date

# Facts where one period was filed BEFORE eval_date and one AFTER
FACTS_WITH_TWO_PERIODS = {
    "facts": {
        "us-gaap": {
            "OperatingIncomeLoss": {
                "units": {
                    "USD": [
                        # Filed before eval_date → should be included
                        {"end": "2022-12-31", "val": 500_000, "filed": "2023-02-15", "form": "10-K"},
                        # Filed after eval_date → must be excluded (no look-ahead)
                        {"end": "2023-12-31", "val": 100_000, "filed": "2024-02-15", "form": "10-K"},
                    ]
                }
            }
        }
    }
}


def test_point_in_time_excludes_future_filings():
    eval_date = date(2023, 6, 1)  # between the two filing dates
    periods = _filter_periods_point_in_time(FACTS_WITH_TWO_PERIODS, eval_date)
    # Only the 2022-12-31 period (filed 2023-02-15) should be available
    assert "2022-12-31" in periods
    assert "2023-12-31" not in periods


def test_point_in_time_includes_same_day_filings():
    eval_date = date(2024, 2, 15)  # exactly the filing date of the second period
    periods = _filter_periods_point_in_time(FACTS_WITH_TWO_PERIODS, eval_date)
    assert "2023-12-31" in periods


def test_no_periods_before_any_filing():
    eval_date = date(2020, 1, 1)  # before all filings
    periods = _filter_periods_point_in_time(FACTS_WITH_TWO_PERIODS, eval_date)
    assert periods == []


def test_score_at_date_uses_only_available_data():
    # With eval_date before 2023-12-31 filing, extraction should use 2022-12-31 data
    # But we don't have enough tags in this fixture to compute ratios,
    # so we just verify it doesn't crash and returns a score
    eval_date = date(2023, 6, 1)
    score, stressed = score_issuer_at_date(FACTS_WITH_TWO_PERIODS, eval_date, threshold=50)
    assert isinstance(score, float)
    assert isinstance(stressed, bool)


def test_score_zero_when_no_periods_available():
    eval_date = date(2010, 1, 1)  # way before any filings
    score, stressed = score_issuer_at_date(FACTS_WITH_TWO_PERIODS, eval_date, threshold=50)
    assert score == 0.0
    assert stressed is False
