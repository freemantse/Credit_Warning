"""Tests for src/ratings/labels.py — absorbing-state as-of + lookahead-free labels."""

from src.ratings.labels import add_months, rating_asof, build_rating_labels
from src.ratings.scale import STATUS_RATED, STATUS_WITHDRAWN, STATUS_DEFAULT, STATUS_NOT_RATED


# ── add_months ───────────────────────────────────────────────────────────────

def test_add_months_clamps_day():
    assert add_months("2020-01-31", 1) == "2020-02-29"   # leap year
    assert add_months("2021-01-31", 1) == "2021-02-28"
    assert add_months("2020-12-15", 3) == "2021-03-15"
    assert add_months("2019-12-31", 12) == "2020-12-31"


# ── rating_asof (absorbing-state forward-fill) ───────────────────────────────

def _events_one_key():
    # One (cik, agency) series: A2(5) → Baa1(7) downgrade → WR (withdrawn).
    return sorted([
        {"effective_date": "2018-05-01", "rating_index": 5, "rating_status": STATUS_RATED},
        {"effective_date": "2020-03-15", "rating_index": 7, "rating_status": STATUS_RATED},
        {"effective_date": "2021-02-01", "rating_index": None, "rating_status": STATUS_WITHDRAWN},
    ], key=lambda e: e["effective_date"])


def test_asof_before_first_is_not_rated():
    assert rating_asof(_events_one_key(), "2017-01-01") == (None, STATUS_NOT_RATED)


def test_asof_forward_fills_last_event():
    assert rating_asof(_events_one_key(), "2019-12-31") == (5, STATUS_RATED)   # still A2
    assert rating_asof(_events_one_key(), "2020-12-31") == (7, STATUS_RATED)   # after downgrade


def test_asof_does_not_carry_through_withdrawal():
    # After the WR, the prior Baa1 is NOT carried forward — it's unrated.
    assert rating_asof(_events_one_key(), "2021-06-30") == (None, STATUS_WITHDRAWN)


# ── build_rating_labels ──────────────────────────────────────────────────────

def _crosswalked_events(cik, agency, triples):
    return [
        {"cik": cik, "agency": agency, "effective_date": d, "rating_index": i, "rating_status": s}
        for d, i, s in triples
    ]


def test_downgrade_label_12m():
    events = _crosswalked_events("0000000001", "MDY", [
        ("2018-05-01", 5, STATUS_RATED),     # A2
        ("2020-03-15", 7, STATUS_RATED),     # Baa1 (downgrade)
    ])
    rows = build_rating_labels(
        events, {"0000000001": ["2019-12-31"]}, data_max_date="2022-12-31", horizons=(12,)
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["rating_index"] == 5            # A2 as of 2019-12-31
    assert r["rating_index_12m"] == 7        # Baa1 by 2020-12-31
    assert r["notch_change_12m"] == 2
    assert r["label_12m"] == 1               # +1 = downgrade
    assert r["distress_12m"] is False        # A2→Baa1 stays well above the CCC+ tail


def test_censored_horizon_is_none_not_stable():
    events = _crosswalked_events("0000000001", "MDY", [("2018-05-01", 5, STATUS_RATED)])
    rows = build_rating_labels(
        events, {"0000000001": ["2019-12-31"]}, data_max_date="2020-06-30", horizons=(12,)
    )
    r = rows[0]
    # 2019-12-31 + 12m = 2020-12-31 > data_max_date → outcome unknown → None.
    assert r["rating_index_12m"] is None
    assert r["label_12m"] is None
    assert r["notch_change_12m"] is None


def test_distress_transition_into_ccc_flagged():
    # B (14, non-distressed) → CCC (17) within 12m: a transition into the CCC+ tail
    # with NO actual default → distress_12m True.
    events = _crosswalked_events("0000000002", "FTC", [
        ("2019-02-01", 14, STATUS_RATED),    # B
        ("2021-06-01", 17, STATUS_RATED),    # CCC (falls into the distress tail)
    ])
    rows = build_rating_labels(
        events, {"0000000002": ["2020-12-31"]}, data_max_date="2022-12-31", horizons=(12,)
    )
    r = rows[0]
    assert r["rating_index"] == 14           # B as of 2020-12-31 (not yet distressed)
    assert r["distress_12m"] is True         # crossed into CCC within (2020-12-31, 2021-12-31]


def test_default_from_nondistress_flagged():
    # BB (11) → D (default) within 12m: a default subsumes distress (D = 21 ≥ CCC+).
    events = _crosswalked_events("0000000004", "MDY", [
        ("2019-02-01", 11, STATUS_RATED),    # BB
        ("2021-03-01", 21, STATUS_DEFAULT),  # D (default)
    ])
    rows = build_rating_labels(
        events, {"0000000004": ["2020-12-31"]}, data_max_date="2022-12-31", horizons=(12,)
    )
    r = rows[0]
    assert r["rating_index"] == 11           # BB as of 2020-12-31
    assert r["distress_12m"] is True         # default event within the 12m window


def test_already_distressed_is_not_a_transition():
    # Already at CCC (17 ≥ CCC+) as of period_end, then defaults: NOT a transition into
    # distress (it was already there), so distress_12m is False — keeps the positive
    # class to genuine new entrants, not issuers parked in the tail.
    events = _crosswalked_events("0000000005", "FTC", [
        ("2020-05-01", 17, STATUS_RATED),    # CCC (already distressed)
        ("2021-03-01", 21, STATUS_DEFAULT),  # D (default)
    ])
    rows = build_rating_labels(
        events, {"0000000005": ["2020-12-31"]}, data_max_date="2022-12-31", horizons=(12,)
    )
    r = rows[0]
    assert r["rating_index"] == 17           # CCC as of 2020-12-31
    assert r["label_12m"] == 1               # still a downgrade (CCC → D)
    assert r["distress_12m"] is False        # but not a NEW transition into distress


def test_upgrade_label():
    events = _crosswalked_events("0000000003", "SPI", [
        ("2018-01-01", 11, STATUS_RATED),    # BB
        ("2019-06-01", 8, STATUS_RATED),     # BBB (upgrade)
    ])
    rows = build_rating_labels(
        events, {"0000000003": ["2018-12-31"]}, data_max_date="2021-12-31", horizons=(12,)
    )
    r = rows[0]
    assert r["label_12m"] == -1              # -1 = upgrade
    assert r["notch_change_12m"] == -3
