# Moody's Adjustment (Formula 2) — Implementation Specification

**Purpose.** This document consolidates the Moody's-style ("Formula 2") adjustment set into a single execution-ready specification. It is extracted from the ratio specs (`LEVERAGE.md`, `INTEREST_COVERAGE.md`, `FREE_CASH_FLOW.md`) and completed with two adjustment families those specs did not yet cover (hybrid securities / equity credit, and securitizations / captive finance).

**Scope.** The Moody's adjustment restates three credit ratios onto Moody's analytic basis:

1. **Adjusted Leverage** = Moody's Adjusted Net Debt / Moody's Adjusted EBITDA
2. **Adjusted Interest Coverage** = Moody's Adjusted EBIT / Moody's Adjusted Interest Expense
3. **Adjusted Free Cash Flow / Retained Cash Flow** (and RCF / Net Debt)

All three share the *same underlying adjustments* (leases, pensions, non-recurring items) applied consistently to debt, EBITDA, EBIT, interest, and cash flow. An input extracted once (e.g. annual operating-lease cost) feeds multiple ratios.

**Data sourcing principle.** Each adjustment leg is one of:

- **Deterministic (XBRL):** computable from a structured `us-gaap` tag, no judgment.
- **LLM footnote extraction:** the number is not in structured XBRL and must be read from a named footnote/table by an LLM, then **grounded** (the extracted number must appear verbatim in a cited source sentence) before use.

**Error-handling principle (applies to every leg).** If a required input is missing, mark the adjusted ratio **null / unavailable** and log which input failed. **Never silently substitute zero.** An adjustment that cannot be computed is *unavailable*, not *zero*.

**Overfunded / negative-deficit floor.** Any "deficit" or "excess" leg is floored at zero (an overfunded pension is not negative debt).

---

## Part 1 — The Moody's Formula-2 Adjustment Set

### 1. Adjusted Leverage

```text
Adjusted Leverage = Moody's Adjusted Net Debt / Moody's Adjusted EBITDA

Moody's Adjusted Net Debt =
    Total Debt (Formula 1 definition)
  + Capitalized Operating Lease Obligation          [Leg A1]
  + Unfunded Pension Deficit (PBO − plan assets)     [Leg B1]
  + Hybrid debt-treated portion                      [Leg G1 — complement]
  + On-balance-sheet securitization / captive debt   [Leg H1 — complement]
  − Unrestricted Cash                                [Leg F]

Moody's Adjusted EBITDA =
    Operating Income
  + D&A
  + Pension service-cost reclassification            [Leg B2]
  + Operating-lease depreciation component
        (2/3 of annual rent expense)                 [Leg A2]
  − Non-recurring gains (if any)                      [Leg C]
  [Does NOT add back restructuring charges — treated as operating]
```

**Net effect vs Formula 1.** Both adjusted debt and adjusted EBITDA increase. The leverage ratio can move either way; for heavily leased businesses (retail, airlines) the debt increase typically dominates and leverage rises.

### 2. Adjusted Interest Coverage

```text
Adjusted Interest Coverage = Moody's Adjusted EBIT / Moody's Adjusted Interest Expense

Moody's Adjusted EBIT =
    Operating Income
  + Pension service-cost reclassification            [Leg B2]
  + Operating-lease depreciation component
        (2/3 of annual rent expense)                 [Leg A2]
  − Non-recurring gains (if any)                      [Leg C]
  [Does NOT add back restructuring charges]

Moody's Adjusted Interest Expense =
    Reported Interest Expense
  + Pension interest-cost reclassification           [Leg B3]
        (excess pension expense over service cost,
         reclassified from operating to interest)
  + Operating-lease interest component
        (1/3 of annual rent expense)                 [Leg A3]
  − Capitalized interest (if any — not yet cash paid) [Leg D]
```

**Net effect vs Formula 1.** Adjusted interest expense is typically higher (lease interest + pension interest reclass); adjusted EBIT is also higher. Net direction depends on which dominates — for heavily leased businesses the interest increase usually outweighs the EBIT increase, compressing coverage.

### 3. Adjusted Free Cash Flow / Retained Cash Flow

```text
Moody's Adjusted FCF =
    Reported Operating Cash Flow
  + Pension cash contributions added back            [Leg B4]
        (deducted from OCF under GAAP; Moody's treats
         as financing — adds back to OCF)
  − Maintenance Capex                                 [Leg E]
        (maintenance only; growth capex treated separately)
  − Dividends paid (common and preferred)

Moody's Retained Cash Flow (RCF) =
    Reported Operating Cash Flow
  + Pension cash-contribution addback                 [Leg B4]
  − Dividends paid
  (RCF excludes capex — cash retained before reinvestment)

RCF / Net Debt = cash generation relative to debt burden
  (companion to Debt/EBITDA in Moody's rating grids)
```

---

### Adjustment legs — definitions, sourcing, and treatment

#### Leg A — Operating leases

Moody's capitalizes operating leases and splits the annual rent/lease cost **1/3 to interest, 2/3 to depreciation**. Post-ASC 842 (FY2019+) the ROU liability is on the balance sheet and tagged; pre-2019 the rent expense lives only in the footnote and must be capitalized by a 5×–10× industry multiple (or PV of minimum commitments if higher).

| Leg | Adjustment | Formula | Source | Structured? |
|----|----|----|----|----|
| A1 | Lease → **debt** | Capitalized operating-lease obligation added to net debt | Post-2019: `us-gaap:OperatingLeaseLiabilityCurrent` + `OperatingLeaseLiabilityNoncurrent` (sum). Pre-2019: annual rent × 5–10× | Post-2019 **XBRL**; pre-2019 **LLM** |
| A2 | Lease → **EBITDA / EBIT** | + 2/3 × annual operating-lease cost (depreciation component) | `us-gaap:OperatingLeaseCost` | **XBRL** where tagged |
| A3 | Lease → **interest** | + 1/3 × annual operating-lease cost (interest component) | `us-gaap:OperatingLeaseCost` | **XBRL** where tagged |

**Consistency rule.** A2 (2/3) and A3 (1/3) come from the **same** `OperatingLeaseCost` figure; together they are the full rent split. Never source them from different figures.

**Where it lives (pre-2019 rent, A1/A2/A3 fallback):** Lease Footnote (typically Note 5–8, "Leases" or "Commitments and Contingencies"). LLM reads the first table/paragraph disclosing total rent expense: *"Total rent expense was $X."*

> **⚠️ Implementation notes — Leg A (learned in build)**
> - **Same figure for both split legs.** A2 (2/3) and A3 (1/3) must be derived from the *same* `OperatingLeaseCost` value. Sourcing them from different figures produces an internally inconsistent split. (1/3 + 2/3 must reconstruct the whole rent.)
> - **Gate A3 on the lease-cost tag *itself*, not a default-to-0.** For the interest leg, the lease-interest term *is* the entire adjustment. If `OperatingLeaseCost` is untagged, the leg is **unavailable** (MissingRatio) — it must NOT default the lease cost to 0, because that silently returns the *unadjusted* coverage figure **mislabeled as "adjusted."** This differs from A1 (leverage), where the ROU liability is the gate and lease cost is an optional add. **(Verified in code: `interest_coverage_adjusted` raises `MissingDataError` when `operating_lease_cost` is untagged — does not default to 0. The pension legs gate identically on their own inputs.)**
> - **Degenerate denominator — deliberate, and safe as currently wired (nuanced).** A2/A3 divide adjusted EBITDA/EBIT by adjusted interest; a negative-EBITDA issuer yields a negative ratio. **Verified in code:** `leverage_adjusted`, `interest_coverage_adjusted`, `interest_coverage_pension_adjusted` guard only `== 0` (a *negative* denominator passes through and produces a negative value); only `pension_ebitda_reclass` guards `≤ 0`. **This negative-passthrough is DELIBERATE, not a bug:** the negative ratio is retained for trajectory *visibility/audit* (so an analyst sees the degenerate value rather than a blank — score.py comment "kept for trajectory tracking"), and it is *safe* because (a) `compute_score` separately gates `leverage_adjusted>5x` on `adjusted_ebitda > 0` and defers to `lease_debt_burden`, so the scorer is not fooled, and (b) the migration engine consumes these values only through a clamping ramp where a negative higher-is-worse value maps to the healthy floor (0) — identical to a MissingRatio — so no migration signal depends on the sign. **Do NOT blanket-"fix" to `≤ 0`:** that would remove audit visibility of the degenerate value for zero computational gain. **The real watch-out:** the clamp-to-0 safety holds only because leverage is *higher-is-worse*. If a *lower-is-worse* adjusted ratio (e.g. coverage) ever went negative AND were given nonzero scoring weight, the ramp would map it to *maximum* severity — meaningful and likely wrong. Today those legs are weight-0 so it is inert; revisit before weighting them. (See §Pitfalls #1.)

#### Leg B — Pensions

Moody's treats the unfunded pension deficit as debt, and reclassifies the non-service portion of pension expense out of operating income (lifting EBITDA/EBIT) and into interest.

| Leg | Adjustment | Formula | Source | Structured? |
|----|----|----|----|----|
| B1 | Pension → **debt** | Unfunded deficit = max(0, PBO − plan assets) | Direct `us-gaap:DefinedBenefitPlanFundedStatusOfPlan`; else derive `DefinedBenefitPlanBenefitObligation` − `...FairValueOfPlanAssets` | **XBRL** where tagged; **LLM** fallback (Funded Status table) |
| B2 | Pension → **EBITDA / EBIT** | Reclass addback = total net periodic benefit cost − service cost | `DefinedBenefitPlanNetPeriodicBenefitCost` − `DefinedBenefitPlanServiceCost` (**both required**) | **XBRL** where both tag; **LLM** otherwise |
| B3 | Pension → **interest** | + pension interest cost (the reclassified excess) | `DefinedBenefitPlanInterestCost` | **XBRL** where tagged; **LLM** otherwise |
| B4 | Pension → **FCF / RCF** | + pension cash contributions (added back to OCF) | `us-gaap:PensionContributions` (or `PaymentsForPensionAndOtherPostretirementBenefits`) | **XBRL** where tagged; **LLM** otherwise |

**B2 completeness rule.** The reclass addback is the *excess over service cost*, which requires the **total** net periodic cost. A service-cost-only figure cannot yield the excess — if the total is missing, B2 is **unavailable** (not approximated).

**Where it lives (LLM fallback):** Pension / Employee-Benefits Footnote (typically Note 8–12, "Employee Benefit Plans" or "Pension and Post-Retirement Benefits"). PBO and Fair Value of Plan Assets are in the **Funded Status** table; service/interest cost in the **Net Periodic Benefit Cost** table; contributions in the **Employer Contributions** table. Full detail is 10-K only; 10-Q shows summary updates.

**Grounding requirement (LLM legs).** Each extracted number (PBO, plan assets, service cost, total periodic cost, contributions) must appear **verbatim** in a cited source sentence/row before use; ungrounded numbers are dropped, never guessed. Provenance records the source of each number. *(Implementation note: deterministic legs set `pension_source="xbrl"` on the `RatioResult`; LLM-extracted figures currently carry their provenance on a separate `PensionFallback` object (`pension_source="llm"`), not yet on `RatioResult` itself — see §Pitfalls #8.)*

> **⚠️ Implementation notes — Leg B (learned in build)**
> - **B1 dimensional-segment trap.** PBO and plan-assets tags can in principle resolve to a *domestic-only* or *OPEB-only* segment instead of the plan **total**, silently understating the deficit. **Verified in code: this is NOT enforced by any dimension-filtering logic — it relies on the SEC companyfacts API shape** (which exposes only the undimensioned aggregate, so the tags resolve to the plan total in practice). The resolver returns the first value-bearing match with no Axis/member check. So the correctness is a *data-source assumption*, not a code guarantee — if a dimensional member ever carried a value, the resolver would take it blindly. A future hardening would add an explicit undimensioned-context check.
> - **B2 needs BOTH tags — a service-cost-only figure is not a partial approximation.** The reclass addback is *(total net periodic cost − service cost)*; the excess is uncomputable without the total. If the total is missing, B2 is **unavailable** — do NOT approximate it from service cost alone. (An early scoping call wrongly deemed this "LLM-only / not a clean tag"; it is in fact deterministic where **both** tag, ~19% of filers.)
> - **B4 must be parallel, never in-place.** The FCF contribution addback must NOT be added by editing the live `moody_adjusted_fcf`, because that ratio is scored (and feeds a downstream co-condition gate) — an in-place edit would silently change scores for every filer that tags contributions. Build a **parallel** flag ratio (`..._pension`) and leave the live ratio untouched. An in-place version is a deliberate score-affecting change requiring its own out-of-sample A/B. *(See §Pitfalls #5.)*
> - **B1 funded-status waterfall & overfunded floor.** Prefer the direct funded-status tag → else derive PBO − assets → else unavailable; floor an overfunded plan at 0 (never negative debt).

#### Leg C — Non-recurring gains (subtract from EBITDA / EBIT)

Moody's subtracts confirmed non-recurring gains from EBITDA. Do **not** subtract recurring items (e.g. investment income).

- **Source:** partially tagged (`us-gaap:GainLossOnDispositionOfAssets`, `GainsLossesOnExtinguishmentOfDebt`, `OtherNonoperatingIncomeExpense`), but **whether a gain is genuinely non-recurring requires LLM judgment** — read MD&A → Results of Operations (first 2–3 paragraphs) and any Other-Income footnote to confirm.
- **Designation:** **LLM required** (judgment-laden). Flag as low-confidence; a gain that cannot be confirmed non-recurring is not subtracted.

> **⚠️ Implementation note — Leg C.** This is the *most* judgment-laden leg — "non-recurring" is a narrative determination, not a number. It is the lowest-confidence LLM leg and the one most prone to noise. Build it last, keep it flag-only, and require an explicit cited justification (which disclosed item, from where) for every subtraction. When in doubt, do not subtract.

#### Leg D — Capitalized interest (subtract from adjusted interest)

Interest capitalized into PP&E is not yet cash-paid; Moody's removes it from the interest denominator.

- **Source:** disclosed in the PP&E footnote or interest-expense footnote. Not consistently tagged.
- **Designation:** **LLM required.** Low breadth (capital-intensive issuers only).

#### Leg E — Maintenance vs growth capex (FCF)

Moody's deducts **maintenance** capex only for baseline FCF; growth capex is treated separately.

- **Where disclosed:** MD&A capital-expenditures discussion or PP&E footnote (voluntary; many issuers do not disclose).
- **Proxy where not disclosed:** Maintenance Capex ≈ D&A (`us-gaap:DepreciationDepletionAndAmortization`), flagged *"maintenance capex proxied by D&A — actual split not disclosed."*
- **Designation:** **LLM required** to detect a disclosed split; D&A-proxy fallback otherwise. (Formula 1 uses total capex — most conservative, most auditable.)

#### Leg F — Unrestricted cash (subtract from net debt)

Net debt subtracts **unrestricted** cash only. Restricted cash must not be netted.

- **Source:** `us-gaap:CashAndCashEquivalentsAtCarryingValue`, less `us-gaap:RestrictedCashAndCashEquivalents` where tagged; else confirm via the ASU 2016-18 reconciliation table at the bottom of the Cash Flow Statement, or Note 1.
- **Designation:** **XBRL** where the restricted tag exists; **LLM** confirmation otherwise. (Current baseline nets total cash — a minor approximation to tighten here.)

---

## Part 2 — Adjustment families to complement (not in the source specs)

These two Moody's Standard Adjustments are **not covered** by `LEVERAGE.md` / `INTEREST_COVERAGE.md` / `FREE_CASH_FLOW.md`. They are included here so the specification is complete. Both are lower-breadth (they apply only to issuers with the relevant capital-structure features) and both are **LLM footnote extraction with mandatory grounding** — there is no clean structured tag for the debt-vs-equity classification each requires. Source for both: *Moody's Cross-Sector Rating Methodology — Hybrid Equity Credit* and *Moody's Approach to Global Standard Adjustments* (ratings.moodys.com).

### Leg G — Hybrid securities / equity credit

**What it is.** Hybrid instruments (junior subordinated notes, perpetual preferred, mandatory convertibles, certain trust-preferreds) sit between debt and equity. Moody's assigns each an **equity credit** of 0% / 25% / 50% / 75% / 100% based on its terms (subordination, coupon deferral, maturity/permanence). The instrument's balance-sheet amount is split: the equity-credit portion is removed from debt (treated as equity); the remainder stays as debt.

```text
Hybrid debt-treated portion   [Leg G1] =
    Hybrid carrying amount × (1 − equity-credit %)
Hybrid equity-treated portion =
    Hybrid carrying amount × (equity-credit %)     → removed from debt

Coupon treatment [Leg G2, coverage/FCF]:
    The portion of hybrid coupon corresponding to the equity-credit %
    is treated like a preferred dividend (financing), NOT interest —
    removed from Adjusted Interest Expense to the extent equity-credited.
```

**Equity-credit determination (the judgment).** Driven by three feature axes Moody's weighs: **subordination** (deeply subordinated → more equity-like), **optional/mandatory coupon deferral** (deferrable → more equity-like), and **permanence** (perpetual or very long-dated, no effective step-up/call incentive → more equity-like). A short-dated, non-deferrable, senior instrument gets ~0% (pure debt); a perpetual, deferrable, deeply subordinated one approaches 100%.

**Where it lives.** Long-term Debt footnote and/or a dedicated "Hybrid / Junior Subordinated / Mezzanine Equity" note; terms (maturity, subordination, deferral, step-up/call) in the instrument description; carrying amounts on the balance sheet or in the debt-schedule table. Mandatorily-redeemable preferred may sit in "mezzanine equity" between liabilities and equity.

| Item | Formula | Source | Structured? |
|----|----|----|----|
| Hybrid carrying amount | Instrument balance | Debt footnote / balance sheet / mezzanine-equity line | Semi-structured (amount sometimes tagged; classification not) |
| Equity-credit % | Moody's 0/25/50/75/100 per terms | Instrument terms in the footnote | **LLM required** (terms → judgment) |
| G1 debt-treated portion | amount × (1 − equity %) | derived | derived |
| G2 coupon reclass | equity-% share of coupon → treat as dividend, remove from interest | coupon rate/amount in footnote | **LLM required** |

**Designation:** **LLM required, mandatory grounding.** The carrying amount and coupon must be extracted verbatim and cited; the equity-credit % must be justified by cited instrument terms (subordination / deferral / permanence). If the terms cannot be located and cited, the equity credit defaults to **0% (full debt)** — the conservative treatment — and the instrument is flagged for analyst review, never given equity credit on an unsupported inference.

**Breadth.** Low — only issuers with hybrid instruments outstanding (utilities, insurers, some industrials). Flag-first: surface the classification and its basis for analyst review; do not let an inferred equity credit reduce debt in a scored path without confirmation.

### Leg H — Securitizations / captive finance

**What it is.** Two related off- or semi-balance-sheet situations Moody's re-integrates:

1. **Securitizations (debt-like treatment).** Where a company has sold/pledged receivables under an arrangement that is, in substance, secured borrowing (recourse, retained risk, or a consolidation the issuer treats as sale), Moody's may add the securitized amount back as debt.
2. **Captive finance subsidiaries (segment separation).** For issuers with a captive finance arm (e.g. an OEM with a financing subsidiary), Moody's typically **analyzes the industrial and financial operations separately**, because a finance captive's high, structurally-normal leverage would distort consolidated industrial-company ratios. The captive's debt is segregated (often assessed against its own asset base / on a debt-to-equity basis) rather than lumped into the parent's Debt/EBITDA.

```text
Securitization add-back  [Leg H1] =
    Off-balance-sheet or sale-treated securitized amount with retained
    recourse/risk, added back to Adjusted Net Debt.

Captive-finance separation [Leg H2] =
    Segregate captive finance debt and earnings from the industrial
    parent; compute industrial-only Adjusted Leverage/Coverage, and
    assess the captive on a finance-company basis (debt/equity, asset
    quality) separately. Do NOT apply corporate Debt/EBITDA thresholds
    to the captive's balance sheet.
```

**Where it lives.** Securitization: the Debt / Variable-Interest-Entity / Transfers-of-Financial-Assets footnote, and MD&A liquidity discussion (recourse, retained interests, program size). Captive finance: segment footnote (ASC 280) and MD&A — the captive is usually a reportable segment with its own assets, debt, and revenue; some issuers publish separate industrial-vs-financial-services balance sheets.

| Item | Formula | Source | Structured? |
|----|----|----|----|
| Securitized amount + recourse status | Add back if substantively secured borrowing | Transfers-of-Financial-Assets / VIE footnote; MD&A liquidity | **LLM required** (recourse/substance judgment) |
| Captive finance debt & earnings | Segregate from industrial parent | Segment footnote (ASC 280); separate financial-services balance sheet if disclosed | Semi-structured (segment amounts sometimes tagged; separation logic is LLM/judgment) |

**Designation:** **LLM required, mandatory grounding.** Recourse/substance for securitizations, and the presence + figures of a captive segment, must be extracted from cited footnote text. If a captive segment's separate figures are not disclosed, flag *"captive finance present, separate figures not disclosed — consolidated ratios distorted"* rather than guessing a split.

**Breadth.** Low and sector-specific (auto/equipment OEMs with finance arms; issuers with receivables-securitization programs). Flag-first and analyst-facing.

---

## Part 3 — Cross-cutting requirements

**1. Provenance.** Every adjusted figure records the source of each input. **Verified field semantics:** `RatioResult` carries two independent optional fields, `lease_source` and `pension_source`, each defaulting to `None`. Deterministic legs set the relevant one to `"xbrl"`. Absence is signalled by `None` (or by the ratio being a `MissingRatio` rather than a `RatioResult`) — there is no stored `"none"` string. The `"llm"` value is **reserved** on `RatioResult` and not yet produced there; today an LLM-extracted figure (the pension fallback) carries `pension_source="llm"` on a *separate* `PensionFallback` object. The two `RatioResult` fields are structurally independent, so lease and pension provenance never collide.

**2. Grounding (all LLM legs).** An extracted number is accepted only if it appears verbatim (within tolerance for unit scaling, e.g. thousands/millions) in its own cited evidence quote, and the quote is present in the located footnote. Ungrounded → dropped, never stored. This is the trust mechanism for every LLM leg (B1–B4 fallback, C, D, E, G, H).

**3. Availability, not zero.** Any leg whose inputs are absent makes its adjustment **unavailable** for that filer/period (the ratio falls back to Formula 1), logged with the missing input. Never zero-substitute.

**4. Consistency across ratios.** The same extracted input feeds every ratio it belongs to (e.g. operating-lease cost drives A2 in EBITDA/EBIT and A3 in interest; pension contributions drive B4 in both FCF and RCF). Extract once, apply everywhere.

**5. Materiality gating (analyst-facing legs).** High-breadth flags (esp. the pension LLM fallback) should fire only when the adjustment is **material** — recommended gate: unfunded deficit / gross debt ≥ ~10% (size-normalized, tunable), to avoid alert fatigue. Lean permissive: surface borderline-material cases with their citation for analyst judgment.

**6. Restructuring is NOT added back** (Moody's F2). It remains an operating cost. (This is the primary divergence from company-reported "adjusted EBITDA," and from S&P's F3 which also refuses it.)

**7. LLM section locators must span the target table.** For every LLM leg, the footnote *locator* — not just the extractor — is load-bearing. A locator that anchors on a weak sub-heading can return a slice that **misses the actual data table** (e.g. the Funded Status table), causing the extractor to abstain even where the data exists. The located slice must span the heading through its data table (heading anchor + an end-boundary), without over-widening to the whole filing. **Validate the locator on real filings**, not just the extractor logic — a correct extractor fed a bad slice produces silent under-coverage.

---

## Part 3.5 — Pitfalls / lessons learned (cross-cutting)

These are the recurring difficulties encountered building the deterministic legs. They are collected here because they apply across multiple legs. **All items below have been verified against the current implementation** (file:line available in the build history); where the code diverges from the ideal, it is flagged as a known gap.

1. **Degenerate denominators — DELIBERATE and safe as wired (not a simple bug).** Every adjusted ratio divides by EBITDA, EBIT, or interest — each can be zero or negative. **Verified state:** `leverage_adjusted`, `interest_coverage_adjusted`, `interest_coverage_pension_adjusted` guard only `== 0`, so a *negative* denominator passes through as a negative value; only `pension_ebitda_reclass` guards `≤ 0`; `moody_adjusted_fcf_pension` has no denominator. **This is intentional, not a gap to blindly close:** (a) the negative is *retained for trajectory audit/visibility* (analyst sees the degenerate value, not a blank); (b) `compute_score` separately gates `leverage_adjusted>5x` on `adjusted_ebitda > 0` and defers to `lease_debt_burden`, so scoring is not fooled by the negative; (c) migration consumes these only via a clamping ramp where a negative higher-is-worse value → healthy floor (0), identical to a MissingRatio, so no trajectory signal depends on the sign. **Rule:** do NOT convert to `≤ 0`/MissingRatio just for tidiness — it removes audit visibility for no computational benefit. **Genuine watch-out:** the safety relies on leverage being *higher-is-worse* (negative → clamps to 0). A *lower-is-worse* adjusted ratio (e.g. coverage) going negative *with nonzero weight* would ramp to *max* severity — wrong. Those legs are weight-0 today (inert); revisit this before ever weighting them.

2. **Gate on the adjustment input itself, never default-to-0.** Where the adjustment term *is* the whole difference from the base ratio (e.g. lease-interest in adjusted coverage), gating must be on that input's tag. Defaulting a missing input to 0 silently returns the **unadjusted** figure relabeled as "adjusted" — a correctness bug that is invisible in output. **Rule:** missing adjustment input → unavailable, not zero. **Verified correct in code:** `interest_coverage_adjusted` and all three pension legs raise `MissingDataError` when their gating input is untagged (they do not default to 0). (`leverage_adjusted` correctly *does* default lease cost to 0, because its gate is the ROU liability, not the cost — the right behaviour for that leg.)

3. **Dimensional-segment resolution — DATA-SOURCE ASSUMPTION, not code-enforced.** XBRL tags for pension items can in principle resolve to a *dimensional member* (domestic-only, OPEB-only) rather than the plan **total**, silently understating the figure. **Verified state:** correctness relies on the SEC companyfacts API exposing only the undimensioned aggregate — there is **no** dimension/Axis/member filter in the resolver; it returns the first value-bearing match. In practice this yields the plan total, but if a dimensional member ever carried a value the resolver would take it blindly. **Rule / hardening:** add an explicit undimensioned-context check rather than relying on the feed's shape.

4. **The scored-rule / grouping invariant.** *(Codebase-specific, not an adjustment-methodology issue — belongs to the scoring/migration wiring, documented separately.)* Any new scored rule key must be registered in every structure that mirrors the scored-rule set, or a downstream module fails to import. Noted here only as a reminder that adding a new adjustment *rule* touches more than the ratio function. *(Handled via the separate migration/scoring wiring doc — do not conflate with the adjustment definitions above.)*

5. **Parallel, not in-place, for any live-scored ratio.** Adding an adjustment leg by editing a ratio that is already scored (or feeds a scoring co-condition) silently changes scores. **Rule:** build a parallel ratio for the adjusted variant; treat any in-place change to a live-scored ratio as a deliberate, separately-validated (out-of-sample A/B) change.

6. **In-sample vs out-of-sample for any scoring-weight decision.** Adjustments validated only on the calibration set flatter themselves. On fresh hold-out data these adjustments added **zero new catch and only false positives** — because they fire on names the base scorecard already flags. **Rule:** never decide a scoring weight on the calibration set; validate on a fresh hold-out, company-out. This is why all adjustments ship flag-first (weight 0) pending sector normalization.

7. **Locator quality (LLM legs).** See Part 3 item 7 — the section locator is as important as the extractor; validate it on real filings.

8. **Independent provenance per family.** A single provenance field cannot express "lease=xbrl, pension=llm." Each adjustment family carries its own source marker, tracked independently. **Verified:** `RatioResult` has independent `lease_source` and `pension_source` fields (both default `None`); deterministic legs set `"xbrl"`. Two caveats: absence is `None` (or a `MissingRatio`), not a stored `"none"`; and `"llm"` is reserved-but-unused on `RatioResult` today — the LLM pension fallback carries `pension_source="llm"` on a separate `PensionFallback` object, so a within-`RatioResult` "lease=xbrl, pension=llm" is not yet realizable. Unifying LLM provenance onto `RatioResult` is a future step.

---

## Part 4 — Execution checklist (per leg)

For each leg, "done" means: computed on the correct basis, sourced per the table, ungrounded/absent inputs handled as *unavailable* (not zero), provenance recorded, and (for LLM legs) every number grounded to a citation.

| Leg | Adjustment | Ratios affected | Sourcing | Acceptance criteria |
|----|----|----|----|----|
| A1 | Lease → debt | Leverage | XBRL (post-2019) / LLM (pre-2019) | ROU current+noncurrent summed; pre-2019 rent×multiple only via grounded LLM; unavailable if neither |
| A2 | Lease → EBITDA/EBIT | Leverage, Coverage | XBRL `OperatingLeaseCost` | +2/3 lease cost; unavailable (MissingRatio) if untagged — never 0 |
| A3 | Lease → interest | Coverage | XBRL `OperatingLeaseCost` | +1/3 lease cost; **same figure as A2**; unavailable if untagged |
| B1 | Pension → debt | Leverage | XBRL / LLM fallback | direct funded-status → else PBO−assets → else unavailable; overfunded floored to 0 |
| B2 | Pension → EBITDA/EBIT | Leverage, Coverage | XBRL (both tags) / LLM | (total periodic − service); **both required** else unavailable |
| B3 | Pension → interest | Coverage | XBRL / LLM | + pension interest cost; unavailable if untagged |
| B4 | Pension → FCF/RCF | FCF, RCF | XBRL / LLM | + contributions added back; unavailable if untagged |
| C | Non-recurring gains | Leverage, Coverage | LLM (judgment) | subtract only LLM-confirmed non-recurring gains, cited; recurring items untouched |
| D | Capitalized interest | Coverage | LLM | subtract cited capitalized interest; unavailable otherwise |
| E | Maint. vs growth capex | FCF | LLM / D&A proxy | disclosed split (grounded) → else D&A proxy, flagged → else total capex |
| F | Unrestricted cash | Leverage | XBRL / LLM confirm | net only unrestricted; do not net restricted cash |
| G | Hybrid equity credit | Leverage, Coverage | LLM (mandatory grounding) | equity-credit % justified by cited terms; unsupported → 0% (full debt) + flag |
| H | Securitization / captive | Leverage | LLM (mandatory grounding) | recourse/substance cited; captive segregated or flagged if figures undisclosed |

**Recommended build order** (deterministic-first, then LLM by breadth × groundability):

1. **Deterministic XBRL legs** — A1 (post-2019), A2, A3, B1, B2, B3, B4, F. Highest reliability, no LLM.
2. **LLM pension fallback (B1–B4 for untagged filers)** — highest-breadth LLM leg; footnote tables are groundable.
3. **LLM capex split (E)** and **capitalized interest (D)** — moderate/low breadth, groundable.
4. **Non-recurring gains (C)** — judgment-laden; build last / lowest confidence.
5. **Hybrids (G)** and **securitization/captive (H)** — low breadth, sector-specific; mandatory grounding, conservative defaults.
6. **Pre-2019 rent (A1 fallback)** — shrinking breadth.

---

## Appendix A — Implementation status (as of handoff)

This appendix is informational — it records what is already built so the coworker knows the starting point. The core spec above is the target regardless of current status.

| Leg | Status | Notes |
|----|----|----|
| A1 lease→debt | **Built (deterministic)** | Post-2019 ROU; committed |
| A2 lease→EBITDA | **Built (deterministic)** | +2/3 lease cost; committed |
| A3 lease→interest | **Built (deterministic)** | +1/3 lease cost; `interest_coverage_adjusted`; committed |
| B1 pension→debt | **Built (deterministic)** | funded-status waterfall; ~26% coverage; committed |
| B2 pension→EBITDA | **Built (deterministic)** | full (total−service) reclass; ~19% both-tagged; committed |
| B3 pension→interest | **Built (deterministic)** | `interest_coverage_pension_adjusted`; committed |
| B4 pension→FCF | **Built (deterministic, parallel flag)** | `moody_adjusted_fcf_pension`; live `moody_adjusted_fcf` untouched; committed |
| B1–B4 LLM fallback | **In progress** | Pension footnote extractor; grounding proven; locator tuning pending |
| C non-recurring gains | **Not built** | LLM judgment-laden |
| D capitalized interest | **Not built** | LLM, low breadth |
| E capex split | **Not built** | D&A proxy only |
| F unrestricted cash | **Approximation** | nets total cash; restricted carve-out pending |
| G hybrids / equity credit | **Not built (newly specified here)** | LLM, mandatory grounding |
| H securitization / captive | **Not built (newly specified here)** | LLM, mandatory grounding |

**Design decision on scoring (applies to all legs).** All adjustments currently ship **flag-first (scoring weight 0)** — a grounded, analyst-facing adjusted number, not a score parameter. Validation showed that adding scoring weight to these adjustments amplifies false positives on structurally-levered / financial sectors without adding catch, because they fire on already-flagged names. Scoring weight for any adjustment is therefore gated on a sector-normalization layer (separate workstream). Grounding — not calibration — is the trust mechanism.

---

## Appendix B — Source references

- **Leverage F2** — `LEVERAGE.md`, "Formula 2 — Moody's-Style"; "Where it lives"; input/tag tables.
- **Coverage F2** — `INTEREST_COVERAGE.md`, "Formula 2 — Moody's-Style" (adjusted EBIT and adjusted interest legs).
- **FCF/RCF F2** — `FREE_CASH_FLOW.md`, "Formula 2 — Moody's-Style" (pension contribution addback, maintenance-vs-growth capex, dividends; RCF/Net Debt).
- **Hybrids (G)** — Moody's *Cross-Sector Rating Methodology: Hybrid Equity Credit*; *Moody's Approach to Global Standard Adjustments* (ratings.moodys.com). *Newly specified in this document.*
- **Securitizations / captive finance (H)** — *Moody's Approach to Global Standard Adjustments*; segment reporting under ASC 280. *Newly specified in this document.*
