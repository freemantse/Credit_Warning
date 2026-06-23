# LLM_COVENANT.md — Covenant Extraction Specification

**Pass name:** `covenant_extraction`
**Owner module (target):** `src/footnote_review.py` (Freeman's repo), function `extract_debt_footnote()` / a dedicated `extract_covenants()`
**Persistence:** `covenants` table (Supabase)
**Model:** `claude-haiku-4-5` (temperature 0 for determinism; see §8)
**Status:** Tier-1 LLM contract. This document is authoritative — prompts and code must conform to it. A prompt change that removes a REQUIRED behavior in this spec is a regression.

---

## 1. Purpose

Extract, from a company's debt-related disclosures, every **financial covenant** the company is contractually required to satisfy, together with the company's **reported actual level** against that covenant and the **cushion** between them. The cushion — not the existence of the covenant — is the credit signal. A covenant with large headroom is routine; a covenant the company is close to breaching is a leading indicator of distress and a likely trigger for forced refinancing, asset sales, or default.

This pass exists because covenant proximity is one of the earliest, hardest, structured signals of credit deterioration, and because covenant language is frequently **not labeled "covenant"** — it hides in prose, in risk factors, and in MD&A liquidity discussions. Keyword matching alone misses it. This pass must catch it anyway.

---

## 2. Scope — what counts as a covenant

A covenant, for this pass, is **any contractual financial requirement, limit, or test the company must satisfy to avoid default, acceleration, or a restriction on its actions.** It does not need to use the word "covenant."

### 2.1 In scope (extract these)

- **Maintenance covenants** — ratios the company must maintain *at all times* or *as tested each period* (e.g. "maintain a consolidated leverage ratio not to exceed 4.00 to 1.00"). Highest signal — these are tested continuously and breach is immediate.
- **Incurrence covenants** — tests that apply only when the company takes an action (e.g. "may not incur additional indebtedness if the fixed charge coverage ratio would be less than 2.00 to 1.00"). Lower immediacy, still material.
- **Springing covenants** — covenants that activate only on a condition (e.g. "a minimum fixed charge coverage ratio applies if availability falls below $X"). Flag the trigger condition explicitly.
- **Negative covenants with a financial test** — restrictions on dividends, buybacks, investments, or asset sales that are *conditioned on a financial ratio* (e.g. "restricted from paying dividends unless the net leverage ratio is below 3.5x").
- **Cross-default / cross-acceleration clauses** — a default under one instrument triggering default under another. Capture as `covenant_type = "cross_default"` with the evidence quote; these amplify any single breach into a systemic one.
- **Minimum liquidity / minimum availability requirements** — (e.g. "maintain minimum liquidity of $250 million").

### 2.2 Out of scope (do NOT extract)

- General descriptions of debt terms with no required threshold (coupon, maturity, principal) — those are debt-schedule data, handled elsewhere.
- Aspirational or forward-looking management targets that are **not contractual** ("we aim to reduce leverage to 3x by 2026"). A target is not a covenant. If the text does not tie the figure to a credit agreement, indenture, or contractual requirement, it is out of scope.
- Covenants of unconsolidated affiliates or third parties that do not bind the issuer.

### 2.3 The critical recall rule (team requirement)

**The word "covenant" will frequently be absent.** The pass MUST extract covenant-like language regardless of labeling. Trigger phrases include, but are not limited to:

- "required to maintain…"
- "may not exceed…" / "shall not be greater than…" / "shall not be less than…"
- "financial maintenance test" / "financial tests"
- "ratio of … to …" tied to a credit agreement or indenture
- "failure to comply with … could result in …default / acceleration"
- "we are subject to certain financial and operating restrictions"
- "the credit agreement contains restrictions that require us to…"

If a sentence imposes a measurable financial condition the company must meet under a debt agreement, it is a covenant for this pass — extract it.

---

## 3. Source sections (what text the pass reads)

The pass reads, in priority order, and SHOULD run against all that are present:

1. **Debt / long-term obligations footnote** (primary). Heading patterns: "Debt", "Long-Term Debt", "Borrowings", "Credit Facilities", "Notes Payable". Window: up to 40,000 characters.
2. **MD&A — Liquidity and Capital Resources** (secondary). Covenant compliance and proximity language frequently appears here, especially for companies under stress. Window: up to 40,000 characters.
3. **Risk Factors** (recall pass only). Distressed companies often disclose covenant pressure in risk factors before it appears in the footnote. Window: up to 40,000 characters; scan for financial-test language only.

If the section locator returns a low-confidence match (no heading anchor; chunk fallback), the extraction MUST record `section_confidence = "low"` on every finding from that window (see §5), because a covenant pulled from an unanchored slice is less reliable.

**A covenant the model never sees because the section locator missed the footnote is a miss no prompt can fix.** Section-locator recall is part of this pass's quality and must be validated alongside it (§9).

---

## 4. Two-pass extraction (recall-critical)

Covenants are the signal you most cannot afford to miss, so this pass runs in **two stages** and unions the results:

- **Stage A — narrow/precise.** Read the debt footnote. Extract covenants with full structured fields (§5). High precision.
- **Stage B — broad/recall.** Scan MD&A Liquidity + Risk Factors for *any* financial-test language per §2.3. Lower precision; the goal is to catch covenants that migrated out of the footnote.

**Deduplication:** after both stages, collapse findings that describe the same covenant. Two findings are the same covenant when they share `covenant_type` AND `direction` AND the same threshold value (within rounding) OR overlapping evidence quotes. Keep the one with the most complete fields (prefer a finding that has `reported_actual` over one that doesn't). Record `sources` as the union of where it was found.

---

## 5. Output schema

Every covenant is one record. Fields marked **REQUIRED** must always be present; **NULLABLE** fields must be `null` with a `null_reason` when not disclosed — **never guessed** (see §6).

```json
{
  "covenant_type":     "max_leverage",      // REQUIRED — controlled vocabulary, see §5.1
  "covenant_subtype":  "maintenance",        // REQUIRED — maintenance | incurrence | springing | negative | cross_default | min_liquidity
  "direction":         "max",                // REQUIRED — "max" (must not exceed) | "min" (must maintain at least)
  "metric_name":       "consolidated_net_leverage", // REQUIRED — free text label of what is tested
  "threshold":         4.0,                  // NULLABLE — the contractual limit, as a number, ONLY if stated verbatim
  "threshold_unit":    "ratio",              // NULLABLE — "ratio" | "usd" | "percent"
  "reported_actual":   3.7,                  // NULLABLE — the company's reported level, ONLY if disclosed
  "cushion":           0.3,                  // DERIVED — see §7; null if threshold or reported_actual is null
  "cushion_pct":       7.5,                  // DERIVED — cushion as % of threshold; null if not computable
  "near_limit":        true,                 // DERIVED — see §7.2
  "springing_trigger": null,                 // NULLABLE — condition that activates a springing covenant
  "evidence_quote":    "The Credit Agreement requires the Company to maintain a consolidated net leverage ratio not to exceed 4.00 to 1.00, tested quarterly. As of the period end, the Company's consolidated net leverage ratio was 3.7 to 1.0.", // REQUIRED — verbatim
  "source":            "10-K 2023-12-31, Debt footnote", // REQUIRED
  "section_confidence":"high",               // REQUIRED — "high" (heading-anchored) | "low" (chunk fallback)
  "null_reason":       null                  // REQUIRED when any NULLABLE field above is null; else null
}
```

### 5.1 `covenant_type` controlled vocabulary

Must be one of (extends Freeman's existing `covenants.covenant_type`):

`max_leverage` · `min_coverage` · `min_net_worth` · `min_liquidity` · `max_capex` · `min_fixed_charge_coverage` · `cross_default` · `other`

If `other`, `metric_name` must describe what is tested. Do not invent new top-level types; use `other` + `metric_name`.

### 5.2 Compatibility with the existing `covenants` table

Freeman's table has: `covenant_type, threshold, direction, reported_actual, near_limit, evidence_quote, source`. This spec is a **superset** — it adds `covenant_subtype, metric_name, threshold_unit, cushion, cushion_pct, springing_trigger, section_confidence, null_reason`. The new columns are additive; the existing columns keep their meaning. The unique key remains `(cik, period_end, covenant_type, evidence_quote)`.

---

## 6. Grounding contract (anti-hallucination) — NON-NEGOTIABLE

This is the most important section. A fabricated covenant is worse than a missed one, because it manufactures a false signal.

1. **Every numeric value** (`threshold`, `reported_actual`) MUST appear **verbatim** in `evidence_quote`. If "4.00 to 1.00" is the threshold, the string "4.00 to 1.00" (or "4.0x", as written) must be in the quote. Reuse the existing `_number_in_text()` check — a finding whose number is not in its quote is **dropped**, not kept.
2. **`evidence_quote` must be a verbatim span** from the source text — not paraphrased, not stitched from non-contiguous fragments. If the limit and the actual appear in different sentences, the quote may span both sentences contiguously; it may not splice distant text.
3. **If a field is not disclosed, it is `null` with a `null_reason`** — never inferred. If the filing states a leverage covenant limit but does not disclose the company's actual leverage, `reported_actual = null`, `null_reason = "actual ratio not disclosed in this filing"`. Do NOT compute the actual from XBRL here and insert it as if disclosed — XBRL-derived actuals are a *separate* downstream join, not an extraction output. (Mixing them corrupts the grounding contract.)
4. **No covenant without an evidence quote.** A finding with an empty or non-matching quote is invalid and dropped.
5. **Extract first, classify second.** The prompt must instruct the model to first quote the relevant sentence(s) verbatim, then assign `covenant_type`/`direction`/`threshold` from that quote. Classifying before quoting invites the model to assert structure the text doesn't support.

---

## 7. Cushion and proximity (the credit signal)

### 7.1 Cushion computation (derived in code, not by the LLM)

The LLM extracts `threshold` and `reported_actual` verbatim. **The cushion is computed in code**, not by the model — never trust the LLM to do arithmetic that feeds a credit decision.

- For `direction = "max"` (must not exceed): `cushion = threshold − reported_actual`. Positive = headroom; negative = **in breach**.
- For `direction = "min"` (must maintain at least): `cushion = reported_actual − threshold`. Positive = headroom; negative = **in breach**.
- `cushion_pct = cushion / |threshold| × 100`.
- If `threshold` or `reported_actual` is null, `cushion = null`, `cushion_pct = null`.

### 7.2 `near_limit` flag

`near_limit = true` when the company is close enough to breach that it is a credit signal:

- `cushion_pct <= 10%` (within 10% of the limit), OR
- `cushion < 0` (already in breach), OR
- the evidence quote contains explicit proximity/waiver language ("obtained a waiver", "amended the covenant", "may not be in compliance", "expects to seek an amendment").

A `near_limit = true` finding with negative cushion (an actual or imminent breach) is among the strongest single signals this whole system produces and should surface prominently to scoring.

### 7.3 Why proximity, not existence

Every leveraged company has covenants. Their existence is noise. The 8%-cushion covenant and the explicit waiver are signal. This pass's value is concentrated entirely in `cushion`, `near_limit`, and breach/waiver language — the prompt and the scoring must both prioritize these over a mere inventory of covenants.

---

## 8. Determinism and consistency

- **Temperature 0** for all covenant extraction calls. Covenant extraction is a factual task; creativity is failure.
- **Self-consistency for high-stakes filings (optional, recall-critical):** for filings where a covenant breach materially affects the score, run the extraction 2–3 times and keep only covenants that appear in the majority of runs. A real covenant reappears identically; a hallucinated one usually does not. This filters residual fabrication beyond the grounding check, at the cost of extra tokens — apply it selectively (e.g. when any finding has `near_limit = true`), not to every filing.

---

## 9. Validation (how we know the pass works)

This pass is tested against a small **golden set** of filings with hand-verified covenant extractions:

- At least 3 filings with **labeled** covenants (footnote, clearly stated) — tests precision.
- At least 3 filings where covenant language is **unlabeled / in prose or risk factors** — tests the §2.3 recall rule.
- At least 2 filings with a **covenant breach or waiver** — tests `near_limit` and breach detection.
- At least 2 filings with **no covenants** — tests that the pass returns an empty list rather than inventing covenants (precision floor).

A prompt change is accepted only if, on the golden set: no labeled covenant is lost, no covenant is fabricated on the no-covenant filings, and the unlabeled-covenant recall does not regress. Track precision (fabrications) and recall (misses) separately — they trade off, and for covenants we bias toward recall while holding fabrications at zero on the no-covenant set.

---

## 10. Prompt requirements (the contract the prompt must implement)

The prompt for Stage A (and, with section text swapped, Stage B) MUST:

1. State the analytical frame: name maintenance, incurrence, springing, negative-with-financial-test, cross-default, and minimum-liquidity covenant types explicitly, and instruct the model to look for each.
2. State the recall rule from §2.3 explicitly, including that the word "covenant" may be absent.
3. Instruct: **quote verbatim first, classify second.**
4. Require the structured JSON of §5, with explicit `null` + `null_reason` for undisclosed fields, and forbid guessing.
5. Require that every number in a finding appear in its `evidence_quote`.
6. Include **few-shot examples** (§10.1): at least one labeled covenant, one unlabeled/prose covenant, and one negative example (looks like a covenant, is not).
7. Instruct the model NOT to compute cushion or arithmetic — only extract `threshold` and `reported_actual` verbatim.

### 10.1 Few-shot examples to embed in the prompt

**Positive — labeled:**
> Text: "The Credit Agreement requires the Company to maintain a consolidated total leverage ratio not to exceed 4.50 to 1.00. As of December 31, the ratio was 4.20 to 1.00."
> Extract: `covenant_type=max_leverage, covenant_subtype=maintenance, direction=max, metric_name=consolidated_total_leverage, threshold=4.5, reported_actual=4.2, evidence_quote="…not to exceed 4.50 to 1.00. As of December 31, the ratio was 4.20 to 1.00."`

**Positive — unlabeled / prose (the team's case):**
> Text: "Under our senior notes indenture, we are restricted from incurring additional indebtedness unless our fixed charge coverage ratio is at least 2.00 to 1.00 on a pro forma basis."
> Extract: `covenant_type=min_fixed_charge_coverage, covenant_subtype=incurrence, direction=min, metric_name=fixed_charge_coverage, threshold=2.0, reported_actual=null, null_reason="pro forma actual not disclosed", evidence_quote="…unless our fixed charge coverage ratio is at least 2.00 to 1.00 on a pro forma basis."`

**Negative — not a covenant:**
> Text: "Management aims to reduce net leverage to approximately 3.0x over the next two fiscal years."
> Extract: *(nothing — this is an aspirational target, not a contractual requirement; not tied to a credit agreement or indenture)*

---

## 11. Failure modes to guard against (lessons to encode)

- **Fabricating a covenant on a filing that has none.** Guard: the no-covenant golden cases; the grounding contract; empty-list is a valid and correct output.
- **Reporting a stale `reported_actual`** from a prior period's text. Guard: `reported_actual` must come from the *same* filing's quote; if the quote references a prior period, note it in `null_reason` and prefer null.
- **Splicing a threshold from one covenant with an actual from another.** Guard: §6.2 contiguous-quote rule.
- **Treating a target as a covenant.** Guard: §2.2 and the negative few-shot.
- **Missing the covenant because the section locator missed the section.** Guard: two-pass design (§4) and section-locator recall validation (§9).
- **Letting the LLM compute cushion** and trusting bad arithmetic. Guard: §7.1 — cushion is computed in code only.

---

## 12. Downstream use

Findings from this pass feed:

- The **covenant-proximity scoring rule** (a `near_limit` or in-breach covenant is a high-severity signal).
- The **trend/migration detector** — emergence of `near_limit = true` where it was absent in prior filings is a strong deterioration-trend signal (the qualitative analogue of a rising score), and appears in the "component sequence" of deterioration (covenant pressure typically follows liquidity tightening and precedes formal distress).
- The **going-concern pass** (separate spec) — covenant breach + going-concern language together is near-conclusive.

---

*This spec follows the same contract-first style as the metric specs (FREE_CASH_FLOW.md, ASSET_COVERAGE.md, etc.): it defines the required behavior, the output shape, and the validation bar, so any prompt or code change can be checked against it rather than judged by feel.*
