# MIGRATION_DETECTOR.md — Rating-Migration (Downgrade-Trend) Detector Specification

**Module to build:** `src/migration.py` (new — analysis/feature module)
**Status:** Tier-1 trend detector. This document is the authoritative contract. Any change that drops a REQUIRED behavior here is a regression.
**Depends on:** `data/backtest_results.json` (score history), `src/rating.py` + `data/rating_calibration.json` (rating boundaries), `src/score.py` (rule groups). Does NOT modify any of them.
**Hard rule:** Do NOT modify `src/score.py`, `src/rating.py`, `src/calibrate.py`, or the live scoring/config path. This is a new, additive module. Do NOT commit until reviewed.

---

## 0. For the implementer (Claude Code): read these BEFORE writing code, and report back

Before writing `src/migration.py`, inspect and report the following. Do not compute or write the module until this is confirmed.

1. **`data/backtest_results.json`** — confirm the per-company trajectory structure. Specifically report:
   - the path to per-period score (expected `cases[i].trajectory[j].score`),
   - the per-period date field(s) (expected `eval_date`, `period_end`, `months_before_event`),
   - whether `has_data` exists per period and how missing periods are marked,
   - **the ordering** (expected newest-first: `months_before_event` 0 → ~115),
   - whether each period carries a `ratios` dict, and the exact key names of the per-metric ratio values inside it.
2. **`src/score.py`** — report `_CORE_RULE_KEYS` and `_ADDITIONAL_RULE_RATIOS` (the rule→ratio mapping), and which ratio each rule reads. This is needed to assign metrics to the component groups in §4.3.
3. **`data/rating_calibration.json`** — report the score cutoffs and their confidence flags, so distance-to-boundary (§4.4) uses the real boundaries (and respects which are low-confidence).
4. **CRITICAL data subtlety to confirm and handle (§3.1):** the backtest takes ~40 snapshots ~90 days apart, but the underlying financials are annual (10-K). So **consecutive 90-day snapshots usually share the same underlying filing and therefore the same score**, changing only when a new filing arrives. Report whether consecutive trajectory entries repeat the same `score`/`period_end`. The trend math MUST be computed across **distinct filings (distinct `period_end`)**, not across the 90-day re-evaluation snapshots — otherwise velocity is mostly zeros punctuated by jumps, and acceleration is meaningless. Confirm how you will collapse the trajectory to one observation per distinct `period_end` (use the score as of each new filing).

**Report all of the above and confirm the plan before writing `src/migration.py`.**

---

## 1. Purpose

Detect, for a given company, whether its credit quality is **on a trajectory toward a rating change** — a downgrade or upgrade *trend* — rather than merely reporting its current rating *level*. The product question this answers: *"This issuer is BBB today; is it drifting toward BB?"*

The distinction is the entire value. A high score means stress that has **already arrived**. A rising score from a still-healthy level means stress that is **coming** — caught early, while there is still time to act. The detector measures the *motion* of the score, not its position.

---

## 2. Scope

### 2.1 In scope
- Per-company **downgrade-trend** and **upgrade-trend** detection from the score time series.
- A directional, graded signal: not just "trend / no trend," but direction (deteriorating / improving / stable), strength, and — where possible — projected time/quarters to the next rating boundary.

### 2.2 Out of scope (for this Tier-1 build)
- Sector-adjusted trend thresholds (deferred to Tier 2 / benchmark layer). Note in the docstring.
- Intra-quarter / real-time signals — the detector operates on the filing-cadence score series.
- Causal attribution beyond the component-sequence signal in §4.3.

---

## 3. Input data and preprocessing

### 3.1 Build the per-filing score series (MANDATORY preprocessing)
For each company, collapse `trajectory[]` to **one observation per distinct `period_end`**, ordered oldest → newest, keeping only periods with `has_data == true`. Each observation = `(period_end, score, ratios)`. This is the series all four components operate on. **Do not compute velocity/acceleration over the raw 90-day snapshots** (see §0.4).

Minimum series length: a company needs **≥4 distinct-filing observations** to be eligible for full trend analysis (velocity needs ≥2, acceleration needs ≥3, persistence needs ≥3). For shorter series, return a `"trend": "insufficient_history"` result rather than a misleading signal.

### 3.2 Direction convention
Score rises = credit deteriorates = movement toward downgrade. Score falls = improvement = movement toward upgrade. State all signals in these terms.

---

## 4. The four components

Each company gets all four computed. They combine in §5.

### 4.1 Velocity — the slope of the score
The rate of score change per year, over a trailing window.
- Compute the slope of `score` vs time (in years) over the trailing **N observations** (default N = 4 distinct filings, ~4 years; if fewer than 4 exist, use all available, minimum 2).
- Use ordinary least-squares slope (reuse a simple linear fit; do not pull in heavy deps if avoidable). Report `velocity_pts_per_year`.
- Positive velocity = deteriorating. The larger, the faster the drift toward downgrade.

### 4.2 Acceleration — is the deterioration speeding up?
The change in velocity — the second derivative.
- Compute velocity over the most recent half of the window and over the prior half; `acceleration = recent_velocity − prior_velocity` (pts/yr², approximately).
- Positive acceleration = the slide is steepening (earliest, strongest warning). Requires ≥3 observations; else `null`.

### 4.3 Component sequence — which metrics are moving first
Deterioration follows a characteristic order. Detecting *which group* is moving identifies the **stage** of deterioration, often before the composite score moves much.

Assign each of the 19 metrics to one of four groups (use the actual rule→ratio mapping from `src/score.py`; the grouping below is the intended assignment — confirm against the real keys):
- **Liquidity** (moves first): liquidity, current_ratio, quick_ratio, cash-related.
- **Coverage** (moves second): interest coverage, ocf_ebitda_conversion, cash_flow_to_debt, rcf_net_debt, moody_adjusted_fcf, fcf.
- **Leverage** (moves third): leverage, debt_to_equity, debt_to_assets, asset/tangible/liquidation coverage, maturity_wall, maturity_coverage_near_term.
- **Qualitative / late** (moves last): profitability/ebitda_margin, revenue_yoy_growth, and (when wired in later) covenant-proximity and going-concern flags.

For each group, compute whether its constituent ratios are **deteriorating** over the trailing window (group-level score contribution rising). Report a `component_sequence` object: which groups are currently deteriorating, and the **earliest-stage** group that is moving (liquidity-only moving = early stage; leverage+qualitative moving = late stage). This is the qualitative analogue of velocity and is the key "trend before level" signal.

> Payment-priority note (the "senior bond paid first, then alert" insight): when liquidity is tightening *while* the company is still servicing senior obligations — i.e. liquidity group deteriorating but leverage not yet — that is the canonical early-stage pattern. Flag `early_stage_liquidity_stress = true` in that case.

### 4.4 Distance-to-boundary — how close to a rating change, and projected timing
Combines level + velocity to project a crossing.
- Map the **current** score to a rating via `src/rating.py`. Find the score cutoff for the **next worse** rating bucket from `data/rating_calibration.json`.
- `distance_to_downgrade = next_boundary_score − current_score` (in score points).
- If `velocity_pts_per_year > 0`: `projected_years_to_downgrade = distance_to_downgrade / velocity_pts_per_year`. Report it. If velocity ≤ 0, report `null` (not drifting toward downgrade).
- **Respect confidence:** if the relevant boundary is flagged `low`/interpolated in the calibration JSON, mark this component `"boundary_confidence": "low"` and do not present the projected timing as reliable. (Per the rating calibration, the BBB→CCC band is interpolated — projections across it are indicative only.)

---

## 5. The trend decision — persistence, breadth, and the one-off-event filter

A raw rising score is not a trend. To fire a trend signal, ALL of the following must hold (this is what prevents crying wolf):

### 5.1 Persistence
The score must be rising across **≥3 consecutive distinct-filing observations** (default; expose as a parameter `min_persistence = 3`). A single-period jump is not a trend.

### 5.2 Breadth
At least **2 of the 4 component groups** (§4.3) must be deteriorating concurrently (default `min_breadth = 2`). One metric moving alone is noise; multiple groups moving together is signal.

### 5.3 One-off-event filter
A trend signal is **suppressed** when the score rise is attributable to a single-period, non-deteriorating event rather than ongoing decline. Heuristics (apply all):
- **Single-spike test:** if essentially all of the score increase occurs in ONE filing-to-filing step and then plateaus (not continuing to rise in subsequent observations), treat as a one-off, not a trend.
- **Breadth-of-one test:** if the rise is driven by a single metric/group (fails §5.2 breadth), suppress.
- **Leverage-spike-without-coverage-decline pattern:** a jump in leverage metrics with NO concurrent deterioration in coverage or liquidity is the signature of a debt-funded acquisition (the Waste Management case), not credit decline — flag `likely_one_off = true` and suppress the downgrade-trend alert (still record the leverage change).
- Record `suppressed_reason` when a trend is suppressed, so the decision is auditable.

> **Validation hook:** the annotated healthy controls (Waste Management acquisition spike, Emerson, Becton Dickinson, Air Products, etc.) are the designed test cases for this filter. They are currently HELD for Phase 5b and may not be in the present case set — note this; when those cases are added, the filter MUST NOT fire a downgrade trend on them.

### 5.4 Decision output
`trend ∈ { "deteriorating", "improving", "stable", "insufficient_history" }`, with a `strength ∈ {weak, moderate, strong}` derived from velocity + acceleration + breadth, and the suppression flags above. "deteriorating" requires persistence AND breadth AND not-suppressed.

---

## 6. Output schema

`detect_migration(company_series) -> dict`:

```json
{
  "cik": "0000018230",
  "company_name": "Caterpillar",
  "observations_used": 7,
  "current_score": 22.0,
  "current_rating": "A",
  "trend": "deteriorating",                 // deteriorating | improving | stable | insufficient_history
  "strength": "moderate",                   // weak | moderate | strong
  "velocity_pts_per_year": 4.3,
  "acceleration": 1.1,                       // null if <3 obs
  "component_sequence": {
    "deteriorating_groups": ["liquidity", "coverage"],
    "earliest_stage_moving": "liquidity",
    "early_stage_liquidity_stress": true
  },
  "distance_to_downgrade": 26.6,             // score points to next worse bucket; null if improving
  "projected_years_to_downgrade": 6.2,       // null if velocity<=0
  "boundary_confidence": "high",             // high | low (from rating calibration)
  "persistence_quarters": 4,
  "breadth_groups": 2,
  "likely_one_off": false,
  "suppressed_reason": null
}
```

---

## 7. Validation (how we know it works) — REQUIRED, report results

Run the detector over all companies in `backtest_results.json` and report:

1. **Distressed names must show deteriorating trends pre-default.** For the distressed cases (e.g. Kodak, Alpha Natural, Rite Aid, Peabody), confirm the detector reports `trend = "deteriorating"` with rising velocity in the run-up to the event_date. Report how many of the scoreable distressed names show a deteriorating trend before default, and the median lead (in observations/years) between first "deteriorating" flag and the event. This is the trend analogue of the catch-rate.
2. **Healthy names must mostly be stable/improving.** For the 19 healthy controls, report how many are correctly `stable`/`improving` vs falsely `deteriorating`. Note that UPS (S&P outlook→negative) and any name the agencies see weakening MAY legitimately show mild deterioration — flag those rather than counting them as errors. ITW, Eli Lilly, Caterpillar are on positive agency outlook — they should NOT show strong deterioration.
3. **One-off filter:** if any annotated/acquisition-driven names are present, confirm they are suppressed (`likely_one_off = true`). If none are present in the current case set, state that and note the filter is untested until Phase 5b cases are added.
4. Report any company whose trend result is surprising (e.g. a healthy name flagged strongly deteriorating, or a distressed name NOT flagged before default) — those are either bugs or genuine findings, list them by name.

---

## 8. Failure modes to guard against

- **Computing velocity over the 90-day snapshots instead of distinct filings** → mostly-zero velocity with spurious jumps. Guard: §3.1 preprocessing.
- **Firing on a single-quarter blip** → cured by §5.1 persistence.
- **Firing on a debt-funded acquisition spike** → cured by §5.3 one-off filter (the Waste Management pattern).
- **Trusting projected timing across an interpolated rating boundary** → cured by §4.4 boundary_confidence flag.
- **Treating a flat-but-high score as a trend** → a company stable at score 70 is NOT "deteriorating"; trend is about motion, not level. Guard: velocity≈0 → "stable" regardless of level.
- **Insufficient history misread as stability** → <4 observations returns "insufficient_history", never a false "stable".

---

## 9. Design decisions flagged for review (defaults chosen; adjust to taste)

These are the judgment calls baked into the defaults above. They are parameters, not hard-coded, so they can be tuned:
- `min_persistence = 3` consecutive filings to call a trend.
- `min_breadth = 2` of 4 component groups deteriorating.
- Velocity window `N = 4` distinct filings.
- Component-group assignment of the 19 metrics (§4.3) — confirm against the real `score.py` keys.
- Strength thresholds (weak/moderate/strong) — set provisionally from velocity+acceleration+breadth; refine after seeing the validation distribution.

The implementer should surface, in the report, what each parameter produced on the validation set so these can be tuned with evidence rather than guessed.

---

## 10. Deliverables and what NOT to do

**Deliver:**
- `src/migration.py` — `detect_migration()` per the schema, the four component functions, the §5 decision logic, parameters exposed per §9, and a module docstring noting this is a flat (non-sector-adjusted) Tier-1 detector with a documented extension point for sector-adjusted trend thresholds (Tier 2).
- A validation report per §7 (printed; also written to `data/migration_validation.json` as an artifact).

**Do NOT:**
- Modify `src/score.py`, `src/rating.py`, `src/calibrate.py`, the config, or the live path.
- Commit anything until reviewed.
- Present low-confidence boundary projections as reliable.

**Report back:** the §0 file-structure confirmation first; then the validation results (§7) — distressed deterioration-catch rate + lead, healthy false-trend count, any surprises by name, and the parameter-effect summary (§9).

---

*This spec follows the contract-first style of LLM_COVENANT.md and the metric specs: it defines required behavior, the output shape, the validation bar, and the explicit design decisions, so the detector can be checked against the contract rather than judged by feel.*
