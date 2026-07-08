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

**Grounding requirement (LLM legs).** Each extracted number (PBO, plan assets, service cost, total periodic cost, contributions) must appear **verbatim** in a cited source sentence/row before use; ungrounded numbers are dropped, never guessed. Provenance must record `xbrl` vs `llm` so the source of every number is auditable.

#### Leg C — Non-recurring gains (subtract from EBITDA / EBIT)

Moody's subtracts confirmed non-recurring gains from EBITDA. Do **not** subtract recurring items (e.g. investment income).

- **Source:** partially tagged (`us-gaap:GainLossOnDispositionOfAssets`, `GainsLossesOnExtinguishmentOfDebt`, `OtherNonoperatingIncomeExpense`), but **whether a gain is genuinely non-recurring requires LLM judgment** — read MD&A → Results of Operations (first 2–3 paragraphs) and any Other-Income footnote to confirm.
- **Designation:** **LLM required** (judgment-laden). Flag as low-confidence; a gain that cannot be confirmed non-recurring is not subtracted.

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

**1. Provenance.** Every adjusted figure records the source of each input: `xbrl` (structured tag) or `llm` (footnote extraction). Lease provenance and pension provenance are tracked independently (a filing can be lease=xbrl, pension=llm). No single field should collapse two sources.

**2. Grounding (all LLM legs).** An extracted number is accepted only if it appears verbatim (within tolerance for unit scaling, e.g. thousands/millions) in its own cited evidence quote, and the quote is present in the located footnote. Ungrounded → dropped, never stored. This is the trust mechanism for every LLM leg (B1–B4 fallback, C, D, E, G, H).

**3. Availability, not zero.** Any leg whose inputs are absent makes its adjustment **unavailable** for that filer/period (the ratio falls back to Formula 1), logged with the missing input. Never zero-substitute.

**4. Consistency across ratios.** The same extracted input feeds every ratio it belongs to (e.g. operating-lease cost drives A2 in EBITDA/EBIT and A3 in interest; pension contributions drive B4 in both FCF and RCF). Extract once, apply everywhere.

**5. Materiality gating (analyst-facing legs).** High-breadth flags (esp. the pension LLM fallback) should fire only when the adjustment is **material** — recommended gate: unfunded deficit / gross debt ≥ ~10% (size-normalized, tunable), to avoid alert fatigue. Lean permissive: surface borderline-material cases with their citation for analyst judgment.

**6. Restructuring is NOT added back** (Moody's F2). It remains an operating cost. (This is the primary divergence from company-reported "adjusted EBITDA," and from S&P's F3 which also refuses it.)

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
