"""
Export the live Supabase `cases` table → data/cases.csv (DB → CSV).

The mirror image of scripts.seed_cases (CSV → DB). Supabase is the source of truth;
this snapshots it back to the committed CSV. The API already auto-exports after each
UI add/delete, so this is for a manual full resync — e.g. after scripts.rebuild_cases,
or to repair drift.

Usage:
    python3 -m scripts.export_cases
"""

from src.store import export_cases_to_csv


def main() -> None:
    n = export_cases_to_csv()
    print(f"Wrote {n} cases → data/cases.csv")


if __name__ == "__main__":
    main()
