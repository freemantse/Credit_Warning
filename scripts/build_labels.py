"""
Build the frozen ML label table (rating_labels) from the stored agency ratings and
financial period_ends.

This is the store-driven orchestrator around src.ratings.labels.build_rating_labels:
  1. read every agency-rating event       (store.get_agency_ratings_grouped)
  2. read the financial period_ends per CIK (store.get_ratios_grouped — the periods
     that actually have features to score)
  3. for each (cik, period_end, agency): the rating now and at +3/+6/+12m, the signed
     migration labels, the 12m notch change, and default_12m — with right-edge
     censoring past the data's last observed action (data_max_date).
  4. upsert into rating_labels                (store.save_rating_labels_bulk)

The pure core (compute_labels / period_ends_by_cik / flatten_events) takes the
grouped dicts directly so it can be unit-tested without Supabase.

Run AFTER seed_agency_ratings + tracking. Usage:
    python3 -m scripts.build_labels
"""

from __future__ import annotations

from typing import Any

from src.ratings.labels import build_rating_labels


def flatten_events(agency_grouped: dict[str, dict[str, list[dict]]]) -> list[dict[str, Any]]:
    """
    Flatten store.get_agency_ratings_grouped (cik → agency → [rows]) into the flat
    event list build_rating_labels consumes ({cik, agency, effective_date,
    rating_index, rating_status}).
    """
    events: list[dict[str, Any]] = []
    for cik, by_agency in agency_grouped.items():
        for agency, rows in by_agency.items():
            for r in rows:
                events.append({
                    "cik": cik,
                    "agency": agency,
                    "effective_date": r["effective_date"],
                    "rating_index": r.get("rating_index"),
                    "rating_status": r["rating_status"],
                })
    return events


def period_ends_by_cik(ratios_grouped: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Map cik → sorted financial period_ends (the rows that have features to label)."""
    return {cik: sorted(periods.keys()) for cik, periods in ratios_grouped.items()}


def compute_labels(
    agency_grouped: dict[str, dict[str, list[dict]]],
    ratios_grouped: dict[str, dict[str, Any]],
    *,
    horizons: tuple[int, ...] = (3, 6, 12),
) -> list[dict[str, Any]]:
    """
    Pure label build from the two grouped store reads. data_max_date is the last
    observed agency action — horizon windows past it are censored (left None), never
    fabricated as 'stable'. Returns [] when there are no agency events to anchor on.
    """
    events = flatten_events(agency_grouped)
    if not events:
        return []
    data_max_date = max(e["effective_date"] for e in events)
    return build_rating_labels(
        events,
        period_ends_by_cik(ratios_grouped),
        data_max_date=data_max_date,
        horizons=horizons,
    )


def main() -> None:
    from src.store import (
        get_agency_ratings_grouped,
        get_ratios_grouped,
        save_rating_labels_bulk,
    )

    agency_grouped = get_agency_ratings_grouped()
    ratios_grouped = get_ratios_grouped()
    labels = compute_labels(agency_grouped, ratios_grouped)

    if not labels:
        print("No labels built — ingest agency ratings (seed_agency_ratings) and "
              "track issuers (ratios) first.")
        return

    save_rating_labels_bulk(labels)

    observed = sum(1 for r in labels if r.get("label_12m") is not None)
    downgrades = sum(1 for r in labels if r.get("label_12m") == 1)
    upgrades = sum(1 for r in labels if r.get("label_12m") == -1)
    defaults = sum(1 for r in labels if r.get("default_12m"))
    print(f"Built {len(labels)} rating_labels rows across "
          f"{len({r['cik'] for r in labels})} issuers.")
    print(f"  12m-observed: {observed}  (downgrade={downgrades}, upgrade={upgrades}, "
          f"default={defaults})")


if __name__ == "__main__":
    main()
