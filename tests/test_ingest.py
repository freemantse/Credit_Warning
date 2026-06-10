"""Tests for src/ingest.py — network is mocked."""

import json
import pathlib
import tempfile
import pytest
from unittest.mock import patch, MagicMock

import src.ingest as ingest


TICKERS_FIXTURE = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019,  "ticker": "MSFT", "title": "Microsoft Corp"},
}

SUBMISSIONS_FIXTURE = {
    "filings": {
        "recent": {
            "form":            ["10-K", "10-Q", "8-K", "10-Q"],
            "filingDate":      ["2023-11-03", "2023-08-04", "2023-08-04", "2023-05-05"],
            "reportDate":      ["2023-09-30", "2023-07-01", "2023-08-04", "2023-04-01"],
            "accessionNumber": ["0000320193-23-000106", "0000320193-23-000077",
                                "0000320193-23-000078", "0000320193-23-000064"],
            "primaryDocument": ["aapl-20230930.htm", "aapl-20230701.htm",
                                "0000320193-23-000078.htm", "aapl-20230401.htm"],
        }
    }
}


@pytest.fixture(autouse=True)
def use_temp_cache(tmp_path, monkeypatch):
    """Redirect cache to a temp dir so tests don't pollute or read real cache."""
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path)


# Real shape of a browse-edgar Atom response for a UNIQUE match (one company's
# filing feed, company-info directly under the feed element). Trimmed from a
# live response for CIK=CHK.
ATOM_SINGLE_FIXTURE = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <company-info>
    <cik>0000895126</cik>
    <cik-href>https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&amp;CIK=0000895126</cik-href>
  </company-info>
  <title>EXPAND ENERGY Corp  (0000895126)</title>
</feed>
"""

# Real shape for MULTIPLE matches ("Company Search Feed", one entry per
# company). Trimmed from a live response for company=penney.
ATOM_MULTI_FIXTURE = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry title="ARRAY(0x55c7c0e5bc80)">
    <content type="text/xml">
      <company-info name="ARRAY(0x55c7c0c90988)">
        <cik>0000077182</cik>
        <state>TX</state>
      </company-info>
    </content>
    <id>urn:tag:www.sec.gov:cik=0000077182</id>
  </entry>
  <entry title="ARRAY(0x55c7c0e1b578)">
    <content type="text/xml">
      <company-info name="ARRAY(0x55c7c0e66648)">
        <cik>0000077193</cik>
        <state>TX</state>
      </company-info>
    </content>
    <id>urn:tag:www.sec.gov:cik=0000077193</id>
  </entry>
  <title>Company Search Feed</title>
</feed>
"""

# What browse-edgar actually returns for an unknown ticker: an HTML page,
# which is not well-formed XML.
HTML_NO_MATCH_FIXTURE = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0 Transitional//EN">
<html lang="ENG">
<head><title>Company Information: </title></head>
<body style="margin: 0">
<img src="/images/bannerSeal.gif" width="95" height="92" border="0">
</body></html>
"""


def _mock_response(data: dict | None = None, text: str = "") -> MagicMock:
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = data
    mock.text = text
    return mock


def test_get_cik_found():
    with patch("requests.get", return_value=_mock_response(TICKERS_FIXTURE)):
        cik = ingest.get_cik("AAPL")
    assert cik == "0000320193"


def test_get_cik_not_found():
    # First request serves the tickers list (no match), the fallback request
    # serves the "no match" HTML page — both lookups fail.
    responses = [_mock_response(TICKERS_FIXTURE), _mock_response(text=HTML_NO_MATCH_FIXTURE)]
    with patch("requests.get", side_effect=responses):
        with pytest.raises(ValueError, match="not found"):
            ingest.get_cik("XXXX")


def test_get_cik_delisted_fallback(tmp_path, monkeypatch):
    """A ticker missing from company_tickers.json resolves via browse-edgar."""
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path)
    responses = [_mock_response(TICKERS_FIXTURE), _mock_response(text=ATOM_SINGLE_FIXTURE)]
    with patch("requests.get", side_effect=responses):
        cik = ingest.get_cik("CHK")
    assert cik == "0000895126"
    # The successful fallback response is cached for offline reruns.
    assert (tmp_path / "ticker_fallback_CHK").exists()


def test_get_cik_failed_fallback_not_cached(tmp_path, monkeypatch):
    """A failed fallback must not poison the cache."""
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path)
    responses = [_mock_response(TICKERS_FIXTURE), _mock_response(text=HTML_NO_MATCH_FIXTURE)]
    with patch("requests.get", side_effect=responses):
        with pytest.raises(ValueError):
            ingest.get_cik("XXXX")
    assert not (tmp_path / "ticker_fallback_XXXX").exists()


def test_parse_ciks_single_match():
    assert ingest._parse_ciks_from_atom(ATOM_SINGLE_FIXTURE) == ["0000895126"]


def test_parse_ciks_multi_match():
    assert ingest._parse_ciks_from_atom(ATOM_MULTI_FIXTURE) == ["0000077182", "0000077193"]


def test_parse_ciks_garbage_html():
    assert ingest._parse_ciks_from_atom(HTML_NO_MATCH_FIXTURE) == []
    assert ingest._parse_ciks_from_atom("") == []


def test_find_cik_by_name_single():
    with patch("requests.get", return_value=_mock_response(text=ATOM_SINGLE_FIXTURE)):
        assert ingest.find_cik_by_name("Expand Energy") == "0000895126"


def test_find_cik_by_name_no_match():
    with patch("requests.get", return_value=_mock_response(text=HTML_NO_MATCH_FIXTURE)):
        with pytest.raises(ValueError, match="No EDGAR company matches"):
            ingest.find_cik_by_name("Nonexistent Corp")


def test_find_cik_by_name_multi_match_lists_candidates():
    with patch("requests.get", return_value=_mock_response(text=ATOM_MULTI_FIXTURE)), \
         patch("src.ingest.get_company_info",
               side_effect=lambda cik: {"name": f"Company {cik[-2:]}"}):
        with pytest.raises(ValueError) as exc_info:
            ingest.find_cik_by_name("Penney")
    msg = str(exc_info.value)
    assert "0000077182" in msg and "0000077193" in msg


def test_cache_written(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path)
    with patch("requests.get", return_value=_mock_response(TICKERS_FIXTURE)):
        ingest.get_cik("AAPL")
    cache_file = tmp_path / "company_tickers.json"
    assert cache_file.exists()
    assert json.loads(cache_file.read_text()) == TICKERS_FIXTURE


def test_cache_read_on_second_call(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path)
    with patch("requests.get", return_value=_mock_response(TICKERS_FIXTURE)) as mock_get:
        ingest.get_cik("AAPL")
        ingest.get_cik("MSFT")  # second call should hit cache for tickers
    # requests.get should only be called once (for the tickers file)
    assert mock_get.call_count == 1


def test_get_filings_filters_form_types():
    with patch("src.ingest.get_submissions", return_value=SUBMISSIONS_FIXTURE):
        filings = ingest.get_filings("0000320193", ["10-K", "10-Q"])
    forms = [f["form"] for f in filings]
    assert "8-K" not in forms
    assert forms.count("10-K") == 1
    assert forms.count("10-Q") == 2


def test_get_filings_returns_all_by_default():
    with patch("src.ingest.get_submissions", return_value=SUBMISSIONS_FIXTURE):
        filings = ingest.get_filings("0000320193")
    assert len(filings) == 4


def test_get_filings_includes_report_date():
    with patch("src.ingest.get_submissions", return_value=SUBMISSIONS_FIXTURE):
        filings = ingest.get_filings("0000320193", ["10-K"])
    assert filings[0]["reportDate"] == "2023-09-30"


def test_get_filings_tolerates_missing_report_date():
    # Submissions data without a reportDate array must not drop any filings
    # (zip stops at the shortest parallel array unless we pad).
    fixture = {
        "filings": {
            "recent": {
                k: v
                for k, v in SUBMISSIONS_FIXTURE["filings"]["recent"].items()
                if k != "reportDate"
            }
        }
    }
    with patch("src.ingest.get_submissions", return_value=fixture):
        filings = ingest.get_filings("0000320193")
    assert len(filings) == 4
    assert all(f["reportDate"] == "" for f in filings)


# --- find_filing_for_period ---

def _filing(filed: str, report: str = "") -> dict:
    return {
        "form": "10-K",
        "filingDate": filed,
        "reportDate": report,
        "accessionNumber": f"acc-{filed}",
        "primaryDocument": f"doc-{filed}.htm",
    }


def test_find_filing_exact_report_date_match():
    filings = [
        _filing("2024-11-01", "2024-09-28"),
        _filing("2023-11-03", "2023-09-30"),
    ]
    match = ingest.find_filing_for_period(filings, "2023-09-30")
    assert match is not None and match["filingDate"] == "2023-11-03"


def test_find_filing_off_calendar_fiscal_year():
    # FY ending 2024-02-03 is filed ~2024-03; the NEXT fiscal year's 10-K is
    # also filed in calendar 2025. With blank reportDates, the filingDate-window
    # fallback must pick the filing submitted shortly AFTER the period end —
    # the old `"2024" in filingDate` heuristic would grab whichever 2024 filing
    # came first in the list, even one covering a different fiscal year.
    filings = [
        _filing("2025-03-20"),  # covers FY ending 2025-02-01
        _filing("2024-03-22"),  # covers FY ending 2024-02-03  ← correct match
        _filing("2023-03-17"),  # covers FY ending 2023-01-28
    ]
    match = ingest.find_filing_for_period(filings, "2024-02-03")
    assert match is not None and match["filingDate"] == "2024-03-22"


def test_find_filing_prefers_report_date_over_window():
    # An amended/late filing may sit closer in time; the exact reportDate match
    # must still win.
    filings = [
        _filing("2024-03-01", ""),            # nearer by filingDate, unknown period
        _filing("2024-06-15", "2023-12-31"),  # exact period match, filed late
    ]
    match = ingest.find_filing_for_period(filings, "2023-12-31")
    assert match is not None and match["filingDate"] == "2024-06-15"


def test_find_filing_no_match_returns_none():
    filings = [_filing("2019-03-01", "2018-12-31")]
    assert ingest.find_filing_for_period(filings, "2023-12-31") is None
