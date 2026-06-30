# LLM_EXTRACTOR_PORT.md — Master Build Contract: Port the Credit LLM Extractor into Freeman's Pipeline

**Supersedes** `LLM_COVENANT.md` and `LLM_GOING_CONCERN.md` as the build contract. Those remain as background reference; THIS document is the single source of truth for the build. Where they conflict, this wins.
**Writes against** `SUPABASE_LLM_SCHEMA.md` (the target tables) — read it first; it is the data contract.
**Doubles as the Claude Code build instruction** — the staged inspect-first gate is in §4. Do NOT modify code until Stage 1 is reported and approved. Do NOT commit until reviewed.

---

## 1. What this is

Port the proven extraction logic from **your** `llm_extractor.py` (battle-tested on your own data) into **Freeman's** live pipeline (`src/footnote_review.py` etc.), writing to the **Supabase** tables in `SUPABASE_LLM_SCHEMA.md` — NOT building fresh from theory. This is the same move as the debt-waterfall port: your tested code is the source, his architecture is the home.

**Scope (first port): three extraction groups.**
1. **Covenants** — from your `DebtFootnoteExtraction.Covenant` → extend his `covenants` table.
2. **Going-concern** — from your `Compliance` (going_concern_doubt / flag) + the spec's Tier-1/Tier-2 split → new `going_concern` table.
3. **Debt maturities** — from your `MaturityYear` schedule → new `llm_debt_maturities` table (fallback to his XBRL `debt_maturities`).

Deferred to a later wave (do NOT build now): loss-provisions, asset-composition, capex-split.

**Three improvements over your extractor (keep these — they are why the specs aren't discarded):**
- **(A) Two-pass covenant recall** — your extractor reads the debt footnote (high precision). Add a second *broad* pass over MD&A + risk-factors to catch covenants NOT labeled "covenant" (team requirement #3). Dedupe the union.
- **(B) Cushion / near_limit computed IN CODE** — your extractor and his both leave proximity to the model. The port computes `cushion`, `cushion_pct`, and `near_limit` deterministically after extraction; the prompt is forbidden to emit them.
- **(C) Going-concern Tier-1/Tier-2 split** — your `going_concern_flag` is binary. Add the soft-precursor tier (earlier warning), with `adverse_conditions` required for any Tier-2 finding.

---

## 2. Source / Target / Reference (what to read)

- **SOURCE (logic to port):** `/mnt/user-data/uploads/llm_extractor.py` (your extractor) — the covenant/compliance/maturity Pydantic models, the verbatim-evidence grounding, the `_year_bucket` maturity mapping, the manual JSON parsing.
- **TARGET (pipeline to port into):** Freeman's `src/footnote_review.py` (existing covenant + loss-provision passes, `_extract`, `review_filing`), `src/llm_review.py` (`parse_json_array`, `quote_in_text`, `warn_if_truncated`, `_to_bool`, `_to_float`), `src/sections.py` (`locate_sections`, chunk fallback), `src/store.py` (`save_covenants` etc.), `src/score.py` (`compute_score`), `api/main.py` + `src/track.py` (call sites).
- **DATA CONTRACT (tables to write):** `SUPABASE_LLM_SCHEMA.md` + Freeman's `supabase/schema.sql`.

---

## 3. Reused infrastructure (do NOT reinvent)

The port REUSES Freeman's existing machinery — these are correct and shared:
- **Grounding:** `_number_in_text()` (±1% after ×1/1e3/1e6/1e9) for every numeric field; `quote_in_text()` (normalized substring, 80-char prefix tolerance) for every evidence quote. A finding failing either is dropped.
- **LLM call:** his `anthropic.Anthropic()` client — already honors `ANTHROPIC_BASE_URL` (the relay/APIYI your extractor uses), so no client changes needed. **Set `temperature=0`** (his code omits it → defaults to 1.0; both specs require 0).
- **Parsing:** `parse_json_array()` (strips fences, json.loads, [] on non-list).
- **Section confidence:** `Section.heading_matched` (truthy = high, None = low) — already exists, just never propagated. The port propagates it onto every finding as `section_confidence`.

---

## 4. STAGE 1 — INSPECT AND CONFIRM (do this first; write NO code; report and stop)

Read SOURCE, TARGET, and the schema, then report:

1. **Confirm the three target tables** match `SUPABASE_LLM_SCHEMA.md` and report the exact `ALTER TABLE covenants ADD COLUMN` set + the `CREATE TABLE going_concern` + `CREATE TABLE llm_debt_maturities` you will apply. Confirm they are idempotent and additive (his existing covenant pass keeps working).
2. **Map your extractor's models → the columns** — produce the field-mapping table (your `Covenant`/`Compliance`/`MaturityYear` fields → Supabase columns), including the normalizations: `threshold_value→threshold`, `evidence→evidence_quote`, `direction "maximum"/"minimum"→"max"/"min"`, `covenant_type` 8-vocab reconciliation, maturity `year_label→bucket "y1".."y5"/"thereafter"`, and **the maturity UNIT** (your millions vs his `debt_maturities.value` — report his unit from `store.py`/`extract.py` and state how you will reconcile).
3. **Report his `_COVENANT_TYPES` whitelist** (currently 4) and the 8-value superset you will widen it to, so new types are not silently dropped.
4. **Report the section-locator gap** — confirm `locate_sections()` returns only mdna/debt/contingencies, and state the new section patterns you will add (risk_factors, auditor_report, going_concern_footnote) and how `section_confidence` will be propagated to findings.
5. **Report the call sites** — the exact lines where `review_filing` is unpacked (track.py, api/main.py ×2) that must change from 3-tuple to 4-tuple, and confirm they will be updated atomically with the signature change.
6. **The covenant-writer decision (2c dependency):** confirm whether the ported covenant pass REPLACES his `extract_debt_footnote` (one writer → extend his table, as the schema assumes) or runs alongside it (two writers → revisit). State which, and why.
7. **The model decision:** your extractor uses `claude-opus-4-8`; his uses `claude-haiku-4-5`. Report both and your recommendation per pass (see §8). Flag it for the reviewer; do not silently pick.

**Report all seven and STOP.** Wait for approval before any code (Stage 2).

---

## 5. STAGE 2 — STAGED BUILD (only after Stage 1 approval; each sub-stage reviewed before the next)

### Stage 2a — Section locator + confidence propagation (pure addition, lowest risk)
- Extend `sections.locate_sections()` to also find **risk_factors**, **auditor_report**, and the **going_concern / basis-of-presentation footnote**, with heading regexes + chunk fallback in the existing style. Keep windowing conventions (≤40k for the new footnote/auditor sections; risk-factors can be large — window it).
- Propagate `Section.heading_matched` → `section_confidence` ("high"/"low") onto every finding produced downstream.
- This breaks nothing (additive). Report which sections now resolve for a sample of filings (including a distressed one with an auditor going-concern paragraph). Review before 2b.

### Stage 2b — Going-concern pass (net-new; one writer; no touch to his covenant code)
- Add `extract_going_concern(section_text, filing_label, client) -> list[GoingConcern]`, reading the auditor-report + going-concern-footnote (Tier-1) and MD&A + risk-factors (Tier-2) sections from 2a.
- Implement the Tier-1/Tier-2 contract: Tier-1 requires the formal substantial-doubt phrase in the quote; Tier-2 requires survival-linkage AND non-empty `adverse_conditions` (else drop as boilerplate). Confidence high/low by tier. Map your `Compliance.going_concern_flag`/`status='going_concern_doubt'` → Tier-1; the soft precursors → Tier-2.
- Add `save_going_concern()` to `store.py` (his style); write to the `going_concern` table.
- Extend `review_filing` to a **4-tuple** (`findings, covenants, loss_provisions, going_concern`) and update the 3 call sites atomically.
- Extend `compute_score` to consume going-concern: Tier-1 = high-severity signal; Tier-2 = moderate, low-confidence (never equal to Tier-1). Add the weights to `DEFAULT_CONFIG["llm"]`.
- Validate (golden set §9). Review before 2c.

### Stage 2c — Covenant pass: port your richer extractor + two-pass + cushion-in-code (the behavior-changing stage)
- Replace his `extract_debt_footnote` covenant extraction with your richer `Covenant` model (ratio_name, unit, testing_frequency, is_springing, springing_trigger, step_down, is_maintenance, 8-type vocab) — Stage A (narrow, debt footnote).
- Add **Stage B** (broad recall sweep over MD&A + risk-factors) for unlabeled covenants; **dedupe** A∪B (same type+direction+threshold or overlapping quotes; keep the most complete; union sources).
- Compute `cushion`, `cushion_pct`, `near_limit` **in code** (not LLM) per the rule: near_limit if cushion_pct ≤ 10%, OR breach, OR waiver/amendment language present. Normalize `direction` to max/min. Widen `_COVENANT_TYPES` to 8.
- Set `temperature=0`. Extend `save_covenants` for the new columns.
- **Backtest:** because this changes covenant output (and thus scores via `near_limit`), re-run the backtest and report catch-rate / FP delta vs the pre-port baseline. Covenant retrofit is APPROVED only if scores hold or improve. Review before commit.

---

## 6. Grounding contract (every finding, every pass) — NON-NEGOTIABLE

1. **Quote first, classify second** — the model emits the verbatim contiguous `evidence_quote` before any classification field.
2. **Numbers must appear in the quote** — every non-null `threshold`/`reported_actual`/maturity `value` verified by `_number_in_text()` against its quote; fail → set null + `null_reason`, never guess.
3. **Quote must appear in the source** — `quote_in_text()`; fail → drop the finding.
4. **Code does the math** — cushion/cushion_pct/near_limit computed in code; the prompt forbidden to emit them.
5. **Empty is valid** — no covenant / no going-concern is the correct, common output; never manufacture findings.
6. **Tier discipline (going-concern)** — Tier-1 requires the formal phrase; Tier-2 requires non-empty `adverse_conditions`; never blend.

---

## 7. Prompts

Use the prompts from `COVENANT_PROMPT.md` (single template + `{PASS_MODE}` narrow/broad for Stage A/B) and `GOING_CONCERN_PROMPT.md` (Tier-1/Tier-2 with the boilerplate negatives), adapted to your extractor's richer covenant fields. These remain valid implementing prompts; keep their few-shots. The prompt must emit your richer fields and must NOT emit cushion/near_limit/section_confidence (code-derived).

---

## 8. Model choice (decide at Stage 1)

Your extractor uses `claude-opus-4-8`; his pipeline uses `claude-haiku-4-5`. Recommendation to confirm:
- **Covenants (2c):** structured extraction, fully code-verified (numbers-in-quote, quote-in-text, cushion-in-code) → **haiku** is defensible and cheap at scale; the grounding contract catches model errors.
- **Going-concern Tier-2 (2b):** the genuine-doubt-vs-boilerplate call is subtle judgment → consider **opus** (or haiku with the self-consistency check in GOING_CONCERN_PROMPT §8) for this pass only.
- Whatever is chosen, **temperature=0** everywhere. Report cost/quality tradeoff; let the reviewer pick.

---

## 9. Validation (golden set; report per pass)

- **Covenants:** a handful of filings incl. one where covenants hide in MD&A prose (tests Stage-B recall) and one near-limit/waiver case (tests cushion/near_limit derivation). Report Stage-A vs Stage-B yield and the dedupe result.
- **Going-concern:** ≥3 formal Tier-1 filings (distressed pre-bankruptcy), ≥3 genuine Tier-2 (precursors, no formal flag), ≥5 healthy with boilerplate (must yield ZERO findings — the false-positive floor), ≥2 affirmative "sufficient for twelve months" (must yield nothing). Report Tier-1 and Tier-2 precision/recall separately.
- **Maturity:** confirm LLM fallback fires only where XBRL `debt_maturities` is absent; spot-check unit consistency.
- **Backtest (2c only):** catch-rate / FP / lead delta vs pre-port baseline; covenant retrofit approved only if held/improved.

---

## 10. What NOT to do

- Do NOT build from scratch — port your extractor's logic.
- Do NOT modify `score.py`'s ratio/threshold logic beyond adding the going-concern consumption path; do NOT touch the migration detector, rating, or calibrate modules.
- Do NOT touch his XBRL `debt_maturities` table (separate `llm_debt_maturities`).
- Do NOT let the LLM emit cushion/near_limit/section_confidence.
- Do NOT collapse the two-tier going-concern into one flag.
- Do NOT change the 3 call sites partially — the 4-tuple change is atomic.
- Do NOT commit until each sub-stage is reviewed; 2c not until the backtest holds.

---

## 11. Order of operations (summary)

Stage 1 inspect → review → 2a locator (review) → 2b going-concern + 4-tuple (review) → 2c covenant port + two-pass + cushion + backtest (review) → commit. Apply the Supabase DDL (from `SUPABASE_LLM_SCHEMA.md`) before the code that writes to the new tables.

*This document is the single build contract; it supersedes the two prior LLM specs and writes against the settled Supabase schema. It ports proven extractor logic rather than building from theory, keeps the three improvements the specs contributed, and gates the live-code changes behind a staged inspect-first review.*
