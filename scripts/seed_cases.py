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

from src.store import add_case

CASES_CSV = pathlib.Path(__file__).resolve().parent.parent / "data" / "cases.csv"


def main() -> None:
    if not CASES_CSV.exists():
        raise SystemExit(f"cases.csv not found at {CASES_CSV}")

    with open(CASES_CSV, newline="") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        saved = add_case(row)
        print(f"  upserted {saved['case_id']:<20} {saved['ticker']:<6} {saved['label']}")

    print(f"Seeded {len(rows)} cases into Supabase.")


if __name__ == "__main__":
    main()
