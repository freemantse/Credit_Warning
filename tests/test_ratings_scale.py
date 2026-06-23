"""Tests for src/ratings/scale.py — notation ↔ rating_index normalization."""

from src.ratings.scale import (
    normalize_rating,
    grade_for_index,
    RATING_SCALE_ROWS,
    STATUS_RATED,
    STATUS_WITHDRAWN,
    STATUS_NOT_RATED,
    STATUS_DEFAULT,
)


def test_sp_fitch_notation():
    assert normalize_rating("AAA") == (0, STATUS_RATED)
    assert normalize_rating("BBB-") == (9, STATUS_RATED)
    assert normalize_rating("BB+") == (10, STATUS_RATED)
    assert normalize_rating("CCC") == (17, STATUS_RATED)


def test_moody_notation_maps_to_same_index():
    assert normalize_rating("Aaa") == (0, STATUS_RATED)
    assert normalize_rating("Baa3") == (9, STATUS_RATED)   # == BBB-
    assert normalize_rating("Ba1") == (10, STATUS_RATED)   # == BB+
    assert normalize_rating("Caa1") == (16, STATUS_RATED)
    assert normalize_rating("Ca") == (19, STATUS_RATED)


def test_default_tokens():
    for tok in ("D", "SD", "RD", "DD", "DDD"):
        idx, status = normalize_rating(tok)
        assert status == STATUS_DEFAULT
        assert idx == 21   # pinned to D


def test_withdrawn_and_not_rated():
    assert normalize_rating("WD") == (None, STATUS_WITHDRAWN)
    assert normalize_rating("WR") == (None, STATUS_WITHDRAWN)   # Moody's withdrawn
    assert normalize_rating("NR") == (None, STATUS_NOT_RATED)
    assert normalize_rating("") == (None, STATUS_NOT_RATED)
    assert normalize_rating(None) == (None, STATUS_NOT_RATED)


def test_watch_decoration_is_stripped():
    # Outlook/watch markers after the core token are ignored.
    assert normalize_rating("BBB+ *-") == (7, STATUS_RATED)
    assert normalize_rating("A2u") == (5, STATUS_RATED)   # unsolicited marker


def test_unrecognised_is_not_rated_not_a_guess():
    assert normalize_rating("ZZZ") == (None, STATUS_NOT_RATED)


def test_grade_boundary():
    assert grade_for_index(9) == "IG"    # BBB-
    assert grade_for_index(10) == "HY"   # BB+
    assert grade_for_index(21) == "D"
    assert grade_for_index(None) is None


def test_scale_rows_cover_full_axis():
    assert len(RATING_SCALE_ROWS) == 22
    by_index = {r[0]: r for r in RATING_SCALE_ROWS}
    assert by_index[0] == (0, "AAA", "Aaa", "IG")
    assert by_index[9] == (9, "BBB-", "Baa3", "IG")
    assert by_index[10][3] == "HY"
    assert by_index[21] == (21, "D", None, "D")
