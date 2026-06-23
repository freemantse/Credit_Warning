# LLM_GOING_CONCERN.md — Going-Concern Detection Specification

**Pass name:** `going_concern_detection`
**Owner module (target):** `src/footnote_review.py` (Freeman's repo), new function `extract_going_concern()`
**Persistence:** `going_concern` table (Supabase) — new; schema in §6.3
**Model:** `claude-haiku-4-5` (temperature 0; see §8)
**Status:** Tier-1 LLM contract. This document is authoritative — prompts and code must conform to it. A prompt change that removes a REQUIRED behavior here is a regression.

---

## 1. Purpose

Detect whether a filing expresses **doubt about the company's ability to continue operating** — the single strongest qualitative leading indicator in corporate credit. The signal comes in two sharply different forms, and the entire design rests on **keeping them separate**:

- **Tier 1 — formal going-concern.** The standardized "substantial doubt about the ability to continue as a going concern" language that management (ASC 205-40) and auditors (AU-C 570 / AS 2415) are *required* to use when conditions are serious. Near-deterministic, very high precision, but **late** — it confirms advanced distress more than it warns early.
- **Tier 2 — soft precursors.** Hedged, worried-but-not-yet-formal language about liquidity, refinancing dependence, or ability to meet obligations, that often appears **quarters before** a formal flag (or in companies that default without ever filing one). Earlier and higher-recall, but **noisier** — it must be separated from routine risk-factor boilerplate (§4).

The value for an early-warning system is concentrated in Tier 2's lead time, anchored by Tier 1's reliability. The two MUST be labeled distinctly and never blended into a single flag — their meaning and trustworthiness are completely different.

---

## 2. Scope

### 2.1 Tier 1 — formal going-concern (HIGH confidence)
Extract when the filing contains the formal substantial-doubt language, in any of its standard forms:
- "substantial doubt about [its / our / the Company's] ability to continue as a going concern"
- "these conditions [or events] raise substantial doubt about [the Company's] ability to continue as a going concern"
- an **auditor's** explanatory / emphasis-of-matter paragraph expressing going-concern doubt
- management's ASC 205-40 going-concern evaluation concluding that substantial doubt exists (even if "alleviated by management's plans" — capture it, and capture whether the doubt is stated as alleviated or not).

### 2.2 Tier 2 — soft precursors (LOW confidence, graded)
Extract genuine survival-linked doubt language that is NOT the formal Tier-1 statement, e.g.:
- "we may not have sufficient liquidity / capital resources to meet our obligations"
- "our ability to continue operations depends on [refinancing / obtaining additional financing / raising capital]"
- "if we are unable to refinance [or raise capital], we may be unable to continue [operations / as a going concern]"
- recurring-losses / negative-working-capital / negative-equity language tied explicitly to ability to operate or meet obligations
- near-term debt maturities described as exceeding the company's available resources.

### 2.3 Out of scope (do NOT extract — see §4 for the hard cases)
- **Affirmative / reassuring** statements ("we believe our cash and cash equivalents will be sufficient to fund operations for at least the next twelve months") — this is the *opposite* of doubt. Do not extract it as a precursor.
- **Growth-financing** language ("we may seek additional capital to fund expansion / acquisitions / R&D") — capital for opportunity, not survival.
- **Generic, conditional risk-factor boilerplate** not tied to current adverse conditions ("we may need to raise additional capital in the future, which may not be available on favorable terms") — see §4.
- Going-concern doubt about an unconsolidated affiliate or third party that does not bind the issuer.

---

## 3. Source sections (what text the pass reads)

In priority order; run against all present:
1. **Auditor's report / report of independent registered public accounting firm** (Tier 1 primary — the auditor's going-concern paragraph lives here). Window ≤ 40,000 chars.
2. **Going-concern / basis-of-presentation footnote** (Tier 1 — management's ASC 205-40 evaluation). ≤ 40,000 chars.
3. **MD&A — Liquidity and Capital Resources** (Tier 2 primary — soft precursors). ≤ 40,000 chars.
4. **Risk Factors** (Tier 2 — but the highest-boilerplate section; apply §4 discrimination most strictly here). ≤ 40,000 chars.

If the section locator returns a low-confidence (unanchored / chunk-fallback) match, record `section_confidence = "low"` on findings from that window. A going-concern statement the model never sees because the locator missed the auditor's report is a miss no prompt can fix — section-locator recall is part of this pass's quality (§9).

---

## 4. The hard part — genuine doubt vs. routine boilerplate

This is the most important section. Tier 2's value and its risk both live here. The discriminator is **survival-linkage plus adverse conditions**, not keywords.

**Extract as genuine doubt (Tier 2) when BOTH:**
1. the language ties the need (for liquidity, financing, refinancing) to the company's **ability to continue operating or meet its obligations** — survival, not growth; AND
2. it is accompanied by, or refers to, **actual adverse conditions** — recurring losses, negative working capital or equity, covenant breach/waiver, near-term maturities it cannot cover, or similar present distress.

**Do NOT extract (boilerplate / out of scope) when ANY:**
- the capital need is tied to **growth, expansion, acquisitions, or opportunity** rather than survival;
- the statement is **affirmatively reassuring** (cash is "sufficient," company "expects to meet" obligations) — this is the opposite signal;
- it is **generic conditional hedging** with no tie to present adverse conditions ("may need capital in the future," "markets may be unfavorable") — the kind of sentence that appears in thousands of healthy filings;
- it is a hypothetical in a risk factor with no indication the condition is currently present.

**The litmus test the prompt must apply:** *Would a credit analyst reading this conclude the company is signaling concern about its own survival, given conditions that actually exist now — or is this standard cautionary language any company might include?* Only the former is a Tier-2 finding.

**Worked discriminations (these go in the prompt as few-shots):**
- ✅ Tier 2: "We have incurred recurring losses and negative cash flows from operations, and our ability to continue operations is dependent upon our ability to obtain additional financing." → survival-linked + adverse condition (recurring losses).
- ✅ Tier 1: "The accompanying financial statements have been prepared assuming the Company will continue as a going concern. … these conditions raise substantial doubt about the Company's ability to continue as a going concern."
- ❌ Not extracted: "We may require additional capital to fund our growth initiatives and pursue strategic acquisitions." → growth, not survival.
- ❌ Not extracted: "We believe our existing cash and cash equivalents will be sufficient to meet our anticipated needs for at least the next twelve months." → affirmative/reassuring.
- ❌ Not extracted: "We may need to raise additional funds in the future, and such funds may not be available on acceptable terms." → generic boilerplate, no present adverse condition.

---

## 5. (reserved)

---

## 6. Output schema

The pass returns at most a small number of findings (often zero or one). Every finding is one record.

```json
{
  "tier":               1,                       // REQUIRED — 1 (formal) | 2 (soft precursor)
  "confidence":         "high",                  // REQUIRED — "high" for tier 1; "low" for tier 2
  "source_party":       "auditor",               // REQUIRED — "auditor" | "management"
  "doubt_alleviated":   false,                   // NULLABLE — tier 1 only: true if doubt stated as alleviated by management's plans; null for tier 2
  "adverse_conditions": ["recurring losses", "negative working capital"], // REQUIRED for tier 2 (the conditions that justify treating it as genuine); [] for tier 1 if not enumerated
  "evidence_quote":     "These conditions raise substantial doubt about the Company's ability to continue as a going concern.", // REQUIRED — verbatim, contiguous
  "section":            "Auditor's Report",      // REQUIRED — which section it came from
  "section_confidence": "high",                  // REQUIRED — high (anchored) | low (chunk fallback)
  "null_reason":        null                     // REQUIRED when any nullable field is null; else null
}
```

### 6.1 Tier discipline
- `tier = 1` ⟹ `confidence = "high"`. A Tier-1 finding MUST quote the formal substantial-doubt language.
- `tier = 2` ⟹ `confidence = "low"`. A Tier-2 finding MUST populate `adverse_conditions` with the present conditions that justify treating it as genuine (per §4); a Tier-2 finding with no adverse conditions is boilerplate and must NOT be emitted.
- Never emit a finding that blends the two (e.g. soft language labeled tier 1). When both a formal statement and soft precursors are present, emit the Tier-1 finding; soft language in the same filing is redundant and omitted.

### 6.2 Empty result is valid and expected
Most healthy filings contain NO going-concern finding. An empty list is the correct, common output. Do not manufacture a finding to fill output — the §4 boilerplate exclusions exist precisely to keep the empty result clean.

### 6.3 `going_concern` table (new)
`cik, period_end, tier, confidence, source_party, doubt_alleviated, adverse_conditions (json/text), evidence_quote, section, section_confidence, null_reason`. Unique key `(cik, period_end, tier, evidence_quote)`.

---

## 7. Grounding contract (anti-hallucination) — NON-NEGOTIABLE

Identical rigor to the covenant pass:
1. **`evidence_quote` must be a verbatim, contiguous span** from the source text — not paraphrased, not stitched from distant fragments.
2. **No finding without an evidence quote.** Empty/non-matching quote ⟹ finding dropped.
3. **Tier-1 requires the formal phrase in the quote.** If the quote does not contain substantial-doubt / going-concern language, it is not Tier 1.
4. **Tier-2 requires both survival-linkage and adverse conditions present in (or directly referenced by) the quote** (§4). If the quote is generic/growth/affirmative, it is not a finding.
5. **Do not infer doubt that the text does not state.** Absence of reassuring language is NOT evidence of doubt. Only affirmative doubt language counts.
6. **Quote first, classify second.** The model writes the verbatim quote, then assigns tier/conditions from it — never the reverse.

---

## 8. Determinism and consistency

- **Temperature 0.**
- **Self-consistency (optional, for Tier 2 only):** Tier-2 findings are the ambiguous ones; for filings where a Tier-2 finding would materially affect the score, run 2–3 times and keep only findings appearing in the majority. Tier-1 formal language is unambiguous and does not need this.

---

## 9. Validation (golden set)

- ≥3 filings with a **formal Tier-1** going-concern statement (auditor and/or management) — tests Tier-1 precision and recall. (Distressed names near default are natural sources — e.g. pre-bankruptcy 10-Ks.)
- ≥3 filings with **genuine Tier-2 precursors but no formal flag** — tests the §4 discrimination on the *positive* side.
- ≥5 **healthy filings containing boilerplate** capital/risk language — tests that §4 correctly EXCLUDES them (the false-positive floor; this is the hardest and most important test).
- ≥2 filings with **affirmative "sufficient for twelve months" language** — must produce NO finding.

Acceptance: no Tier-1 statement missed; zero findings on the boilerplate/affirmative healthy set; Tier-2 genuine precursors caught. Track Tier-1 and Tier-2 precision/recall **separately** — they have different bars (Tier 1: near-perfect; Tier 2: bias to recall but zero boilerplate false positives on the healthy set).

---

## 10. Prompt requirements (the contract the prompt must implement)

The prompt MUST:
1. Define Tier 1 (formal substantial-doubt) and Tier 2 (survival-linked soft precursor) distinctly, and require the model to label every finding with its tier.
2. State the §4 discriminator explicitly: Tier 2 requires survival-linkage AND present adverse conditions; growth/affirmative/generic-boilerplate language is excluded.
3. Apply the §4 litmus test in the model's own reasoning before emitting a Tier-2 finding.
4. Enforce quote-first-classify-second and the §7 grounding rules.
5. Require `confidence = high` for tier 1, `low` for tier 2; require `adverse_conditions` populated for every Tier-2 finding.
6. State that an empty list is a valid, expected, common output, and that absence of reassurance is not doubt.
7. Include the §4 few-shots: one Tier-1, one genuine Tier-2 (with adverse conditions), and at least three negatives (growth, affirmative, generic boilerplate).
8. Forbid the model from inventing adverse conditions not present in the text.

---

## 11. Failure modes to guard against

- **Labeling boilerplate "we may need capital" as a going-concern precursor** → the dominant false-positive risk. Guard: §4 survival-linkage + adverse-conditions requirement; the boilerplate negatives in the golden set.
- **Reading affirmative reassurance as doubt** ("cash sufficient for 12 months" flagged as a precursor) → Guard: §2.3, §7.5, an affirmative few-shot negative.
- **Blending tiers** (soft language emitted as tier 1) → Guard: §6.1 tier discipline; tier-1 requires the formal phrase in the quote.
- **Manufacturing a finding on a healthy filing** → Guard: empty-list-is-valid; boilerplate golden cases.
- **Missing the auditor's paragraph because the locator missed the auditor's report** → Guard: §3 section priority + locator recall validation.
- **Inferring doubt from financial weakness the text doesn't characterize** → Guard: §7.5 — only affirmative doubt language in the text counts; the model does not editorialize.

---

## 12. Downstream use

- **Scoring:** a Tier-1 finding is a high-severity signal (among the strongest the system produces). A Tier-2 finding is a moderate, lower-confidence signal weighted accordingly — never equal to Tier 1.
- **Migration / trend detector:** the *transition across filings* is itself a powerful deterioration sequence — none → Tier-2 precursor → Tier-1 formal flag is the qualitative late-stage of the component-sequence (after liquidity tightening and covenant pressure). Emergence of a Tier-2 finding where prior filings had none is a strong qualitative deterioration-trend signal.
- **Covenant pass:** a covenant breach/waiver (LLM_COVENANT.md §7) co-occurring with a going-concern finding is near-conclusive of severe distress — the two passes reinforce each other.

---

*This spec follows the contract-first style of LLM_COVENANT.md and the metric specs: it defines required behavior, the output shape, the validation bar, and — most importantly for this pass — the explicit rule for separating genuine survival doubt from routine boilerplate, so any prompt or code change can be checked against the contract rather than judged by feel.*
