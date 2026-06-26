"""
Seed the `agency_ratings` table from the canonical source-of-truth CSV
(``data/agency_ratings.csv``, built by scripts.build_agency_ratings_csv).

This is the single agency-ratings ingestion path: CIKs and rating indices are already
resolved/normalised in the canonical CSV (built by scripts.build_agency_ratings_csv), so
this is a thin read-and-upsert. Rows are projected to the agency_ratings columns by
store.save_agency_ratings_bulk (one row per cik + agency + effective_date), which is
idempotent on its primary key.

By default this REPLACES the table (clears it first) so agency_ratings mirrors the CSV
exactly — a plain upsert would leave behind any rows the CSV no longer contains (e.g. a
retired source), which then feed stale labels downstream. Pass --no-replace to upsert
without clearing (incremental add).

Run AFTER build_agency_ratings_csv (and before build_labels). Usage:
    python3 -m scripts.load_agency_ratings
    python3 -m scripts.load_agency_ratings --no-replace   # incremental upsert, keep existing rows
"""

from __future__ import annotations

import argparse
import pathlib

from src.ratings.ingest import load_csv
from src.store import save_agency_ratings_bulk, clear_agency_ratings

CANONICAL_CSV = pathlib.Path(__file__).resolve().parent.parent / "data" / "agency_ratings.csv"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-replace", action="store_true",
                    help="upsert without clearing first (keeps rows absent from the CSV)")
    args = ap.parse_args()

    if not CANONICAL_CSV.exists():
        raise SystemExit(
            f"Missing {CANONICAL_CSV} — run scripts.build_agency_ratings_csv first."
        )

    if not args.no_replace:
        clear_agency_ratings()
        print("Cleared agency_ratings (replace mode) — table will mirror the CSV.")

    df = load_csv(CANONICAL_CSV)
    # NaN → None so optional columns (rating_index for withdrawn/NR, source_*) upsert as
    # SQL NULL rather than the float nan.
    events = df.where(df.notna(), None).to_dict(orient="records")

    # rating_index is an INTEGER column, but pandas reads it as float (e.g. 11.0) because
    # withdrawn/NR rows leave it null. Coerce back to nullable int for the upsert.
    for e in events:
        ri = e.get("rating_index")
        e["rating_index"] = int(round(float(ri))) if ri is not None else None

    # Chunk the upsert — a single request with ~12.7k rows can exceed payload limits.
    BATCH = 500
    for i in range(0, len(events), BATCH):
        save_agency_ratings_bulk(events[i : i + BATCH])
        print(f"  upserted {min(i + BATCH, len(events))}/{len(events)}", flush=True)

    issuers = {str(e["cik"]).zfill(10) for e in events}
    print(f"Upserted {len(events)} agency-rating events across {len(issuers)} issuers "
          f"into agency_ratings.")


if __name__ == "__main__":
    main()
