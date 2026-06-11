"""
SEC EDGAR ingestion: ticker → CIK → filings + company facts.

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
  find_cik_by_name(name)    → CIK lookup by company name (works for delisted issuers)
  get_company_facts(cik)    → full XBRL companyfacts JSON dict
  get_filings(cik, forms)   → list of filing metadata dicts
  get_filing_text(cik, ...) → raw filing document text (for LLM review)
"""

from __future__ import annotations

import json
import re
import time
import pathlib
import urllib.parse
import xml.etree.ElementTree as ET
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

# Classic EDGAR company-search endpoint. Unlike company_tickers.json (which only
# lists CURRENTLY-listed tickers), browse-edgar knows every registrant that ever
# filed — including bankrupt/delisted companies. With output=atom it returns
# parseable XML instead of HTML.
BROWSE_EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar"


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


def _get_text(url: str, cache_key: str) -> str:
    """
    Like _get() but for non-JSON responses (e.g. browse-edgar Atom XML).

    Same disk cache + rate limiting; the cache file has no ".json" suffix
    because the content is raw text. Callers that discover the response is
    useless (e.g. a "no match" HTML page) should call _evict_text_cache()
    so a future run retries instead of being stuck with the failure forever.
    """
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")

    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    _last_request_time = time.monotonic()

    text = resp.text
    cache_path.write_text(text, encoding="utf-8", errors="replace")
    return text


def _evict_text_cache(cache_key: str) -> None:
    """Delete a _get_text() cache entry (used to avoid caching failed lookups)."""
    cache_path = CACHE_DIR / cache_key
    if cache_path.exists():
        cache_path.unlink()


# ── Public ingestion functions ───────────────────────────────────────────────

def _parse_ciks_from_atom(xml_text: str) -> list[str]:
    """
    Extract all distinct CIKs from a browse-edgar Atom response.

    browse-edgar returns two XML shapes:
      - Unique match: the company's filing feed, with one <company-info><cik>
        directly under the feed element.
      - Multiple matches: a "Company Search Feed" with one <entry> per company,
        each containing <content><company-info><cik>.
    A failed lookup returns an HTML page, which is not well-formed XML — the
    parse fails and we return [].

    All elements inherit the Atom default namespace, so we match on the local
    tag name ("cik") rather than the qualified name.

    Returns:
        Distinct zero-padded 10-digit CIKs in document order ([] if none).
    """
    try:
        # The response declares encoding="ISO-8859-1"; ElementTree refuses a
        # str carrying an encoding declaration, so re-encode to bytes first.
        root = ET.fromstring(xml_text.encode("iso-8859-1", errors="replace"))
    except (ET.ParseError, ValueError):
        return []

    ciks: list[str] = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]  # strip namespace
        if tag == "cik" and el.text and el.text.strip().isdigit():
            cik = el.text.strip().zfill(10)
            if cik not in ciks:
                ciks.append(cik)
    return ciks


def get_cik(ticker: str) -> str:
    """
    Resolve a stock ticker to its SEC Central Index Key (CIK).

    Resolution order:
      1. company_tickers.json — SEC's list of CURRENTLY-listed tickers
         (case-insensitive match).
      2. browse-edgar fallback — its CIK= parameter also accepts ticker
         symbols and retains many DELISTED tickers (e.g. bankrupt issuers),
         which company_tickers.json drops.

    The CIK is zero-padded to 10 digits because EDGAR API URLs require that format.

    Raises:
        ValueError — if neither lookup finds the ticker.
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

    # ── Delisted-ticker fallback ──────────────────────────────────────────────
    cache_key = f"ticker_fallback_{ticker_upper}"
    params = urllib.parse.urlencode({
        "action": "getcompany",
        "CIK": ticker_upper,
        "type": "10-K",
        "owner": "include",
        "count": "10",
        "output": "atom",
    })
    xml_text = _get_text(f"{BROWSE_EDGAR_URL}?{params}", cache_key)
    ciks = _parse_ciks_from_atom(xml_text)
    if len(ciks) == 1:
        return ciks[0]

    # Don't cache the failure — EDGAR's data could change, and a poisoned
    # cache would make this ticker permanently unresolvable.
    _evict_text_cache(cache_key)
    raise ValueError(
        f"Ticker {ticker!r} not found in SEC company tickers list or EDGAR "
        f"browse fallback. If the company is delisted, look up its CIK with "
        f"find_cik_by_name() or at https://www.sec.gov/cgi-bin/browse-edgar"
    )


def find_cik_by_name(name: str) -> str:
    """
    Resolve a company NAME to its CIK via EDGAR company search.

    This works for delisted/bankrupt registrants that no longer appear in
    company_tickers.json. The search is EDGAR's prefix/substring match on the
    registrant's conformed name, restricted to companies that filed 10-Ks.

    Identity verification: once you have a CIK, get_company_info(cik) returns
    the current name, tickers and formerNames from the submissions API — use
    that to confirm you matched the right entity.

    Raises:
        ValueError — zero matches, or multiple matches (the message lists each
        candidate as "Name (CIK ##########)" so a human can pick the right one,
        e.g. for data/cases.csv).
    """
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    cache_key = f"name_search_{slug}"
    params = urllib.parse.urlencode({
        "action": "getcompany",
        "company": name.strip(),
        "type": "10-K",
        "owner": "include",
        "count": "40",
        "output": "atom",
    })
    xml_text = _get_text(f"{BROWSE_EDGAR_URL}?{params}", cache_key)
    ciks = _parse_ciks_from_atom(xml_text)

    if len(ciks) == 1:
        return ciks[0]

    if not ciks:
        _evict_text_cache(cache_key)  # don't cache the failure
        raise ValueError(f"No EDGAR company matches name {name!r}")

    # Multiple matches: fetch each candidate's current name so the error
    # message is actionable. Cap at 10 to bound the extra API calls.
    candidates = []
    for cik in ciks[:10]:
        try:
            info = get_company_info(cik)
            candidates.append(f"{info['name']} (CIK {cik})")
        except Exception:
            candidates.append(f"<name lookup failed> (CIK {cik})")
    more = f" … and {len(ciks) - 10} more" if len(ciks) > 10 else ""
    raise ValueError(
        f"Name {name!r} matches {len(ciks)} EDGAR companies — "
        f"pick the CIK and use it directly: " + "; ".join(candidates) + more
    )


def resolve_identifier(identifier: str) -> str:
    """
    Resolve either a ticker or a CIK to a zero-padded 10-digit CIK.

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
      filings.recent.reportDate  = ["2023-09-30", "2023-07-01", ...]
      filings.recent.accessionNumber = ["0000320193-23-000106", ...]
      filings.recent.primaryDocument  = ["aapl-20230930.htm", ...]

    zip() stitches these arrays together into (form, date, accession, doc) tuples.
    We convert each matching tuple into a dict for easier downstream use.

    reportDate is the fiscal period end the filing covers (a 10-K's reportDate
    equals its fiscal-year-end date) — the reliable key for matching a filing to
    an XBRL period (see find_filing_for_period). filingDate is merely when it
    was submitted, typically 2-4 months later.

    Args:
        form_types: list of form type strings to include, e.g. ["10-K"].
                    Defaults to ["10-K", "10-Q", "8-K"] if not specified.

    Returns:
        List of dicts with keys: form, filingDate, reportDate, accessionNumber,
        primaryDocument. Ordered newest-first (as EDGAR returns them).
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
    # reportDate may be absent or shorter in odd submissions data; pad with ""
    # so zip() (which stops at the shortest array) never drops filings over it.
    report_dates = recent.get("reportDate", [])
    if len(report_dates) < len(forms):
        report_dates = list(report_dates) + [""] * (len(forms) - len(report_dates))

    filings = []
    # zip() stops at the shortest list, keeping the iteration safe if arrays differ in length.
    for form, date, report, acc, doc in zip(forms, dates, report_dates, accessions, docs):
        if form in form_types:
            filings.append({
                "form": form,
                "filingDate": date,
                "reportDate": report,
                "accessionNumber": acc,
                "primaryDocument": doc,
            })
    return filings


def find_filing_for_period(filings: list[dict], period: str) -> dict | None:
    """
    Pick the filing that covers a given fiscal period end (e.g. "2023-09-30").

    Matching strategy, most reliable first:
      1. Exact reportDate match — a 10-K's reportDate IS its fiscal period end,
         in the same YYYY-MM-DD format as the XBRL period strings.
      2. Nearest filingDate within 0-12 months AFTER the period end — 10-Ks are
         filed 2-4 months after fiscal year end. Handles submissions data with a
         missing/blank reportDate.
      3. Calendar-year substring (the old heuristic): first filing whose
         filingDate contains the period's year. Kept as a last resort so odd
         data never matches worse than before.

    Why not the old `period[:4] in filingDate` alone?
      Off-calendar fiscal years break it: a FY ending 2024-01-31 is filed around
      2024-03 — but so is the NEXT fiscal year's 10-K (filed 2025-03), and a
      calendar-year match against "2024" can grab a filing covering a different
      period entirely.

    Args:
        filings: dicts from get_filings(), newest-first.
        period:  fiscal period end as "YYYY-MM-DD".

    Returns:
        The best-matching filing dict, or None if nothing plausibly matches.
    """
    from datetime import date as _date

    # 1. Exact fiscal-period match.
    for f in filings:
        if f.get("reportDate") == period:
            return f

    # 2. Nearest filing submitted within a year after the period end.
    def _parse(s: str) -> _date | None:
        try:
            y, m, d = s.split("-")
            return _date(int(y), int(m), int(d))
        except (ValueError, AttributeError):
            return None

    period_dt = _parse(period)
    if period_dt is not None:
        candidates = []
        for f in filings:
            filed = _parse(f.get("filingDate", ""))
            if filed is None:
                continue
            lag = (filed - period_dt).days
            if 0 <= lag <= 366:
                candidates.append((lag, f))
        if candidates:
            return min(candidates, key=lambda c: c[0])[1]

    # 3. Last resort: the legacy calendar-year heuristic.
    for f in filings:
        if period[:4] in f.get("filingDate", ""):
            return f
    return None


def filing_doc_url(cik: str, accession: str, document: str) -> str:
    """
    Build the public SEC EDGAR archive URL for a specific filing document.

    The single source of truth for the EDGAR archive URL pattern — shared by
    get_filing_text (to fetch the document) and the LLM review pipeline (to
    attach a traceable source link to each finding).

    URL construction:
      /Archives/edgar/data/{cik_int}/{accession_no_hyphens}/{document}
      The CIK in the path is an INTEGER (no leading zeros), unlike API URLs.
      The accession number has its hyphens removed (e.g. "0000320193-23-000106"
      becomes "000032019323000106").

    Args:
        cik:       Zero-padded 10-digit CIK string (or any int-coercible CIK).
        accession: Hyphenated accession number, e.g. "0000320193-23-000106".
        document:  Primary document filename, e.g. "aapl-20230930.htm".
    """
    cik_padded = cik.zfill(10)
    acc_clean = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data"
        f"/{int(cik_padded)}/{acc_clean}/{document}"
    )


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
        Callers should run it through sections.locate_sections() to extract the
        MD&A / footnote slices rather than sending raw HTML to an LLM.
    """
    cik_padded = cik.zfill(10)

    # EDGAR archive URLs use the CIK as an integer (strip leading zeros) and
    # accession numbers with no hyphens — both handled by filing_doc_url.
    acc_clean = accession.replace("-", "")
    url = filing_doc_url(cik, accession, document)

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
    # errors="replace" substitutes the Unicode replacement character (U+FFFD) 
    # for any byte sequences that aren't valid UTF-8. Old filings sometimes use
    # Windows-1252 or Latin-1 encoding, which would otherwise cause decode errors.
    cache_path.write_text(text, encoding="utf-8", errors="replace")
    return text


# ── CLI convenience ──────────────────────────────────────────────────────────
# Run directly to inspect EDGAR data for a ticker.
# Usage:  python3 -m src.ingest AAPL
#         python3 -m src.ingest --name "Sears Holdings"   # delisted-company lookup

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 2 and sys.argv[1] == "--name":
        # Company-name search (works for delisted issuers with no live ticker).
        name_query = " ".join(sys.argv[2:])
        print(f"Searching EDGAR for company name {name_query!r}...")
        cik = find_cik_by_name(name_query)
    else:
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
