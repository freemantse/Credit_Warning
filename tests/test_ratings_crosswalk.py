"""Tests for src/ratings/crosswalk.py — PermID/RIC → CIK (offline, stub resolver)."""

from pathlib import Path

from src.ratings.ingest import load_csv, extract_events
from src.ratings.crosswalk import build_crosswalk, attach_cik, write_unmatched

FIXTURES = Path(__file__).parent / "fixtures"


def stub_resolver(ticker, name):
    """Offline resolver: only XYZ / OLDX resolve; Ghost Industries does not."""
    return {"XYZ": "0000000001", "OLDX": "0000000002"}.get((ticker or "").upper())


def _xref():
    return load_csv(FIXTURES / "mock_universe_xref.csv")


def test_resolved_and_unmatched_split():
    resolved, unmatched = build_crosswalk(_xref(), resolver=stub_resolver)
    assert set(resolved) == {"XYZ.O", "OLD.PK"}
    assert resolved["XYZ.O"]["cik"] == "0000000001"
    assert resolved["XYZ.O"]["permid"] == "4295900001"
    # The ghost name never resolves → surfaced for manual review, not dropped silently.
    assert len(unmatched) == 1
    assert unmatched[0]["ric"] == "NOMATCH.X"


def test_attach_cik_keeps_only_resolved_events():
    resolved, _ = build_crosswalk(_xref(), resolver=stub_resolver)
    events = extract_events(load_csv(FIXTURES / "mock_us_ratings_history.csv"))
    joined = attach_cik(events, resolved)
    # Every kept event has a cik + source_ric and no bare 'ric'.
    assert joined
    assert all("cik" in e and "source_ric" in e and "ric" not in e for e in joined)
    assert {e["cik"] for e in joined} == {"0000000001", "0000000002"}


def test_write_unmatched(tmp_path):
    _, unmatched = build_crosswalk(_xref(), resolver=stub_resolver)
    out = tmp_path / "unmatched.csv"
    n = write_unmatched(unmatched, out)
    assert n == 1
    assert out.exists()
    assert "NOMATCH.X" in out.read_text()
