"""
SEC EDGAR ingestion: ticker → CIK → filings + company facts.

How EDGAR data is structured:
  Every public US company has a CIK (Central Index Key) — a 10-digit number
  that EDGAR uses internally. To fetch data you need:
    1. The CIK for your ticker (from company_tickers.json)
    2. The XBRL companyfacts JSON  →  all tagged financial values ever reported
    3. The submissions JSON        →  metadata for all filings (dates, form types)
    4. Optionally: the raw filing document text (for LLM qualitative review)

Caching strategy:
  All HTTP responses are written to disk as JSON (or plain text for filing docs).
  On the next call the cache file is returned immediately — no HTTP request.
  EDGAR data for past periods is immutable, so the cache never goes stale.
  This means the first track() call for a ticker takes 10–30 s; subsequent
  calls for the same ticker are near-instant.

Rate limiting:
  SEC EDGAR asks for a maximum of 10 requests/second. We limit to 8 to be safe.
  A global _last_request_time float tracks when the last request was made.

Key functions:
  get_cik(ticker)           → "0000320193"  (zero-padded 10-digit CIK)
  get_company_facts(cik)    → full XBRL companyfacts JSON dict
  get_filings(cik, forms)   → list of filing metadata dicts
  get_filing_text(cik, ...) → raw filing document text (for LLM review)
"""

from __future__ import annotations

import json
import time
import pathlib
import urllib.parse
from typing import Any

import requests
import os


# ── Cache directory ──────────────────────────────────────────────────────────
# On Vercel (production serverless), the only writable location is /tmp.
# The VERCEL env var is automatically set by Vercel at runtime.
# Locally we keep the cache inside the project directory for easy inspection.
if os.environ.get("VERCEL"):
    CACHE_DIR = pathlib.Path("/tmp/edgar_cache")
else:
    CACHE_DIR = pathlib.Path(__file__).parent.parent / "cache"

CACHE_DIR.mkdir(exist_ok=True)  # create the directory if it doesn't exist yet


# ── EDGAR API constants ──────────────────────────────────────────────────────
# SEC EDGAR requires a User-Agent header that identifies your application and
# includes contact information. Requests without a proper User-Agent may be
# rate-limited or blocked. See: https://www.sec.gov/developer
USER_AGENT = "CreditWarning/1.0 freeman.tse@xpef.org"

BASE_URL = "https://data.sec.gov"  # EDGAR structured data API

# This URL template is kept for reference but not currently used — ticker search
# is done through company_tickers.json instead, which is more reliable.
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt=2000-01-01&enddt=2030-01-01&forms=10-K"


# ── Rate limiting ────────────────────────────────────────────────────────────
# We track the timestamp of the last outbound HTTP request and sleep if the
# next call would be too soon. time.monotonic() is used (not time.time())
# because it's immune to system clock adjustments.
_last_request_time: float = 0.0
_MIN_INTERVAL = 1.0 / 8  # 0.125 s between requests = max 8 requests/second


# ── Core HTTP helper ─────────────────────────────────────────────────────────

def _get(url: str, cache_key: str) -> dict:
    """
    Fetch a URL with disk caching and rate limiting. Returns parsed JSON.

    Flow:
      1. If a cached file exists for cache_key, read and return it immediately.
      2. Otherwise, check how long since the last request and sleep if needed.
      3. Make the HTTP GET request with the required User-Agent header.
      4. Parse the response as JSON, write it to disk, and return the dict.

    The cache_key is used as the filename: <CACHE_DIR>/<cache_key>.json
    Using a descriptive key (e.g. "0000320193_facts") makes the cache
    directory easy to inspect manually.
    """
    cache_path = CACHE_DIR / (cache_key + ".json")

    # Cache hit — return immediately without any HTTP request or rate-limit check.
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    # ── Rate limit enforcement ────────────────────────────────────────────────
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL:
        # Sleep only the remaining fraction of the interval, not a full fixed delay.
        time.sleep(_MIN_INTERVAL - elapsed)

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()  # raise an HTTPError for 4xx/5xx responses

    # Record the time immediately after the response arrives.
    _last_request_time = time.monotonic()

    data = resp.json()

    # Write to cache so the next call returns instantly.
    cache_path.write_text(json.dumps(data))

    return data


# ── Public ingestion functions ───────────────────────────────────────────────

def get_cik(ticker: str) -> str:
    """
    Resolve a stock ticker to its SEC Central Index Key (CIK).

    EDGAR identifies companies by CIK, not by ticker symbol. The company_tickers
    endpoint returns a JSON object like:
      {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
        ...
      }

    We search through all entries for a ticker match (case-insensitive).
    The CIK is zero-padded to 10 digits because EDGAR API URLs require that format.

    Raises:
        ValueError — if the ticker is not found in EDGAR's list.
    """
    data = _get(
        "https://www.sec.gov/files/company_tickers.json",
        "company_tickers",  # cached as company_tickers.json
    )
    ticker_upper = ticker.upper()

    for entry in data.values():
        if entry.get("ticker", "").upper() == ticker_upper:
            # cik_str is stored as an integer (e.g. 320193).
            # zfill(10) pads it to "0000320193" as required by EDGAR API URLs.
            return str(entry["cik_str"]).zfill(10)

    raise ValueError(f"Ticker {ticker!r} not found in SEC company tickers list")


def resolve_identifier(identifier: str) -> str:
    """
    Resolve either a ticker or a CIK to a zero-padded 10-digit CIK.

    The CIK is SEC EDGAR's canonical, permanent identifier for a company. Unlike
    tickers and company names — which change over rebrands, ticker swaps, and
    reincorporations — a CIK is assigned once and never changes. Callers should
    prefer this over get_cik() so that historical companies whose ticker has
    since changed (and therefore no longer appears in company_tickers.json) can
    still be looked up directly by CIK.

    Accepts:
      - A ticker symbol, e.g. "AAPL"  → resolved via get_cik().
      - A bare CIK, e.g. "320193" or "0000320193", with or without a "CIK"
        prefix → zero-padded and returned directly (no HTTP request).

    A value is treated as a CIK if, once any leading "CIK" prefix and leading
    zeros are stripped, it is all digits and at most 10 digits long.

    Returns:
        Zero-padded 10-digit CIK string, e.g. "0000320193".
    """
    raw = identifier.strip()

    # Allow an optional "CIK" / "cik" prefix, e.g. "CIK0000320193".
    candidate = raw
    if candidate[:3].upper() == "CIK":
        candidate = candidate[3:]

    # If what's left is all digits and fits in 10 digits, it's a CIK already.
    if candidate.isdigit() and len(candidate) <= 10:
        return candidate.zfill(10)

    # Otherwise treat it as a ticker and resolve through the ticker list.
    return get_cik(raw)


def get_company_info(cik: str) -> dict[str, Any]:
    """
    Return identity metadata for a company from its submissions JSON.

    Ticker and name are mutable display attributes, not identity — this helper
    pulls the *current* values plus the history needed to recognise a company
    across rebrands and ticker changes. Key your storage on the CIK and treat
    everything here as a refreshable snapshot.

    Returns a dict with:
        cik:         Zero-padded 10-digit CIK (the canonical key).
        name:        Current legal/display name.
        tickers:     List of current ticker symbols (a company can have several).
        exchanges:   List of exchanges the tickers trade on.
        formerNames: List of {name, from, to} dicts for prior names, if any.
                     Useful for matching historical filings and "f/k/a" display.
    """
    cik_padded = cik.zfill(10)
    submissions = get_submissions(cik_padded)
    return {
        "cik": cik_padded,
        "name": submissions.get("name", ""),
        "tickers": submissions.get("tickers", []),
        "exchanges": submissions.get("exchanges", []),
        "formerNames": submissions.get("formerNames", []),
    }


def get_company_facts(cik: str) -> dict[str, Any]:
    """
    Fetch the full XBRL companyfacts JSON for a company.

    This is the primary data source for all ratio extraction. It contains every
    financial value the company has ever tagged in any SEC filing, organised by:
      facts → taxonomy (us-gaap) → concept (Revenues) → units (USD) → entries

    Each entry has: val, end, start, form, filed, accn (accession number).

    The JSON can be several MB for large companies with long filing histories.
    It is cached to disk so it's only fetched once per ticker.
    """
    cik_padded = cik.zfill(10)
    # EDGAR companyfacts URL uses zero-padded CIK in the filename.
    url = f"{BASE_URL}/api/xbrl/companyfacts/CIK{cik_padded}.json"
    return _get(url, f"{cik_padded}_facts")


def get_submissions(cik: str) -> dict[str, Any]:
    """
    Fetch the filing submission history for a company.

    Returns a JSON object with a "filings.recent" key containing parallel arrays
    (not a list of objects) for form type, date, accession number, etc.
    The parallel array structure is why get_filings() uses zip() to combine them.

    Used internally by get_filings() — not usually called directly.
    """
    cik_padded = cik.zfill(10)
    url = f"{BASE_URL}/submissions/CIK{cik_padded}.json"
    return _get(url, f"{cik_padded}_submissions")


def get_filings(cik: str, form_types: list[str] | None = None) -> list[dict]:
    """
    Return a filtered list of filing metadata dicts for a company.

    EDGAR's submissions JSON uses parallel arrays for efficiency:
      filings.recent.form        = ["10-K", "10-Q", "10-K", ...]
      filings.recent.filingDate  = ["2023-11-02", "2023-08-04", ...]
      filings.recent.accessionNumber = ["0000320193-23-000106", ...]
      filings.recent.primaryDocument  = ["aapl-20230930.htm", ...]

    zip() stitches these arrays together into (form, date, accession, doc) tuples.
    We convert each matching tuple into a dict for easier downstream use.

    Args:
        form_types: list of form type strings to include, e.g. ["10-K"].
                    Defaults to ["10-K", "10-Q", "8-K"] if not specified.

    Returns:
        List of dicts with keys: form, filingDate, accessionNumber, primaryDocument.
        Ordered newest-first (as EDGAR returns them).
    """
    if form_types is None:
        form_types = ["10-K", "10-Q", "8-K"]

    submissions = get_submissions(cik)

    # Navigate into the "recent" filings section which holds the parallel arrays.
    recent = submissions.get("filings", {}).get("recent", {})

    # Extract each parallel array. .get() with [] default handles missing keys.
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])

    filings = []
    # zip() stops at the shortest list, keeping the iteration safe if arrays differ in length.
    for form, date, acc, doc in zip(forms, dates, accessions, docs):
        if form in form_types:
            filings.append({
                "form": form,
                "filingDate": date,
                "accessionNumber": acc,
                "primaryDocument": doc,
            })
    return filings


def get_filing_text(cik: str, accession: str, document: str) -> str:
    """
    Fetch the raw text of a specific filing document from the EDGAR archives.

    Used to get the MD&A (Management Discussion & Analysis) and footnote text
    from a 10-K filing for LLM qualitative review (see src/llm_review.py).

    URL construction:
      EDGAR archive paths look like:
        /Archives/edgar/data/{cik_int}/{accession_no_hyphens}/{document}
      The CIK in the path is an INTEGER (no leading zeros), unlike the API URLs.
      The accession number has its hyphens removed (e.g. "0000320193-23-000106"
      becomes "000032019323000106").

    Cache key construction:
      We can't use the URL directly as a filename (contains slashes).
      Instead we build: "<cik>_<accession>_<document_with_slashes_replaced>"

    Args:
        cik:       Zero-padded 10-digit CIK string.
        accession: Hyphenated accession number, e.g. "0000320193-23-000106".
        document:  Primary document filename, e.g. "aapl-20230930.htm".

    Returns:
        Raw text content of the filing. May be HTML (most 10-Ks are .htm files).
        Callers should slice to ~12 000 chars for LLM token budget reasons.
    """
    cik_padded = cik.zfill(10)

    # EDGAR archive URLs use the CIK as an integer (strip leading zeros).
    # Accession numbers in archive URLs have no hyphens.
    acc_clean = accession.replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data"
        f"/{int(cik_padded)}/{acc_clean}/{document}"
    )

    # Build a filesystem-safe cache key. Replace "/" in document names with "_"
    # to avoid creating subdirectories in the cache folder.
    cache_key = f"{cik_padded}_{acc_clean}_{document.replace('/', '_')}"
    cache_path = CACHE_DIR / cache_key  # no ".json" suffix — this is a text file

    # Return cached text immediately if it exists.
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")

    # Rate-limit text file fetches the same way as JSON endpoints.
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    # Use a longer timeout (60 s) because filing HTML files can be several MB.
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    _last_request_time = time.monotonic()

    text = resp.text
    # errors="replace" substitutes the Unicode replacement character (U+FFFD) for
    # any byte sequences that aren't valid UTF-8. Old filings sometimes use
    # Windows-1252 or Latin-1 encoding, which would otherwise cause decode errors.
    cache_path.write_text(text, encoding="utf-8", errors="replace")
    return text


# ── CLI convenience ──────────────────────────────────────────────────────────
# Run directly to inspect EDGAR data for a ticker.
# Usage:  python -m src.ingest AAPL

if __name__ == "__main__":
    import sys

    # Accepts a ticker (e.g. AAPL) or a bare CIK (e.g. 320193 / 0000320193).
    identifier = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(f"Resolving CIK for {identifier}...")
    cik = resolve_identifier(identifier)
    print(f"  CIK: {cik}")

    info = get_company_info(cik)
    print(f"  Name: {info['name']}")
    print(f"  Tickers: {', '.join(info['tickers']) or '—'}")
    if info["formerNames"]:
        former = ", ".join(fn.get("name", "") for fn in info["formerNames"])
        print(f"  Formerly: {former}")

    filings = get_filings(cik, ["10-K", "10-Q"])
    # Count filings by form type and display the totals.
    counts = {}
    for f in filings:
        counts[f["form"]] = counts.get(f["form"], 0) + 1
    for form, n in sorted(counts.items()):
        print(f"  {form}: {n} filings")

    print("Fetching company facts (XBRL)...")
    facts = get_company_facts(cik)
    n_concepts = len(facts.get("facts", {}).get("us-gaap", {}))
    print(f"  us-gaap concepts available: {n_concepts}")
