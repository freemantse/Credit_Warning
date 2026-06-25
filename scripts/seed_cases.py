"""
One-time migration: seed the Supabase `cases` table from data/cases.csv.

Idempotent — re-running upserts on case_id, so it's safe to run repeatedly (e.g.
after editing the CSV, or to repair the table). Reads the CSV directly (NOT
load_cases(), which now prefers Supabase) so it always migrates from the flat
file regardless of current DB state.

Usage:
    python3 -m scripts.seed_cases
"""

import csv
import pathlib

from src.ingest import resolve_identifier
from src.store import add_case, list_cases, delete_case

CASES_CSV = pathlib.Path(__file__).resolve().parent.parent / "data" / "cases.csv"


def main() -> None:
    if not CASES_CSV.exists():
        raise SystemExit(f"cases.csv not found at {CASES_CSV}")

    with open(CASES_CSV, newline="") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        # Resolve ticker → CIK via EDGAR when the CSV CIK is blank, so the stored
        # CIK is authoritative rather than a hand-typed guess. event_type / agency
        # ride through add_case unchanged (they're part of the case columns).
        if not (row.get("cik") or "").strip():
            ident = (row.get("ticker") or row.get("company_name") or "").strip()
            try:
                row["cik"] = resolve_identifier(ident)
            except Exception as e:
                print(f"  ! could not resolve CIK for {ident!r}: {e}")
        saved = add_case(row)
        print(f"  upserted {saved['case_id']:<18} {saved['ticker']:<6} "
              f"{saved['event_type']:<9} {saved['agency'] or '—'}")

    # Make the table AUTHORITATIVE to the CSV: drop any case no longer in the roster.
    # Stale rows from earlier seeds (e.g. delisted names with no LSEG history) would
    # otherwise linger and show up as data_gap rows in the migration backtest.
    csv_ids = {row["case_id"] for row in rows}
    stale = [c["case_id"] for c in list_cases() if c["case_id"] not in csv_ids]
    for cid in stale:
        delete_case(cid)
        print(f"  pruned stale {cid}")

    print(f"Seeded {len(rows)} cases into Supabase ({len(stale)} stale pruned).")


if __name__ == "__main__":
    main()
