"""
Rebuild the backtest case library programmatically from the agency-rating universe.

The hand-curated data/cases.csv was thin and skewed toward defaults/HY. This script
regenerates it by sampling real rating events from data/agency_ratings.csv, RESTRICTED
to issuers that already have ingested financials (so cases are scorable, not data_gap),
with extra weight on the two under-represented pools the model is weakest on:

  • BBB-band UPGRADES        — issuer rising from BBB+/BBB/BBB- (start index 7–9)
  • AAA/AA/A-band DOWNGRADES  — issuer slipping from AAA…A- (start index 0–6)

plus distress/default transitions, a few generic up/downgrades, and stable-IG controls.

Eligibility = the CIKs in build_scoring_matrix() (issuers with ingested fundamentals).
Each event-case must also have at least `--min-snapshots` point-in-time scoring periods
within the 24-month pre-event window (matching the backtest's max-lead cap), so the
model actually has something to score.

Writes data/cases.csv (same schema as before). Run `python3 -m scripts.seed_cases`
afterward to make the Supabase `cases` table authoritative.

Usage:
    python3 -m scripts.rebuild_cases                       # default composition
    python3 -m scripts.rebuild_cases --bbb-upgrades 16 --senior-downgrades 16
    python3 -m scripts.rebuild_cases --dry-run             # print, don't write
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import random
from collections import defaultdict
from typing import Any

from src.rating import RATING_SCALE, rating_index
from src.ratings.labels import add_months
from src.ratings.scale import DISTRESS_INDEX

RATINGS_CSV = pathlib.Path(__file__).resolve().parent.parent / "data" / "agency_ratings.csv"
CASES_CSV = pathlib.Path(__file__).resolve().parent.parent / "data" / "cases.csv"

CASE_COLUMNS = ["case_id", "company_name", "ticker", "cik", "label",
                "event_type", "agency", "event_date", "notes"]

# Rating-band index boundaries (RATING_SCALE: 0=AAA … 21=D).
A_BAND_MAX = rating_index("A-")     # 6  — AAA/AA/A band is indices 0..6
BBB_BAND_MIN = rating_index("BBB+")  # 7
BBB_BAND_MAX = rating_index("BBB-")  # 9  — BBB band is indices 7..9
MAX_LEAD_MONTHS = 24                 # mirrors migration_backtest.DEFAULT_MAX_LEAD_MONTHS


def _letter(idx: int | None) -> str:
    return RATING_SCALE[idx] if idx is not None and 0 <= idx < len(RATING_SCALE) else "?"


def _scoring_periods_by_cik() -> dict[str, list[str]]:
    """cik → sorted distinct period_end dates the model can score (has financials)."""
    from src.model.features import build_scoring_matrix
    df = build_scoring_matrix()
    out: dict[str, set[str]] = defaultdict(set)
    for cik, period in zip(df["cik"].astype(str).str.zfill(10), df["period_end"].astype(str)):
        out[cik].add(period)
    return {cik: sorted(p) for cik, p in out.items()}


def _events_by_issuer() -> dict[tuple[str, str], list[dict[str, Any]]]:
    """(cik, agency) → its rating events sorted ascending by effective_date."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    with open(RATINGS_CSV, newline="") as f:
        for r in csv.DictReader(f):
            cik = (r.get("cik") or "").zfill(10)
            agency = r.get("agency") or ""
            ri = r.get("rating_index")
            r["rating_index"] = int(round(float(ri))) if (ri not in (None, "")) else None
            r["cik"] = cik
            by_key[(cik, agency)].append(r)
    for evs in by_key.values():
        evs.sort(key=lambda e: e["effective_date"])
    return by_key


def _classify(prev_idx: int | None, new_idx: int | None, status: str) -> tuple[str, str] | None:
    """Map an event to (event_type, pool), or None if it's not a usable case.

    event_type is the case column (downgrade/upgrade/default); pool drives sampling.
    A transition into the distress tail (CCC+ or default) is a `default` case — it's
    what the model's distress head targets.
    """
    if status == "default" or (new_idx is not None and new_idx >= DISTRESS_INDEX):
        return ("default", "default")
    if prev_idx is None or new_idx is None:
        return None
    if new_idx > prev_idx:  # higher index = worse rating → downgrade
        if prev_idx <= A_BAND_MAX:
            return ("downgrade", "senior_downgrade")
        return ("downgrade", "other_downgrade")
    if new_idx < prev_idx:  # upgrade
        if BBB_BAND_MIN <= prev_idx <= BBB_BAND_MAX:
            return ("upgrade", "bbb_upgrade")
        return ("upgrade", "other_upgrade")
    return None  # affirm / no notch change


def _has_window_snapshots(periods: list[str], event_date: str, min_snapshots: int) -> bool:
    """True if ≥ min_snapshots scorable periods fall in [event-24m, event)."""
    earliest = add_months(event_date, -MAX_LEAD_MONTHS)
    n = sum(1 for p in periods if earliest <= p < event_date)
    return n >= min_snapshots


# Issuer-paid agencies (fundamentally anchored). EJR is investor-paid and faster — it
# nicks pristine names one notch on market signals and rates cash-rich growth names
# CCC, neither of which a FUNDAMENTALS model can be expected to predict. So a case is a
# fair test only if it is a real default, a big3 (issuer-paid) action, or a MULTI-NOTCH
# EJR move (a one-notch EJR blip / a `?→CCC` cold-start is not). Persistence can't be
# used here — EJR coverage is too sparse to observe reversals (see labels.credible_events).
_ISSUER_PAID = {"MDY", "FTC", "SPI"}


def _is_credible_case(prev_idx: int | None, new_idx: int | None, agency: str, status: str) -> bool:
    """Whether a rating event is a fundamentally-meaningful case (vs EJR sentiment)."""
    if status == "default":
        return True                       # a real default is always a valid case
    if agency in _ISSUER_PAID:
        return True                       # issuer-paid action — keep regardless of notch
    if prev_idx is None or new_idx is None:
        return False                      # EJR cold-start (e.g. ?→CCC on a healthy name)
    return abs(new_idx - prev_idx) >= 2   # EJR: require a multi-notch move


def _build_candidates(events_by_issuer, periods_by_cik, min_snapshots):
    """Pool name → list of candidate event-case dicts (eligible + scorable)."""
    pools: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (cik, agency), evs in events_by_issuer.items():
        periods = periods_by_cik.get(cik)
        if not periods:
            continue  # no ingested financials → not eligible
        prev_idx: int | None = None
        for e in evs:
            status = e.get("rating_status") or ""
            new_idx = e["rating_index"]
            verdict = _classify(prev_idx, new_idx, status)
            prev_for_event = prev_idx
            prev_idx = new_idx if new_idx is not None else prev_idx
            if verdict is None:
                continue
            # Drop EJR sentiment cases the fundamentals model can't be expected to catch.
            if not _is_credible_case(prev_for_event, new_idx, agency, status):
                continue
            event_type, pool = verdict
            event_date = e["effective_date"]
            if not (e.get("ticker") or "").strip():
                continue  # need a ticker for a readable case_id / display
            if not _has_window_snapshots(periods, event_date, min_snapshots):
                continue
            pools[pool].append({
                "cik": cik, "agency": agency,
                "ticker": (e.get("ticker") or "").upper(),
                "company_name": e.get("company_name") or "",
                "event_type": event_type, "event_date": event_date,
                "prev_idx": prev_for_event, "new_idx": new_idx,
                "n_snapshots": sum(1 for p in periods
                                   if add_months(event_date, -MAX_LEAD_MONTHS) <= p < event_date),
            })
    return pools


CONTROL_MAX_GAP = 2   # max |implied − agency| notches for a control (fundamentals must fit the rating)


def _build_controls(events_by_issuer, periods_by_cik, min_snapshots, implied_by_cik=None):
    """Stable investment-grade issuers (no migration ever) as healthy controls."""
    implied_by_cik = implied_by_cik or {}
    controls: list[dict[str, Any]] = []
    # Per cik, pick the agency timeline with the most events.
    best: dict[str, tuple[str, list[dict]]] = {}
    for (cik, agency), evs in events_by_issuer.items():
        if cik not in best or len(evs) > len(best[cik][1]):
            best[cik] = (agency, evs)
    for cik, (agency, evs) in best.items():
        periods = periods_by_cik.get(cik)
        if not periods or len(periods) < min_snapshots:
            continue
        if not (evs[-1].get("ticker") or "").strip():
            continue
        idxs = [e["rating_index"] for e in evs if e["rating_index"] is not None]
        statuses = {e.get("rating_status") for e in evs}
        # A control is an OBVIOUSLY healthy name — the precision check the FP rate is
        # supposed to measure. Require A-band or better (AAA…A-) for the whole history,
        # never defaulted, and tight (≤ 2 notches of drift). Looser "any IG" controls
        # include weaker BBB credits the model can legitimately flag, which makes the
        # FP rate read as noise rather than a real false-alarm signal.
        if (not idxs or max(idxs) > A_BAND_MAX or "default" in statuses
                or (max(idxs) - min(idxs)) > 2):
            continue
        anchor = periods[-1]  # most recent scorable period as the control anchor
        # The model now uses implied_vs_agency_gap, so a name whose FUNDAMENTALS imply a
        # much worse rating than the agency assigns (e.g. EJR rates it AA- but the
        # implied rating is BBB+) is one the model SHOULD flag — it isn't a fair
        # "healthy control." Require the implied rating at the anchor to be within
        # CONTROL_MAX_GAP notches of the agency rating (when an implied rating exists).
        agency_at_anchor = next((e["rating_index"] for e in reversed(evs)
                                 if e.get("rating_index") is not None and e["effective_date"] <= anchor), None)
        implied_at_anchor = (implied_by_cik.get(cik, {}).get(anchor) or {}).get("rating_index")
        if (implied_at_anchor is not None and agency_at_anchor is not None
                and abs(implied_at_anchor - agency_at_anchor) > CONTROL_MAX_GAP):
            continue
        controls.append({
            "cik": cik, "agency": agency,
            "ticker": (evs[-1].get("ticker") or "").upper(),
            "company_name": evs[-1].get("company_name") or "",
            "event_type": "control", "event_date": anchor,
            "prev_idx": min(idxs), "new_idx": max(idxs), "n_snapshots": len(periods),
        })
    return controls


def _dedup_by_cik(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one case per issuer — the event with the most scorable snapshots."""
    best: dict[str, dict[str, Any]] = {}
    for c in cands:
        cur = best.get(c["cik"])
        if cur is None or c["n_snapshots"] > cur["n_snapshots"]:
            best[c["cik"]] = c
    return list(best.values())


def _sample(pool: list[dict[str, Any]], n: int, rng: random.Random, used: set[str]):
    """Pick up to n cases from a pool, skipping issuers already used (cross-pool)."""
    avail = [c for c in _dedup_by_cik(pool) if c["cik"] not in used]
    # Deterministic order, then shuffle with the seeded rng for variety.
    avail.sort(key=lambda c: (-c["n_snapshots"], c["cik"], c["event_date"]))
    rng.shuffle(avail)
    chosen = avail[:n]
    for c in chosen:
        used.add(c["cik"])
    return chosen


def _to_case_row(c: dict[str, Any], used_ids: set[str]) -> dict[str, str]:
    et = c["event_type"]
    label = "healthy" if et in ("upgrade", "control") else "distressed"
    year = c["event_date"][:4]
    base_id = f"{(c['ticker'] or c['cik']).lower()}-{year}"
    case_id = base_id
    i = 2
    while case_id in used_ids:
        case_id = f"{base_id}-{i}"
        i += 1
    used_ids.add(case_id)
    prev_l, new_l = _letter(c.get("prev_idx")), _letter(c.get("new_idx"))
    if et == "control":
        notes = f"{c['agency']} · stable IG {new_l}–{prev_l}"
    elif et == "default":
        notes = f"{c['agency']} {prev_l}→{new_l} (distress/default transition)"
    elif et == "downgrade":
        kind = "A-band downgrade" if (c.get("prev_idx") if c.get("prev_idx") is not None else 99) <= A_BAND_MAX else "downgrade"
        notes = f"{c['agency']} {prev_l}→{new_l} ({kind})"
    else:  # upgrade
        kind = "BBB-band upgrade" if BBB_BAND_MIN <= (c.get("prev_idx") if c.get("prev_idx") is not None else -1) <= BBB_BAND_MAX else "upgrade"
        notes = f"{c['agency']} {prev_l}→{new_l} ({kind})"
    return {
        "case_id": case_id, "company_name": c["company_name"], "ticker": c["ticker"],
        "cik": c["cik"], "label": label, "event_type": et,
        "agency": c["agency"], "event_date": c["event_date"], "notes": notes,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Rebuild the backtest case library")
    ap.add_argument("--bbb-upgrades", type=int, default=14, help="BBB-band upgrade cases")
    ap.add_argument("--senior-downgrades", type=int, default=14, help="AAA/AA/A-band downgrade cases")
    ap.add_argument("--defaults", type=int, default=10, help="distress/default cases")
    ap.add_argument("--other-upgrades", type=int, default=6)
    ap.add_argument("--other-downgrades", type=int, default=6)
    ap.add_argument("--controls", type=int, default=10, help="stable-IG control cases")
    ap.add_argument("--min-snapshots", type=int, default=2,
                    help="min scorable periods within 24m before the event")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true", help="print the roster, don't write")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    print("Loading scoring universe (issuers with ingested financials)…")
    periods_by_cik = _scoring_periods_by_cik()
    print(f"  eligible issuers: {len(periods_by_cik)}")
    events_by_issuer = _events_by_issuer()
    pools = _build_candidates(events_by_issuer, periods_by_cik, args.min_snapshots)
    # Implied ratings (per cik→period) so controls can be screened on the implied-vs-
    # agency gap the model now keys off — a name whose fundamentals badly lag its rating
    # isn't a fair "healthy control."
    from src.store import get_implied_ratings_grouped
    implied_by_cik = get_implied_ratings_grouped()
    controls = _build_controls(events_by_issuer, periods_by_cik, args.min_snapshots, implied_by_cik)
    print("  candidate pools:", {k: len(_dedup_by_cik(v)) for k, v in pools.items()},
          "controls:", len(_dedup_by_cik(controls)))

    used: set[str] = set()
    targets = [
        ("bbb_upgrade", args.bbb_upgrades),
        ("senior_downgrade", args.senior_downgrades),
        ("default", args.defaults),
        ("other_downgrade", args.other_downgrades),
        ("other_upgrade", args.other_upgrades),
    ]
    selected: list[dict[str, Any]] = []
    for pool_name, n in targets:
        picked = _sample(pools.get(pool_name, []), n, rng, used)
        selected.extend(picked)
        print(f"  {pool_name}: requested {n}, got {len(picked)}")
    picked_controls = _sample(controls, args.controls, rng, used)
    selected.extend(picked_controls)
    print(f"  controls: requested {args.controls}, got {len(picked_controls)}")

    # Stable, readable ordering: distressed first (default, downgrade), then healthy.
    order = {"default": 0, "downgrade": 1, "upgrade": 2, "control": 3}
    selected.sort(key=lambda c: (order.get(c["event_type"], 9), c["event_date"], c["ticker"]))
    used_ids: set[str] = set()
    rows = [_to_case_row(c, used_ids) for c in selected]

    print(f"\nTotal cases: {len(rows)}")
    if args.dry_run:
        for r in rows:
            print(f"  {r['case_id']:<20} {r['ticker']:<6} {r['event_type']:<9} "
                  f"{r['agency']:<4} {r['event_date']}  {r['notes']}")
        return

    with open(CASES_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CASE_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} cases → {CASES_CSV}")
    print("Next: python3 -m scripts.seed_cases  (makes the Supabase cases table authoritative)")


if __name__ == "__main__":
    main()
