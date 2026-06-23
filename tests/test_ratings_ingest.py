"""Tests for src/ratings/ingest.py — column detection + change-event extraction."""

from pathlib import Path

from src.ratings.ingest import load_csv, detect_columns, extract_events
from src.ratings.scale import STATUS_DEFAULT, STATUS_WITHDRAWN, STATUS_RATED

FIXTURES = Path(__file__).parent / "fixtures"


def _history():
    return load_csv(FIXTURES / "mock_us_ratings_history.csv")


def test_detect_columns_by_pattern():
    cols = detect_columns(list(_history().columns))
    assert cols["date_col"] == "Date"
    assert cols["instrument_col"] == "Instrument"
    assert cols["name_col"] == "CommonName"
    assert set(cols["agencies"]) == {"MDY", "FTC"}
    assert "date" not in cols["agencies"]["MDY"]["rating_col"].lower()
    assert "date" in cols["agencies"]["MDY"]["date_col"].lower()


def _events():
    return extract_events(_history())


def test_events_dedupe_to_changes_only():
    # XYZ.O MDY appears in three rows but only changes once (A2 → Baa1): 2 events,
    # not 3 — the repeated 2021 snapshot of Baa1 (same effective date) is collapsed.
    xyz_mdy = [e for e in _events() if e["ric"] == "XYZ.O" and e["agency"] == "MDY"]
    assert len(xyz_mdy) == 2
    first, second = sorted(xyz_mdy, key=lambda e: e["effective_date"])
    assert first["rating_index"] == 5 and first["rating_action"] == "new"          # A2
    assert second["rating_index"] == 7 and second["rating_action"] == "downgrade"   # Baa1
    assert second["effective_date"] == "2020-03-15"


def test_fitch_downgrade_detected():
    xyz_ftc = [e for e in _events() if e["ric"] == "XYZ.O" and e["agency"] == "FTC"]
    assert len(xyz_ftc) == 2
    assert xyz_ftc[-1]["rating_action"] == "downgrade"   # A → BBB+


def test_withdrawal_event_has_no_index():
    old_mdy = [e for e in _events() if e["ric"] == "OLD.PK" and e["agency"] == "MDY"]
    wd = [e for e in old_mdy if e["rating_status"] == STATUS_WITHDRAWN]
    assert len(wd) == 1
    assert wd[0]["rating_index"] is None
    assert wd[0]["rating_action"] == "withdrawn"


def test_default_event_pinned_to_D():
    old_ftc = [e for e in _events() if e["ric"] == "OLD.PK" and e["agency"] == "FTC"]
    dflt = [e for e in old_ftc if e["rating_status"] == STATUS_DEFAULT]
    assert len(dflt) == 1
    assert dflt[0]["rating_index"] == 21
    assert dflt[0]["rating_action"] == "default"
