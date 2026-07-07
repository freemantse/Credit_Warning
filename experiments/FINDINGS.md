# Validation Findings — session 2026-07-07

Three out-of-sample / held-out validation results, recorded for Freeman. **Documentation only — no changes to the scoring code, not committed to the scorer.** All three reuse the production machinery (`score_issuer_at_date` / `detect_migration` / `evaluate_*`) read-only against a **frozen `DEFAULT_CONFIG`**, on data (LSEG ratings, ~239 mapped names) that is mostly held out of the 95-case `cases.csv`.

Report artifacts (raw numbers):
- `experiments/oos_distress_report.txt` — out-of-sample distress scorecard + FP.
- `experiments/transition_pilot_report.txt` — transition hit-rate×lead curves, FP, wrong-direction, and the like-for-like gap table.
- Supporting modules (uncommitted, `experiments/`): `transition_validation.py`, `oos_distress_validation.py`.

---

## 1. Transition prediction (`migration.py`) — NEGATIVE

**The velocity/trajectory verdict does not predict rating transitions.** Tested on 228 usable LSEG-rated companies (314 downgrade + 296 upgrade events, 24-month per-direction dedup), leak-free, verbatim machinery.

- Headline (`trend`) hit-rate is **flat-to-declining with lead window**, not rising — downgrade 17→16→16→15% at L = 3/6/12/24mo; upgrade 14→13→9→10%. Genuine early warning would *rise* toward 12–24mo; a flat/declining curve is the coincidence signature (model and agency both reacting to the same just-filed 10-K).
- **Like-for-like (hit − its own directional stable-FP):** the only positive discriminator is velocity-downgrade (+13→+6pp) but it **peaks at L=3 and decays** — coincidence, not lead. Upgrade velocity goes to **−2pp** by L=24. Boundary variant ≈ 0/negative. No definition shows a gap that rises with lead time in either direction.
- **Conclusion:** score-velocity is not a transition predictor. **Future direction: model transitions off the Moody's rating-grid (levels/notch structure), not score velocity.**

## 2. Distress detector — OUT-OF-SAMPLE: catch generalizes, FP does not

Frozen `DEFAULT_CONFIG` run on **held-out LSEG names** (51 distressed = rating reached Caa1/CCC+; 147 clearly-healthy = never single-B or worse; 74 ambiguous single-B and 11 `cases.csv` overlaps excluded).

| | catch-rate | FP-rate |
|---|---|---|
| in-sample (95 cases, hand-set config, **no hold-out**) | 95.9% | 5.8% |
| **out-of-sample (held-out, same frozen config)** | **91.4%** (32/35) | **16.7%** (968/5,812) |
| Δ | **−4.5pp** | **+10.9pp** |

- **Catch generalizes** — 91.4% on names never seen (median lead ~47mo). Reassuring for sensitivity.
- **FP roughly triples (5.8%→16.7%).** The low in-sample 5.8% was **an artifact of only 19 curated healthy controls**; on 147 held-out healthy names the detector flags ~1 in 6 period-snapshots.
- Caveats (see report): distress-tier (Caa1+) is a proxy, not the bankruptcy events the 95 used; LSEG survivorship; look-back-defined healthy controls. The in-sample headline is **apparent (in-sample) performance** — validated for sensitivity, not specificity.

## 3. FP diagnosis — SECTOR-NORMALIZATION problem, not a threshold problem

Across the 968 held-out healthy FPs (76/147 companies clean; 71 flagged; top-10 = 36%):

- **Driven by the whole debt/cash-flow rule family**, not one rule: cash_flow_to_debt<30% (918 snaps), leverage>5x (825, avg 12.4/17), rcf_net_debt<15% (916), liquidation_asset_coverage, coverage<2x — all firing near max *together*, which also trips the ≥4-severe escalation floor.
- **Concentrated in structurally-levered sectors:** Financials 227 FP, Consumer Non-Cyclicals 184, Industrials 122, REITs 115 (only 7 cos), Utilities 68 (4 cos), Energy/pipelines 59. Worst offenders: Fannie Mae, Farmer Mac, GATX, AIG, banks; Federal Realty, BRT, Getty (REITs); AEP (utility); Plains All American (MLP); Altria, Conagra, Constellation (levered staples) — all investment-grade throughout.
- **Not marginal:** stressed-healthy scores median 65, mean 69.7, p90 97, 67 pegged at 100. **64% score ≥60 (solidly flagged), only 36% in 50–60** → a threshold move (e.g. 50→60) would clear ~a third while gutting catch and leaving the majority flagged. **This is not a threshold problem.**

**Two distinct sub-drivers:**
1. **Financials (largest bucket, 227) — SCOPE issue.** Banks/GSEs/insurers should arguably not be scored by corporate-debt ratios at all. **There is no financial-institution suppression in the scoring path today** (the "Aflac financial-institution suppression boundary" in `cases.csv` is an untested aspiration).
2. **REITs / utilities / pipelines / levered staples — CALIBRATION issue.** The ratios apply, but healthy/severe thresholds sit at industrial norms; these sectors' *normal* capital structure reads as distress. This is the unbuilt **Tier-2 sector-adjustment layer** that `calibrate.py` and `migration.py` both explicitly defer.

**Fix deferred (not attempted here).** Any fix (financial-institution scoping and/or sector-relative thresholds) **must be validated on a fresh hold-out — NOT these 147**, which are now in-sample for the fix.

---

## 4. `coverage_adjusted<2x` A/B — weight adds NO catch, only FP (fresh hold-out)

Session 2026-07-07. A/B of the Moody's Formula-2 adjusted-coverage rule (lease-interest leg: EBITDA / (interest + ⅓×operating-lease cost)), **weight 0 (OFF) vs 14 (ON, matching core `coverage<2x`)**, on a **fresh hold-out** — 169 distressed / 761 healthy companies joinable via SEC `company_tickers.json` but **excluded from both `cases.csv` and the OOS/FP diagnosis set** (genuinely untouched). Same label rule as the OOS test (distressed = LT rating reached Caa1/CCC+; healthy = never single-B or worse). Report: `experiments/coverage_adj_ab_report.txt`.

| | catch | FP periods |
|---|---|---|
| OFF (w=0) | 120/140 (85.7%) | 6,633/27,437 (24.2%) |
| ON (w=14) | 120/140 (85.7%) | 6,842/27,437 (24.9%) |
| Δ | **+0 (0 new catches)** | **+209 periods** |

- **Zero new catches** — every distressed name caught with the rule on was already caught with it off; it never pushed a missed issuer over threshold (including in lease-heavy sectors). Rule fired on 300/761 healthy companies (broad), all additive FP.
- **FP rise by sector — not the lease-heavy trio.** Biggest increase is **Financials +143** (`OperatingLeaseCost` is tagged there, and financials are the dominant FP driver regardless). The lease-heavy trio barely moved — **Real Estate +12, Utilities +4, Energy +0** — because they are **already saturated** (FP 46% / 67% / 35% with the rule *off*, from the core rules).
- **Decision: keep flag-first (weight 0).** Valuable as an audit flag (visibility into lease-adjusted coverage); must **not** score until the sector-normalization layer exists.

### Pattern — 3rd confirmation (leverage, pension, coverage)
All three Moody's Formula-2 adjustments are **directionally correct but amplify FP on structurally-levered / financial sectors WITHOUT adding catch**, because they fire on names the scorecard **already flags**. **Scoring-weight for every Moody's adjustment is therefore gated on the (unbuilt) sector-normalization layer.** Until it exists, adjustments ship **flag-first, weight 0** — real as audit signals, inert in scoring.

---

## 5. Correction — pension-EBITDA reclass is DETERMINISTIC (~19%), not LLM-only

Earlier scoping (and the `concepts.py` tag comments) characterized the Moody's pension-EBITDA reclassification as needing footnote/LLM extraction ("not a single clean tag"). **That was too pessimistic.** The total is a single XBRL tag — `us-gaap:DefinedBenefitPlanNetPeriodicBenefitCost` — populated for **29%** of cached filers, and the reclass addback is simply **(net periodic benefit cost − service cost)**, computable deterministically where **both** tag (**~19%**, 245/1,291 cached). A service-cost-only figure is *not* a partial approximation of the reclass — the excess is uncomputable without the total — so the leg requires both tags and is otherwise MissingRatio (no guess). Built 2026-07-07 as `pension_ebitda_reclass` (flag-first, weight 0, `pension_source="xbrl"`). LLM fallback is only needed for the ~81% that don't tag both.

(The three deterministic pension flag legs — B3 interest `interest_coverage_pension_adjusted`, B2 EBITDA `pension_ebitda_reclass`, B4 FCF `moody_adjusted_fcf_pension` — all ship flag-first weight 0, score-neutral. B4 is a **parallel** flag, deliberately NOT an in-place edit of `moody_adjusted_fcf` because that ratio is scored (weight 8) and feeds the lease Option-C gate — editing it would move scores for ~14% of filers, deferred as a separate A/B-gated change.)

---

### One-line summary for Freeman
Catch is real and generalizes (~91% held-out); the 5.8% FP was a curated-control artifact (true ~17%), driven by missing sector normalization (financials shouldn't use these ratios; REITs/utilities/pipelines/staples need sector-relative thresholds); and the migration trajectory engine does not predict rating transitions (coincidence, not lead) — transition modeling should be grid-based, not velocity-based.
