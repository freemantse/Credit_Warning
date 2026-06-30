> **REJECTED (measured)** — `finding_locality_measure.py` showed MD&A findings are
> diffuse (RAD: tone spans 68% of MD&A, ~30% single-extractor routing footprint), so a
> triage layer saves little and concentrates mis-tag risk. Real fix if scale demands:
> prompt-merge the extractors, not triage. Overlap is tolerable now via chunking + tier.

# Unified Layer-1 (Semantic Triage) — Design Plan

**Status:** design only (not built). Build when scale (many companies) justifies the
token saving; for current scale, per-extractor chunking + a rate-tier bump suffice.
**Mechanism (settled):** layer-1 is an **LLM that UNDERSTANDS** — a cheap semantic
triage, NOT a deterministic keyword locator. Understanding is the point: it must catch
covenant/credit language that uses NO obvious keyword (e.g. "required to maintain a
ratio of indebtedness to EBITDA of not more than 4.50" — no word "covenant"). A
keyword locator would miss exactly these; that is why layer-1 is an LLM.

---

## 1. The problem it solves (verified)

Today, 4 built LLM extractors each independently read their sections, so the same text
is sent to the LLM multiple times. Measured overlap:

| Section | Read by | # LLM calls today |
|---|---|---|
| **MD&A** | qualitative, covenants Stage B, covenant breach, going-concern | **4×** |
| Debt footnote | covenants Stage A, covenant breach | 2× |
| Risk factors | covenants Stage B, going-concern | 2× |
| Contingencies | loss provisions | 1× |
| Auditor report / GC footnote | going-concern | 1× |

MD&A is the heaviest waste (read 4×, and it is the largest section at the 60k/100k cap).
The redundancy is wasted tokens — bad, and it multiplies rate-limit pressure.

The built LLM extractors (for reference): covenants (`extract_debt_footnote`,
`extract_covenants_broad`, `extract_covenant_breach`, `run_covenants`), loss provisions
(`extract_loss_provisions`), going concern (`extract_going_concern`), qualitative/tone
(`review_text`). The one GAP: debt-maturity **schedule** LLM extraction is design-only
(currently XBRL next-12-months bucket only) — best built INTO layer-1 rather than as
another standalone redundant reader.

---

## 2. The architecture: read once → understand → route

```
ONE-TIME (already exists, free, deterministic):
  fetch filing → locate_sections (regex+density) → {debt, mdna, risk_factors,
  contingencies, auditor_report, going_concern_footnote}

LAYER 1 — semantic triage (NEW; a cheap LLM that UNDERSTANDS):
  Reads each (large/multi-read) section ONCE. A lean prompt asks the model to TAG each
  paragraph by relevance — by MEANING, not keyword:
    covenant? going-concern? contingency? maturity-table? credit-relevant tone?
  Output: tiny — just paragraph indices + tags (no few-shots, no structured schema).
  Generous: when in doubt, tag it (protect recall; the strict layer-2 filters).

LAYER 2 — strict extractors (EXISTING, logic UNCHANGED, just fed less):
  Each extractor runs ONLY on the paragraphs layer-1 tagged for it:
    covenant ext ← covenant-tagged   |  going-concern ext ← GC-tagged
    provision ext ← contingency-tagged |  maturity ext (the gap) ← maturity-tagged
    tone/qualitative ← credit-tone-tagged (→ review queue, NOT auto-score)
  Each keeps its grounded, quoted, validated extraction (the proven accuracy floor).
```

**Why LLM-understanding, not a locator:** a deterministic keyword/density locator is free
but reintroduces the exact keyword-dependence the covenant design deliberately rejected —
it would miss keyword-free covenants. Layer-1 must *understand* relevance to catch those.
A deterministic signal may be used as an ADDITIONAL hint, never as the gate.

---

## 3. Where the token saving actually comes from (honest)

The saving is **killing the repetition**, not aggressive trimming:
- Today: MD&A read 4× in full (~4 × ~15k tok of section text).
- Layer-1: MD&A read ONCE for triage (~15k tok + a small lean prompt), then each
  extractor reads only its tagged subset.
- Net win is real for MULTI-read sections (MD&A 4×, debt 2×, risk 2×). For SINGLE-read
  sections (contingencies, auditor) layer-1 ADDS cost — so do NOT route those through
  layer-1; extract them directly. Layer-1 is surgical: apply it to multi-read / oversized
  sections only.
- Saving scales with how small the tagged subset is, but even if the subset is largish,
  "read once + route" beats "read 4×." That repetition-kill is the durable win.

Model: use the EXISTING `claude-haiku-4-5` (already the cheap model in-family). No
provider switch — the integration cost of a second provider isn't worth it for a
classification task Haiku already does. Keep the layer-1 prompt LEAN (tiny output).

---

## 4. Principles preserved

- **Accuracy:** layer-2 extractors unchanged (same prompts, grounding, quote-validation,
  null-on-ambiguity). Layer-1 only decides what text to feed them.
- **Generous L1 / strict L2:** L1 over-tags (don't miss); L2 filters strictly. The
  recall/precision asymmetry, applied system-wide.
- **Tone as review-flag, not score:** L1 tags credit-tone paragraphs → routed to the
  human review queue, never an automatic score input (avoids the sentiment/FinBERT trap).
- **News as evidence (separate fast-clock pipeline):** when filing language is tricky/
  ambiguous, recent company news is attached to the SAME review queue as corroborating
  evidence (pos/neg/neutral tag is a reviewer scan-aid, not a score input). News never
  overrides the filing; the filing stays authoritative.

---

## 5. The real risks (be clear-eyed before building)

1. **Cost may move, not vanish.** L1 reads the section to classify it; saving depends on
   multi-read sections + a meaningfully smaller tagged subset. Mitigate: apply L1 only to
   multi-read/oversized sections; keep its prompt lean.
2. **Risk concentration (the big one).** A layer-1 MIS-TAG starves ALL downstream
   extractors of that paragraph — today an error in one extractor doesn't affect the
   others. This concentrates the failure point, which is a robustness downgrade for an
   accuracy-critical system. Mitigations:
   - L1 generous to near-inclusion (when unsure, tag for everything).
   - For sections SMALL enough to fit the budget whole (e.g. a 3k debt footnote), SKIP L1
     and send the whole section directly — no routing risk. Use L1 routing ONLY when a
     section is too big to send whole (MD&A). I.e. L1 is a fallback for oversized
     sections, not a mandatory gate.
3. **Big rebuild touching all 4 extractors.** Significant effort vs. a working system +
   a proven cheaper fix (chunking). Build when scale justifies.

---

## 6. Recommended sequencing

- **Now (current scale):** per-extractor CHUNKING (already proven in
  `experiments/covenant_chunk_test.py`) handles the rate limit; a TIER bump (via
  supervisor) gives headroom. Lower risk, no rebuild. The MD&A-4× waste is a cost
  annoyance, not a blocker, at a handful of companies.
- **When scaling to many companies:** build the layer-1 triage — and start with the
  **MD&A-only** version (the 4×-read section = biggest win, contained scope), as a
  SEPARATE file/experiment first (don't touch proven extractors), measure the real
  token saving + parity, then integrate.
- **Maturity extractor (the gap):** build INTO layer-1 (so it consumes already-read debt
  footnote paragraphs) rather than as a 5th standalone redundant reader.

---

## 7. One-line summary

Layer-1 = a cheap LLM that UNDERSTANDS relevance (semantic triage, catches keyword-free
language), reading each multi-read/oversized section ONCE and routing tagged paragraphs
to the unchanged strict extractors — killing the MD&A-4× redundancy. Real but bounded
saving; the main risk is mis-tag concentration (mitigate: generous tagging + whole-section
fallback for small sections). Build the surgical MD&A-only version when scale justifies;
chunking + tier bump suffice now.
