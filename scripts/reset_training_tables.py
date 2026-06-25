"""
DESTRUCTIVE — wipe the previous training run's data from Supabase before a fresh
retrain on a new universe.

Truncates the rating-derived/model-output tables:
  - agency_ratings        (old rating labels — replaced by scripts.load_agency_ratings)
  - rating_labels         (old ML labels — rebuilt by scripts.build_labels)
  - migration_predictions (old model outputs — rewritten by src.model.predict)
and clears the active model_registry row.

It deliberately KEEPS the feature tables (ratios, implied_ratings, llm_findings,
debt_maturities, covenants, loss_provisions, companies): those are auditable EDGAR
measurements, not "trained data", and re-tracking upserts over them idempotently.

Requires the Supabase SERVICE-ROLE key (anon cannot DELETE under RLS). Prints row
counts before/after. Refuses to run without --yes.

Usage:
    python3 -m scripts.reset_training_tables --yes
    python3 -m scripts.reset_training_tables --yes --include-features   # also wipe ratios/implied
"""

from __future__ import annotations

import argparse

from src.store import _client

# Cleared on every reset (rating labels + model outputs).
_TRAINING_TABLES = ("agency_ratings", "rating_labels", "migration_predictions")
# Only cleared with --include-features (the EDGAR-derived feature tables).
_FEATURE_TABLES = (
    "ratios", "implied_ratings", "llm_findings", "debt_maturities",
    "covenants", "loss_provisions",
)


def _count(client, table: str) -> int | None:
    try:
        return client.table(table).select("cik", count="exact").limit(1).execute().count
    except Exception as e:
        print(f"  [{table}: count failed: {e}]")
        return None


def _truncate(client, table: str) -> None:
    # supabase-py requires a filter on delete; cik is never this sentinel, so this
    # matches every row.
    client.table(table).delete().neq("cik", "__none__").execute()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true", help="confirm the destructive wipe")
    ap.add_argument("--include-features", action="store_true",
                    help="ALSO wipe ratios/implied_ratings/findings/maturities/covenants/provisions")
    args = ap.parse_args()

    tables = list(_TRAINING_TABLES) + (list(_FEATURE_TABLES) if args.include_features else [])

    client = _client()
    print("Tables to wipe:", ", ".join(tables))
    print("Row counts BEFORE:")
    for t in tables:
        print(f"  {t:22} {_count(client, t)}")

    if not args.yes:
        raise SystemExit("\nRefusing to delete without --yes. Re-run with --yes to proceed.")

    for t in tables:
        _truncate(client, t)
    try:
        client.table("model_registry").delete().eq("id", "active").execute()
        print("Cleared model_registry active row.")
    except Exception as e:
        print(f"[model_registry clear skipped: {e}]")

    print("Row counts AFTER:")
    for t in tables:
        print(f"  {t:22} {_count(client, t)}")
    print("\nDone. Now: load_agency_ratings → track → build_labels → train.")


if __name__ == "__main__":
    main()
