"""
Build the frozen, lookahead-free ML label table from agency rating events.

Two responsibilities:
  rating_asof()         — the rating in effect for an issuer/agency as of a date,
                          forward-filling the last event but STOPPING at a
                          withdrawal (→ unrated) or default (→ D). Withdrawals are
                          not ratings, so a stale rating is never carried through one.
  build_rating_labels() — for each financial period_end, the rating now and at
                          +3/+6/+12m, the signed migration labels, the 12m notch
                          change, and distress_12m. Forward windows that extend past
                          the dataset's last date are CENSORED (left None), never
                          fabricated as "stable".

distress_12m is the rare-event target: a TRANSITION into the distress tail (any event
reaching index ≥ DISTRESS_INDEX = CCC+, or a default) within 12m, from a non-distressed
start. It broadens the old default-only signal (≈8 events) into something trainable
(hundreds), and subsumes default since D (21) ≥ DISTRESS_INDEX.

Sign convention (index space, higher = worse): notch_change/label POSITIVE = DOWNGRADE.
  label_Nm = +1 downgrade, -1 upgrade, 0 stable; None when uncomputable/censored.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from src.ratings.scale import DISTRESS_INDEX, STATUS_DEFAULT, STATUS_NOT_RATED


def add_months(date_str: str, months: int) -> str:
    """Add `months` to a 'YYYY-MM-DD' date (clamping the day to month length)."""
    d = date.fromisoformat(date_str)
    total = (d.year * 12 + (d.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    # Clamp the day to the target month's length (e.g. Jan 31 + 1m → Feb 28/29).
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = (next_month_first - date(year, month, 1)).days
    return date(year, month, min(d.day, last_day)).isoformat()


def events_by_key(events: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Group events by (cik, agency) and sort each group by effective_date ascending."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in events:
        key = (e["cik"], e["agency"])
        grouped.setdefault(key, []).append(e)
    for key in grouped:
        grouped[key].sort(key=lambda e: e["effective_date"])
    return grouped


def rating_asof(sorted_events: list[dict[str, Any]], as_of: str) -> tuple[int | None, str]:
    """
    The rating (index, status) in effect as of `as_of` for one (cik, agency).

    Takes the LAST event with effective_date <= as_of (forward-fill). A withdrawal
    yields (None, 'withdrawn') and a default yields (D, 'default') — neither carries
    a prior notch forward. No event yet → (None, 'not_rated').
    """
    current: dict[str, Any] | None = None
    for e in sorted_events:                  # ascending by effective_date
        if e["effective_date"] <= as_of:
            current = e
        else:
            break
    if current is None:
        return (None, STATUS_NOT_RATED)
    return (current["rating_index"], current["rating_status"])


def _label(idx_now: int | None, idx_fut: int | None) -> int | None:
    """Signed migration label: +1 downgrade, -1 upgrade, 0 stable; None if either side unknown."""
    if idx_now is None or idx_fut is None:
        return None
    if idx_fut > idx_now:
        return 1     # index rose → worse → downgrade
    if idx_fut < idx_now:
        return -1    # upgrade
    return 0


def build_rating_labels(
    events: list[dict[str, Any]],
    period_ends_by_cik: dict[str, list[str]],
    *,
    data_max_date: str,
    horizons: tuple[int, ...] = (3, 6, 12),
) -> list[dict[str, Any]]:
    """
    Build rating_labels rows (grain: cik, period_end, agency).

    Args:
        events:             flat list of agency events (cik, agency, effective_date,
                            rating_index, rating_status, …) — already crosswalked.
        period_ends_by_cik: the financial period_ends (from ratios/implied_ratings)
                            to label, per CIK.
        data_max_date:      the dataset's last observation date ("YYYY-MM-DD"). A
                            horizon window ending after this is censored → None.
        horizons:           forward windows in months (default 3, 6, 12).

    Returns:
        One row per (cik, period_end, agency) that has any rating coverage, with
        rating_index{,_3m,_6m,_12m}, label_{3,6,12}m, notch_change_12m, distress_12m.
    """
    grouped = events_by_key(events)
    rows: list[dict[str, Any]] = []

    for (cik, agency), evs in grouped.items():
        for period_end in period_ends_by_cik.get(cik, []):
            idx_now, _status_now = rating_asof(evs, period_end)

            row: dict[str, Any] = {
                "cik": cik,
                "period_end": period_end,
                "agency": agency,
                "rating_index": idx_now,
            }

            idx_by_h: dict[int, int | None] = {}
            for h in horizons:
                target = add_months(period_end, h)
                if target > data_max_date:
                    idx_h = None                    # censored — outcome not yet observable
                else:
                    idx_h, _ = rating_asof(evs, target)
                idx_by_h[h] = idx_h
                row[f"rating_index_{h}m"] = idx_h
                row[f"label_{h}m"] = _label(idx_now, idx_h)

            # 12m-specific summary fields.
            idx_12 = idx_by_h.get(12)
            row["notch_change_12m"] = (
                idx_12 - idx_now if (idx_12 is not None and idx_now is not None) else None
            )

            # distress_12m: a TRANSITION into the distress tail within 12 months — the
            # issuer is rated and NOT already distressed at period_end, and within the
            # (observed) window some event either defaults or reaches index ≥
            # DISTRESS_INDEX (CCC+). Subsumes the old default-only signal (D ≥ CCC+).
            window_end = add_months(period_end, 12)
            observed_12m = window_end <= data_max_date
            not_distressed_now = idx_now is not None and idx_now < DISTRESS_INDEX
            distress_12m = observed_12m and not_distressed_now and any(
                period_end < e["effective_date"] <= window_end
                and (
                    e["rating_status"] == STATUS_DEFAULT
                    or (e["rating_index"] is not None and e["rating_index"] >= DISTRESS_INDEX)
                )
                for e in evs
            )
            row["distress_12m"] = bool(distress_12m)

            rows.append(row)

    return rows
