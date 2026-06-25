"""Tests for the LONG action-log path in src/ratings/ingest.py.

Covers detect_long_columns, the canonical-series picker (richest series wins,
noise series excluded), and extract_events_long across MDY / FTC / EJR.
"""

from pathlib import Path

from src.ratings.ingest import load_csv, detect_long_columns, extract_events_long
from src.ratings.scale import STATUS_DEFAULT, STATUS_RATED

FIXTURES = Path(__file__).parent / "fixtures"


def _history():
    return load_csv(FIXTURES / "mock_long_ratings_history.csv")


def _events():
    return extract_events_long(_history())


def test_detect_long_columns():
    cols = detect_long_columns(list(_history().columns))
    assert cols["instrument_col"] == "Instrument"
    assert cols["date_col"] == "Date"
    assert cols["rating_col"] == "Issuer Rating"
    assert cols["source_col"] == "Rating Source Description"


def test_only_us_nrsros_kept():
    # Egan-Jones is first-class here; DBRS / R&I / JCR are dropped.
    assert {e["agency"] for e in _events()} == {"MDY", "FTC", "EJR"}


def test_canonical_series_is_richest_not_first():
    # ABC.N MDY: "Senior Unsecured" (3 dated actions) beats "Long-term Issuer Rating"
    # (2 actions ending WR). So the kept series is A2 → Baa1 (a downgrade), NOT the
    # one that withdraws — and the LGD / Commercial-Paper noise is excluded entirely.
    mdy = [e for e in _events() if e["ric"] == "ABC.N" and e["agency"] == "MDY"]
    assert len(mdy) == 2
    first, second = sorted(mdy, key=lambda e: e["effective_date"])
    assert first["rating_index"] == 5 and first["rating_action"] == "new"          # A2
    assert second["rating_index"] == 7 and second["rating_action"] == "downgrade"   # Baa1
    assert second["effective_date"] == "2020-03-15"
    # No event ever came from the withdrawn Long-term Issuer Rating series.
    assert all(e["rating_status"] == STATUS_RATED for e in mdy)


def test_fitch_and_egan_jones_downgrades():
    ftc = [e for e in _events() if e["ric"] == "ABC.N" and e["agency"] == "FTC"]
    ejr = [e for e in _events() if e["ric"] == "ABC.N" and e["agency"] == "EJR"]
    assert [e["rating_action"] for e in sorted(ftc, key=lambda e: e["effective_date"])] == ["new", "downgrade"]
    assert [e["rating_action"] for e in sorted(ejr, key=lambda e: e["effective_date"])] == ["new", "downgrade"]


def test_default_event_pinned_to_D():
    ftc = [e for e in _events() if e["ric"] == "DEF.PK" and e["agency"] == "FTC"]
    dflt = [e for e in ftc if e["rating_status"] == STATUS_DEFAULT]
    assert len(dflt) == 1
    assert dflt[0]["rating_index"] == 21
    assert dflt[0]["rating_action"] == "default"


def test_corporate_family_rating_used_when_only_series():
    # DEF.PK MDY has only a Corporate Family Rating series → it's the canonical one.
    mdy = [e for e in _events() if e["ric"] == "DEF.PK" and e["agency"] == "MDY"]
    assert len(mdy) == 2
    assert sorted(e["rating_action"] for e in mdy) == ["downgrade", "new"]
