"""
Resolve the LSEG universe (PermID / RIC / ticker / name) to the canonical CIK.

The crosswalk is the riskiest step: the delisted/defaulted tail — exactly the names
the downgrade model most needs — is where ticker matching breaks (dead or recycled
tickers). So resolution is layered (ticker → name) and anything that fails is
emitted to unmatched.csv for manual review, never silently dropped.

Network resolution reuses the existing SEC EDGAR bridges (src.ingest.get_cik,
find_cik_by_name). The resolver is injectable so tests run offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pandas as pd

# A resolver maps (ticker, name) → a 10-digit CIK string, or None when unresolved.
Resolver = Callable[[str | None, str | None], str | None]


def _detect_xref_columns(columns: list[str]) -> dict[str, str | None]:
    """Pattern-detect the universe_xref columns (names vary across LSEG configs)."""
    low = {c: c.lower() for c in columns}

    def find(pred) -> str | None:
        return next((c for c in columns if pred(low[c])), None)

    return {
        "permid": find(lambda l: "permid" in l or l == "perm_id"),
        "ric": find(lambda l: l == "ric" or "instrument" in l or l.endswith(".ric")),
        "ticker": find(lambda l: "ticker" in l),
        "name": find(lambda l: "commonname" in l or "company" in l or l == "name"),
        "cusip": find(lambda l: "cusip" in l),
        "isin": find(lambda l: "isin" in l),
    }


def default_resolver(ticker: str | None, name: str | None) -> str | None:
    """
    Production resolver: ticker → CIK via SEC, then a name fallback for the
    delisted tail. Returns None on any miss or ambiguity (ambiguous name matches
    raise ValueError in find_cik_by_name and are treated as unresolved → manual review).
    """
    from src.ingest import get_cik, find_cik_by_name

    if ticker:
        try:
            return get_cik(ticker)
        except ValueError:
            pass
        except Exception:
            pass
    if name:
        try:
            return find_cik_by_name(name)
        except ValueError:
            pass
        except Exception:
            pass
    return None


def _clean(v: Any) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def build_crosswalk(
    xref: pd.DataFrame,
    resolver: Resolver = default_resolver,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """
    Resolve each universe row to a CIK.

    Args:
        xref:     the universe_xref DataFrame.
        resolver: (ticker, name) → CIK | None. Defaults to the SEC-backed resolver;
                  inject a stub in tests to stay offline.

    Returns:
        (resolved, unmatched):
          resolved  — {ric: {"cik", "permid", "ticker", "name", "cusip", "isin"}}
                      for every row whose RIC and CIK both resolved.
          unmatched — list of the raw-ish row dicts that could not be resolved
                      (for write_unmatched / manual review).
    """
    cols = _detect_xref_columns(list(xref.columns))
    if cols["ric"] is None:
        raise ValueError("Could not detect a RIC/instrument column in universe_xref")

    resolved: dict[str, dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []

    for _, row in xref.iterrows():
        ric = _clean(row[cols["ric"]]) if cols["ric"] else None
        ticker = _clean(row[cols["ticker"]]) if cols["ticker"] else None
        name = _clean(row[cols["name"]]) if cols["name"] else None
        permid = _clean(row[cols["permid"]]) if cols["permid"] else None
        cusip = _clean(row[cols["cusip"]]) if cols["cusip"] else None
        isin = _clean(row[cols["isin"]]) if cols["isin"] else None

        rec = {"ric": ric, "permid": permid, "ticker": ticker, "name": name,
               "cusip": cusip, "isin": isin}

        cik = resolver(ticker, name) if ric else None
        if ric and cik:
            resolved[ric] = {"cik": cik, "permid": permid, "ticker": ticker,
                             "name": name, "cusip": cusip, "isin": isin}
        else:
            rec["reason"] = "no RIC" if not ric else "ticker+name unresolved"
            unmatched.append(rec)

    return resolved, unmatched


def attach_cik(events: list[dict[str, Any]], resolved: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Join the crosswalk onto rating events, returning only events whose RIC resolved.
    Each kept event gains cik / source_permid / source_ric (and drops the bare ric).
    Events for unresolved RICs are filtered out (their RICs are already in unmatched).
    """
    out: list[dict[str, Any]] = []
    for e in events:
        info = resolved.get(e.get("ric"))
        if not info:
            continue
        out.append({
            **{k: v for k, v in e.items() if k != "ric"},
            "cik": info["cik"],
            "source_permid": info.get("permid"),
            "source_ric": e.get("ric"),
        })
    return out


def write_unmatched(unmatched: list[dict[str, Any]], path: str | Path) -> int:
    """Write the unmatched rows to a CSV for manual review. Returns the row count."""
    pd.DataFrame(unmatched).to_csv(path, index=False)
    return len(unmatched)
