#!/usr/bin/env python3
"""
experiments/build_sector_routing.py — STANDALONE, READ-ONLY (writes data/sector_routing.csv).

Materialize the sector-routing table for the sector-gated fixes, using Freeman's
cloned methodology classifier as the router. For every DISTINCT company in
sample_sec_financials.csv (cik+sic) we call classify_methodology(... use_llm=False)
— the deterministic SIC-table path: offline, reproducible, no LLM key, no network.

Import path: the classifier lives at ./joywin-methodology-classifier (a sibling
package inside this repo). We make it importable via sys.path.insert on that folder
— NOT `pip install -e` — to keep this non-invasive (no environment mutation, matches
the sys.path.insert(ROOT) idiom the other experiments use). If it were installed as a
dependency later, this shim is harmless.

Note on `methodology_source`: with use_llm=False every row is `sic_table` (clean SIC
hit) or `default` (no/ambiguous SIC) — never `llm_hybrid`. The LLM confirm/override
step is intentionally off here; `low`-confidence rows are exactly the ambiguous SICs
that an --llm run would resolve, and they are the analyst-review queue.

Run:  python experiments/build_sector_routing.py
Writes data/sector_routing.csv + prints the distribution. No commits.
"""
from __future__ import annotations

import csv
import pathlib
import subprocess
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLASSIFIER_DIR = ROOT / "joywin-methodology-classifier"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CLASSIFIER_DIR))   # make the classifier importable

from methodology_classifier import classify_methodology  # noqa: E402


def classifier_commit() -> str:
    """Full git commit hash of the classifier repo — provenance stamped on every row
    so the routing table is traceable to the exact classifier version that produced it."""
    try:
        return subprocess.run(
            ["git", "-C", str(CLASSIFIER_DIR), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"

SRC = ROOT / "sample_sec_financials.csv"
OUT = ROOT / "data" / "sector_routing.csv"

_REPORT: list[str] = []
_raw = print
def emit(*a):
    s = " ".join(str(x) for x in a); _raw(s, flush=True); _REPORT.append(s)


def load_distinct_companies():
    """cik(zero-padded 10) → {name, sic, sic_description}, first occurrence per cik."""
    out: dict[str, dict] = {}
    with open(SRC, newline="") as f:
        for r in csv.DictReader(f):
            cik = (r.get("cik") or "").strip().zfill(10)
            if not cik or cik == "0000000000":
                continue
            if cik in out:
                continue
            out[cik] = {
                "name": (r.get("company_name") or "").strip(),
                "sic": (r.get("sic") or "").strip(),
                "sic_description": (r.get("sic_description") or "").strip(),
            }
    return out


def main() -> int:
    companies = load_distinct_companies()
    commit = classifier_commit()
    emit("=" * 96)
    emit("SECTOR ROUTING TABLE — deterministic SIC classification (use_llm=False, offline)")
    emit("=" * 96)
    emit(f"distinct companies in {SRC.name}: {len(companies)}")
    emit(f"classifier commit (provenance, stamped per row): {commit}")

    rows = []
    for cik, c in sorted(companies.items()):
        mc = classify_methodology(
            sic=c["sic"] or None,
            sic_description=c["sic_description"] or None,
            name=c["name"] or None,
            use_llm=False,
        )
        rows.append({
            "cik": cik,
            "name": c["name"],
            "sic": c["sic"],
            "moodys_methodology": mc.moodys_methodology,
            "sp_sector": mc.sp_sector,
            "methodology_confidence": mc.confidence,
            "methodology_source": mc.source,
            "is_financial": mc.is_financial,
            "notes": mc.notes,
            "classifier_commit": commit,   # provenance: classifier version that produced this row
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    emit(f"wrote {OUT.relative_to(ROOT)}  ({len(rows)} rows)")

    # ── distribution ──────────────────────────────────────────────────────────
    conf = Counter(r["methodology_confidence"] for r in rows)
    src = Counter(r["methodology_source"] for r in rows)
    fin = sum(1 for r in rows if r["is_financial"])
    emit("\n── confidence tally ──")
    for k in ("high", "medium", "low"):
        emit(f"   {k:<7} {conf.get(k,0):>4}  ({100*conf.get(k,0)/len(rows):.0f}%)")
    emit(f"   source: " + ", ".join(f"{k}={v}" for k, v in src.most_common()))
    emit(f"\nis_financial == True: {fin} companies")

    emit("\n── top 15 Moody's buckets ──")
    for bucket, n in Counter(r["moodys_methodology"] for r in rows).most_common(15):
        emit(f"   {n:>4}  {bucket}")

    low = [r for r in rows if r["methodology_confidence"] == "low"]
    emit(f"\n── LOW-confidence rows (analyst-review queue): {len(low)} ──")
    for r in sorted(low, key=lambda r: r["name"]):
        emit(f"   {r['name'][:34]:<36} sic={r['sic']:<6} M={r['moodys_methodology']!r} src={r['methodology_source']} fin={r['is_financial']}")

    # ── sanity checks on known cases ──────────────────────────────────────────
    emit("\n" + "=" * 96)
    emit("SANITY CHECKS (known cases)")
    emit("=" * 96)
    by_cik = {r["cik"]: r for r in rows}
    def find(substr):
        for r in rows:
            if substr.upper() in r["name"].upper():
                return r
        return None
    checks = [
        ("AMERICAN ELECTRIC", "moodys_methodology", "Regulated Electric and Gas Utilities"),
        ("AUTOMATIC DATA",    "moodys_methodology", "Software"),
        ("SKYWORKS",          "moodys_methodology", "Semiconductors"),
        ("BRISTOL",           "moodys_methodology", "Pharmaceuticals"),
        ("JPMORGAN",          "is_financial",       True),
        ("BERKLEY",           "is_financial",       True),
        ("AFLAC",             "is_financial",       True),
    ]
    for substr, field, expected in checks:
        r = find(substr)
        if r is None:
            emit(f"   ? {substr:<20} NOT in table"); continue
        got = r[field]
        ok = "OK " if str(got) == str(expected) else "!! "
        emit(f"   {ok}{r['name'][:26]:<28} {field}={got!r}  (expected {expected!r})  [conf={r['methodology_confidence']}]")
    # report-only (bucket varies): Caleres/Apogee (retail/industrial), U-Haul (mixed-filer)
    for substr in ("CALERES", "APOGEE", "U-HAUL", "U-HAUL HOLDING", "AMERCO"):
        r = find(substr)
        if r:
            emit(f"   -- {r['name'][:26]:<28} M={r['moodys_methodology']!r} fin={r['is_financial']} conf={r['methodology_confidence']}  (report-only)")

    (ROOT / "experiments" / "build_sector_routing_report.txt").write_text("\n".join(_REPORT) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
