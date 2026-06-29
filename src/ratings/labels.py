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


# Agencies whose actions are issuer-paid and fundamentally anchored. Egan-Jones (EJR)
# is investor-paid, broader, and faster — valuable for coverage but it assigns extreme
# letters on earnings/market signals (e.g. CCC on cash-rich, debt-free growth names)
# and reverses transient one-notch moves quickly (its COVID-2020 cuts). The label
# policy keeps EJR for coverage but denoises those blips. SPI listed for forward-compat
# even though the current dataset carries only MDY/FTC/EJR.
ISSUER_PAID_AGENCIES = frozenset({"MDY", "FTC", "SPI"})

# How the label/case pipelines may treat EJR events (see credible_events):
#   "all"      — keep every event (legacy / baseline behaviour).
#   "denoised" — keep issuer-paid as-is; for EJR keep an action only when it is a real
#                default, a multi-notch (>= MULTI_NOTCH) move, or PERSISTENT (not
#                reversed within PERSIST_MONTHS). Transient single-notch EJR blips are
#                dropped, so a forward-fill never turns one into a spurious label.
#   "big3"     — drop EJR entirely (label only on issuer-paid agencies).
LABEL_POLICIES = ("all", "denoised", "big3")
PERSIST_MONTHS = 12
MULTI_NOTCH = 2


def _months_diff(d_from: str, d_to: str) -> int:
    a, b = date.fromisoformat(d_from), date.fromisoformat(d_to)
    return (b.year - a.year) * 12 + (b.month - a.month)


def credible_events(
    evs: list[dict[str, Any]], agency: str, policy: str,
    *, persist_months: int = PERSIST_MONTHS, multi_notch: int = MULTI_NOTCH,
) -> list[dict[str, Any]]:
    """
    Filter one (cik, agency) event sequence (ascending by effective_date) to the
    "credible" rating actions used for labeling / case selection, per `policy`.

    The single source of truth for EJR denoising, shared by build_rating_labels (the
    training-label source) and scripts.rebuild_cases (the backtest case pool), so both
    apply ONE policy. Dropped events are simply omitted; rating_asof then forward-fills
    the last credible rating across them, so a transient blip never creates a label.
    """
    if policy == "all" or agency in ISSUER_PAID_AGENCIES:
        return evs
    if policy == "big3":
        return []
    # policy == "denoised", agency == EJR.
    kept: list[dict[str, Any]] = []
    last_idx: int | None = None
    n = len(evs)
    for i, e in enumerate(evs):
        idx = e.get("rating_index")
        if e.get("rating_status") == STATUS_DEFAULT:
            kept.append(e)
            last_idx = idx if idx is not None else last_idx
            continue
        if idx is None:                      # withdrawal / not-rated: a status change, not a blip
            kept.append(e)
            continue
        if last_idx is None or idx == last_idx:   # first notch (baseline) or affirmation
            kept.append(e)
            last_idx = idx
            continue
        move = idx - last_idx                 # + = downgrade (worse), - = upgrade
        nxt = next((evs[j] for j in range(i + 1, n) if evs[j].get("rating_index") is not None), None)
        persistent = True
        if nxt is not None:
            nxt_move = nxt["rating_index"] - idx
            reversed_soon = (
                _months_diff(e["effective_date"], nxt["effective_date"]) < persist_months
                and nxt_move != 0 and (move > 0) != (nxt_move > 0)   # opposite direction
            )
            persistent = not reversed_soon
        if abs(move) >= multi_notch or persistent:
            kept.append(e)
            last_idx = idx
        # else: transient single-notch EJR blip → drop (last_idx unchanged)
    return kept


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
    label_policy: str = "all",
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
        evs = credible_events(evs, agency, label_policy)   # denoise per the label policy
        if not evs:                                        # e.g. EJR dropped under "big3"
            continue
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
