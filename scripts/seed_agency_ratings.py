"""
Ingest the real LSEG agency-rating history for the roster issuers into Supabase.

Reads the two file-based drops —
  data/ratings_history_raw.csv  (LONG action log: one row per dated rating action)
  data/universe_xref.csv        (RIC ↔ ticker ↔ PermID crosswalk)
— extracts each issuer's Moody's / Fitch / Egan-Jones long-term rating trajectory
(src.ratings.ingest.extract_events_long), attaches the authoritative CIK from the
seeded case roster, and upserts the change events into the `agency_ratings` table.

Network-free: the CIKs come from the `cases` roster (already resolved by
scripts.seed_cases / src.track), NOT from EDGAR — so the agency ratings land under
exactly the same CIK as the tracked ratios, which is what build_labels joins on.
Run AFTER seed_cases (and ideally after tracking), e.g. via scripts.seed_demo.

Usage:
    python3 -m scripts.seed_agency_ratings
"""

from __future__ import annotations

import collections
import pathlib
from typing import Any

import pandas as pd

from src.ratings.ingest import load_csv, extract_events_long
from src.ratings.crosswalk import _detect_xref_columns, _clean, attach_cik
from src.store import save_agency_ratings_bulk

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
HISTORY_CSV = DATA_DIR / "ratings_history_raw.csv"
XREF_CSV = DATA_DIR / "universe_xref.csv"


def ric_candidates_by_ticker(xref: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    """Map each ticker (upper-cased) → [{ric, permid, name}] from the universe xref."""
    cols = _detect_xref_columns(list(xref.columns))
    out: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for _, row in xref.iterrows():
        ric = _clean(row[cols["ric"]]) if cols["ric"] else None
        ticker = _clean(row[cols["ticker"]]) if cols["ticker"] else None
        if not ric or not ticker:
            continue
        out[ticker.upper()].append({
            "ric": ric,
            "permid": _clean(row[cols["permid"]]) if cols["permid"] else None,
            "name": _clean(row[cols["name"]]) if cols["name"] else None,
        })
    return out


def events_by_ric(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group extracted events by their RIC."""
    out: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for e in events:
        out[e["ric"]].append(e)
    return out


def select_roster_rics(
    roster_cik_by_ticker: dict[str, str],
    ric_cands: dict[str, list[dict[str, Any]]],
    evs_by_ric: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """
    Build the crosswalk.attach_cik `resolved` map for the roster: each roster
    ticker → its single best RIC (the one with the MOST rating events), carrying the
    authoritative CIK from the case roster + the PermID/name from the xref.

    Picking one RIC per ticker avoids two RICs of the same issuer writing conflicting
    (cik, agency, effective_date) rows. Tickers with no rated RIC are skipped.
    """
    resolved: dict[str, dict[str, Any]] = {}
    for ticker, cik in roster_cik_by_ticker.items():
        best: tuple[int, dict[str, Any]] | None = None
        for cand in ric_cands.get(ticker.upper(), []):
            n = len(evs_by_ric.get(cand["ric"], []))
            if n and (best is None or n > best[0]):
                best = (n, cand)
        if best:
            cand = best[1]
            resolved[cand["ric"]] = {
                "cik": str(cik).zfill(10),
                "permid": cand.get("permid"),
                "ticker": ticker,
                "name": cand.get("name"),
            }
    return resolved


def main() -> None:
    if not HISTORY_CSV.exists() or not XREF_CSV.exists():
        raise SystemExit(f"Need {HISTORY_CSV.name} and {XREF_CSV.name} in {DATA_DIR}")

    # Roster CIKs — prefer the Supabase `cases` table (authoritative after seeding),
    # fall back to data/cases.csv so this still runs offline / pre-seed.
    from src.backtest import load_cases
    roster = load_cases()
    roster_cik_by_ticker = {
        (c["ticker"] or "").upper(): (c["cik"] or "").zfill(10)
        for c in roster
        if (c.get("ticker") or "").strip() and (c.get("cik") or "").strip()
    }
    if not roster_cik_by_ticker:
        raise SystemExit("No roster tickers with CIKs — run scripts.seed_cases first.")

    xref = load_csv(XREF_CSV)
    history = load_csv(HISTORY_CSV)
    events = extract_events_long(history)
    evs_by_ric = events_by_ric(events)
    ric_cands = ric_candidates_by_ticker(xref)

    resolved = select_roster_rics(roster_cik_by_ticker, ric_cands, evs_by_ric)
    attached = attach_cik(events, resolved)

    save_agency_ratings_bulk(attached)

    # Per-issuer summary so the operator can eyeball coverage.
    per_cik_agency: dict[tuple[str, str], int] = collections.Counter(
        (e["cik"], e["agency"]) for e in attached
    )
    missing = sorted(set(roster_cik_by_ticker) - {r["ticker"].upper() for r in resolved.values()})
    print(f"Resolved {len(resolved)} roster RICs; upserted {len(attached)} agency-rating events.")
    for (cik, agency), n in sorted(per_cik_agency.items()):
        print(f"  {cik}  {agency}  {n:>3} events")
    if missing:
        print(f"No rating history found for: {', '.join(missing)}")


if __name__ == "__main__":
    main()
