"""Tests for scripts/build_labels.py — the store-driven label orchestrator.

The pure core (compute_labels / flatten_events / period_ends_by_cik) is exercised
with synthetic grouped dicts shaped exactly like the store reads, so no Supabase is
needed. Verifies the wiring into ratings.labels.build_rating_labels: per-period
as-of rating, signed 12m label, and right-edge censoring at the last observed action.
"""

from scripts.build_labels import compute_labels, flatten_events, period_ends_by_cik
from src.ratings.scale import STATUS_RATED, STATUS_DEFAULT


def _agency_grouped():
    # cik → agency → [event rows ascending], mirroring get_agency_ratings_grouped.
    # Issuer 3's late 2023 action pushes data_max_date out so issuers 1 & 2's 12m
    # windows are observed (not censored).
    return {
        "0000000001": {
            "MDY": [
                {"effective_date": "2018-05-01", "rating_index": 5, "rating_status": STATUS_RATED},   # A2
                {"effective_date": "2020-03-15", "rating_index": 7, "rating_status": STATUS_RATED},   # Baa1 (downgrade)
            ],
        },
        "0000000002": {
            "FTC": [
                {"effective_date": "2019-02-01", "rating_index": 14, "rating_status": STATUS_RATED},   # B
                {"effective_date": "2021-03-01", "rating_index": 21, "rating_status": STATUS_DEFAULT},  # D
            ],
        },
        "0000000003": {
            "MDY": [
                {"effective_date": "2018-01-01", "rating_index": 8, "rating_status": STATUS_RATED},
                {"effective_date": "2023-06-01", "rating_index": 8, "rating_status": STATUS_RATED},   # latest action
            ],
        },
    }


def _ratios_grouped():
    # cik → period_end → (ratio payload, ignored here).
    return {
        "0000000001": {"2019-12-31": {}, "2018-12-31": {}},
        "0000000002": {"2020-12-31": {}},
    }


def test_flatten_events_shape():
    flat = flatten_events(_agency_grouped())
    assert {"0000000001", "0000000002"} <= {e["cik"] for e in flat}
    assert all({"cik", "agency", "effective_date", "rating_index", "rating_status"} <= e.keys() for e in flat)


def test_period_ends_sorted_per_cik():
    pe = period_ends_by_cik(_ratios_grouped())
    assert pe["0000000001"] == ["2018-12-31", "2019-12-31"]
    assert pe["0000000002"] == ["2020-12-31"]


def test_compute_labels_downgrade_and_default():
    labels = compute_labels(_agency_grouped(), _ratios_grouped(), horizons=(12,))
    by_key = {(r["cik"], r["period_end"]): r for r in labels}

    # Issuer 1 @ 2019-12-31: A2 now, Baa1 by 2020-12-31 → +1 downgrade, +2 notches.
    r1 = by_key[("0000000001", "2019-12-31")]
    assert r1["rating_index"] == 5 and r1["rating_index_12m"] == 7
    assert r1["label_12m"] == 1 and r1["notch_change_12m"] == 2

    # Issuer 2 @ 2020-12-31: B now (non-distressed), D within 12m → downgrade + distress
    # flagged. The 2023 anchor keeps the 2021-12-31 window observed (not censored).
    r2 = by_key[("0000000002", "2020-12-31")]
    assert r2["label_12m"] == 1 and r2["distress_12m"] is True


def test_compute_labels_censors_past_data_max():
    # Self-contained: the only action is in 2018, so data_max_date is 2018-05-01. A
    # 2019-12-31 period's 12m window ends 2020-12-31 > data_max → censored to None
    # (right-edge censoring), never fabricated as 'stable'.
    grouped = {"X": {"MDY": [{"effective_date": "2018-05-01", "rating_index": 5, "rating_status": STATUS_RATED}]}}
    ratios = {"X": {"2019-12-31": {}}}
    labels = compute_labels(grouped, ratios, horizons=(12,))
    assert len(labels) == 1
    assert labels[0]["rating_index_12m"] is None and labels[0]["label_12m"] is None


def test_no_events_returns_empty():
    assert compute_labels({}, _ratios_grouped()) == []
