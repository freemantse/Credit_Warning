# COVENANT_PROMPT.md — Covenant Extraction Prompt (implements LLM_COVENANT.md)

This file contains the production prompt for the `covenant_extraction` pass. It implements the contract in `LLM_COVENANT.md` (§10). It is a **single adaptable template** used for both passes — the only thing that changes between Stage A (narrow) and Stage B (broad) is the `{PASS_MODE}` block and the `{SECTION_TEXT}` fed in. It is structured so the broad pass can be split into a separate recall-tuned prompt later if validation shows the single template under-recalls (see "Splitting later" at the end).

**Model:** `claude-haiku-4-5`, **temperature 0**.
**Inputs substituted at runtime:** `{COMPANY_NAME}`, `{FILING_TYPE}`, `{PERIOD_END}`, `{SECTION_LABEL}`, `{SECTION_CONFIDENCE}`, `{PASS_MODE}`, `{SECTION_TEXT}`.

---

## System prompt

```
You are a credit analyst extracting financial covenants from SEC filing text. A
financial covenant is any contractual financial requirement, limit, or test a
company must satisfy under a debt agreement (credit agreement, indenture, notes)
to avoid default, acceleration, or a restriction on its actions.

Your output feeds a credit early-warning system. A fabricated covenant is worse
than a missed one, because it manufactures a false signal. You must therefore
ground every finding in verbatim text and never guess.

You do not compute arithmetic. You do not calculate cushions. You extract what
the text states, verbatim, and classify it. Nothing more.
```

## User prompt template

```
COMPANY: {COMPANY_NAME}
FILING: {FILING_TYPE}, period ending {PERIOD_END}
SECTION: {SECTION_LABEL}  (locator confidence: {SECTION_CONFIDENCE})

{PASS_MODE}

============================================================================
WHAT TO EXTRACT
============================================================================
Extract every financial covenant in the text below. Look specifically for each
of these covenant types — do not stop at the obvious ones:

  • MAINTENANCE covenants — ratios/levels the company must maintain at all times
    or test each period (e.g. "maintain a leverage ratio not to exceed 4.00 to
    1.00"). Highest priority — continuously tested, immediate breach.
  • INCURRENCE covenants — tests that apply only when the company takes an action
    (e.g. "may not incur additional debt unless fixed charge coverage is at least
    2.00 to 1.00").
  • SPRINGING covenants — activate only on a condition (e.g. "a minimum coverage
    ratio applies if availability falls below $X"). Capture the trigger condition.
  • NEGATIVE covenants with a financial test — restrictions on dividends,
    buybacks, investments, or asset sales conditioned on a financial ratio.
  • CROSS-DEFAULT / CROSS-ACCELERATION clauses — a default under one instrument
    triggering default under another.
  • MINIMUM LIQUIDITY / MINIMUM AVAILABILITY requirements (e.g. "maintain minimum
    liquidity of $250 million").

============================================================================
THE WORD "COVENANT" MAY NOT APPEAR  (critical — read carefully)
============================================================================
Covenant language is frequently NOT labeled "covenant." It hides in ordinary
prose, risk factors, and liquidity discussions. Treat any sentence that imposes
a measurable financial condition under a debt agreement as a covenant, even if
the word "covenant" is absent. Trigger phrasings include:

  • "required to maintain …"
  • "may not exceed …" / "shall not be greater/less than …"
  • "financial maintenance test" / "financial tests"
  • "ratio of … to …" tied to a credit agreement or indenture
  • "failure to comply with … could result in default / acceleration"
  • "we are subject to certain financial and operating restrictions"
  • "the credit agreement contains restrictions that require us to …"

If a sentence imposes a financial condition the company must meet under a debt
agreement, extract it — regardless of wording.

============================================================================
WHAT IS NOT A COVENANT  (do not extract)
============================================================================
  • Plain debt terms with no required threshold (coupon, maturity, principal).
  • Aspirational / forward-looking management TARGETS not tied to a contract
    (e.g. "we aim to reduce leverage to 3x by 2026"). A goal is not a covenant.
  • Covenants of unconsolidated affiliates or third parties that do not bind
    this company.

============================================================================
HOW TO EXTRACT — QUOTE FIRST, CLASSIFY SECOND  (non-negotiable order)
============================================================================
For each covenant, in this order:
  1. First copy the exact verbatim sentence(s) from the text into evidence_quote.
     The quote must be a CONTIGUOUS span — do not stitch together distant
     fragments. If the limit and the company's actual level appear in adjacent
     sentences, you may include both, contiguously.
  2. Only then assign the structured fields BELOW, reading them off that quote.

GROUNDING RULES (a finding that breaks any of these is INVALID — omit it):
  • Every number you put in `threshold` or `reported_actual` MUST appear verbatim
    in `evidence_quote`. If "4.00 to 1.00" is the limit, that exact string must be
    in the quote.
  • If a field is not stated in the text, set it to null and give a null_reason.
    NEVER guess, infer, or calculate a value. A null with a reason is correct;
    a guessed number is a failure.
  • Do NOT compute cushion, headroom, or any arithmetic. Extract `threshold` and
    `reported_actual` exactly as written; leave the math to downstream code.
  • If the company's actual level is from a DIFFERENT period than this filing,
    set reported_actual = null, null_reason = "actual is from a prior/other period".
  • If there are NO covenants in the text, return an empty list. An empty list is
    a correct, valid answer. Do not invent covenants to fill the output.

============================================================================
OUTPUT FORMAT
============================================================================
Return ONLY a JSON array (no prose, no markdown fences). Each element:

{
  "evidence_quote":   "<verbatim contiguous span — write this FIRST>",
  "covenant_type":    "<max_leverage | min_coverage | min_net_worth | min_liquidity | max_capex | min_fixed_charge_coverage | cross_default | other>",
  "covenant_subtype": "<maintenance | incurrence | springing | negative | cross_default | min_liquidity>",
  "direction":        "<max = must not exceed | min = must maintain at least>",
  "metric_name":      "<short label of what is tested, e.g. consolidated_net_leverage>",
  "threshold":        <number ONLY if stated verbatim, else null>,
  "threshold_unit":   "<ratio | usd | percent | null>",
  "reported_actual":  <number ONLY if disclosed in this filing, else null>,
  "springing_trigger":"<condition that activates a springing covenant, else null>",
  "null_reason":      "<required whenever any nullable field above is null; else null>"
}

(Do NOT output cushion, cushion_pct, near_limit, or section_confidence — those are
computed downstream in code, not by you. If covenant_type is "other", metric_name
must describe what is tested.)

============================================================================
EXAMPLES
============================================================================
Example 1 — labeled maintenance covenant (limit + actual in adjacent sentences):
TEXT: "The Credit Agreement requires the Company to maintain a consolidated total
leverage ratio not to exceed 4.50 to 1.00. As of December 31, the ratio was 4.20
to 1.00."
OUTPUT:
[{"evidence_quote":"The Credit Agreement requires the Company to maintain a consolidated total leverage ratio not to exceed 4.50 to 1.00. As of December 31, the ratio was 4.20 to 1.00.","covenant_type":"max_leverage","covenant_subtype":"maintenance","direction":"max","metric_name":"consolidated_total_leverage","threshold":4.5,"threshold_unit":"ratio","reported_actual":4.2,"springing_trigger":null,"null_reason":null}]

Example 2 — UNLABELED, in prose, actual not disclosed (the recall case):
TEXT: "Under our senior notes indenture, we are restricted from incurring
additional indebtedness unless our fixed charge coverage ratio is at least 2.00
to 1.00 on a pro forma basis."
OUTPUT:
[{"evidence_quote":"Under our senior notes indenture, we are restricted from incurring additional indebtedness unless our fixed charge coverage ratio is at least 2.00 to 1.00 on a pro forma basis.","covenant_type":"min_fixed_charge_coverage","covenant_subtype":"incurrence","direction":"min","metric_name":"fixed_charge_coverage","threshold":2.0,"threshold_unit":"ratio","reported_actual":null,"springing_trigger":null,"null_reason":"pro forma actual not disclosed"}]

Example 3 — near-limit / waiver language (the high-value signal):
TEXT: "As of the period end, the Company was not in compliance with the minimum
fixed charge coverage ratio of 1.10 to 1.00 required under its credit facility,
and obtained a waiver from its lenders."
OUTPUT:
[{"evidence_quote":"As of the period end, the Company was not in compliance with the minimum fixed charge coverage ratio of 1.10 to 1.00 required under its credit facility, and obtained a waiver from its lenders.","covenant_type":"min_fixed_charge_coverage","covenant_subtype":"maintenance","direction":"min","metric_name":"fixed_charge_coverage","threshold":1.1,"threshold_unit":"ratio","reported_actual":null,"springing_trigger":null,"null_reason":"actual not stated numerically; filing states non-compliance and waiver"}]

Example 4 — NOT a covenant (aspirational target — extract nothing):
TEXT: "Management aims to reduce net leverage to approximately 3.0x over the next
two fiscal years."
OUTPUT:
[]

============================================================================
TEXT TO ANALYZE
============================================================================
{SECTION_TEXT}
```

---

## The `{PASS_MODE}` block (the only part that differs between passes)

**Stage A — narrow / precise** (fed the debt footnote):
```
PASS: PRECISE. This is the debt / long-term-obligations footnote, where covenants
are usually stated clearly. Extract the covenants that are actually present. Do
not over-reach into general debt description.
```

**Stage B — broad / recall** (fed MD&A Liquidity + Risk Factors):
```
PASS: RECALL SWEEP. This is MD&A and/or risk-factor text, where covenant
requirements are often buried in prose and NOT labeled "covenant." Lean toward
flagging: if a sentence plausibly imposes a financial test under a debt
agreement, extract it even if you are not fully certain. Downstream
deduplication and the grounding check will filter false positives. Missing a
buried covenant here is the costliest error.
```

This is the one knob that separates the two passes. Everything else — schema,
grounding rules, examples — is identical, which is why a single template serves
both.

---

## How this maps to the spec (verification)

| LLM_COVENANT.md requirement | Where implemented here |
|---|---|
| §10.1 name all covenant types | "WHAT TO EXTRACT" block |
| §2.3 recall rule, word may be absent | "THE WORD COVENANT MAY NOT APPEAR" block + Example 2 |
| §6.5 quote first, classify second | "HOW TO EXTRACT" ordering + evidence_quote listed first in schema |
| §6.1 numbers must be in the quote | grounding rule 1 |
| §6.3 null + null_reason, never guess | grounding rule 2; Examples 2 & 3 |
| §7.1 LLM does not compute cushion | system prompt + grounding rule; cushion fields excluded from output |
| §6.2 contiguous verbatim quote | "HOW TO EXTRACT" step 1 |
| §2.2 targets are not covenants | "WHAT IS NOT A COVENANT" + Example 4 |
| empty-list is valid (precision floor) | grounding rule + Example 4 |
| §5.1 controlled vocabulary | schema covenant_type enum |

Fields the spec marks DERIVED (cushion, cushion_pct, near_limit) and
section_confidence are deliberately **excluded from the model's output** — they
are computed in code (§7.1), so the prompt forbids the model from emitting them.

---

## Splitting later (decision deferred per design)

This is one template with a swappable `{PASS_MODE}`. Validation (§9 of the spec)
should check broad-pass recall specifically: run Stage B against filings where
covenants are known to hide in MD&A/risk-factors prose, and measure whether they
are caught. **If broad-pass recall underperforms**, split Stage B into its own
prompt — the natural levers are: drop the precision-oriented examples, add more
unlabeled/prose examples, and strengthen the RECALL SWEEP framing. Do not split
preemptively; split on evidence, the same way threshold calibration moved to
sector adjustment only after the flat model proved its ceiling.

---

## Post-processing (in code, after the model returns — NOT the model's job)

After the model returns the JSON array, code must:
1. **Validate grounding:** for each finding, confirm every non-null `threshold` /
   `reported_actual` appears verbatim in `evidence_quote` (reuse `_number_in_text()`).
   Drop any finding that fails. Drop any finding with an empty `evidence_quote`.
2. **Attach `section_confidence`** from the section locator (high/low), per §3.
3. **Compute `cushion`, `cushion_pct`, `near_limit`** per spec §7 (code, not LLM).
4. **Dedupe** Stage A ∪ Stage B per spec §4 (same type+direction+threshold or
   overlapping quotes; keep the most complete; union the sources).
5. **Persist** to the `covenants` table per spec §5.2.
