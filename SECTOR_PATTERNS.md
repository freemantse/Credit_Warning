# SECTOR_PATTERNS.md — Company-Type Interpretation Patterns (Tier-2 foundation)

**Status:** Design spec / interpretation catalogue. Authoritative for the Tier-2 benchmark layer and for the dashboard interpretation text. Not yet implemented — the numeric benchmarks depend on the peer-universe data (Freeman's Supabase); the interpretation rules below can be authored now.
**Relationship to the system:** Tier 1 (flat scoring, score→rating, migration) is sector-blind and correctly flags level/motion in the aggregate. This document defines the Tier-2 layer that re-interprets Tier-1 output *by company type*. It is the conceptual backbone of the benchmark layer.

---

## 0. The core principle

A benchmark is **not only a number** (e.g. "pharma leverage threshold = 3.5x"). It is an **interpretation rule** — an instruction that tells the system, and the analyst reading the dashboard, *how to read a pattern for this type of company*: when rising leverage means strength vs distress, when negative free cash flow means growth vs death, when a ratio swing means the commodity cycle vs the company failing.

The flat (Tier-1) model is sector-blind. It judges every company against one cross-sector standard, so it systematically misreads patterns that are *normal for a type*. Each entry below names a pattern the flat model misreads, gives the interpretation rule, and — most importantly — gives the **distress-tell**: the condition that flips the benign pattern into genuine distress.

> **The meta-rule (non-negotiable):** every "this is normal for the type" rule MUST be paired with a distress-tell. Otherwise the catalogue is just an excuse-list that explains away every red flag. The interpretation rule says "this pattern is usually benign for this type — UNLESS [tell], which means it is real." The tell is what keeps the layer a detector and not a blindfold.

Each pattern, on the dashboard, becomes a sentence the analyst can read — e.g. *"Leverage rising, but for pharma with this earnings growth this is normal capital allocation, not a downgrade signal"* — instead of a bare red number.

---

## Legend — evidence basis

- **[EVIDENCE]** — surfaced directly by this system's own 95-case backtest / validation. We have seen the flat model misread these names.
- **[ANALYST]** — standard credit-analysis knowledge for company types not yet (or only thinly) represented in the 95-case library. To be confirmed empirically as those sectors are added to the peer universe.

---

## 1. Buyback / R&D-heavy blue chip  **[EVIDENCE]**

- **Applies to:** large-cap pharma, established tech, consumer staples.
- **Seen in:** Eli Lilly (migration vel +15.1 read as deteriorating, while Moody's *upgraded* A1→Aa3); Amgen (rating calibration: model → CCC vs S&P BBB+); UPS (false-positive in score backtest).
- **What the flat model misreads:** the company levers up and/or runs heavy buybacks, capex, and R&D. Leverage rises, FCF compresses → the flat model reads deterioration.
- **Reality:** the spending is a sign of *strength and confidence* — returning cash to shareholders, funding a drug pipeline or product ramp — funded by strong, growing earnings. Agencies often *upgrade* these names while the flat model flags them.
- **Interpretation rule:** for these sectors, leverage rising *alongside* strong/growing earnings and stable coverage is normal capital allocation, not distress.
- **Distress-tell:** leverage rises **AND coverage / margins also fall** (interest coverage weakening, EBITDA margin declining). Benign = leverage up, coverage holds. Real = leverage up, coverage down. The co-movement of coverage is the discriminator.
- **Dashboard instruction:** *"Leverage rising, but earnings/coverage strong — for this sector, buyback/R&D-driven leverage is normal capital allocation. Watch coverage: if it falls with leverage, re-evaluate."*
- **Data Tier-2 needs:** sector distribution of leverage *conditional on* coverage strength, so "high leverage + high coverage" can be scored as the sector norm.

---

## 2. Asset-heavy / capital-lease operator  **[EVIDENCE]**

- **Applies to:** logistics, transport (air/rail/trucking), retail-with-owned-stores.
- **Seen in:** UPS (high debt-to-equity from buybacks + lease-heavy balance sheet → score false positive).
- **What the flat model misreads:** structurally high absolute debt (planes, trucks, facilities, capitalized leases) reads as over-levered.
- **Reality:** asset-heavy operators carry more debt *at every rating level* — the assets are productive and financeable. Their A-rated normal is another sector's distress zone.
- **Interpretation rule:** judge absolute leverage against the *sector* distribution, not the cross-sector one. High leverage is the baseline, not the signal.
- **Distress-tell:** leverage rising **faster than the asset base / revenue** (leverage growth outpacing the productive capacity it funds), OR asset-coverage deteriorating (the assets backing the debt losing value). Benign = debt scales with productive assets. Real = debt outruns them.
- **Dashboard instruction:** *"High leverage is structural for asset-heavy operators. Concern only if leverage outpaces asset/revenue growth or asset coverage weakens."*
- **Data Tier-2 needs:** sector leverage and asset-coverage distributions for transport/logistics.

---

## 3. Chronically-stressed survivor  **[EVIDENCE]**

- **Applies to:** declining retail, some commodity, secular-decline businesses.
- **Seen in:** Sears, Frontier, Revlon, Rite Aid (pinned at high stress scores for years without fresh default trend; the migration detector's flat-but-high and cap-saturation guards already handle these).
- **What the flat model misreads:** a high *level* score every period reads as a persistent alarm.
- **Reality:** the business operates at permanently thin margins / high leverage as its *normal* state. It has been "sick" for years; the level is chronic, not news.
- **Interpretation rule:** for chronically-stressed types, a high *level* is not a fresh signal — only *change* (motion) matters. (The migration detector encodes this; Tier-2 extends it to the level score so the dashboard doesn't show a permanent false alarm.)
- **Distress-tell:** fresh deterioration *on top of* the chronic baseline — score rising above its own multi-year band, liquidity tightening, or a covenant/going-concern event appearing where there was none. Benign = stable-at-high. Real = rising-above-its-own-norm.
- **Dashboard instruction:** *"Chronically stressed but stable — high level is this company's baseline, not a fresh signal. Watch for deterioration above its own multi-year range."*
- **Data Tier-2 needs:** per-company historical score band (to define "above its own norm"), not just a sector benchmark.

---

## 4. Commodity / cyclical producer  **[EVIDENCE]**

- **Applies to:** oil & gas E&P, mining, coal, chemicals.
- **Seen in:** Whiting, Oasis, Chesapeake (ratios swung violently with the oil cycle; the 2020 names were "structurally invisible" because the killing blow was a commodity shock, not a balance-sheet trend).
- **What the flat model misreads:** a single bad year (low commodity price) looks like distress; a good year looks like recovery. Neither is structural.
- **Reality:** the ratios track the commodity cycle, not the company's underlying health. A leverage spike may be the cycle; a recovery may be a price rebound, not a fix.
- **Interpretation rule:** judge commodity producers **through-the-cycle** (multi-year averages), not on a single year. Treat single-year ratio swings as cycle noise until confirmed across the cycle.
- **Distress-tell:** balance-sheet damage that *persists across* the cycle — leverage staying high *after* prices recover, or liquidity exhausted at the cycle trough (no runway to survive to the next upturn). Benign = ratios swing but recover with prices. Real = damage that doesn't heal when prices do. **Limitation:** a sudden commodity *shock* (2020 COVID oil crash) that post-dates the last filing is structurally invisible to any filing-based detector — see migration `structurally_invisible`.
- **Dashboard instruction:** *"Ratios track the commodity cycle — judge through-the-cycle, not single-year. Concern if leverage stays high after prices recover, or liquidity is exhausted at the trough."*
- **Data Tier-2 needs:** through-the-cycle (multi-year) sector averages; a commodity-price context flag would strengthen this.

---

## 5. Regulated utility  **[ANALYST]**

- **Applies to:** regulated electric / gas / water utilities.
- **What the flat model misreads:** 5–6x leverage reads as severe distress by industrial standards.
- **Reality:** regulated utilities run high leverage and stay A-rated *because* their cash flows are rate-regulated, predictable, and near-guaranteed. High stable leverage is investment-grade-normal for the type.
- **Interpretation rule:** for regulated utilities, high *stable* leverage with predictable regulated cash flow is normal; the real signal is regulatory/rate-case risk and cash-flow predictability, not the leverage ratio.
- **Distress-tell:** an adverse regulatory outcome (rate case denied), cash-flow predictability breaking down, OR — the catastrophic case — a large liability outside the regulated model (cf. PG&E wildfire liability, which had no ratio footprint and was structurally invisible). Benign = high leverage + stable regulated cash flow. Real = leverage high AND regulatory support or cash-flow stability eroding.
- **Dashboard instruction:** *"High leverage is normal for regulated utilities given predictable cash flows. Concern is regulatory/rate-case risk and cash-flow stability, not the leverage level."*
- **Data Tier-2 needs:** utility-sector leverage distribution; ideally a regulatory-jurisdiction context. **Confirm empirically** once utilities are in the peer universe.

---

## 6. Bank / financial institution  **[ANALYST]**

- **Applies to:** banks, insurers, broker-dealers, specialty finance.
- **What the flat model misreads:** a bank is "leveraged" ~10:1 *by design* — leverage/coverage ratios are meaningless or alarming when read as if it were an industrial.
- **Reality:** financial institutions use an entirely different metric set — capital adequacy (Tier 1 / CET1 ratios), deposit stability, loan quality / non-performing loans, liquidity coverage ratio. (This system already suppresses leverage/coverage for financials — the right instinct; this is the starkest "different rules entirely" type.)
- **Interpretation rule:** do not apply leverage/coverage scoring to financials at all. Use (or flag the need for) a financial-institution-specific metric set.
- **Distress-tell:** capital ratios falling toward regulatory minimums, deposit flight, rising non-performing loans, or a liquidity-coverage breach. The tells are entirely different from the corporate set.
- **Dashboard instruction:** *"Financial institution — corporate leverage/coverage metrics do not apply. Assess capital adequacy, deposit stability, and asset quality."*
- **Data Tier-2 needs:** a separate FI metric module — out of scope for the corporate model; flag clearly rather than mis-score.

---

## 7. Early-stage / high-growth (pre-profit)  **[ANALYST]**

- **Applies to:** biotech, pre-profit / high-growth tech, some pre-scale consumer.
- **What the flat model misreads:** negative FCF, cash burn, no earnings → reads as terminal distress.
- **Reality:** the company is *funding growth*, not declining. Negative FCF is the plan, backed by raised capital and a long runway.
- **Interpretation rule:** for pre-profit growth companies, negative FCF + strong liquidity + revenue growth = normal; the real signal is **runway** (cash ÷ burn rate), not FCF being negative.
- **Distress-tell:** runway shortening *and* financing access drying up — liquidity tightening with no new capital raised, burn accelerating without revenue growth, or a down-round / failed raise. Benign = burning cash with ample runway and growth. Real = runway < ~12 months and capital markets closed to them.
- **Dashboard instruction:** *"Pre-profit growth company — negative FCF is expected. Watch runway (cash ÷ burn) and financing access, not FCF sign."*
- **Data Tier-2 needs:** runway metric (cash / burn rate); growth-stage classification. **Confirm empirically** when growth names are added.

---

## 8. Asset-light services  **[ANALYST]** *(thinly evidenced — annotated healthy controls)*

- **Applies to:** consulting, software/SaaS, staffing, business services.
- **Touched by:** the Phase-5b annotated healthy controls (Accenture-type) and Paychex — names with little debt and few tangible assets.
- **What the flat model misreads:** asset-coverage / tangible-asset-coverage / liquidation-coverage metrics misfire because there are almost no tangible assets to cover the debt.
- **Reality:** asset-light businesses are valued on recurring revenue, contracts, and cash generation — not balance-sheet assets. Low asset coverage is structural, not weakness.
- **Interpretation rule:** for asset-light services, asset-coverage ratios are noise; weight cash-flow stability, revenue recurrence, and customer retention instead.
- **Distress-tell:** the cash-flow / revenue base itself deteriorating — recurring revenue declining, customer churn rising, or operating cash flow turning negative. Benign = thin assets, strong recurring cash flow. Real = the cash-flow engine weakening.
- **Dashboard instruction:** *"Asset-light — asset-coverage ratios are not meaningful here. Assess recurring revenue and cash-flow stability."*
- **Data Tier-2 needs:** down-weight asset-coverage metrics for the sector; revenue-recurrence indicator.

---

## 9. How this feeds Tier 2 and the dashboard

1. **Classification first:** each company is assigned a type (reuse the `classify()` SIC + description logic from `thresholds.py`). The type selects which interpretation rules apply.
2. **Tier-2 re-scoring:** the sector benchmark distributions (from the peer universe) replace the cross-sector thresholds for the rules the type cares about, and *suppress / down-weight* the rules the type renders meaningless (asset-coverage for asset-light; leverage/coverage for financials).
3. **Distress-tell as the guard:** the interpretation rule only *relaxes* the flag when the benign condition holds; the moment the distress-tell fires (coverage falls with leverage, runway shortens, damage persists across the cycle), the flag stands. This is what keeps Tier-2 honest — it is not a blanket excuse for a sector.
4. **Dashboard text:** each applied rule emits its `dashboard instruction` sentence, so the analyst sees *why* a pattern is or is not a concern, in the language of how that industry works — not a bare number.

---

## 10. Open items

- The **numeric** benchmarks (sector distributions) require the peer universe in Freeman's Supabase (~6 healthy names per sector) — blocked on the Freeman data-layer sync.
- The **[ANALYST]** patterns (utilities, banks, growth) must be **confirmed empirically** as those sectors enter the library — do not hard-code their thresholds from theory; author the interpretation rule now, calibrate the numbers from data later.
- Every distress-tell needs to become a concrete, computable condition (e.g. "coverage falls with leverage" → a coded co-movement check). This catalogue defines them in analyst language; the Tier-2 build translates each into code.
- This closes the loop on the migration detector's documented buyback blind spot (Lilly/Amgen): it is **Pattern 1**, and Tier-2 is where it is correctly handled — not a defect, a known type-pattern awaiting the interpretation layer.

---

*This spec follows the contract-first style of the metric and LLM specs: it defines the interpretation rules and — critically — the distress-tell that keeps each rule a detector rather than an excuse, so the Tier-2 build and the dashboard text can be checked against the catalogue rather than improvised.*
