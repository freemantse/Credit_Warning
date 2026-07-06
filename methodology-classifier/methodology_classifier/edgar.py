"""
Minimal SEC EDGAR resolver: ticker / CIK → SIC + issuer identity.

Self-contained (Python stdlib only) so the package needs no third-party HTTP client.
Only used when the input lacks a SIC column and you ask the CLI to resolve it.

SEC requires a descriptive User-Agent with contact info on every request. Set it via
the SEC_USER_AGENT environment variable, e.g.:
    export SEC_USER_AGENT="Your Name your.email@example.com"
A generic default is used if unset, but SEC may throttle or block anonymous traffic.

Endpoints:
    https://www.sec.gov/files/company_tickers.json                 (ticker → CIK)
    https://data.sec.gov/submissions/CIK##########.json            (CIK → sic, name)
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.request

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_DEFAULT_UA = "methodology-classifier (set SEC_USER_AGENT env var with your contact)"


def _ssl_context() -> ssl.SSLContext:
    """
    Build an SSL context. Prefer certifi's CA bundle when it's installed — the Python
    stdlib on macOS often ships without usable system roots, which otherwise fails with
    CERTIFICATE_VERIFY_FAILED. Falls back to the default context when certifi is absent.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()

# In-memory caches so a batch run fetches the ticker map once and never re-fetches an
# issuer already seen this run.
_ticker_map: dict[str, str] | None = None
_info_cache: dict[str, dict] = {}


def _get(url: str) -> bytes:
    """HTTP GET with the SEC-required User-Agent header."""
    ua = os.environ.get("SEC_USER_AGENT", _DEFAULT_UA)
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:  # noqa: S310 — fixed SEC hosts
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return raw


def _ticker_to_cik() -> dict[str, str]:
    """Load and cache the SEC ticker → 10-digit CIK map."""
    global _ticker_map
    if _ticker_map is None:
        data = json.loads(_get(_TICKERS_URL))
        _ticker_map = {
            str(row["ticker"]).upper(): str(row["cik_str"]).zfill(10)
            for row in data.values()
        }
    return _ticker_map


def resolve_cik(identifier: str) -> str:
    """
    Resolve a ticker OR CIK string to a canonical 10-digit CIK.

    A bare number (optionally 'CIK'-prefixed) is treated as a CIK and zero-padded; a
    non-numeric string is looked up as a ticker. Raises ValueError if unresolved.
    """
    ident = str(identifier).strip()
    cand = ident[3:] if ident[:3].upper() == "CIK" else ident
    if cand.isdigit():
        return cand.zfill(10)
    cik = _ticker_to_cik().get(ident.upper())
    if not cik:
        raise ValueError(f"Could not resolve ticker {identifier!r} to a CIK")
    return cik


def get_company_info(cik: str) -> dict:
    """
    Return {cik, name, sic, sic_description, tickers} from EDGAR submissions for a CIK.

    Cached per CIK for the process lifetime. Raises on network/JSON errors.
    """
    cik = str(cik).zfill(10)
    if cik in _info_cache:
        return _info_cache[cik]
    data = json.loads(_get(_SUBMISSIONS_URL.format(cik=cik)))
    info = {
        "cik": cik,
        "name": data.get("name", ""),
        "sic": data.get("sic") or None,
        "sic_description": data.get("sicDescription") or None,
        "tickers": data.get("tickers", []),
    }
    _info_cache[cik] = info
    return info


def resolve_sic(identifier: str) -> dict:
    """
    Convenience: ticker-or-CIK → {cik, name, sic, sic_description}.

    Returns {"error": "..."} on any failure so a batch caller can record it and keep
    going instead of aborting the whole run.
    """
    try:
        cik = resolve_cik(identifier)
        info = get_company_info(cik)
        return {
            "cik": info["cik"],
            "name": info.get("name"),
            "sic": info.get("sic"),
            "sic_description": info.get("sic_description"),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
