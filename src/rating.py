"""
Score → agency-rating calibration (Tier 1, FLAT / non-sector-adjusted).

Maps Freeman's 0–100 credit-stress score to an S&P-style letter-rating bucket
(AA / A / BBB / BB / B / CCC / D), anchored on REAL agency ratings paired with
the model's point-in-time backtest scores.

  • Healthy companies contribute (latest score, current S&P rating).
  • Distressed companies contribute (near-default score, "D").

Boundaries between adjacent rating buckets are placed with the same
"threshold that best separates two groups" logic used in src/calibrate.py
(Youden's J), reused here per adjacent rating-bucket pair.

╔══════════════════════════════════════════════════════════════════════════╗
║ TIER-1 LIMITATION — this is a FLAT mapping.                                ║
║ The same score means different ratings in different sectors (an asset-light║
║ software firm and a capital-heavy utility at score 45 are not equal credit ║
║ risks). This module deliberately ignores sector. It will be SUPERSEDED by  ║
║ a sector-adjusted mapping once the benchmark layer (Tier 2) exists — see   ║
║ the documented extension point score_to_rating_sector_adjusted() below.    ║
╚══════════════════════════════════════════════════════════════════════════╝

ANCHOR GAP (be honest about it): the calibration data has investment-grade
anchors (AA–BBB) from healthy issuers and a large D floor from defaults, but
FEW OR NO anchors for BB / B / CCC. Boundaries touching an empty bucket are
INTERPOLATED across the IG→D gap and flagged "confidence": "low" in the output
artifact — never emitted as if they were anchored.

Outputs (written by main(), reviewed before any use):
  data/rating_calibration.json — ordered cutoffs, each with confidence + anchor count.
This module does NOT touch score.py or the live scoring path.
"""

from __future__ import annotations

import csv
import json
import pathlib
from collections import defaultdict
from typing import Any

# Reuse the exact group-separation primitive from the threshold calibrator.
from src.calibrate import _youden_threshold

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT = pathlib.Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
RATINGS_CSV = ROOT / "ratings_lookup_scaffold.csv"
RESULTS_PATH = DATA_DIR / "backtest_results.json"
CALIBRATION_PATH = DATA_DIR / "rating_calibration.json"

# ── Rating buckets, ordered by INCREASING risk (== increasing score) ───────────
BUCKET_ORDER = ["AA", "A", "BBB", "BB", "B", "CCC", "D"]
BUCKET_ORD = {b: i for i, b in enumerate(BUCKET_ORDER)}

# S&P notch → coarse bucket.
SP_TO_BUCKET = {
    "AAA": "AA", "AA+": "AA", "AA": "AA", "AA-": "AA",
    "A+": "A", "A": "A", "A-": "A",
    "BBB+": "BBB", "BBB": "BBB", "BBB-": "BBB",
    "BB+": "BB", "BB": "BB", "BB-": "BB",
    "B+": "B", "B": "B", "B-": "B",
    "CCC+": "CCC", "CCC": "CCC", "CCC-": "CCC", "CC": "CCC", "C": "CCC",
    "D": "D", "SD": "D",
}
# Moody's notch → same coarse bucket (cross-check only).
MOODY_TO_BUCKET = {
    "Aaa": "AA", "Aa1": "AA", "Aa2": "AA", "Aa3": "AA",
    "A1": "A", "A2": "A", "A3": "A",
    "Baa1": "BBB", "Baa2": "BBB", "Baa3": "BBB",
    "Ba1": "BB", "Ba2": "BB", "Ba3": "BB",
    "B1": "B", "B2": "B", "B3": "B",
    "Caa1": "CCC", "Caa2": "CCC", "Caa3": "CCC", "Ca": "CCC", "C": "CCC",
    "D": "D",
}

MIN_ANCHORS = 3  # a boundary is "high" confidence only if both sides have ≥ this


def _map_sp(sp: str) -> str | None:
    sp = (sp or "").strip()
    if not sp or sp.lower() == "unrated":
        return None
    return SP_TO_BUCKET.get(sp)


def _map_moody(m: str) -> str | None:
    return MOODY_TO_BUCKET.get((m or "").strip())


def _score_for_case(case: dict) -> float | None:
    """
    The single score paired with a company's rating.

    Healthy → latest score; distressed → score nearest the event date (near-default
    peak). Both reduce to "the snapshot with the smallest months_before_event that
    has data" (trajectory is newest-first; the anchor/event sits at mbe≈0).
    """
    snaps = [s for s in case.get("trajectory", []) if s.get("has_data")]
    if not snaps:
        return None
    return min(snaps, key=lambda s: s["months_before_event"])["score"]


# ── Step 1: build (score, rating) pairs ────────────────────────────────────────

def load_pairs(ratings_csv: pathlib.Path | str = RATINGS_CSV,
               results_path: pathlib.Path | str = RESULTS_PATH) -> tuple[list[dict], list[dict]]:
    """
    Join the ratings CSV to the backtest scores on CIK (both 10-digit zero-padded).
    Returns (pairs, skipped) where each pair is
        {cik, name, bucket, sp, moody_bucket, score, label}.
    """
    ratings = {r["cik"]: r for r in csv.DictReader(open(ratings_csv))}
    results = json.loads(pathlib.Path(results_path).read_text())
    cases = {c["cik"]: c for c in results["cases"]}

    pairs: list[dict] = []
    skipped: list[dict] = []
    for cik, r in ratings.items():
        bucket = _map_sp(r.get("sp_rating", ""))
        if bucket is None:
            skipped.append({"name": r.get("company_name"), "reason": "blank/unrated sp_rating"})
            continue
        case = cases.get(cik)
        if case is None:
            skipped.append({"name": r.get("company_name"), "reason": "no backtest case"})
            continue
        score = _score_for_case(case)
        if score is None:
            skipped.append({"name": r.get("company_name"), "reason": "no scored snapshot (data-gap)"})
            continue
        pairs.append({
            "cik": cik,
            "name": r.get("company_name"),
            "bucket": bucket,
            "sp": r.get("sp_rating", "").strip(),
            "moody_bucket": _map_moody(r.get("moody_rating", "")),
            "score": float(score),
            "label": r.get("label", "").strip().lower(),
        })
    return pairs, skipped


# ── Step 2/3: derive monotonic cutoffs with confidence flags ───────────────────

def _bucket_scores(pairs: list[dict]) -> dict[str, list[float]]:
    by = defaultdict(list)
    for p in pairs:
        by[p["bucket"]].append(p["score"])
    return {b: sorted(v) for b, v in by.items()}

def derive_cutoffs(pairs: list[dict]) -> dict:
    """
    Build ascending score thresholds for the 6 adjacent-bucket boundaries.

    Directly anchored boundaries (both adjacent buckets have data) use Youden's J
    to find the best separating score. Boundaries that touch an EMPTY bucket
    (BB/B/CCC have no anchors) are interpolated evenly across the IG→D gap and
    flagged low confidence. A boundary is "high" confidence only when BOTH sides
    carry ≥ MIN_ANCHORS companies.
    """
    by = _bucket_scores(pairs)

    def n(b: str) -> int:
        return len(by.get(b, []))

    def youden_between(lo_bucket: str, hi_bucket: str) -> float | None:
        lo, hi = by.get(lo_bucket), by.get(hi_bucket)
        if not lo or not hi:
            return None
        # higher score == higher risk: predict the HIGHER-risk bucket if score >= t.
        t, _, _ = _youden_threshold(hi, lo, higher_worse=True)
        return t

    boundaries = [(BUCKET_ORDER[i], BUCKET_ORDER[i + 1]) for i in range(len(BUCKET_ORDER) - 1)]

    # 1) Direct Youden thresholds where both sides have data.
    direct: dict[tuple, float] = {}
    for lo, hi in boundaries:
        t = youden_between(lo, hi)
        if t is not None:
            direct[(lo, hi)] = t

    # 2) Anchor the default entry with the IG-vs-D split (strongly supported:
    #    all IG anchors vs all D anchors), used to pin the top of the gap.
    ig_scores = [s for b in ("AA", "A", "BBB") for s in by.get(b, [])]
    d_scores = by.get("D", [])
    ig_d_split = None
    if ig_scores and d_scores:
        ig_d_split, _, _ = _youden_threshold(d_scores, ig_scores, higher_worse=True)

    # 3) Lower edge of the interpolation gap = last directly-anchored IG boundary.
    last_ig = direct.get(("A", "BBB")) or direct.get(("AA", "A"))
    gap_lo = last_ig if last_ig is not None else (max(ig_scores) if ig_scores else 0.0)
    gap_hi = ig_d_split if ig_d_split is not None else (gap_lo + 1.0)

    # The 4 boundaries spanning BBB→D (BBB|BB, BB|B, B|CCC, CCC|D) — interpolate
    # evenly; CCC|D lands on the anchored IG-vs-D split.
    gap_boundaries = [("BBB", "BB"), ("BB", "B"), ("B", "CCC"), ("CCC", "D")]
    span = gap_hi - gap_lo
    interp: dict[tuple, float] = {}
    for k, pair in enumerate(gap_boundaries, start=1):
        interp[pair] = gap_lo + span * (k / len(gap_boundaries))

    # 4) Assemble ascending, enforce strict monotonicity, attach confidence.
    out: list[dict] = []
    prev = float("-inf")
    EPS = 0.01
    for lo, hi in boundaries:
        if (lo, hi) in direct:
            thr = direct[(lo, hi)]
            anchored = True
        else:
            thr = interp[(lo, hi)]
            anchored = False
        # monotonic clamp
        if thr <= prev:
            thr = prev + EPS
        prev = thr

        lo_n, hi_n = n(lo), n(hi)
        # High confidence only when both adjacent buckets are themselves anchored.
        high = anchored and lo_n >= MIN_ANCHORS and hi_n >= MIN_ANCHORS
        if high:
            conf, note = "high", f"anchored (Youden); n_{lo}={lo_n}, n_{hi}={hi_n}"
        elif anchored:
            conf, note = "low", f"anchored but thin (n_{lo}={lo_n}, n_{hi}={hi_n} < {MIN_ANCHORS})"
        elif (lo, hi) == ("CCC", "D"):
            conf, note = "low", (f"interpolated — no CCC anchors; D-entry pinned to IG-vs-D "
                                 f"split (n_D={n('D')})")
        else:
            conf, note = "low", f"interpolated — no anchors for {lo}/{hi}"

        out.append({
            "between": [lo, hi],
            "threshold": round(thr, 2),
            "confidence": conf,
            "anchor_count": lo_n + hi_n,
            "anchors_lo": lo_n,
            "anchors_hi": hi_n,
            "note": note,
        })

    return {
        "buckets": BUCKET_ORDER,
        "boundaries": out,
        "ig_vs_d_split": round(ig_d_split, 2) if ig_d_split is not None else None,
        "bucket_anchor_counts": {b: n(b) for b in BUCKET_ORDER},
    }


# ── score → rating (reads the artifact) ─────────────────────────────────────────

def _bucket_for_score(score: float, boundaries: list[dict]) -> str:
    """Walk ascending thresholds; the first boundary above `score` names the bucket."""
    for b in boundaries:
        if score < b["threshold"]:
            return b["between"][0]
    return boundaries[-1]["between"][1]  # above the last threshold → most-risky bucket


def score_to_rating(score: float, calibration_path: pathlib.Path | str = CALIBRATION_PATH) -> dict:
    """
    Map a credit-stress score to a letter-rating bucket (FLAT Tier-1 mapping).

    Returns {"rating": "BBB", "confidence": "high"|"low", "score": score}.

    Confidence is bucket-level: "high" for buckets with ≥3 real anchors
    (AA / A / BBB / D), "low" for the interpolated middle (BB / B / CCC) that has
    no agency anchors in the calibration set. See module docstring for the
    sector-adjustment caveat — the same score maps to different real ratings
    across sectors, which this flat mapping cannot express.
    """
    calib = json.loads(pathlib.Path(calibration_path).read_text())
    boundaries = calib["boundaries"]
    rating = _bucket_for_score(float(score), boundaries)
    anchored_buckets = {b for b, c in calib["bucket_anchor_counts"].items() if c >= MIN_ANCHORS}
    confidence = "high" if rating in anchored_buckets else "low"
    return {"rating": rating, "confidence": confidence, "score": float(score)}


# ── Tier 2 extension point (NOT built) ──────────────────────────────────────────

def score_to_rating_sector_adjusted(score: float, sector_group: str,
                                     calibration_path: pathlib.Path | str = CALIBRATION_PATH) -> dict:
    """
    TIER 2 (NOT IMPLEMENTED) — sector-relative score→rating mapping.

    Would shift the flat Tier-1 cutoffs per `sector_group` using the benchmark
    layer (peer-group score distributions), because the same score implies a
    different rating in, e.g., asset-light software vs capital-heavy utilities.
    Depends on the benchmark layer, which does not exist yet.
    """
    raise NotImplementedError(
        "Tier 2 sector-adjusted score→rating requires the benchmark layer (not built). "
        "Adjust the Tier-1 cutoffs from score_to_rating per sector_group here once "
        "peer-group score distributions exist."
    )


# ── Step 4: validation (deterministic holdout) ─────────────────────────────────

def validate(pairs: list[dict], holdout_every: int = 5) -> dict:
    """
    Deterministic ~20% holdout (every Nth company by CIK — reproducible, no RNG).
    Derive cutoffs on the train split, map the holdout, and report bucket agreement.
    """
    ordered = sorted(pairs, key=lambda p: p["cik"])
    holdout = [p for i, p in enumerate(ordered) if i % holdout_every == 0]
    train = [p for i, p in enumerate(ordered) if i % holdout_every != 0]

    calib = derive_cutoffs(train)
    boundaries = calib["boundaries"]

    rows, exact, within1, big = [], 0, 0, []
    for p in holdout:
        pred = _bucket_for_score(p["score"], boundaries)
        dist = abs(BUCKET_ORD[pred] - BUCKET_ORD[p["bucket"]])
        if dist == 0:
            exact += 1
        if dist <= 1:
            within1 += 1
        moody_disagrees = p["moody_bucket"] is not None and p["moody_bucket"] != p["bucket"]
        if dist >= 2:
            big.append({"name": p["name"], "score": round(p["score"], 1),
                        "predicted": pred, "sp": p["bucket"], "notches": dist,
                        "moody_bucket": p["moody_bucket"], "moody_disagrees": moody_disagrees})
        rows.append({"name": p["name"], "score": round(p["score"], 1),
                     "predicted": pred, "sp": p["bucket"], "dist": dist})

    n = len(holdout)
    return {
        "holdout_n": n,
        "train_n": len(train),
        "exact_match": exact,
        "exact_pct": round(100 * exact / n, 1) if n else 0.0,
        "within_one_notch": within1,
        "within_one_pct": round(100 * within1 / n, 1) if n else 0.0,
        "big_misses": big,
        "rows": rows,
    }


# ── CLI: run the full analysis, write artifact, print report ────────────────────

def main() -> int:
    pairs, skipped = load_pairs()
    by = _bucket_scores(pairs)
    calib = derive_cutoffs(pairs)
    val = validate(pairs)

    # Cross-check: where S&P and Moody's disagree on a company (widens the band).
    sp_moody_disagree = [
        {"name": p["name"], "sp": p["bucket"], "moody": p["moody_bucket"], "score": round(p["score"], 1)}
        for p in pairs
        if p["moody_bucket"] is not None and p["moody_bucket"] != p["bucket"]
    ]

    artifact = {
        "_meta": {
            "tier": 1,
            "kind": "flat_non_sector_adjusted",
            "supersede_with": "score_to_rating_sector_adjusted (Tier 2, needs benchmark layer)",
            "pairs_used": len(pairs),
            "skipped": skipped,
            "min_anchors_for_high_confidence": MIN_ANCHORS,
            "note": "Score→rating cutoffs anchored on real S&P ratings. BB/B/CCC have no "
                    "anchors → those boundaries are interpolated and flagged low confidence.",
        },
        "buckets": calib["buckets"],
        "bucket_anchor_counts": calib["bucket_anchor_counts"],
        "ig_vs_d_split": calib["ig_vs_d_split"],
        "boundaries": calib["boundaries"],
        "validation": {
            "holdout_n": val["holdout_n"], "train_n": val["train_n"],
            "exact_pct": val["exact_pct"], "within_one_pct": val["within_one_pct"],
        },
    }
    CALIBRATION_PATH.write_text(json.dumps(artifact, indent=2))

    # ── Report ────────────────────────────────────────────────────────────────
    print(f"\nStep 1 — pairs built: {len(pairs)}   skipped: {len(skipped)}")
    for s in skipped:
        print(f"   skip: {s['name']} ({s['reason']})")
    print(f"\n{'bucket':<6}{'n':>4}{'min':>8}{'median':>9}{'max':>8}")
    import statistics
    for b in BUCKET_ORDER:
        v = by.get(b, [])
        if v:
            print(f"{b:<6}{len(v):>4}{min(v):>8.1f}{statistics.median(v):>9.1f}{max(v):>8.1f}")
        else:
            print(f"{b:<6}{0:>4}{'—':>8}{'—':>9}{'—':>8}  (no anchors)")

    print("\nStep 2/3 — derived cutoffs (ascending score → bucket):")
    print(f"   {'boundary':<12}{'thr':>8}  {'confidence':<6}  anchors(lo/hi)  note")
    for b in calib["boundaries"]:
        lo, hi = b["between"]
        print(f"   {lo+'|'+hi:<12}{b['threshold']:>8.2f}  {b['confidence']:<6}  "
              f"{b['anchors_lo']}/{b['anchors_hi']:<10}  {b['note']}")
    print(f"   (IG-vs-D anchored split = {calib['ig_vs_d_split']})")

    # human-readable score→bucket ranges
    print("\n   resulting score→rating ranges:")
    prev = 0.0
    for b in calib["boundaries"]:
        lo = b["between"][0]
        print(f"      {lo:<4} : {prev:>6.2f} – {b['threshold']:.2f}   [{b['confidence']}]")
        prev = b["threshold"]
    print(f"      {calib['boundaries'][-1]['between'][1]:<4} : {prev:>6.2f} – 100.00")

    print(f"\nStep 4 — validation ({val['holdout_n']} holdout / {val['train_n']} train, deterministic 20%):")
    print(f"   exact-bucket match : {val['exact_match']}/{val['holdout_n']} ({val['exact_pct']}%)")
    print(f"   within-one-notch   : {val['within_one_notch']}/{val['holdout_n']} ({val['within_one_pct']}%)")
    if val["big_misses"]:
        print(f"   ≥2-bucket disagreements ({len(val['big_misses'])}):")
        for m in val["big_misses"]:
            md = f"  [Moody's={m['moody_bucket']} — disagrees, widens band]" if m["moody_disagrees"] else ""
            print(f"      {m['name']}: score {m['score']} → predicted {m['predicted']} vs S&P {m['sp']} "
                  f"({m['notches']} notches){md}")
    else:
        print("   ≥2-bucket disagreements: none")

    if sp_moody_disagree:
        print(f"\nS&P vs Moody's disagreements across all pairs ({len(sp_moody_disagree)}):")
        for d in sp_moody_disagree:
            print(f"   {d['name']}: S&P={d['sp']}  Moody's={d['moody']}  (score {d['score']})")
    else:
        print("\nS&P vs Moody's: no bucket-level disagreements among paired companies")

    print(f"\nArtifact written: {CALIBRATION_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
