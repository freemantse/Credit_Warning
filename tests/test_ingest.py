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


def _mock_response(data: dict) -> MagicMock:
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = data
    return mock


def test_get_cik_found():
    with patch("requests.get", return_value=_mock_response(TICKERS_FIXTURE)):
        cik = ingest.get_cik("AAPL")
    assert cik == "0000320193"


def test_get_cik_not_found():
    with patch("requests.get", return_value=_mock_response(TICKERS_FIXTURE)):
        with pytest.raises(ValueError, match="not found"):
            ingest.get_cik("XXXX")


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
