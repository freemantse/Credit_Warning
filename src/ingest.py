"""
SEC EDGAR ingestion: ticker → CIK → filings + company facts.

All HTTP responses are cached to disk. Rate-limited to ~8 req/sec.
User-Agent follows SEC EDGAR etiquette requirements.
"""

from __future__ import annotations

import json
import time
import pathlib
import urllib.parse
from typing import Any

import requests

import os
# On Vercel, /tmp is the only writable directory; elsewhere use project cache/
if os.environ.get("VERCEL"):
    CACHE_DIR = pathlib.Path("/tmp/edgar_cache")
else:
    CACHE_DIR = pathlib.Path(__file__).parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

USER_AGENT = "CreditWarning/1.0 freeman.tse@xpef.org"
BASE_URL = "https://data.sec.gov"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt=2000-01-01&enddt=2030-01-01&forms=10-K"

_last_request_time: float = 0.0
_MIN_INTERVAL = 1.0 / 8  # 8 req/sec


def _get(url: str, cache_key: str) -> dict:
    """Fetch URL with disk cache and rate limiting. Returns parsed JSON."""
    cache_path = CACHE_DIR / (cache_key + ".json")
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    _last_request_time = time.monotonic()

    data = resp.json()
    cache_path.write_text(json.dumps(data))
    return data


def get_cik(ticker: str) -> str:
    """
    Resolve a ticker symbol to its SEC CIK (zero-padded to 10 digits).
    Uses the EDGAR company tickers JSON endpoint.
    """
    data = _get(
        "https://www.sec.gov/files/company_tickers.json",
        "company_tickers",
    )
    ticker_upper = ticker.upper()
    for entry in data.values():
        if entry.get("ticker", "").upper() == ticker_upper:
            return str(entry["cik_str"]).zfill(10)
    raise ValueError(f"Ticker {ticker!r} not found in SEC company tickers list")


def get_company_facts(cik: str) -> dict[str, Any]:
    """
    Fetch XBRL company facts for a CIK.
    Returns the full companyfacts JSON (cached to disk).
    """
    cik_padded = cik.zfill(10)
    url = f"{BASE_URL}/api/xbrl/companyfacts/CIK{cik_padded}.json"
    return _get(url, f"{cik_padded}_facts")


def get_submissions(cik: str) -> dict[str, Any]:
    """Fetch filing submission history for a CIK."""
    cik_padded = cik.zfill(10)
    url = f"{BASE_URL}/submissions/CIK{cik_padded}.json"
    return _get(url, f"{cik_padded}_submissions")


def get_filings(cik: str, form_types: list[str] | None = None) -> list[dict]:
    """
    Return a list of filing dicts for a CIK, filtered to `form_types`.
    Each dict has keys: accessionNumber, filingDate, form, primaryDocument.
    """
    if form_types is None:
        form_types = ["10-K", "10-Q", "8-K"]

    submissions = get_submissions(cik)
    recent = submissions.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    filings = []
    for form, date, acc, doc in zip(forms, dates, accessions, docs):
        if form in form_types:
            filings.append(
                {
                    "form": form,
                    "filingDate": date,
                    "accessionNumber": acc,
                    "primaryDocument": doc,
                }
            )
    return filings


def get_filing_text(cik: str, accession: str, document: str) -> str:
    """
    Fetch the raw text of a specific filing document.
    Used to extract MD&A and footnote text for LLM review.
    """
    cik_padded = cik.zfill(10)
    acc_clean = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik_padded)}/{acc_clean}/{document}"
    cache_key = f"{cik_padded}_{acc_clean}_{document.replace('/', '_')}"
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")

    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    _last_request_time = time.monotonic()

    text = resp.text
    cache_path.write_text(text, encoding="utf-8", errors="replace")
    return text


if __name__ == "__main__":
    import sys

    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(f"Resolving CIK for {ticker}...")
    cik = get_cik(ticker)
    print(f"  CIK: {cik}")

    filings = get_filings(cik, ["10-K", "10-Q"])
    counts = {}
    for f in filings:
        counts[f["form"]] = counts.get(f["form"], 0) + 1
    for form, n in sorted(counts.items()):
        print(f"  {form}: {n} filings")

    print("Fetching company facts (XBRL)...")
    facts = get_company_facts(cik)
    n_concepts = len(facts.get("facts", {}).get("us-gaap", {}))
    print(f"  us-gaap concepts available: {n_concepts}")
