# Covenant Recall & Reasoning — Design Notes

**Status:** design only (not built). Future phase, implemented as the `part='covenants'`
worker job once the `full` worker is committed.
**Problem this solves:** (1) *recall* — catching covenants expressed without the word
"covenant" or scattered across sections; (2) *accuracy* — making covenant judgments
auditable and human-checkable, never blindly scored.
**Hard constraint:** accuracy is paramount; cost/time (Anthropic rate limit 10k tokens/min,
$/filing) is a real second constraint that shapes — but does not override — the design.

---

## 1. The two real problems

1. **Recall.** Covenant information is NOT confined to one paragraph or one footnote. It
   *concentrates* in the Long-Term Debt footnote but *scatters* across a defined set of
   sections: Commitments & Contingencies, Subsequent Events, Going Concern, MD&A
   (Liquidity), and the Credit Agreement exhibit. A covenant is often phrased with **no
   keyword** — e.g. "the Company is required to maintain a ratio of consolidated
   indebtedness to EBITDA of not more than 4.50 to 1.00" contains no "covenant".
   → Pure keyword="covenant" search misses these.

2. **Accuracy / trust.** Even when found, a covenant judgment can be wrong (real quote but
   wrong interpretation; maintenance vs incurrence confusion; a quote that doesn't actually
   support the claim). → LLM output must be grounded and human-checkable, not auto-scored.

---

## 2. The funnel (cheap → expensive)

Run the FREE deterministic filters before any LLM, so the expensive LLM budget is spent
only on a pre-narrowed candidate set. "Generous" then applies *within* an already-narrowed
pool, not the whole filing.

```
FREE deterministic narrowing (zero LLM cost):
  (a) Section selection — feed only covenant-relevant sections
      (Debt footnote + Commitments/Contingencies + Subsequent Events
       + Going Concern + MD&A Liquidity + Credit Agreement exhibit).
      NOT every footnote (skip inventory, stock-comp, pension, revenue-rec, etc.).
      NOT just one paragraph (covenants scatter across the set above).
  (b) Keyword pre-filter — drop clearly-irrelevant paragraphs using TWO keyword families:
        • financial-measure terms: leverage, EBITDA, interest/fixed-charge coverage,
          net worth, liquidity, availability, capex, debt/indebtedness, current ratio…
        • covenant/legal terms: covenant, comply/compliance, default, breach, waiver,
          amend/amendment, "required to maintain", "shall not exceed", event of default,
          acceleration.
  (c) XBRL check — if DebtInstrumentCovenant* tags exist, use them (free, accurate);
      never treat a null tag as compliance.
        ↓ (only survivors reach the LLM)

CHEAP generous LAYER-1 (semantic triage — high recall):
  small/fast model (Haiku), tiny output (paragraph numbers only), batched input.
  Prompt: "List paragraphs containing ANY financial covenant / obligation to maintain a
  financial measure / compliance statement / breach / waiver — regardless of wording.
  When in doubt, INCLUDE it." (Generosity = low inclusion bar.)
        ↓ (only flagged paragraphs)

EXPENSIVE strict LAYER-2 (careful reasoning — high precision):
  grounded extraction over flagged paragraphs:
    • verbatim quote required (no quote → no finding; no-fabrication floor)
    • extract structured slots: threshold, exact ratio name as written, testing
      frequency, step-down schedule, compliance status, headroom
    • classify maintenance (real trigger) vs incurrence (action-gated, NOT a stress trigger)
    • <think> reasoning exposed for each finding (why it's a covenant; why maintenance/incurrence)
    • emit an ADVISED assessment + confidence, NOT a final score
    • null-on-ambiguity → escalate to manual review (never guess a threshold)
        ↓
  ADVISE → human-review packet (reasoning + quote + advised severity) → human confirms
  BEFORE it affects the credit score.
        ↓
  cached forever (cost paid once per cik/period/part); the off-Vercel worker absorbs the time.
```

---

## 3. Layer 1 generous, Layer 2 strict — tuned as a PAIR

**Why generous layer-1:** the two layers have *asymmetric* failure costs.
- Layer-1 false NEGATIVE (drops a covenant) → gone forever, layer-2 never sees it → **fatal miss.**
- Layer-1 false POSITIVE (keeps junk) → layer-2 discards it → **cheap, self-correcting.**
When errors are asymmetric, bias toward the cheap error: **high recall first, precision second.**
Layer-1's job = *don't miss anything*; layer-2's job = *don't be fooled*.

**Correct mental model:** this is the recall/precision asymmetry at INFERENCE — NOT training
"overfit". No training happens here. (A too-strict layer-1 loosely "overfits to the typical
covenant" and fails to generalize to oddly-phrased ones, but the lever is the prompt's
inclusion threshold, not regularization/training.)

**The pairing rule:** generous layer-1 is only safe because strict layer-2 holds the line.
The more you let through layer-1, the harder layer-2 must filter. Tune them together.

**"Generous" is bounded by cost** — infinitely generous = "read everything" = the cost
problem you were escaping. Target: generous enough that an unusually-phrased covenant still
gets flagged; not so generous you re-read the whole filing.

---

## 4. Cost / time levers (real second constraint)

- **Make layer-1 cheap per unit:** smallest model, tiny output, batched input → generous
  ≠ expensive.
- **Free filters first** (section selection, keyword pre-filter, XBRL) → expensive LLM only
  on survivors.
- **9→3 period cap:** covenant present-state needs only recent periods → ~5× cost cut, free.
- **One-time-per-filing caching:** each (cik, period, part) runs ONCE, stored forever →
  cost amortizes to ~0 over time; on-demand + cached = pay once, the first time anyone looks.
- **The worker decouples time from money:** a 5-min thorough run is fine in the worker
  (no 60s clock). Generosity costs *money* (tokens); the worker makes *time* irrelevant.

---

## 5. Measure, then tune (do NOT tune on assumption)

Run 3–5 real filings (e.g. RAD, BBBY, + a couple healthy), capture, then set the dial on facts.

**Measure per filing:**
- Token counts (API returns them): layer-1 in/out, layer-2 in/out → real $/filing.
- Funnel narrowing counts: paragraphs total → after keyword → flagged by L1 → confirmed by L2
  (shows where each stage earns its place).
- **Recall proxy:** for filings whose covenants are known by hand, did layer-1 *flag the
  paragraphs* containing every known covenant? (Missed one → layer-1 too strict.)
- Time per job (and how much was rate-limit waiting).

**Capture:** start manual (print numbers for 3–5 filings); later add a structured per-job log
or a `cost_log` table once the worker runs continuously.

**Tune:**
- Recall failure (missed a known covenant) → ALWAYS fix: loosen layer-1, even at cost.
- Cost trim → ONLY when the measured $/filing is actually painful; trim the stage that
  dominates tokens (usually layer-2 → reduce layer-1 over-flagging).
- Bias: keep generous unless measured cost forces a trim (accuracy is the priority).

**Likely outcome:** cost turns out small (Haiku cheap + hard funnel narrowing + period cap +
caching), so the abstract "generous = expensive" tension is smaller than it feels — but you
won't *know* until you measure. Facts first, dial second.

---

## 6. How this becomes the `part='covenants'` worker job

- The `llm_jobs` table already has a `part` column: `going_concern / covenants / breach / full`.
  Today only `full` is wired (runs whole `review_filing`).
- Per-part = add branches in the worker's `_run_job`:
  `if part == 'covenants': run THIS funnel only` (anchor → triage → strict extract → advise).
- Benefits: bounded/smaller job (fits the worker easily; maybe even Vercel's 60s if ever
  synchronous); on-demand ("Analyze covenants for X" button → `part='covenants'` job → cache);
  independent caching (re-running covenants doesn't re-run going-concern); per-part review packet.
- **Sequencing:** finish + commit the `full` worker → decide finder approach → build the
  `covenants` part. Each step bounded, builds on the last.

---

## 7. Approaches considered (for the recall "finder" stage)

- **A — Keyword anchor:** code picks relevant paragraphs by keyword. Cheap (free filter),
  but recall capped by keyword-list completeness; misses covenants using un-listed measures.
- **B — Semantic coarse-pass (preferred):** LLM triages relevance by *meaning* in a cheap
  first pass; no keyword-list blind spot, higher recall; costs one extra cheap LLM call.
- **C — XBRL-first:** use structured covenant tags where present (free, accurate); sparse in
  practice → use as a pre-check layered under A/B, not standalone.
- **D — Embedding/similarity retrieval:** retrieve paragraphs by semantic similarity; over-
  engineered for this scale, fiddly thresholds → skip.

**Recommended synthesis:** XBRL check (free) → keyword pre-filter (free, gross narrowing) →
**B semantic coarse-pass** (generous, on survivors) → strict grounded extraction → advise →
review → cache. Section selection sits ABOVE all of these (which sections you feed in is the
recall decision above the finder choice).

---

## 8. Rejected / out of scope

- **RAG** — wrong fit; you have the precise source in hand, not a search problem; adds a
  retrieval failure mode. Your quote-grounding already beats it.
- **Training / fine-tuning (incl. BioReason-style)** — a general LLM ALREADY knows covenants;
  training needs labeled data you don't have and *reduces* auditability. Adopt BioReason's
  *epistemics* (explicit reasoning, advise-don't-decide, human-in-loop) — NOT its mechanism
  (training, per-sentence classification).
- **Per-sentence LLM classification** — hundreds of calls/filing → catastrophic on the rate
  limit; breaks multi-sentence covenants. Use candidate *paragraphs* in one reasoning pass.
- **FinBERT / sentiment models** — measure equity-sentiment, not credit-direction; parked.

---

*This design is the synthesis of a long working session: keyword-anchoring + advised-score
ideas + BioReason transparency + existing quote-grounding/null-rules + cost discipline.
It is NOT yet built. Next concrete step is unrelated: finish + commit the `full` worker.*
