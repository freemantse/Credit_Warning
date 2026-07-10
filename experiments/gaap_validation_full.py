#!/usr/bin/env python3
"""
experiments/gaap_validation_full.py — STANDALONE, READ-ONLY.

Full GAAP-input validation harness: every cached LSEG-panel company (71/74), every
fiscal year the panel and our XBRL both cover, with the 3-tier field classification
baked in. Scales the pilot (experiments/gaap_validation_pilot.py) and reuses its
machinery: the concept resolvers / building-block functions from src.extract
(_resolve_first_opt, gross_debt, capex_total, _get_available_periods), the
exact-then-nearest(±10d) period join, unit-scale detection, and the emit/REPORT_TXT
idiom. Values are read via the SAME resolvers the ratios use (not re-extracted).

TIERS (baked in from the column mapping):
  TIER 1 — clean 1:1, candidate extraction errors when they diverge:
    core (always expected): revenue, total_assets, operating_cashflow, capex
    conditional (diff only where both populate; coverage reported):
        dividends_paid, pension_pbo, pension_plan_assets, pension_service_cost,
        operating_lease_liability_current/noncurrent, and the combined lease
        liability (our current+noncurrent vs LSEG's combined column)
  TIER 2 — definitional wedge, EXPECTED (reported, never counted as error):
    operating_income (LSEG strips non-recurring), interest_expense (LSEG nets
    capitalized), total_debt per-component (LSEG "Debt - LT - Total" vs our
    component C; LSEG "ST Debt & Current LTD" vs our A+B), cash (LSEG column
    chosen per row by source_tags["cash"]).
  TIER 3 — no analog, SKIPPED: ROU assets, minority interest, operating_lease_cost,
    pension net-periodic/interest/contributions.

Tolerance: MATCH <1%, CLOSE 1-5%, MISMATCH >5% (pilot's).

Checkpoint: per-company results appended to gaap_validation_full_checkpoint.jsonl;
a re-run skips companies already present (resumable). All companies are cached, so
no network is needed. Delete the checkpoint to force a clean recompute.

Run:  python experiments/gaap_validation_full.py            (resume/build + report)
      python experiments/gaap_validation_full.py --fresh    (delete checkpoint first)
Writes experiments/gaap_validation_full_report.txt. No DB writes, no prod changes.
"""
from __future__ import annotations

import csv
import json
import pathlib
import statistics
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.extract import (  # noqa: E402
    _resolve_first_opt, gross_debt, capex_total, _get_available_periods, MissingDataError,
)

CACHE = ROOT / "cache"
PANEL = ROOT / "gaap_78_panel.csv"
REPORT_TXT = ROOT / "experiments" / "gaap_validation_full_report.txt"
CHECKPOINT = ROOT / "experiments" / "gaap_validation_full_checkpoint.jsonl"

DATE_TOL_DAYS = 10
PCT_MATCH, PCT_CLOSE = 1.0, 5.0

# ── field → LSEG column, with tier + subtype ───────────────────────────────────
# subtype: "core" (T1 always-expected) | "cond" (T1 diff-where-both) | "t2"
T1_CORE = {
    "revenue":            "Revenue from Business Activities - Total",
    "total_assets":       "Total Assets",
    "operating_cashflow": "Net Cash Flow from Operating Activities",
    "capex":              "Capital Expenditures - Total",
}
T1_COND = {
    "dividends_paid":                    "Dividends Paid - Cash - Total - Cash Flow",
    "pension_pbo":                       "Projected Benefit Obligation",
    "pension_plan_assets":               "Fair Value of Plan Assets",
    "pension_service_cost":              "Service Cost",
    "operating_lease_liability_current": "Operating Lease Liabilities - Current Portion/Short-Term",
    "operating_lease_liability_noncurrent": "Operating Lease Liabilities - Long-Term",
    "operating_lease_liability_combined":   "Operating Lease Liabilities - Long-Term & Short-Term",
}
LSEG_OP_INCOME = "Operating Profit before Non-Recurring Income/Expense"
LSEG_INT_EXP   = "Interest Expense - Net of Capitalized Interest"
LSEG_DEBT_LT   = "Debt - Long-Term - Total"
LSEG_DEBT_STC  = "Short-Term Debt & Current Portion of Long-Term Debt"
LSEG_CASH_PLAIN = "Cash & Cash Equivalents"
LSEG_CASH_TOTAL = "Cash & Cash Equivalents - Total"
LSEG_PBO, LSEG_PA, LSEG_FS = "Projected Benefit Obligation", "Fair Value of Plan Assets", "Funded Status - including Unfunded Plan Obligations"

# cash source tags that map to the plain LSEG column (narrow carrying value);
# anything broader (ST investments / restricted cash) maps to the "- Total" column.
_CASH_PLAIN_TAGS = {"us-gaap/CashAndCashEquivalentsAtCarryingValue", "us-gaap/Cash"}

# ── KNOWN residuals (pre-annotated so they surface as KNOWN, not new findings) ──
# (cik, field, period-or-None) → reason. period None = all years.
KNOWN = {
    ("0000004904", "capex", None): "AEP: ~60% low — utility construction-expenditure tag ambiguous (instead-of for AEP, additive for Matson); deferred to sector routing",
    ("0000723612", "capex", None): "Avis: MISSING — fleet capex not in companyfacts (reported net/off-XBRL)",
    ("0000003453", "capex", "2021-12-31"): "Matson FY2021: 4.6% — excluded ambiguous PaymentsForConstructionInProcess add-on",
    ("0000003499", "capex", None): "Alexander's: no capex tag in companyfacts (tiny REIT)",
    ("0000007789", "capex", None): "Associated Banc-Corp: sector-scope N/A (bank — capex immaterial)",
    ("0000011544", "capex", None): "W.R. Berkley: sector-scope N/A (insurer — capex immaterial)",
    ("0000002488", "operating_cashflow", "2025-12-27"): "AMD FY2025: 18.7% — pre-existing OCF divergence, under investigation",
}
def known_reason(cik, field, period):
    return KNOWN.get((cik, field, period)) or KNOWN.get((cik, field, None))

# ── SECTOR SCOPE (real sector router: data/sector_routing.csv) ─────────────────
# Financials (banks/insurers/REITs): revenue/OCF/operating_income/debt/capex are
# definitionally different (insurance revenue, bank cash flows, REIT development
# spend), so they're routed OUT of the error worklist and match rate, and reported
# separately. The financial set is now the is_financial==True column of the
# materialized sector-routing table (experiments/build_sector_routing.py, which
# calls Freeman's methodology classifier — is_financial is pure SIC 60-67, so it
# does not depend on the LLM step). Replaces the former hand-maintained list.
def _load_financial_ciks():
    path = ROOT / "data" / "sector_routing.csv"
    fin = {}
    if path.exists():
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if (r.get("is_financial") or "").strip() == "True":
                    fin[(r["cik"] or "").zfill(10)] = r.get("moodys_methodology", "")
    return fin

FINANCIAL_CIKS = _load_financial_ciks()
# Spin-off / restatement names: as-originally-filed XBRL diverges from LSEG's
# as-restated series → revenue/income wedges are restatement artifacts, not errors.
RESTATEMENT_CIKS = {"0000001800": "Abbott (AbbVie spin 2013)",
                    "0000004281": "Howmet (Arconic/Alcoa spin-offs)"}
MIN_YEAR = 2010          # drop pre-XBRL-reliable fiscal years (companyfacts coverage)
MIN_DENOM = 50_000_000   # Tier-2 hidden-error flag ignores small-|LSEG| denominators


def scope_of(cik):
    if cik in FINANCIAL_CIKS:
        return "financial"
    if cik in RESTATEMENT_CIKS:
        return "restatement"
    return "in-scope"

_REPORT: list[str] = []
_raw = print
def emit(*a):
    s = " ".join(str(x) for x in a); _raw(s, flush=True); _REPORT.append(s)


def _f(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_panel():
    """cik → {period_end → {lseg_col → float}}, plus names."""
    out, names = {}, {}
    with open(PANEL, newline="") as f:
        for r in csv.DictReader(f):
            cik = (r.get("SEC_CIK") or "").strip().zfill(10)
            pe = (r.get("Period End Date") or "").strip()
            if not cik or not pe:
                continue
            names.setdefault(cik, (r.get("CompanyName_provided") or cik))
            out.setdefault(cik, {})[pe] = r
    return out, names


def near(my_pe, lseg_dates):
    if my_pe in lseg_dates:
        return my_pe, 0
    md = date.fromisoformat(my_pe); best, bo = None, None
    for d in lseg_dates:
        o = abs((date.fromisoformat(d) - md).days)
        if o <= DATE_TOL_DAYS and (bo is None or o < bo):
            best, bo = d, o
    return (best, (date.fromisoformat(best) - md).days) if best else (None, None)


def extract_mine(facts, pe):
    """Resolve every needed code field for one (company, period) via the shared
    resolvers/building blocks. Returns a dict of field → value (None if absent)."""
    m = {}
    for concept in ("revenue", "total_assets", "operating_cashflow", "operating_income",
                    "interest_expense", "dividends_paid", "pension_pbo", "pension_plan_assets",
                    "pension_service_cost", "pension_funded_status",
                    "operating_lease_liability_current", "operating_lease_liability_noncurrent"):
        v, _ = _resolve_first_opt(facts, concept, pe, None)
        m[concept] = v
    # capex (component sum) and its parts
    cx = capex_total(facts, pe)
    m["capex"] = cx[0] if cx else None
    # combined lease liability = current + noncurrent (where at least one populates)
    lc, ln = m["operating_lease_liability_current"], m["operating_lease_liability_noncurrent"]
    m["operating_lease_liability_combined"] = None if (lc is None and ln is None) else (lc or 0.0) + (ln or 0.0)
    # cash + its winning source tag (drives the LSEG column choice)
    cash, cash_tag = _resolve_first_opt(facts, "cash", pe, None)
    m["cash"], m["_cash_tag"] = cash, cash_tag
    # gross_debt component waterfall (A+B+C) for the Tier-2 per-component debt diff
    try:
        _, gd_in, _ = gross_debt(facts, pe)
        m["debt_C_noncurrent"] = gd_in.get("long_term_noncurrent")
        m["debt_AB_shortcurrent"] = (gd_in.get("short_term_components") or 0.0) + (gd_in.get("current_portion_ltd") or 0.0)
    except MissingDataError:
        m["debt_C_noncurrent"] = None
        m["debt_AB_shortcurrent"] = None
    return m


def verdict(mine, lseg):
    if lseg is None:
        return "no-lseg", None
    if mine is None:
        return "MISSING", None
    if lseg == 0:
        return ("MATCH", 0.0) if mine == 0 else ("MISMATCH", float("inf"))
    pct = abs(mine - lseg) / abs(lseg) * 100
    return ("MATCH" if pct < PCT_MATCH else "CLOSE" if pct <= PCT_CLOSE else "MISMATCH"), pct


def compute_company(cik, facts, prows):
    """Return list of comparison-row dicts for one company (all matched periods)."""
    lseg_dates = sorted(prows.keys())
    rows = []
    for my_pe in _get_available_periods(facts):
        if int(my_pe[:4]) < MIN_YEAR:          # coverage filter: pre-XBRL-reliable years
            continue
        lpe, off = near(my_pe, lseg_dates)
        if lpe is None:
            continue
        lr = prows[lpe]
        m = extract_mine(facts, my_pe)

        def add(field, tier, subtype, mine, lseg_col, lseg_val=None):
            lseg = lseg_val if lseg_val is not None else _f(lr.get(lseg_col))
            v, pct = verdict(mine, lseg)
            rows.append({"cik": cik, "period": my_pe, "lseg_period": lpe, "offset": off,
                         "tier": tier, "subtype": subtype, "field": field,
                         "mine": mine, "lseg": lseg, "pct": pct, "verdict": v})

        for field, col in T1_CORE.items():
            add(field, 1, "core", m[field], col)
        for field, col in T1_COND.items():
            add(field, 1, "cond", m[field], col)
        # Tier 2
        add("operating_income", 2, "t2", m["operating_income"], LSEG_OP_INCOME)
        add("interest_expense", 2, "t2", m["interest_expense"], LSEG_INT_EXP)
        add("debt_lt_componentC", 2, "t2", m["debt_C_noncurrent"], LSEG_DEBT_LT)
        add("debt_shortcurrent_AB", 2, "t2", m["debt_AB_shortcurrent"], LSEG_DEBT_STC)
        # cash: pick LSEG column by our winning cash tag
        cash_col = LSEG_CASH_PLAIN if (m["_cash_tag"] in _CASH_PLAIN_TAGS) else LSEG_CASH_TOTAL
        add("cash", 2, "t2", m["cash"], cash_col)
        rows[-1]["cash_tag"] = (m["_cash_tag"] or "").replace("us-gaap/", "")
        rows[-1]["cash_col"] = cash_col
        # pension funded status (raw), carry PBO/PA for the sign check
        add("pension_funded_status", "pension", "fs", m["pension_funded_status"], LSEG_FS)
        rows[-1]["lseg_pbo"] = _f(lr.get(LSEG_PBO))
        rows[-1]["lseg_pa"] = _f(lr.get(LSEG_PA))
    return rows


def build_checkpoint(panel, names):
    done = set()
    if CHECKPOINT.exists():
        for line in CHECKPOINT.read_text().splitlines():
            if line.strip():
                done.add(json.loads(line)["cik"])
    cached = {p.name.split("_")[0] for p in CACHE.glob("*_facts.json")}
    todo = [c for c in sorted(panel) if c in cached and c not in done]
    emit(f"companies: {len(panel)} in panel, {len([c for c in panel if c in cached])} cached, "
         f"{len(done)} already checkpointed, {len(todo)} to compute")
    with open(CHECKPOINT, "a") as ck:
        for cik in todo:
            try:
                facts = json.loads((CACHE / f"{cik}_facts.json").read_text())
                rows = compute_company(cik, facts, panel[cik])
            except Exception as e:
                rows = []
                emit(f"  ! {names[cik]} ({cik}) failed: {type(e).__name__}: {e}")
            ck.write(json.dumps({"cik": cik, "name": names[cik], "rows": rows}) + "\n")
    # load all back
    allrows = []
    for line in CHECKPOINT.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            allrows.extend(rec["rows"])
    return allrows


def main() -> int:
    if "--fresh" in sys.argv and CHECKPOINT.exists():
        CHECKPOINT.unlink()
    if not PANEL.exists():
        emit(f"STOP: panel not found at {PANEL}"); REPORT_TXT.write_text("\n".join(_REPORT)); return 1

    panel, names = load_panel()
    emit("=" * 100)
    emit("FULL GAAP-INPUT VALIDATION — all cached panel companies, all overlapping fiscal years, 3-tier")
    emit("=" * 100)
    rows = build_checkpoint(panel, names)

    # ── unit scale (revenue, exact-matched) ────────────────────────────────────
    rev_pairs = [(r["mine"], r["lseg"]) for r in rows
                 if r["field"] == "revenue" and r["offset"] == 0 and r["mine"] and r["lseg"]]
    med = statistics.median(l / m for m, l in rev_pairs) if rev_pairs else None
    emit(f"\nUnit scale: median(LSEG/mine) over {len(rev_pairs)} exact revenue pairs = "
         f"{med:.4f} → {'ones (dollars)' if med and abs(med-1)<0.02 else 'NON-UNITY — investigate'}")
    if not (med and abs(med - 1) < 0.02):
        emit("STOP: units not confirmed 1:1."); REPORT_TXT.write_text("\n".join(_REPORT)); return 1

    # ── 1) HEADLINE — Tier-1 match rates (IN-SCOPE only) ───────────────────────
    emit("\n" + "=" * 100); emit("1) HEADLINE"); emit("=" * 100)
    def scored(rs):  # verdicts that count toward a rate
        return [r for r in rs if r["verdict"] in ("MATCH", "CLOSE", "MISMATCH")]
    def insc(rs):    # non-financial, non-restatement
        return [r for r in rs if scope_of(r["cik"]) == "in-scope"]
    t1 = [r for r in rows if r["tier"] == 1]
    n_fin = len({r["cik"] for r in rows if scope_of(r["cik"]) == "financial"})
    n_res = len({r["cik"] for r in rows if scope_of(r["cik"]) == "restatement"})
    emit(f"  total comparisons: {len(rows)}  (Tier-1 {len(t1)}, "
         f"Tier-2 {len([r for r in rows if r['tier']==2])}, pension {len([r for r in rows if r['tier']=='pension'])})")
    emit(f"  scope: {n_fin} financial + {n_res} restatement companies routed to sector-scope (excluded from rates/worklist);"
         f" fiscal years < {MIN_YEAR} dropped (coverage)")
    emit(f"\n  IN-SCOPE Tier-1 match rates:")
    emit(f"  {'Tier-1 field':<38}{'scored':>8}{'MATCH':>8}{'CLOSE':>8}{'MISS':>7}{'match%':>9}{'MISSING':>9}")
    for field in list(T1_CORE) + list(T1_COND):
        rs = insc([r for r in rows if r["field"] == field])
        sc = scored(rs); mm = [r for r in sc if r["verdict"] == "MISMATCH"]
        miss = [r for r in rs if r["verdict"] == "MISSING"]
        mrate = f"{100*len([r for r in sc if r['verdict']=='MATCH'])/len(sc):.1f}%" if sc else "n/a"
        emit(f"  {field:<38}{len(sc):>8}{len([r for r in sc if r['verdict']=='MATCH']):>8}"
             f"{len([r for r in sc if r['verdict']=='CLOSE']):>8}{len(mm):>7}{mrate:>9}{len(miss):>9}")
    sc_all = scored(insc(t1))
    emit(f"\n  IN-SCOPE Tier-1 overall: {100*len([r for r in sc_all if r['verdict']=='MATCH'])/len(sc_all):.1f}% MATCH, "
         f"{100*len([r for r in sc_all if r['verdict'] in ('MATCH','CLOSE')])/len(sc_all):.1f}% within CLOSE "
         f"({len(sc_all)} scored)")

    # ── 2) THE WORKLIST — Tier-1 MISMATCHES minus known residuals ──────────────
    emit("\n" + "=" * 100); emit("2) THE WORKLIST — Tier-1 MISMATCHES (excl. known residuals), by field then magnitude")
    emit("=" * 100)
    def excluded(r):  # sector-scope, restatement, or a documented known residual
        return (scope_of(r["cik"]) != "in-scope"
                or known_reason(r["cik"], r["field"], r["period"]) is not None)
    worklist = [r for r in t1 if r["verdict"] == "MISMATCH" and not excluded(r)]
    # also surface core-field MISSING (should-have) that isn't excluded
    core_missing = [r for r in t1 if r["subtype"] == "core" and r["verdict"] == "MISSING"
                    and not excluded(r)]
    if not worklist and not core_missing:
        emit("  (empty — every Tier-1 mismatch is a known residual)")
    for field in list(T1_CORE) + list(T1_COND):
        fr = sorted([r for r in worklist if r["field"] == field], key=lambda r: -(r["pct"] or 0))
        if not fr:
            continue
        emit(f"\n  ── {field} ({len(fr)} mismatches) ──")
        for r in fr[:25]:
            emit(f"    {r['pct']:6.1f}%  {names[r['cik']][:30]:<32}{r['period']}  "
                 f"mine={r['mine']:,.0f}  LSEG={r['lseg']:,.0f}")
        if len(fr) > 25:
            emit(f"    … +{len(fr)-25} more")
    if core_missing:
        emit(f"\n  ── core fields MISSING on our side (LSEG populated), non-known ({len(core_missing)}) ──")
        for r in sorted(core_missing, key=lambda r: (r["field"], names[r["cik"]]))[:30]:
            emit(f"    {names[r['cik']][:30]:<32}{r['period']}  {r['field']:<20}  LSEG={r['lseg']:,.0f}")

    # ── 3) TIER-2 wedge summary ────────────────────────────────────────────────
    emit("\n" + "=" * 100); emit("3) TIER-2 DEFINITIONAL WEDGES (expected — NOT errors)"); emit("=" * 100)
    emit("  signed wedge = (mine-LSEG)/LSEG %; negative = LSEG higher than ours")
    for field, note in [("operating_income", "LSEG strips non-recurring → LSEG can be higher or lower"),
                        ("interest_expense", "LSEG nets capitalized interest → LSEG typically LOWER (our wedge positive)"),
                        ("debt_lt_componentC", "LSEG 'Debt-LT-Total' vs our component C"),
                        ("debt_shortcurrent_AB", "LSEG 'ST Debt & Current LTD' vs our A+B"),
                        ("cash", "LSEG column chosen by our cash source tag")]:
        rs = [r for r in rows if r["field"] == field and r["mine"] is not None and r["lseg"]]
        if not rs:
            emit(f"\n  {field}: no populated pairs"); continue
        signed = sorted(((r["mine"] - r["lseg"]) / r["lseg"] * 100) for r in rs)
        med = statistics.median(signed)
        p10, p90 = signed[len(signed)//10], signed[min(len(signed)-1, 9*len(signed)//10)]
        emit(f"\n  {field}: n={len(rs)}  median {med:+.1f}%  [p10 {p10:+.1f}%, p90 {p90:+.1f}%]  — {note}")
        # outliers: wedge > 50% AND large enough denominator AND same sign (not a
        # loss-year/near-zero artifact) AND in-scope — the residue after suppressing
        # small-denominator noise is what might hide a real error under a wedge label.
        big = sorted([r for r in rs
                      if abs(r["lseg"]) >= MIN_DENOM and (r["mine"] * r["lseg"]) > 0
                      and scope_of(r["cik"]) == "in-scope"
                      and abs((r['mine']-r['lseg'])/r['lseg']) > 0.50],
                     key=lambda r: -abs((r['mine']-r['lseg'])/r['lseg']))
        if big:
            emit(f"     ⚠ {len(big)} in-scope rows |wedge|>50%, |LSEG|≥${MIN_DENOM/1e6:.0f}M, same-sign "
                 f"(possible hidden error; small-denom/loss/financial rows suppressed):")
            for r in big[:8]:
                w = (r['mine']-r['lseg'])/r['lseg']*100
                extra = f"  [cash_tag={r.get('cash_tag')}]" if field == "cash" else ""
                emit(f"        {w:+7.0f}%  {names[r['cik']][:28]:<30}{r['period']}  mine={r['mine']:,.0f} LSEG={r['lseg']:,.0f}{extra}")

    # ── 4) PENSION coverage + funded-status sign convention ────────────────────
    emit("\n" + "=" * 100); emit("4) PENSION — coverage + funded-status sign convention"); emit("=" * 100)
    for field in ("pension_pbo", "pension_plan_assets", "pension_service_cost"):
        rs = [r for r in rows if r["field"] == field]
        pop = [r for r in rs if r["lseg"] is not None]
        both = [r for r in pop if r["mine"] is not None]
        mm = [r for r in both if r["verdict"] == "MISMATCH"]
        emit(f"  {field:<24} LSEG populates {len(pop)}/{len(rs)} rows; both populate {len(both)}; "
             f"MATCH {len([r for r in both if r['verdict']=='MATCH'])}, MISMATCH {len(mm)}")
    fs = [r for r in rows if r["field"] == "pension_funded_status"]
    sign = [r for r in fs if r["lseg"] is not None and r.get("lseg_pbo") and r.get("lseg_pa")]
    as_minus_pbo = sum(1 for r in sign if abs(r["lseg"] - (r["lseg_pa"] - r["lseg_pbo"])) <= 0.02*abs(r["lseg"] or 1))
    pbo_minus_as = sum(1 for r in sign if abs(r["lseg"] - (r["lseg_pbo"] - r["lseg_pa"])) <= 0.02*abs(r["lseg"] or 1))
    emit(f"\n  funded-status sign check on {len(sign)} rows where LSEG PBO,PA,FS all populate:")
    emit(f"    LSEG FS ≈ (plan_assets − PBO): {as_minus_pbo}   LSEG FS ≈ (PBO − plan_assets): {pbo_minus_as}")
    if not sign:
        conv = None; emit("    → cannot establish (no rows with all three) — SKIP funded-status diff")
    elif as_minus_pbo >= pbo_minus_as and as_minus_pbo > 0:
        conv = "assets_minus_pbo"; emit("    → convention: LSEG FS = plan_assets − PBO (negative=underfunded) — SAME as our us-gaap DefinedBenefitPlanFundedStatusOfPlan")
    elif pbo_minus_as > 0:
        conv = "pbo_minus_assets"; emit("    → convention: LSEG FS = PBO − plan_assets (positive=underfunded) — OPPOSITE sign to ours; negate before diffing")
    else:
        conv = None; emit("    → inconclusive — SKIP funded-status diff")
    if conv:
        both = [r for r in fs if r["lseg"] is not None and r["mine"] is not None]
        agree = 0
        for r in both:
            lseg_cmp = r["lseg"] if conv == "assets_minus_pbo" else -r["lseg"]
            if abs(r["mine"] - lseg_cmp) <= max(0.02*abs(lseg_cmp or 1), 1):
                agree += 1
        emit(f"    our pension_funded_status vs LSEG (sign-aligned): {agree}/{len(both)} within 2% (both populate)")

    # ── 5) KNOWN residuals — confirm they reproduce ────────────────────────────
    emit("\n" + "=" * 100); emit("5) KNOWN RESIDUALS — confirm reproduced (flag any that changed)"); emit("=" * 100)
    for (cik, field, period), reason in KNOWN.items():
        rs = [r for r in rows if r["cik"] == cik and r["field"] == field
              and (period is None or r["period"] == period)]
        if not rs:
            emit(f"  ? {names.get(cik,cik)} {field} {period or '(all)'}: NO ROW — verify (expected {reason[:50]})")
            continue
        verds = {r["verdict"] for r in rs}
        # known = expected NOT to be clean MATCH (they're residual issues)
        still = "still-divergent" if verds & {"MISMATCH", "MISSING"} else "NOW MATCHES(!)"
        flag = "" if "still" in still else "  ⚠ CHANGED"
        emit(f"  {still:<16}{names.get(cik,cik)[:26]:<28}{field:<20}{period or '(all yrs)':<13} {verds}{flag}")

    REPORT_TXT.write_text("\n".join(_REPORT) + "\n")
    _raw(f"\n(report → {REPORT_TXT}; checkpoint → {CHECKPOINT})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
