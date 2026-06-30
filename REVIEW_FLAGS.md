# REVIEW_FLAGS.md — Human-Review Flag Mechanism (design note)

**Status:** Design note / future feature. Not yet built. Captures a cross-cutting principle: where the system makes a judgment under genuine ambiguity that *matters*, it must surface that ambiguity — with the verbatim filing quote(s) and a reason — for human review, rather than resolving it silently inside a confident-looking output.
**Build order:** the covenant-similarity (dedupe-ambiguity) case is the trigger and the first to build; the others follow the same pattern.
**Not in scope of any committed stage** — this is additive, sits across the LLM passes, and partly touches Freeman's dashboard (the queue view).

---

## 1. The principle

A credit early-warning tool informs real money decisions. An analyst trusts it more when it says *"I'm not sure about this one — here's the filing language, you decide"* than when it silently makes a borderline call that looks authoritative. So: **the machine distinguishes what it is confident about from what needs human eyes, and for the latter it emits the verbatim evidence and a reason — it does not silently resolve the ambiguity.**

This is the opposite of a black box. It is how a junior analyst escalates to a senior one: not by hiding the hard call, but by surfacing it with the source document attached.

The trigger example (covenant similarity): two covenant statements may be the *same* covenant restated, or *two distinct* covenants. Silently merging loses a real covenant; silently splitting double-counts. When the system can't be confident which, it should flag *"possible duplicate or two distinct covenants — review these two quotes"* and let an analyst decide in seconds against the actual 10-K language.

---

## 2. The calibration law (the heart of this design)

The whole value of this feature lives in *what gets flagged*. Flag too much → analysts drown in review items and ignore the queue (alert fatigue; the feature becomes noise). Flag too little → the real ambiguities slip through silently (the feature is useless). "Productive — never too much, never too little."

**The rule that achieves this: flag = UNCERTAIN *and* MATERIAL.**

- **Uncertainty alone is not enough.** Most low-confidence findings don't affect any decision — flagging all of them is noise.
- **Materiality alone is not enough.** A material finding the system is *confident* about needs no review — it's just a result.
- **Only the intersection** — a judgment the system is genuinely unsure about *that also affects the credit assessment* — earns a flag.

This single rule is what keeps the queue sparse and high-value. Every flag type below is an instance of "uncertain AND material," with the uncertainty and materiality conditions made precise so the flag fires narrowly.

A useful sanity target: on a healthy, clean filer, the review queue should be **near-empty**. Flags should cluster on the genuinely ambiguous, materially-stressed cases. If healthy names generate many flags, the calibration is too loose.

---

## 3. The flag types

Each carries: `needs_review` (bool), `review_reason` (which type), `review_evidence` (the verbatim quote(s) — and the candidate pair for similarity), `review_priority` (high/low, for triage).

### 3.1 Covenant-similarity / dedupe-ambiguity  **(build first — the trigger)**
- **Uncertain:** two covenants are same `covenant_type` + `direction` and sit in the *borderline band* — they did NOT meet the merge bar but are suspiciously close: one threshold null while the other has a value, OR thresholds differ but evidence quotes partially overlap. (Confidently-merged and confidently-distinct pairs are NOT flagged.)
- **Material:** at least one of the pair is a maintenance covenant or carries `near_limit` (i.e. it could move the score). Two clearly-immaterial incurrence tests don't need review.
- **Carries:** both verbatim quotes + both sources, reason "possible duplicate or two distinct covenants."
- **Priority:** high if either is near_limit.

### 3.2 Low section-confidence on a material finding
- **Uncertain:** the finding came from a chunk-fallback (unanchored) section (`section_confidence = "low"`) — the locator wasn't sure it grabbed the right section, so the extraction *could* be from the wrong place.
- **Material:** the finding affects the score — a `near_limit` covenant, a Tier-1 going-concern, or a material loss provision. (A low-confidence finding that doesn't move the score is not flagged — that's the "not too much" cut.)
- **Carries:** the quote + which section it was pulled from, reason "extracted from an unanchored section — verify it's the right context."

### 3.3 Tier-2 going-concern that is pivotal
- **Uncertain:** Tier-2 is the inherently-subtle tier (soft precursor; already low-confidence by construction).
- **Material:** the Tier-2 finding is *pivotal* — it is the deciding signal nudging an otherwise-clean company toward concern, NOT merely corroborating an already-stressed one. (A Tier-2 precursor on a company already flagged by ratios needs no review; a Tier-2 precursor that is the *only* warning does.)
- **Carries:** the quote + the `adverse_conditions`, reason "soft going-concern precursor is the deciding signal — confirm it's genuine."
- **Priority:** high (it's the sole driver).

### 3.4 Null-actual near_limit blind spot
- **Uncertain:** a covenant has a disclosed `threshold` but no `reported_actual`, so cushion is uncomputable. `near_limit = False` here means *"we cannot tell,"* not *"safe"* — a genuine blind spot the numeric rule silently reads as not-near.
- **Material:** it's a *maintenance* covenant (breach can trigger default). An incurrence test we can't measure matters less. 
- **Carries:** the covenant quote + threshold, reason "maintenance covenant present but current level not disclosed — proximity unknown, check the filing / other periods for the actual."
- **Priority:** high if the company is otherwise stressed.

> Note 3.4 is the honest counterpart to the 2c-i decision that null-actual → near_limit=False. That default is correct for *scoring* (don't fabricate proximity), but it hides a real blind spot — this flag surfaces it for human eyes without inflating the score. The two work together: score conservatively, flag the uncertainty.

---

## 4. How a flag surfaces (decided)

**A field on the record + a dashboard-derived queue — NOT a separate review table.**

- Each finding (covenant / going-concern) carries `needs_review`, `review_reason`, `review_evidence`, `review_priority` as columns (additive, nullable — like the 2c-i columns).
- The "review queue" is simply the dashboard filtering `needs_review = true`, sorted by priority. No separate table.

**Why this and not a separate review-queue table:** the flag is *about* a specific finding, so it belongs *on* that finding — one source of truth, it travels with the record, nothing to keep in sync. A separate table would duplicate state and could drift from the findings it references ("too much"). A field with no way to surface it would leave flags invisible ("too little"). Field-on-record + filter-view is the productive minimum that is still complete: one mechanism, one derived view.

---

## 5. What this is NOT (non-goals — the "not too much" guardrails)

- **NOT a confidence score on every finding.** This is a *sparse* flag on the uncertain-AND-material subset, not a per-finding probability. Scoring everything would be noise.
- **NOT a second scoring path.** A flag does not change the score — `near_limit`, tiers, and weights are unchanged. The flag is purely a *review signal* layered beside the score.
- **NOT a catch-all.** Only the four defined ambiguity types flag. New types are added deliberately, each with its own uncertain-AND-material condition — never a generic "flag anything unusual."
- **NOT auto-resolution.** The system never *acts* on a flag (never auto-merges, auto-drops). It surfaces; the human decides.

---

## 6. Calibration validation (how to know it's "just right")

When built, validate the flag *rate*, not just correctness:
- **Healthy filers → near-empty queue.** Run the flag logic over the healthy controls; the review queue should be sparse (single-digit flags total, ideally zero on the cleanest names). A flood on healthy names = calibration too loose → tighten the materiality condition.
- **Distressed/ambiguous filers → flags cluster where they should.** The covenant-similarity flag should fire on the genuine restatement-vs-distinct cases (e.g. the Tuesday Morning max_leverage pair), not on confidently-distinct covenants.
- **Hand-check a sample of flags:** each should be a judgment a human would genuinely want to review — if a flagged item is obvious either way, the condition is too loose.
- **Hand-check what was NOT flagged:** confirm no genuinely ambiguous *and* material case slipped through — if so, the condition is too tight.

The acceptance bar is the principle itself: a queue an analyst would actually work through because every item earns its place.

---

## 7. Build order

1. **Covenant-similarity (3.1)** — the trigger; the dedupe gap documented in 2c-ii is its first home. Replaces "silently don't-merge → cosmetic double-count" with "flag the borderline pair for review."
2. **Null-actual near_limit (3.4)** — small, high-value, pairs naturally with the 2c-i near_limit logic.
3. **Low section-confidence (3.2)** and **Tier-2 pivotal going-concern (3.3)** — once the field + dashboard view exist.

Each is additive (new nullable columns + a dashboard filter), gated by the calibration validation in §6, and committed separately.

---

*This note captures a principle worth building deliberately: surface genuine, material uncertainty with its evidence for human review, rather than resolving it silently. The calibration law (flag = uncertain AND material) is what keeps it productive — a sparse, high-value queue, never alert-fatigue noise, never a missed real ambiguity.*
