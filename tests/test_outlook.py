"""Tests for src/rating.py rating_outlook() — pure arithmetic, no network."""

from src.rating import (
    rating_outlook,
    RatingOutlookResult,
    OUTLOOK_POSITIVE,
    OUTLOOK_STABLE,
    OUTLOOK_NEGATIVE,
    rating_index,
)


def pt(period_end, rating_letter=None, score=None):
    return {
        "period_end": period_end,
        "rating_index": rating_index(rating_letter) if rating_letter else None,
        "score": score,
    }


# ── Trend-driven outlook (no agency data) ────────────────────────────────────

def test_worsening_implied_rating_is_negative():
    # A (idx 5) → BBB (idx 8): rating deteriorated → downgrade pressure.
    series = [pt("2021-12-31", "A"), pt("2022-12-31", "BBB+"), pt("2023-12-31", "BBB")]
    res = rating_outlook(series)
    assert res.outlook == OUTLOOK_NEGATIVE
    assert res.trend_pressure == 1
    assert res.rating_change == rating_index("BBB") - rating_index("A")  # +3
    assert any("deteriorated" in r for r in res.reasons)


def test_improving_implied_rating_is_positive():
    # BBB (8) → A (5): improved → upgrade pressure.
    series = [pt("2021-12-31", "BBB"), pt("2022-12-31", "BBB+"), pt("2023-12-31", "A")]
    res = rating_outlook(series)
    assert res.outlook == OUTLOOK_POSITIVE
    assert res.trend_pressure == -1


def test_flat_rating_and_score_is_stable():
    series = [pt("2021-12-31", "BBB", 30.0), pt("2022-12-31", "BBB", 31.0), pt("2023-12-31", "BBB", 30.0)]
    res = rating_outlook(series)
    assert res.outlook == OUTLOOK_STABLE
    assert res.trend_pressure == 0


def test_score_fallback_catches_sub_notch_deterioration():
    # Implied letter stuck at BBB, but the stress score climbs 30→48 (+18 ≥ 10).
    series = [pt("2021-12-31", "BBB", 30.0), pt("2022-12-31", "BBB", 40.0), pt("2023-12-31", "BBB", 48.0)]
    res = rating_outlook(series)
    assert res.outlook == OUTLOOK_NEGATIVE
    assert res.trend_pressure == 1
    assert any("Stress score rose" in r for r in res.reasons)


def test_score_fallback_improving_is_positive():
    series = [pt("2021-12-31", "BBB", 50.0), pt("2022-12-31", "BBB", 40.0), pt("2023-12-31", "BBB", 32.0)]
    res = rating_outlook(series)
    assert res.outlook == OUTLOOK_POSITIVE
    assert res.trend_pressure == -1


# ── Gap-driven outlook (mean-reversion vs. agency) ───────────────────────────

def test_implied_below_agency_is_negative():
    # Implied BBB (8) sits below agency A (5) → agency expected to catch down.
    series = [pt("2023-12-31", "BBB", 30.0)]
    res = rating_outlook(series, agency_rating_index=rating_index("A"))
    assert res.outlook == OUTLOOK_NEGATIVE
    assert res.gap == rating_index("BBB") - rating_index("A")  # +3
    assert res.gap_pressure == 1
    assert any("below the agency" in r for r in res.reasons)


def test_implied_above_agency_is_positive():
    series = [pt("2023-12-31", "A", 20.0)]
    res = rating_outlook(series, agency_rating_index=rating_index("BBB"))
    assert res.outlook == OUTLOOK_POSITIVE
    assert res.gap_pressure == -1
    assert any("above the agency" in r for r in res.reasons)


def test_trend_and_gap_disagree_nets_to_stable():
    # Trend worsening (A→BBB) but implied still above agency (BBB above BB) → offset.
    series = [pt("2021-12-31", "A"), pt("2023-12-31", "BBB")]
    res = rating_outlook(series, agency_rating_index=rating_index("BB"))
    assert res.trend_pressure == 1
    assert res.gap_pressure == -1
    assert res.outlook == OUTLOOK_STABLE
    assert any("offset" in r for r in res.reasons)


# ── Edge cases ───────────────────────────────────────────────────────────────

def test_insufficient_history_is_stable_with_note():
    res = rating_outlook([pt("2023-12-31", "BBB")])
    assert isinstance(res, RatingOutlookResult)
    assert res.outlook == OUTLOOK_STABLE
    assert any("Insufficient history" in r for r in res.reasons)


def test_no_usable_data_returns_none():
    assert rating_outlook([]) is None
    assert rating_outlook([pt("2023-12-31")]) is None  # no rating, no score, no agency


def test_window_limits_to_recent_periods():
    # 6 periods, window default 4: the early improvement is outside the window, so
    # only the recent worsening (A→BBB across the last 4) drives the signal.
    series = [
        pt("2018-12-31", "BBB"), pt("2019-12-31", "A"),   # outside window
        pt("2020-12-31", "A"), pt("2021-12-31", "A-"),
        pt("2022-12-31", "BBB+"), pt("2023-12-31", "BBB"),
    ]
    res = rating_outlook(series)
    assert res.periods_used == 4
    assert res.outlook == OUTLOOK_NEGATIVE
