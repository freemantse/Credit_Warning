"""
One-time consolidation — build ``data/agency_ratings.csv``, the SINGLE SOURCE OF TRUTH
for agency ratings, by cleaning and resolving the LSEG raw drop:

  data/ratings_history_raw.csv  LSEG action log — MDY/FTC/EJR, ~8,800 global RICs
  data/universe_xref.csv        RIC → ticker/name crosswalk (resolves the LSEG side)

Pipeline:
  LSEG:  extract_events_long → restrict the xref to RICs that actually have events →
         build_crosswalk (RIC→CIK) → attach_cik → tag source="lseg". Rows that never
         resolve to a CIK are dropped — EDGAR is US-only, so most LSEG foreign RICs
         fall out here (expected, reported).

(The Kaggle snapshot source was removed: it was confined to 2009–2016 and, as point
snapshots rather than a change log, its only-S&P series forward-filled into fake
"stable" labels past its coverage. Agency coverage is now LSEG-only: MDY/FTC/EJR.)

Writes the canonical CSV (one row per resolved rating action) and prints a scorecard:
unique issuers, per-agency counts, and drop counts.

Network: resolves each candidate ticker/name against SEC EDGAR (throttled to 8 req/sec
in src.ingest). Restricting to RICs-with-events keeps this to the issuers we can use.

After this runs and the CSV looks right, remove the raw rating CSV; everything
downstream reads only the canonical CSV via scripts.load_agency_ratings.

Usage:
    python3 -m scripts.build_agency_ratings_csv
    python3 -m scripts.build_agency_ratings_csv --limit-lseg 50      # fast smoke test
"""

from __future__ import annotations

import argparse
import collections
import pathlib
from typing import Any

import pandas as pd

from src.ratings.ingest import load_csv, extract_events_long
from src.ratings.crosswalk import build_crosswalk, attach_cik, default_resolver

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
HISTORY_CSV = DATA_DIR / "ratings_history_raw.csv"
XREF_CSV = DATA_DIR / "universe_xref.csv"
OUT_CSV = DATA_DIR / "agency_ratings.csv"

# Canonical column order for the single source-of-truth CSV. The first nine are exactly
# what scripts.load_agency_ratings forwards to store.save_agency_ratings_bulk; ticker /
# company_name / source are kept for human traceability.
CANONICAL_COLUMNS = (
    "cik", "agency", "effective_date", "rating_index", "rating_raw",
    "rating_status", "rating_action", "source_permid", "source_ric",
    "ticker", "company_name", "source",
)


def _resolve_lseg(limit: int | None) -> tuple[list[dict[str, Any]], int, int]:
    """
    Build CIK-attached LSEG events. Restricts the xref to RICs that actually carry
    long-term events before resolving, so we only spend EDGAR calls on usable issuers.
    Returns (events, n_event_rics, n_resolved_rics).
    """
    history = load_csv(HISTORY_CSV)
    xref = load_csv(XREF_CSV)

    events = extract_events_long(history)
    event_rics = {e["ric"] for e in events}

    # Keep only xref rows whose RIC has events (the only ones worth resolving).
    cols = list(xref.columns)
    ric_col = next((c for c in cols if c.lower() == "ric" or "instrument" in c.lower()), cols[0])
    xref_used = xref[xref[ric_col].isin(event_rics)].copy()
    if limit is not None:
        xref_used = xref_used.head(limit)
        keep = set(xref_used[ric_col])
        events = [e for e in events if e["ric"] in keep]

    resolved, _unmatched = build_crosswalk(xref_used, resolver=default_resolver)
    # ric → ticker/name for the canonical CSV (attach_cik keeps source_ric but not these).
    meta_by_ric = {ric: {"ticker": info.get("ticker"), "name": info.get("name")}
                   for ric, info in resolved.items()}

    attached = attach_cik(events, resolved)
    out: list[dict[str, Any]] = []
    for e in attached:
        meta = meta_by_ric.get(e.get("source_ric"), {})
        out.append({
            "cik": str(e["cik"]).zfill(10),
            "agency": e["agency"],
            "effective_date": e["effective_date"],
            "rating_index": e.get("rating_index"),
            "rating_raw": e.get("rating_raw"),
            "rating_status": e.get("rating_status"),
            "rating_action": e.get("rating_action"),
            "source_permid": e.get("source_permid"),
            "source_ric": e.get("source_ric"),
            "ticker": meta.get("ticker"),
            "company_name": meta.get("name"),
            "source": "lseg",
        })
    return out, len(event_rics), len(resolved)


def consolidate(limit_lseg: int | None) -> list[dict[str, Any]]:
    """Resolve LSEG events and dedupe on (cik, agency, effective_date)."""
    lseg_rows, n_event_rics, n_resolved = _resolve_lseg(limit_lseg)
    print(f"LSEG: {n_event_rics} RICs with events → {n_resolved} resolved to a CIK "
          f"→ {len(lseg_rows)} events (US filers; foreign RICs dropped).")

    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in lseg_rows:
        by_key[(row["cik"], row["agency"], row["effective_date"])] = row

    return sorted(by_key.values(), key=lambda r: (r["cik"], r["agency"], r["effective_date"]))


def scorecard(rows: list[dict[str, Any]]) -> None:
    """Print the usable-issuer count and per-agency breakdown."""
    issuers = {r["cik"] for r in rows}
    per_agency = collections.Counter(r["agency"] for r in rows)
    per_source = collections.Counter(r["source"] for r in rows)
    print("\n── Canonical agency_ratings.csv scorecard ──────────────────────────────")
    print(f"  rows (rating actions): {len(rows)}")
    print(f"  UNIQUE ISSUERS (CIKs available to use): {len(issuers)}")
    print(f"  by source: {dict(per_source)}")
    print("  by agency:")
    for ag, n in per_agency.most_common():
        print(f"    {ag:5} {n:>6}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit-lseg", type=int, default=None,
                    help="resolve only the first N LSEG RICs-with-events (smoke test)")
    ap.add_argument("--out", type=pathlib.Path, default=OUT_CSV)
    args = ap.parse_args()

    rows = consolidate(args.limit_lseg)
    df = pd.DataFrame(rows, columns=list(CANONICAL_COLUMNS))
    df.to_csv(args.out, index=False)
    print(f"\nWrote {len(df)} rows → {args.out}")
    scorecard(rows)

    # Sanity: the primary key must be unique (downstream upsert relies on it).
    dupes = df.duplicated(subset=["cik", "agency", "effective_date"]).sum()
    if dupes:
        print(f"  ERROR: {dupes} duplicate (cik,agency,effective_date) rows remain.")
    else:
        print("  PK (cik, agency, effective_date) is unique. ✓")


if __name__ == "__main__":
    main()
