"""
LLM structured extraction of debt-footnote and loss-provision details.

Why a separate module from llm_review.py?
  llm_review.py extracts QUALITATIVE findings and enforces a hard "no digits" rule — numbers are forbidden there because they belong to the deterministic XBRL path (extract.py). This module is the deliberate exception: it extracts HYBRID output — structured numbers (covenant thresholds, accrued provision amounts) WHERE they can be reliably parsed, plus a qualitative flag and a verbatim quote for everything else.

The anti-hallucination contract (critical):
  An LLM asked for numbers will sometimes invent them. So every non-null numeric field returned by the model is validated against its own evidence_quote: if the number does not actually appear in the quote (allowing for unit scaling like "$500 million" → 500000000), it is dropped to None. The qualitative flag and
  the verbatim quote always survive. This keeps the structured numbers honest and is why these signals are only allowed a small, capped contribution to the stress score (see score.py).

Input:
  These functions receive a pre-located section slice from sections.py — the
  debt footnote or the commitments-and-contingencies footnote — NOT the whole
  filing. That keeps each LLM call small and on-target.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import anthropic

from src.llm_review import (
    Finding,
    parse_json_array,
    quote_in_text,
    review_text,
    warn_if_truncated,
)

logger = logging.getLogger(__name__)

# Same fast/cheap model used for the qualitative review — structured extraction
# from a focused excerpt is well within Haiku's capability.
MODEL = "claude-haiku-4-5-20251001"

# ── Covenant vocabulary (Stage 2c-i) ──────────────────────────────────────────
# Widened 4 -> 8 superset (LLM_COVENANT §5.1). The prompt emits these target
# tokens directly; the map normalizes any source-style / synonym token and falls
# back to "other" — a covenant is NEVER dropped on covenant_type alone.
_COVENANT_TYPES = (
    "max_leverage", "min_coverage", "min_net_worth", "min_liquidity",
    "max_capex", "min_fixed_charge_coverage", "cross_default", "other",
)
_COVENANT_VOCAB = {
    # target tokens pass through
    **{t: t for t in _COVENANT_TYPES},
    # source bare types -> target (Stage-1 approved map)
    "leverage": "max_leverage",
    "interest_coverage": "min_coverage",
    "fixed_charge_coverage": "min_fixed_charge_coverage",
    "minimum_liquidity": "min_liquidity",
    "minimum_cash": "min_liquidity",          # collapse (approved)
    "capex": "max_capex",
    "incurrence": "other",                     # subtype, not a type -> other + subtype=incurrence
}
_DIRECTIONS = ("max", "min")

# Matches numeric tokens like "4.0", "58,744", "500", "0.5" in an evidence quote.
_NUM_RE = re.compile(r"\d[\d,]*\.?\d*")

# near_limit condition 3 (LLM_COVENANT §7.2 — ACTUAL current breach / waiver /
# proximity language) is implemented in Stage 2c-iii as a footnote-level grounded
# LLM judgment: see COVENANT_BREACH_SYSTEM / extract_covenant_breach below. The
# earlier regex approach (a _WAIVER_RE pattern) was removed: it could not separate
# a present breach from covenant TERMS ("events of default including breach"),
# negations ("are not in default"), or conditionals ("if we have failed to
# maintain ..."). That semantic distinction is what the grounded pass makes;
# _BREACH_PRESENT_RE (defined with that pass) survives only as a necessary-but-
# not-sufficient confirmatory gate, never as the decider.

# Stage-A (precise) covenant prompt — COVENANT_PROMPT.md adapted to the richer
# Covenant fields. Built with str.replace (NOT .format) because the few-shots
# contain literal JSON braces. cushion / cushion_pct / near_limit /
# section_confidence are DELIBERATELY NOT emitted — code derives them.
COVENANT_SYSTEM = """You are a credit analyst extracting financial covenants from SEC filing text. A
financial covenant is any contractual financial requirement, limit, or test a
company must satisfy under a debt agreement (credit agreement, indenture, notes)
to avoid default, acceleration, or a restriction on its actions.

Your output feeds a credit early-warning system. A fabricated covenant is worse
than a missed one, because it manufactures a false signal. You must therefore
ground every finding in verbatim text and never guess.

You do not compute arithmetic. You do not calculate cushions or proximity. You
extract what the text states, verbatim, and classify it. Nothing more."""

# {PASS_MODE} blocks — Stage A (PRECISE, 2c-i) and Stage B (RECALL SWEEP, 2c-ii).
COVENANT_PASS_PRECISE = (
    "PASS: PRECISE. This is the debt / long-term-obligations footnote, where "
    "covenants are usually stated clearly. Extract the covenants that are actually "
    "present. Do not over-reach into general debt description."
)

# Stage B recall sweep over MD&A + risk factors. Leans aggressive on recall, BUT
# the finding must be a PRESENT contractual requirement, not a hypothetical — this
# instruction + the grounding gates (quote_in_text, _number_in_text) are the
# fabrication controls (a broad sweep over risk-factor prose is the FP risk).
COVENANT_PASS_RECALL = (
    "PASS: RECALL SWEEP. This is MD&A and/or risk-factor text, where covenant "
    "requirements are often buried in prose and NOT labeled \"covenant.\" Lean "
    "toward flagging: if a sentence plausibly states a financial test the company "
    "is subject to under a debt agreement, extract it even if you are not fully "
    "certain. Downstream deduplication and the grounding check will filter false "
    "positives. Missing a buried covenant here is the costliest error.\n\n"
    "CRITICAL — PRESENT REQUIREMENT, NOT A HYPOTHETICAL: extract only an ACTUAL "
    "covenant the company is CURRENTLY subject to. Quote the sentence stating the "
    "real requirement (e.g. \"we are required to maintain a leverage ratio not to "
    "exceed 4.00 to 1.00\"). Do NOT extract forward-looking or hypothetical "
    "descriptions of covenant RISK — e.g. \"we may become subject to covenants in "
    "the future\", \"our future debt agreements could contain restrictions that "
    "might limit us\", \"covenants in our agreements could restrict our ability "
    "to ...\". A description of what covenants might do, or might exist, is NOT a "
    "covenant. If the sentence does not state a real, current contractual test, "
    "return nothing for it."
)

COVENANT_USER = """COMPANY: {COMPANY_NAME}
FILING: {FILING_TYPE}, period ending {PERIOD_END}
SECTION: {SECTION_LABEL}  (locator confidence: {SECTION_CONFIDENCE})

{PASS_MODE}

============================================================================
WHAT TO EXTRACT
============================================================================
Extract every financial covenant in the text below. Look specifically for each
covenant type - do not stop at the obvious ones:
  - MAINTENANCE covenants - ratios/levels the company must maintain at all times
    or test each period (e.g. "maintain a leverage ratio not to exceed 4.00 to 1.00").
  - INCURRENCE covenants - tests that apply only when the company takes an action
    (e.g. "may not incur additional debt unless fixed charge coverage is at least 2.00 to 1.00").
  - SPRINGING covenants - activate only on a condition (e.g. "a minimum coverage
    ratio applies if availability falls below $X"). Capture the trigger condition.
  - NEGATIVE covenants with a financial test - restrictions on dividends, buybacks,
    investments, or asset sales conditioned on a financial ratio.
  - CROSS-DEFAULT / CROSS-ACCELERATION clauses - a default under one instrument
    triggering default under another.
  - MINIMUM LIQUIDITY / MINIMUM AVAILABILITY requirements.

============================================================================
THE WORD "COVENANT" MAY NOT APPEAR
============================================================================
Covenant language is frequently NOT labeled "covenant." Treat any sentence that
imposes a measurable financial condition under a debt agreement as a covenant,
even if the word "covenant" is absent ("required to maintain ...", "may not exceed
...", "shall not be greater/less than ...", "financial maintenance test", "ratio of
... to ..." tied to a credit agreement/indenture, "failure to comply ... could
result in default/acceleration").

============================================================================
WHAT IS NOT A COVENANT  (do not extract)
============================================================================
  - Plain debt terms with no required threshold (coupon, maturity, principal).
  - Aspirational / forward-looking management TARGETS not tied to a contract
    ("we aim to reduce leverage to 3x by 2026"). A goal is not a covenant.
  - Covenants of unconsolidated affiliates / third parties that do not bind this company.

============================================================================
HOW TO EXTRACT - QUOTE FIRST, CLASSIFY SECOND
============================================================================
For each covenant, in this order:
  1. First copy the exact VERBATIM, CONTIGUOUS sentence(s) into evidence_quote. Do
     not stitch distant fragments. If the limit and the company's actual level are
     in adjacent sentences, include both contiguously.
  2. Only then assign the fields, reading them off that quote.

GROUNDING RULES (a finding breaking any of these is INVALID - omit it):
  - Every number in `threshold` or `reported_actual` MUST appear verbatim in
    `evidence_quote`. If "4.00 to 1.00" is the limit, that string must be in the quote.
  - If a field is not stated, set it null and give a null_reason. NEVER guess/infer/calculate.
  - Do NOT compute cushion, headroom, proximity, or whether the company is "near" the
    limit - extract `threshold` and `reported_actual` exactly as written; code does the math.
  - If the actual level is from a DIFFERENT period than this filing, set reported_actual
    = null, null_reason = "actual is from a prior/other period".
  - If there are NO covenants, return an empty list []. That is a correct answer.

============================================================================
OUTPUT FORMAT
============================================================================
Return ONLY a JSON array (no prose, no markdown fences). Each element:

{
  "evidence_quote":    "<verbatim contiguous span - write this FIRST>",
  "covenant_type":     "<max_leverage | min_coverage | min_net_worth | min_liquidity | max_capex | min_fixed_charge_coverage | cross_default | other>",
  "direction":         "<max = must not exceed | min = must maintain at least>",
  "ratio_name":        "<exact name as written, e.g. Consolidated Net Leverage Ratio; else null>",
  "threshold":         <number ONLY if stated verbatim, else null>,
  "unit":              "<ratio | usd | percent | null>",
  "reported_actual":   <number ONLY if disclosed in this filing, else null>,
  "testing_frequency": "<e.g. quarterly | at all times | when availability < $X; else null>",
  "is_springing":      <true | false | null>,
  "springing_trigger": "<the activating condition if springing, else null>",
  "step_down":         "<step-down/step-up schedule if disclosed, else null>",
  "is_maintenance":    <true (breach can trigger default) | false (incurrence-only) | null>,
  "null_reason":       "<required whenever any nullable field above is null; else null>"
}

(Do NOT output cushion, cushion_pct, near_limit, or section_confidence - those are
computed downstream in code, not by you. If covenant_type is "other", ratio_name
must describe what is tested.)

============================================================================
EXAMPLES
============================================================================
Example 1 - labeled maintenance covenant (limit + actual in adjacent sentences):
TEXT: "The Credit Agreement requires the Company to maintain a consolidated total leverage ratio not to exceed 4.50 to 1.00, tested quarterly. As of December 31, the ratio was 4.20 to 1.00."
OUTPUT:
[{"evidence_quote":"The Credit Agreement requires the Company to maintain a consolidated total leverage ratio not to exceed 4.50 to 1.00, tested quarterly. As of December 31, the ratio was 4.20 to 1.00.","covenant_type":"max_leverage","direction":"max","ratio_name":"consolidated total leverage ratio","threshold":4.5,"unit":"ratio","reported_actual":4.2,"testing_frequency":"quarterly","is_springing":false,"springing_trigger":null,"step_down":null,"is_maintenance":true,"null_reason":null}]

Example 2 - UNLABELED incurrence covenant, actual not disclosed:
TEXT: "Under our senior notes indenture, we are restricted from incurring additional indebtedness unless our fixed charge coverage ratio is at least 2.00 to 1.00 on a pro forma basis."
OUTPUT:
[{"evidence_quote":"Under our senior notes indenture, we are restricted from incurring additional indebtedness unless our fixed charge coverage ratio is at least 2.00 to 1.00 on a pro forma basis.","covenant_type":"min_fixed_charge_coverage","direction":"min","ratio_name":"fixed charge coverage ratio","threshold":2.0,"unit":"ratio","reported_actual":null,"testing_frequency":null,"is_springing":false,"springing_trigger":null,"step_down":null,"is_maintenance":false,"null_reason":"pro forma actual not disclosed"}]

Example 3 - near-limit / waiver language:
TEXT: "As of the period end, the Company was not in compliance with the minimum fixed charge coverage ratio of 1.10 to 1.00 required under its credit facility, and obtained a waiver from its lenders."
OUTPUT:
[{"evidence_quote":"As of the period end, the Company was not in compliance with the minimum fixed charge coverage ratio of 1.10 to 1.00 required under its credit facility, and obtained a waiver from its lenders.","covenant_type":"min_fixed_charge_coverage","direction":"min","ratio_name":"minimum fixed charge coverage ratio","threshold":1.1,"unit":"ratio","reported_actual":null,"testing_frequency":null,"is_springing":false,"springing_trigger":null,"step_down":null,"is_maintenance":true,"null_reason":"actual not stated numerically; filing states non-compliance and waiver"}]

Example 4 - NOT a covenant (aspirational target - extract nothing):
TEXT: "Management aims to reduce net leverage to approximately 3.0x over the next two fiscal years."
OUTPUT:
[]

============================================================================
TEXT TO ANALYZE
============================================================================
{SECTION_TEXT}"""

PROVISION_PROMPT = """You are a credit analyst reading the COMMITMENTS AND CONTINGENCIES
footnote of a SEC 10-K filing. Identify LOSS PROVISIONS and CONTINGENCIES — accrued
liabilities or disclosed exposures for litigation, regulatory matters, environmental
claims, or similar.

Return a JSON array. Each item must be:
{
  "matter": "<short label, e.g. 'patent litigation' — no numbers>",
  "provision_amount": <accrued/disclosed dollar amount if stated, else null>,
  "is_material": <true if described as material or potentially significant to the company>,
  "qualitative_flag": "<short note, e.g. 'reasonably possible loss, not accrued'>",
  "evidence_quote": "<verbatim excerpt, max 240 chars, containing any number you report>",
  "source": "<the filing label provided>"
}

Rules:
- Every number you put in provision_amount MUST appear verbatim in evidence_quote.
- If an amount is not explicitly stated, use null — never estimate.
- evidence_quote must be a direct quote, not a paraphrase.
- Omit items you cannot quote. Return [] if no loss provisions or contingencies are described.
"""


@dataclass
class Covenant:
    """
    One financial covenant extracted from the debt footnote (Stage 2c-i: richer
    fields ported from the source extractor + cushion/near_limit derived in code).

    Numeric fields are nullable: kept only when the figure appears verbatim in
    evidence_quote (see _number_in_text). near_limit / cushion / cushion_pct are
    DERIVED IN CODE (not emitted by the LLM). The original 7 fields are unchanged
    (so save_covenants / score.py keep working); the rest default for back-compat.
    """
    covenant_type: str          # 8-vocab: max_leverage | min_coverage | min_net_worth | min_liquidity | max_capex | min_fixed_charge_coverage | cross_default | other
    threshold: float | None     # the limit, if reliably parsed
    direction: str              # "max" | "min"
    reported_actual: float | None  # current level, if disclosed
    near_limit: bool            # DERIVED in code (cushion_pct<=10% OR breach OR waiver/amendment language)
    evidence_quote: str         # verbatim quote (required)
    source: str
    # ── Stage 2c-i richer fields (all default for back-compat) ──
    covenant_subtype: str | None = None      # maintenance | incurrence | springing | negative | cross_default | min_liquidity
    ratio_name: str | None = None            # verbatim name, e.g. "Consolidated Net Leverage Ratio"
    unit: str | None = None                  # ratio | usd | percent
    testing_frequency: str | None = None     # e.g. "quarterly", "at all times"
    is_springing: bool | None = None
    springing_trigger: str | None = None
    step_down: str | None = None
    is_maintenance: bool | None = None       # True = breach can trigger default; False = incurrence-only
    cushion: float | None = None             # DERIVED in code
    cushion_pct: float | None = None         # DERIVED in code
    section_confidence: str = "low"          # high (heading-anchored) | low (chunk fallback)
    null_reason: str | None = None
    # ── Stage 2c-iii: why near_limit is True + the breach/waiver evidence ──
    near_limit_reason: str | None = None          # "cushion" | "breach" (numeric) | "waiver/breach disclosed" (language)
    near_limit_evidence_quote: str | None = None  # verbatim breach/waiver sentence (language path only; the covenant's own quote stays in evidence_quote)
    near_limit_section: str | None = None         # where the breach was disclosed: "Debt footnote" | "MD&A" (language path only)


@dataclass
class LossProvision:
    """One litigation/contingency provision from the commitments footnote."""
    matter: str                     # short label of the matter
    provision_amount: float | None  # accrued amount, if reliably parsed
    is_material: bool
    qualitative_flag: str           # e.g. "reasonably possible loss, not accrued"
    evidence_quote: str             # verbatim quote (required)
    source: str


def _to_bool(val) -> bool:
    """
    Coerce a JSON value to bool without the bool("false") trap.

    These flags feed score points directly (covenant proximity, material
    provisions), so a model that emits the string "false" must not count as
    True. Strings are accepted only for the explicit affirmatives.
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes")
    return False


def _to_float(val) -> float | None:
    """Coerce a JSON value to float, or None if it isn't a usable number."""
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return None


def _number_in_text(value: float | None, text: str) -> bool:
    """
    Anti-hallucination guard: is `value` actually present in `text`?

    True when value is None (nothing to verify). Otherwise we extract every
    numeric token from the quote and accept the value if it matches one within 1%
    — directly OR after scaling by 1e3 / 1e6 / 1e9, so "$500 million" in the quote
    backs a value of 500000000, and "4.0x" backs 4.0.
    """
    if value is None:
        return True
    nums: list[float] = []
    for tok in _NUM_RE.findall(text):
        try:
            nums.append(float(tok.replace(",", "")))
        except ValueError:
            continue
    target = abs(value)
    for n in nums:
        for scale in (1, 1e3, 1e6, 1e9):
            scaled = n * scale
            tol = max(target * 0.01, 0.01)
            if abs(target - scaled) <= tol:
                return True
    return False


def _opt_bool(val) -> bool | None:
    """Like _to_bool, but absent/None stays None (vs defaulting to False)."""
    return None if val is None else _to_bool(val)


def _norm_unit(val) -> str | None:
    """Normalize a unit string to ratio | usd | percent | None."""
    s = str(val or "").strip().lower()
    if not s:
        return None
    if "ratio" in s or s in ("x", "to 1.00", "to 1") or s.endswith("x"):
        return "ratio"
    if "%" in s or "percent" in s:
        return "percent"
    if "usd" in s or "$" in s or "dollar" in s or "million" in s or "billion" in s:
        return "usd"
    return None


def _derive_subtype(covenant_type: str, is_springing: bool | None, is_maintenance: bool | None) -> str:
    """Derive covenant_subtype in code (precedence: springing > cross_default > min_liquidity > incurrence > maintenance)."""
    if is_springing:
        return "springing"
    if covenant_type == "cross_default":
        return "cross_default"
    if covenant_type == "min_liquidity":
        return "min_liquidity"
    if is_maintenance is False:
        return "incurrence"
    return "maintenance"


def _derive_cushion(threshold: float | None, reported_actual: float | None,
                    direction: str) -> tuple[float | None, float | None]:
    """
    Cushion / cushion_pct IN CODE (LLM_COVENANT §7.1), sign-explicit:
      max (ceiling, breach when actual>threshold): cushion = threshold - actual
      min (floor,   breach when actual<threshold): cushion = actual - threshold
    Positive = headroom, negative = in breach. None when either input is null.
    """
    if threshold is None or reported_actual is None:
        return None, None
    cushion = (threshold - reported_actual) if direction == "max" else (reported_actual - threshold)
    cushion_pct = (cushion / abs(threshold) * 100.0) if threshold != 0 else None
    return round(cushion, 6), (round(cushion_pct, 4) if cushion_pct is not None else None)


def _derive_near_limit(cushion: float | None,
                       cushion_pct: float | None) -> tuple[bool, str | None]:
    """
    Numeric near_limit IN CODE (Stage 2c-i) — returns (near_limit, reason).
    TRUE when EITHER:
      1. cushion < 0 (in breach)            -> reason "breach", OR
      2. cushion_pct <= 10 (thin headroom)  -> reason "cushion".
    When cushion is uncomputable (threshold or reported_actual is null),
    (False, None). The breach branch is checked first only to pick the more
    precise reason; the boolean is identical to the original order (a breach
    always has cushion_pct <= 10).

    Language-based breach/waiver near_limit (LLM_COVENANT §7.2 condition 3) is
    NOT decided here. Stage 2c-iii restores it as a footnote-level GROUNDED LLM
    judgment (extract_covenant_breach) — the term-vs-present-state architecture
    proven on going-concern — which stamps near_limit=True +
    near_limit_reason="waiver/breach disclosed" + the verbatim quote + section
    onto the matched covenant in review_filing, AFTER dedupe. Regex was abandoned
    in 2c-i because it could not separate a present breach from covenant TERMS,
    negations, and conditionals.
    """
    if cushion is not None and cushion < 0:
        return True, "breach"
    if cushion_pct is not None and cushion_pct <= 10.0:
        return True, "cushion"
    return False, None


def _validate_covenant(raw: dict, fallback_source: str, section_conf: str = "low") -> Covenant | None:
    """
    Validate one raw covenant dict (Stage 2c-i).

    - covenant_type: map-or-"other" via _COVENANT_VOCAB (NEVER dropped on type);
      whitelist widened 4 -> 8.
    - direction: normalize maximum/minimum -> max/min; infer from type if missing.
    - threshold / reported_actual: grounded by _number_in_text (drop to None if not
      in the quote) — never guessed.
    - cushion / cushion_pct / near_limit: DERIVED IN CODE (the LLM is forbidden to
      emit them; any model-supplied near_limit is ignored).
    - covenant_subtype: derived in code.
    Dropped only when evidence_quote is empty.
    """
    if not isinstance(raw, dict):
        return None
    evidence_quote = str(raw.get("evidence_quote", "") or "").strip()
    if not evidence_quote:
        return None

    ct_raw = str(raw.get("covenant_type", "") or "").strip().lower()
    covenant_type = _COVENANT_VOCAB.get(ct_raw, ct_raw if ct_raw in _COVENANT_TYPES else "other")

    direction = str(raw.get("direction", "") or "").strip().lower()
    direction = {"maximum": "max", "minimum": "min"}.get(direction, direction)
    if direction not in _DIRECTIONS:
        direction = "max" if covenant_type in ("max_leverage", "max_capex") else "min"

    threshold = _to_float(raw.get("threshold"))
    reported_actual = _to_float(raw.get("reported_actual"))
    if not _number_in_text(threshold, evidence_quote):
        threshold = None
    if not _number_in_text(reported_actual, evidence_quote):
        reported_actual = None

    is_springing = _opt_bool(raw.get("is_springing"))
    is_maintenance = _opt_bool(raw.get("is_maintenance"))
    subtype = _derive_subtype(covenant_type, is_springing, is_maintenance)
    cushion, cushion_pct = _derive_cushion(threshold, reported_actual, direction)
    near_limit, near_limit_reason = _derive_near_limit(cushion, cushion_pct)

    return Covenant(
        covenant_type=covenant_type,
        threshold=threshold,
        direction=direction,
        reported_actual=reported_actual,
        near_limit=near_limit,                       # DERIVED, not from the LLM
        evidence_quote=evidence_quote[:600],
        source=str(raw.get("source", "") or fallback_source).strip(),
        covenant_subtype=subtype,
        ratio_name=(str(raw.get("ratio_name", "") or "").strip() or None),
        unit=_norm_unit(raw.get("unit")),
        testing_frequency=(str(raw.get("testing_frequency", "") or "").strip() or None),
        is_springing=is_springing,
        springing_trigger=(str(raw.get("springing_trigger", "") or "").strip() or None),
        step_down=(str(raw.get("step_down", "") or "").strip() or None),
        is_maintenance=is_maintenance,
        cushion=cushion,
        cushion_pct=cushion_pct,
        section_confidence=section_conf,
        null_reason=(str(raw.get("null_reason", "") or "").strip() or None),
        near_limit_reason=near_limit_reason,   # numeric reason; language path overwrites in review_filing
    )


def _validate_provision(raw: dict, fallback_source: str) -> LossProvision | None:
    """Validate one raw provision dict; drop a hallucinated amount to None."""
    if not isinstance(raw, dict):
        return None
    matter = str(raw.get("matter", "")).strip()
    evidence_quote = str(raw.get("evidence_quote", "")).strip()
    if not matter or not evidence_quote:
        return None

    amount = _to_float(raw.get("provision_amount"))
    if not _number_in_text(amount, evidence_quote):
        amount = None

    return LossProvision(
        matter=matter,
        provision_amount=amount,
        is_material=_to_bool(raw.get("is_material", False)),
        qualitative_flag=str(raw.get("qualitative_flag", "")).strip(),
        evidence_quote=evidence_quote[:240],
        source=str(raw.get("source", "") or fallback_source).strip(),
    )


# Max section characters sent per footnote LLM call. The located sections are
# capped at 40k chars by sections.py; this is the single trim applied here.
MAX_SECTION_CHARS = 40_000

# Stage B (recall sweep) trims MD&A / risk-factors to a larger window: covenant
# language in liquidity / indebtedness risk factors sits deeper than the 40k
# footnote cap (risk_factors' own section cap is 80k). Tunable recall lever.
_COV_RECALL_MAX_CHARS = 60_000


def _extract(
    section_text: str,
    filing_label: str,
    system_prompt: str,
    client: anthropic.Anthropic | None,
) -> tuple[list[dict], str]:
    """
    Shared LLM call: send the located section to Claude.

    Returns (raw JSON array, the exact excerpt the model saw) — the excerpt is
    what evidence quotes must be verified against, so a genuine quote can never
    fail verification because of our own truncation. Returns ([], excerpt) on
    any failure (empty section, malformed JSON) so a broken call never blocks
    the rest of the pipeline.
    """
    excerpt = section_text[:MAX_SECTION_CHARS]
    if not excerpt.strip():
        return [], excerpt
    if client is None:
        client = anthropic.Anthropic()

    user_prompt = (
        f"Filing: {filing_label}\n\n"
        f"Section text:\n{excerpt}\n\n"
        "Return your answer as a JSON array only — no other text."
    )
    message = client.messages.create(
        model=MODEL,
        # A covenant/provision with a max-length quote is ~150 output tokens, so
        # this covers ~100 items — far beyond any real footnote. Generous on
        # purpose: max_tokens is a ceiling, not a spend (only generated tokens
        # are billed), while hitting the cap truncates the JSON mid-array and
        # the whole call degrades to zero items. ~16k is also the non-streaming
        # limit before SDK HTTP timeouts become a concern.
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    warn_if_truncated(message, filing_label)
    return parse_json_array(message.content[0].text), excerpt


def extract_debt_footnote(
    section_text: str,
    filing_label: str,
    client: anthropic.Anthropic | None = None,
    *,
    section_conf: str = "low",
    company_name: str = "",
    period_end: str = "",
    filing_type: str = "10-K",
    max_chars: int = MAX_SECTION_CHARS,
    model: str = MODEL,
) -> list[Covenant]:
    """
    Stage A covenant extraction from a located debt-footnote slice (Stage 2c-i:
    richer Covenant model, cushion/near_limit derived in code).

    Self-contained call (does NOT reuse _extract, so the loss-provision pass is
    untouched): Haiku, temperature=0, the COVENANT_PROMPT.md PRECISE template.
    Grounding reuses _number_in_text (numbers in quote) + quote_in_text (quote in
    source). `section_conf` is stamped onto every covenant (from the locator).

    Returns validated Covenant objects (may be empty). Ungrounded numbers are
    nulled; covenants without a verifiable quote are dropped.
    """
    return _run_covenant_pass(
        section_text, filing_label, client,
        section_conf=section_conf, pass_mode=COVENANT_PASS_PRECISE,
        section_label="Debt footnote", company_name=company_name,
        period_end=period_end, filing_type=filing_type, max_chars=max_chars, model=model,
    )


def _run_covenant_pass(
    section_text: str,
    filing_label: str,
    client: anthropic.Anthropic | None,
    *,
    section_conf: str,
    pass_mode: str,
    section_label: str,
    company_name: str = "",
    period_end: str = "",
    filing_type: str = "10-K",
    max_chars: int = MAX_SECTION_CHARS,
    model: str = MODEL,
) -> list[Covenant]:
    """
    Shared covenant LLM pass (Stage A and Stage B both delegate here, so the
    call + validation + grounding logic cannot drift). Haiku, temperature=0,
    the COVENANT_PROMPT.md template with the given `pass_mode` (PRECISE / RECALL).
    Stamps `section_conf` onto every covenant; drops findings whose quote isn't
    in the section excerpt (quote_in_text) — same grounding as before.
    """
    excerpt = (section_text or "")[:max_chars]
    if not excerpt.strip():
        return []
    if client is None:
        client = anthropic.Anthropic()  # honours ANTHROPIC_BASE_URL (relay) from env

    # str.replace (not .format) — the few-shots contain literal JSON braces.
    user_prompt = (
        COVENANT_USER
        .replace("{COMPANY_NAME}", company_name or "(issuer)")
        .replace("{FILING_TYPE}", filing_type)
        .replace("{PERIOD_END}", period_end or "")
        .replace("{SECTION_LABEL}", section_label)
        .replace("{SECTION_CONFIDENCE}", section_conf)
        .replace("{PASS_MODE}", pass_mode)
        .replace("{SECTION_TEXT}", excerpt)
    )
    message = client.messages.create(
        model=model,
        max_tokens=16000,
        temperature=0,
        system=COVENANT_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    warn_if_truncated(message, filing_label)
    raw = parse_json_array(message.content[0].text)
    out = [_validate_covenant(r, filing_label, section_conf) for r in raw]
    return [c for c in out if c is not None and quote_in_text(c.evidence_quote, excerpt)]


def extract_covenants_broad(
    section_text: str,
    filing_label: str,
    client: anthropic.Anthropic | None = None,
    *,
    section_label: str,
    section_conf: str = "low",
    company_name: str = "",
    period_end: str = "",
    filing_type: str = "10-K",
    max_chars: int = _COV_RECALL_MAX_CHARS,
    model: str = MODEL,
) -> list[Covenant]:
    """
    Stage B (2c-ii): broad recall sweep for covenants hiding in MD&A / risk-factor
    prose (team requirement #3 — covenants not in the debt footnote). RECALL pass
    mode (leans aggressive), but the prompt requires a PRESENT contractual
    requirement (not a hypothetical) and the grounding gates filter the rest.
    Larger window than the footnote (_COV_RECALL_MAX_CHARS). Returns validated
    Covenant objects (may be empty); the caller dedupes Stage A ∪ Stage B.
    """
    return _run_covenant_pass(
        section_text, filing_label, client,
        section_conf=section_conf, pass_mode=COVENANT_PASS_RECALL,
        section_label=section_label, company_name=company_name,
        period_end=period_end, filing_type=filing_type, max_chars=max_chars, model=model,
    )


# ── Stage A ∪ Stage B dedupe (2c-ii) ──────────────────────────────────────────
_COV_MERGE_FIELDS = (
    "threshold", "reported_actual", "ratio_name", "unit", "testing_frequency",
    "is_springing", "springing_trigger", "step_down", "is_maintenance",
    "covenant_subtype", "null_reason",
)


def _threshold_match(a: float | None, b: float | None) -> bool:
    """Equal within rounding tolerance; both-null counts as equal."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= max(0.01, 0.01 * max(abs(a), abs(b)))


def _quotes_overlap(q1: str, q2: str) -> bool:
    """One evidence_quote contained in the other (normalized substring)."""
    return quote_in_text(q1, q2) or quote_in_text(q2, q1)


def _same_covenant(a: Covenant, b: Covenant) -> bool:
    """Same covenant_type + direction AND (threshold within tol OR overlapping quote)."""
    return (
        a.covenant_type == b.covenant_type
        and a.direction == b.direction
        and (_threshold_match(a.threshold, b.threshold)
             or _quotes_overlap(a.evidence_quote, b.evidence_quote))
    )


def _field_count(c: Covenant) -> int:
    return sum(getattr(c, f) is not None for f in _COV_MERGE_FIELDS)


def _primary(x: Covenant, y: Covenant, x_is_a: bool, y_is_a: bool) -> Covenant:
    """Pick which finding to keep on a match (priority order, as approved)."""
    xa, ya = x.reported_actual is not None, y.reported_actual is not None
    if xa != ya:                                   # (1) reported_actual non-null
        return x if xa else y
    xc, yc = x.section_confidence == "high", y.section_confidence == "high"
    if xc != yc:                                   # (2) higher section_confidence
        return x if xc else y
    xf, yf = _field_count(x), _field_count(y)
    if xf != yf:                                   # (3) more populated fields
        return x if xf > yf else y
    if x_is_a != y_is_a:                           # (4) Stage A over Stage B
        return x if x_is_a else y
    return x


def _merge_into(primary: Covenant, secondary: Covenant) -> None:
    """Fill primary's null fields from secondary; union source; recompute cushion/near_limit."""
    for f in _COV_MERGE_FIELDS:
        if getattr(primary, f) is None and getattr(secondary, f) is not None:
            setattr(primary, f, getattr(secondary, f))
    # Union sources: split already-unioned strings on "; " first, then de-dup, so a
    # source can't repeat across successive merges (avoids "Debt; MD&A; MD&A").
    parts: list[str] = []
    for s in (primary.source, secondary.source):
        parts.extend(p.strip() for p in (s or "").split(";") if p.strip())
    primary.source = "; ".join(dict.fromkeys(parts))          # union, order-preserving, de-duped
    if secondary.section_confidence == "high":                 # keep the better confidence
        primary.section_confidence = "high"
    # Recompute derived fields AFTER the merge on the merged threshold/actual/direction.
    # Numeric only: dedupe runs BEFORE the breach pass (review_filing), so no
    # language near_limit exists yet to clobber. near_limit_reason is recomputed
    # to match; the language path stamps it (and the evidence) afterwards.
    primary.cushion, primary.cushion_pct = _derive_cushion(
        primary.threshold, primary.reported_actual, primary.direction)
    primary.near_limit, primary.near_limit_reason = _derive_near_limit(
        primary.cushion, primary.cushion_pct)


def _dedupe_covenants(stage_a: list[Covenant], stage_b: list[Covenant]) -> list[Covenant]:
    """
    Collapse Stage A ∪ Stage B (and any intra-stage dupes) to one row per covenant
    (LLM_COVENANT §4). Matches merge into the higher-priority record (most complete),
    sources unioned, cushion/near_limit recomputed. Stage-B-only findings are kept
    as full covenants (not downgraded). Processing order: all Stage A first, then
    Stage B, so a footnote (Stage A) record is the default primary on a tie.

    KNOWN DEDUPE GAP (cosmetic, deliberately not fixed):
      A Stage-A covenant with a disclosed threshold and a Stage-B *restatement* of
      the SAME covenant with a null threshold and a differently-worded quote will
      NOT merge — _same_covenant needs a threshold match OR a quote overlap, and a
      null-vs-number threshold matches neither while different wording gives no
      overlap. So the same covenant can surface as two rows (observed on Tuesday
      Morning's secured-net-leverage test: Stage-A thr=8.0 + Stage-B thr=None).

      This is COUNT-ONLY, with NO score impact: each row carries its own
      code-derived near_limit, and a null-actual restatement is near_limit=False,
      so it adds no covenant_proximity points. It is NOT fixed by loosening
      _same_covenant to merge on covenant_type + direction alone, because that
      would over-merge genuinely DISTINCT same-type covenants (e.g. a maintenance
      leverage test and a separate incurrence leverage test) — silently dropping a
      real covenant, which is worse than over-counting. The proper future fix, if
      the count ever matters (e.g. a confusing dashboard), is a grounded-LLM
      "do these two quotes describe the same single covenant?" judgment (the
      term-vs-present-state architecture proven on going-concern), NOT a broader
      lexical merge rule.
    """
    merged: list[list] = []   # each: [covenant, is_stage_a]
    for c, is_a in ([(x, True) for x in stage_a] + [(y, False) for y in stage_b]):
        hit = next((e for e in merged if _same_covenant(e[0], c)), None)
        if hit is None:
            merged.append([c, is_a])
            continue
        existing, existing_is_a = hit[0], hit[1]
        prim = _primary(existing, c, existing_is_a, is_a)
        sec = c if prim is existing else existing
        _merge_into(prim, sec)
        hit[0] = prim
        hit[1] = existing_is_a if prim is existing else is_a
    return [e[0] for e in merged]


def extract_loss_provisions(
    section_text: str,
    filing_label: str,
    client: anthropic.Anthropic | None = None,
) -> list[LossProvision]:
    """
    Extract loss provisions/contingencies from a located contingencies slice.

    Same validation contract as extract_debt_footnote: amounts must be quote-backed
    or they are nulled; items without a verifiable quote are dropped.
    """
    raw, excerpt = _extract(section_text, filing_label, PROVISION_PROMPT, client)
    out = [_validate_provision(r, filing_label) for r in raw]
    return [p for p in out if p is not None and quote_in_text(p.evidence_quote, excerpt)]


# ── Going-concern pass (LLM_EXTRACTOR_PORT Stage 2b) ─────────────────────────
# Net-new pass. Reads the 2a auditor-report + going-concern-footnote (Tier-1
# primary) and MD&A + risk-factors (Tier-2). Implements GOING_CONCERN_PROMPT.md
# / LLM_GOING_CONCERN.md: Tier-1 = formal substantial-doubt (high confidence);
# Tier-2 = soft survival-linked precursor (low, requires adverse_conditions).
# Does NOT touch the covenant pass. Haiku, temperature=0, single-shot.

# Larger trim than the 40k footnote cap: the Tier-2 sources (MD&A Liquidity,
# risk factors) carry survival language deeper into the section. Tier-1 sources
# (auditor/GC-footnote) are far shorter, so this only matters for MD&A/risk.
_GC_MAX_SECTION_CHARS = 60_000

# Tier-1 grounding: the formal substantial-doubt / going-concern phrase MUST be
# in the quote, else the finding is not Tier-1 (dropped).
_GC_FORMAL_RE = re.compile(r"substantial doubt|going concern", re.IGNORECASE)
_GC_SOURCE_PARTIES = ("auditor", "management")

GOING_CONCERN_SYSTEM = """You are a credit analyst reading SEC filing text for one purpose: to detect \
whether the filing expresses doubt about the company's ability to continue \
operating — "going concern" risk. This is one of the strongest qualitative \
signals in corporate credit, and it comes in two very different forms that you \
must keep strictly separate:

  - TIER 1 - FORMAL going-concern: the standardized "substantial doubt about the
    ability to continue as a going concern" language that auditors and management
    are REQUIRED to use when conditions are serious. Unambiguous, high-confidence.

  - TIER 2 - SOFT PRECURSOR: hedged language signaling concern about survival
    BEFORE a formal flag - but ONLY when it is tied to the company's ability to
    keep operating AND accompanied by real adverse conditions. Lower-confidence.

Your output feeds a credit early-warning system. A fabricated or boilerplate
"finding" is worse than a missed one - it manufactures a false alarm. Most
healthy filings contain NO going-concern finding, and returning an empty list is
the correct, common answer. You ground every finding in verbatim text, you never
infer doubt the text does not state, and the absence of reassuring language is
NOT evidence of doubt."""

# NOTE: built with str.replace (NOT .format) because the few-shot examples below
# contain literal JSON braces.
GOING_CONCERN_USER = """COMPANY: {COMPANY_NAME}
FILING: {FILING_TYPE}, period ending {PERIOD_END}
SECTION: {SECTION_LABEL}  (locator confidence: {SECTION_CONFIDENCE})

============================================================================
WHAT TO DETECT - TWO TIERS, KEPT SEPARATE
============================================================================
TIER 1 - FORMAL going-concern (confidence = high). Extract when the text contains
the formal substantial-doubt language, in any standard form:
  - "substantial doubt about [its / our / the Company's] ability to continue as a
     going concern"
  - "these conditions [or events] raise substantial doubt about ... ability to
     continue as a going concern"
  - an AUDITOR's explanatory / emphasis-of-matter paragraph expressing going-
     concern doubt
  - management's going-concern evaluation concluding substantial doubt exists
     (capture even if stated to be "alleviated by management's plans" - and record
     whether it is alleviated or not).

TIER 2 - SOFT PRECURSOR (confidence = low). Extract hedged survival-doubt language
that is NOT the formal Tier-1 statement - but ONLY under the strict test below.

============================================================================
THE TIER-2 TEST - genuine doubt vs. routine boilerplate  (read carefully)
============================================================================
This is the hard part. Extract a Tier-2 finding ONLY when BOTH are true:
  (1) SURVIVAL-LINKED: the language ties a need (liquidity, financing,
      refinancing) to the company's ABILITY TO CONTINUE OPERATING or MEET ITS
      OBLIGATIONS - survival, not growth; AND
  (2) ADVERSE CONDITIONS PRESENT: it is accompanied by, or refers to, real
      current adverse conditions - recurring losses, negative working capital or
      equity, covenant breach/waiver, near-term maturities it cannot cover, etc.

DO NOT extract (these are NOT findings - return nothing for them):
  - GROWTH financing - capital for expansion, acquisitions, R&D, opportunity.
  - AFFIRMATIVE / REASSURING statements - e.g. "we believe our cash will be
    sufficient to fund operations for at least the next twelve months." This is
    the OPPOSITE of doubt.
  - GENERIC BOILERPLATE - conditional hedging with no tie to present adverse
    conditions ("we may need to raise capital in the future," "funds may not be
    available on favorable terms"). This appears in thousands of healthy filings.
  - HYPOTHETICALS in risk factors where there is no sign the condition exists now.

LITMUS TEST to apply before emitting any Tier-2 finding: "Would a credit analyst
conclude this company is signaling concern about its own SURVIVAL, given
conditions that ACTUALLY EXIST NOW - or is this standard cautionary language any
company might include?" Only the former is a finding.

Absence of reassurance is NOT doubt. Financial weakness you infer but the text
does not characterize as survival-threatening is NOT a finding. Do not editorialize.

============================================================================
HOW TO EXTRACT - QUOTE FIRST, CLASSIFY SECOND
============================================================================
For each finding, in this order:
  1. First copy the exact VERBATIM, CONTIGUOUS sentence(s) into evidence_quote.
     No paraphrase; no stitching distant fragments.
  2. Then assign the fields, reading them off that quote:
       - TIER 1 requires the formal substantial-doubt / going-concern phrase to be
         present in the quote. If it is not there, it is not Tier 1.
       - TIER 2 requires the quote (or text it directly references) to show BOTH
         survival-linkage AND adverse conditions; list those conditions in
         adverse_conditions. A Tier-2 finding with no adverse conditions is
         boilerplate - do NOT emit it.

If there is no going-concern language, return an empty list []. That is the
correct, expected answer for most filings.

============================================================================
OUTPUT FORMAT
============================================================================
Return ONLY a JSON array (no prose, no markdown fences). Each element:

{
  "evidence_quote":     "<verbatim contiguous span - write this FIRST>",
  "tier":               <1 | 2>,
  "confidence":         "<high for tier 1 | low for tier 2>",
  "source_party":       "<auditor | management>",
  "doubt_alleviated":   <true|false for tier 1 if stated; null for tier 2>,
  "adverse_conditions": [<present conditions, REQUIRED non-empty for tier 2; [] allowed for tier 1>],
  "description":        "<one-sentence summary of the finding>",
  "null_reason":        "<required when a nullable field is null; else null>"
}

(Do NOT output section or section_confidence - code attaches those. When both a
formal Tier-1 statement and soft language appear in the same filing, emit only the
Tier-1 finding.)

============================================================================
EXAMPLES
============================================================================
Example 1 - TIER 1, formal (auditor/management substantial doubt):
TEXT: "The accompanying financial statements have been prepared assuming the
Company will continue as a going concern. The Company has incurred recurring
losses and has a net capital deficiency. These conditions raise substantial doubt
about the Company's ability to continue as a going concern."
OUTPUT:
[{"evidence_quote":"These conditions raise substantial doubt about the Company's ability to continue as a going concern.","tier":1,"confidence":"high","source_party":"management","doubt_alleviated":false,"adverse_conditions":["recurring losses","net capital deficiency"],"description":"Management states substantial doubt about going-concern.","null_reason":null}]

Example 2 - TIER 2, genuine (survival-linked + adverse conditions, no formal flag):
TEXT: "We have incurred recurring losses from operations and negative cash flows,
and our ability to continue our operations is dependent upon our ability to obtain
additional debt or equity financing."
OUTPUT:
[{"evidence_quote":"We have incurred recurring losses from operations and negative cash flows, and our ability to continue our operations is dependent upon our ability to obtain additional debt or equity financing.","tier":2,"confidence":"low","source_party":"management","doubt_alleviated":null,"adverse_conditions":["recurring losses","negative cash flows from operations"],"description":"Operations dependent on raising financing amid recurring losses.","null_reason":null}]

Example 3 - NOT a finding (growth financing):
TEXT: "We may seek additional capital to fund our growth initiatives, expand our
manufacturing capacity, and pursue strategic acquisitions."
OUTPUT:
[]

Example 4 - NOT a finding (affirmative / reassuring - the opposite signal):
TEXT: "We believe our existing cash and cash equivalents, together with cash
generated from operations, will be sufficient to meet our anticipated cash needs
for at least the next twelve months."
OUTPUT:
[]

Example 5 - NOT a finding (generic boilerplate, no present adverse condition):
TEXT: "We may need to raise additional funds in the future to respond to business
opportunities or challenges, and such financing may not be available on terms
favorable to us, if at all."
OUTPUT:
[]

============================================================================
TEXT TO ANALYZE
============================================================================
{SECTION_TEXT}"""


@dataclass
class GoingConcern:
    """
    One going-concern finding (LLM_GOING_CONCERN.md / SUPABASE_LLM_SCHEMA §4).

    tier 1 = formal substantial-doubt (confidence high); tier 2 = soft survival-
    linked precursor (confidence low, adverse_conditions required non-empty).
    `null_reason` is carried for audit but NOT persisted (no column in §4).
    cik/period_end/created_at are added by save_going_concern, not here.
    """
    tier: int                       # 1 | 2
    confidence: str                 # "high" (tier 1) | "low" (tier 2)
    status: str | None              # "going_concern_doubt" for tier 1, else None
    going_concern_flag: bool        # True only for tier 1 (formal doubt present)
    source_party: str | None        # "auditor" | "management" | None
    doubt_alleviated: bool | None   # tier 1 only; None for tier 2
    adverse_conditions: list        # present conditions (required non-empty for tier 2)
    description: str | None          # one-line summary
    evidence_quote: str             # verbatim, contiguous (required)
    section: str | None             # which located section
    section_confidence: str         # "high" | "low" (from sections.section_confidence)
    source: str                     # filing label, e.g. "10-K 2019-12-31, Auditor's Report"
    null_reason: str | None = None


def _validate_going_concern(
    raw: dict, filing_label: str, section_label: str, section_conf: str
) -> GoingConcern | None:
    """
    Validate one raw going-concern dict. Enforces the grounding/tier contract:
      - evidence_quote required (non-empty);
      - tier in (1, 2);
      - Tier-1 requires the formal substantial-doubt phrase IN the quote;
      - Tier-2 requires non-empty adverse_conditions (else it is boilerplate);
      - confidence/going_concern_flag/status are set from the tier in code, not
        trusted from the model.
    Returns None (dropped) on any failure.
    """
    if not isinstance(raw, dict):
        return None
    evidence_quote = str(raw.get("evidence_quote", "") or "").strip()
    if not evidence_quote:
        return None
    try:
        tier = int(raw.get("tier"))
    except (TypeError, ValueError):
        return None
    if tier not in (1, 2):
        return None
    # Tier-1 grounding: the formal phrase must be in the quote.
    if tier == 1 and not _GC_FORMAL_RE.search(evidence_quote):
        return None
    # Tier-2 grounding: adverse_conditions must be present.
    adverse_raw = raw.get("adverse_conditions") or []
    if isinstance(adverse_raw, str):
        adverse_raw = [adverse_raw]
    adverse = [str(a).strip() for a in adverse_raw if str(a).strip()] if isinstance(adverse_raw, list) else []
    if tier == 2 and not adverse:
        return None

    sp = str(raw.get("source_party", "") or "").strip().lower()
    source_party = sp if sp in _GC_SOURCE_PARTIES else None
    doubt_alleviated = None
    if tier == 1 and raw.get("doubt_alleviated") is not None:
        doubt_alleviated = _to_bool(raw.get("doubt_alleviated"))

    return GoingConcern(
        tier=tier,
        confidence="high" if tier == 1 else "low",
        status="going_concern_doubt" if tier == 1 else None,
        going_concern_flag=(tier == 1),
        source_party=source_party,
        doubt_alleviated=doubt_alleviated,
        adverse_conditions=adverse,
        description=(str(raw.get("description", "") or "").strip() or None),
        evidence_quote=evidence_quote[:2000],
        section=section_label or None,
        section_confidence=section_conf,
        source=filing_label,
        null_reason=(str(raw.get("null_reason", "") or "").strip() or None),
    )


def extract_going_concern(
    section_text: str,
    filing_label: str,
    client: anthropic.Anthropic | None = None,
    *,
    section_label: str = "",
    section_conf: str = "low",
    company_name: str = "",
    period_end: str = "",
    filing_type: str = "10-K",
    max_chars: int = _GC_MAX_SECTION_CHARS,
    model: str = MODEL,
) -> list[GoingConcern]:
    """
    Extract going-concern findings from one located section slice.

    Haiku, temperature=0, single-shot (no self-consistency: at temp 0 the runs are
    identical, so self-consistency would require temp>0 and contradict the temp-0
    contract — Tier-2 quality is validated on the golden set instead).

    Grounding reuses quote_in_text(); section_confidence is stamped from the 2a
    section_confidence() helper (passed in via `section_conf`). Returns [] on any
    failure so a broken call never blocks the pipeline.
    """
    excerpt = (section_text or "")[:max_chars]
    if not excerpt.strip():
        return []
    if client is None:
        # anthropic.Anthropic() honours ANTHROPIC_BASE_URL (the APIYI relay) from env.
        client = anthropic.Anthropic()

    # str.replace (not .format) — the prompt's few-shots contain literal { } braces.
    user_prompt = (
        GOING_CONCERN_USER
        .replace("{COMPANY_NAME}", company_name or "(issuer)")
        .replace("{FILING_TYPE}", filing_type)
        .replace("{PERIOD_END}", period_end or "")
        .replace("{SECTION_LABEL}", section_label or "(section)")
        .replace("{SECTION_CONFIDENCE}", section_conf)
        .replace("{SECTION_TEXT}", excerpt)
    )
    message = client.messages.create(
        model=model,
        max_tokens=16000,
        temperature=0,
        system=GOING_CONCERN_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    warn_if_truncated(message, filing_label)
    raw = parse_json_array(message.content[0].text)
    out = [_validate_going_concern(r, filing_label, section_label, section_conf) for r in raw]
    return [g for g in out if g is not None and quote_in_text(g.evidence_quote, excerpt)]


def _collapse_going_concern(findings: list[GoingConcern]) -> list[GoingConcern]:
    """
    Collapse going-concern findings across sections (LLM_GOING_CONCERN §6.1):
      - dedupe by (tier, evidence_quote);
      - if any Tier-1 finding exists, drop all Tier-2 (redundant in that filing).
    """
    seen: set = set()
    deduped: list[GoingConcern] = []
    for g in findings:
        key = (g.tier, g.evidence_quote)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(g)
    if any(g.tier == 1 for g in deduped):
        deduped = [g for g in deduped if g.tier == 1]
    return deduped


# ── Covenant breach / waiver pass (Stage 2c-iii) ─────────────────────────────
# Restores LLM_COVENANT §7.2 condition 3 (the language-based near_limit signal
# DEFERRED in 2c-i). A footnote-level GROUNDED judgment — NOT per-covenant and
# NOT regex — asking once over the debt footnote (+ MD&A) whether the filing
# discloses a PRESENT breach / waiver / forbearance / non-compliance, as a fact
# about THIS period. Architecture cloned from the going-concern pass: quote-first,
# an explicit "what is NOT a finding" block (covenant TERMS, negations,
# conditionals — the three classes that defeated _WAIVER_RE), and a litmus test.
#
# Why footnote-level, not per-covenant: in 2c-i the genuine waiver sentences
# (Halcon / Sanchez / Tailored Brands) sat in DIFFERENT sentences than any single
# covenant's evidence_quote, so a per-covenant-quote judgment would never see
# them. The finding is mapped onto covenant rows downstream (Stage 2 wiring).
#
# Scope (Stage 2c-iii decision): debt footnote + MD&A ONLY. Risk-factors are
# EXCLUDED — that section is hypothetical-heavy ("a future breach could ..."),
# exactly the conditional class we must not fire on. Add later only if validation
# shows missed breaches.
#
# This pass is NET-NEW and not yet wired to near_limit / score / schema: it is
# validated in isolation (offline judgment harness, tests #2 + #3) BEFORE the
# score path is touched.

# Present-state confirmatory gate (necessary-but-not-sufficient): some breach /
# waiver / non-compliance TOKEN must literally appear in the quote. The LLM makes
# the semantic term-vs-present-state call; this regex can only REJECT a finding
# whose quote carries no such language (defense against a hallucinated finding),
# so it cannot manufacture a false positive. It is intentionally NOT the decider
# (that approach failed twice in 2c-i). Broad on present-state affirmatives so it
# does not reject a genuine waiver phrased without a specific keyword.
_BREACH_PRESENT_RE = re.compile(
    r"waiver|forbearance|amend(?:ment|ed)\b"
    r"|not\s+in\s+compliance|non-?compliance"
    r"|in\s+breach|breached\b|covenant\s+violation|violated"
    r"|in\s+default|event\s+of\s+default"
    r"|(?:did\s+not|failed\s+to)\s+(?:comply|satisfy|maintain|meet)",
    re.IGNORECASE,
)
_BREACH_STATUSES = (
    "waiver_obtained", "breach", "non_compliance", "forbearance", "default", "amendment",
)

_BREACH_MAX_SECTION_CHARS = 60_000

COVENANT_BREACH_SYSTEM = """You are a credit analyst reading SEC filing text for ONE purpose: to detect \
whether the filing discloses that the company is CURRENTLY in breach of, in \
default under, or not in compliance with a financial covenant — or has obtained \
a WAIVER, FORBEARANCE, or amendment because of such a failure.

This is one of the strongest single signals in corporate credit, and it is \
surrounded by language that LOOKS similar but is NOT a present breach. You must \
keep these strictly separate:

  - A PRESENT BREACH / WAIVER is a statement of FACT about this reporting period:
    the company HAS failed a covenant, IS not in compliance, or HAS obtained a
    waiver / entered a forbearance / amended a covenant because of a failure.

  - COVENANT TERMS are the contract's definitions of what WOULD constitute a
    default ("events of default include a breach of any covenant"). They describe
    the rules, not a present failure. NOT a finding.

  - NEGATIONS state the company is fine ("we were not in default", "we were in
    compliance with all covenants"). The OPPOSITE of a breach. NOT a finding.

  - HYPOTHETICALS / CONDITIONALS describe what could happen ("if we fail to
    maintain the ratio, we would be in default", "a future breach could result in
    acceleration"). Forward-looking risk, not a present fact. NOT a finding.

Your output feeds a credit early-warning system. A fabricated breach is worse \
than a missed one — it manufactures a false alarm. Most filings disclose NO \
present breach or waiver, and returning an empty list is the correct, common \
answer. Ground every finding in verbatim text; never infer a breach the text \
does not state."""

# NOTE: built with str.replace (NOT .format) — the few-shots contain literal { }.
COVENANT_BREACH_USER = """COMPANY: {COMPANY_NAME}
FILING: {FILING_TYPE}, period ending {PERIOD_END}
SECTION: {SECTION_LABEL}  (locator confidence: {SECTION_CONFIDENCE})

============================================================================
WHAT TO DETECT — A PRESENT BREACH OR WAIVER (a fact about THIS period)
============================================================================
Extract a finding ONLY when the text states, as a present fact about this
reporting period, that the company:
  - was NOT in compliance with / FAILED / BREACHED / VIOLATED a financial
    covenant; or
  - IS in default (or an event of default has occurred and is continuing) under a
    debt agreement for a financial-covenant reason; or
  - OBTAINED A WAIVER, entered into a FORBEARANCE agreement, or AMENDED a covenant
    because of (or to avoid) such a failure.

============================================================================
WHAT IS NOT A FINDING  (return nothing for these — read carefully)
============================================================================
These three classes look like a breach but are NOT. Do NOT extract them:

  1. COVENANT TERMS / DEFINITIONS — the contract describing what would count as a
     default. NOT a present breach.
     e.g. "Events of default under the Credit Agreement include the breach of any
     covenant, a default in payment, or a material adverse change."

  2. NEGATIONS — the company stating it is in compliance / not in default. This is
     the OPPOSITE signal.
     e.g. "As of December 31, we were in compliance with all financial covenants."
     e.g. "We were not in default under any of our debt agreements."

  3. HYPOTHETICALS / CONDITIONALS — what could or would happen, not what has.
     e.g. "If we have failed to maintain the required ratio, the lenders could
     accelerate the debt."
     e.g. "A future breach of our covenants could result in cross-default."

LITMUS TEST to apply before emitting any finding: "Is the filing stating, as a
present fact about THIS reporting period, that the company HAS breached / been
waived — or is it merely DEFINING what a breach would be, DENYING a breach, or
WARNING of a possible future one? Only the first is a finding."

============================================================================
HOW TO EXTRACT — QUOTE FIRST, CLASSIFY SECOND
============================================================================
For each finding, in this order:
  1. First copy the exact VERBATIM, CONTIGUOUS sentence(s) into evidence_quote.
     No paraphrase; no stitching distant fragments. The quote MUST contain the
     present-state breach/waiver language (e.g. "was not in compliance",
     "obtained a waiver", "entered into a forbearance agreement").
  2. Then assign the fields, reading them off that quote.

If the filing discloses no present breach or waiver, return an empty list []. That
is the correct, expected answer for most filings.

============================================================================
OUTPUT FORMAT
============================================================================
Return ONLY a JSON array (no prose, no markdown fences). Each element:

{
  "evidence_quote":     "<verbatim contiguous span — write this FIRST>",
  "breach_or_waiver":   true,
  "status":             "<waiver_obtained | breach | non_compliance | forbearance | default | amendment>",
  "covenant_reference": "<the covenant named in the quote, e.g. 'maximum consolidated leverage ratio'; null if the quote names no specific covenant>",
  "description":        "<one-sentence summary of the present breach/waiver>",
  "null_reason":        "<required when covenant_reference is null; else null>"
}

============================================================================
EXAMPLES
============================================================================
Example 1 — present non-compliance + waiver (a finding):
TEXT: "As of December 31, 2019, the Company was not in compliance with the maximum
total leverage ratio under its Credit Agreement and obtained a waiver from its
lenders through March 31, 2020."
OUTPUT:
[{"evidence_quote":"As of December 31, 2019, the Company was not in compliance with the maximum total leverage ratio under its Credit Agreement and obtained a waiver from its lenders through March 31, 2020.","breach_or_waiver":true,"status":"waiver_obtained","covenant_reference":"maximum total leverage ratio","description":"Company was out of compliance with its leverage covenant and obtained a lender waiver.","null_reason":null}]

Example 2 — COVENANT TERMS, not a present breach (NOT a finding):
TEXT: "Events of default under the Credit Agreement include the breach of any
covenant, the failure to pay principal or interest when due, and the occurrence of
a material adverse change."
OUTPUT:
[]

Example 3 — NEGATION, the company is compliant (NOT a finding):
TEXT: "As of the end of the period, we were in compliance with all financial
covenants and were not in default under any of our debt agreements."
OUTPUT:
[]

Example 4 — HYPOTHETICAL / CONDITIONAL (NOT a finding):
TEXT: "If we have failed to maintain the required fixed charge coverage ratio, our
lenders could declare an event of default and accelerate the indebtedness."
OUTPUT:
[]

Example 5 — present forbearance (a finding):
TEXT: "The Company did not satisfy the minimum interest coverage covenant for the
quarter ended September 30, 2019, and on October 15, 2019 entered into a
forbearance agreement with its lenders."
OUTPUT:
[{"evidence_quote":"The Company did not satisfy the minimum interest coverage covenant for the quarter ended September 30, 2019, and on October 15, 2019 entered into a forbearance agreement with its lenders.","breach_or_waiver":true,"status":"forbearance","covenant_reference":"minimum interest coverage covenant","description":"Company missed its interest coverage covenant and entered a forbearance agreement.","null_reason":null}]

============================================================================
TEXT TO ANALYZE
============================================================================
{SECTION_TEXT}"""


@dataclass
class CovenantBreach:
    """
    One footnote-level covenant breach / waiver finding (Stage 2c-iii).

    A PRESENT-STATE disclosure that the company is in breach / default / non-
    compliance, or has obtained a waiver / forbearance / amendment. Footnote-level
    (not per-covenant): downstream wiring (Stage 2) maps it onto covenant rows to
    set near_limit. `covenant_reference` is the free-text covenant the quote names
    (used for that mapping); null means an "orphan" breach with no named covenant.
    cik/period_end/created_at are added by the saver, not here.
    """
    breach_or_waiver: bool          # always True for a kept finding
    status: str | None              # waiver_obtained | breach | non_compliance | forbearance | default | amendment
    covenant_reference: str | None  # covenant named in the quote, else None (orphan)
    evidence_quote: str             # verbatim, contiguous (required), contains the present-state language
    description: str | None         # one-line summary
    section: str | None             # which located section (Debt / MD&A)
    section_confidence: str         # "high" | "low" (from sections.section_confidence)
    source: str                     # filing label, e.g. "10-K 2019-12-31, Debt"
    null_reason: str | None = None


def _validate_covenant_breach(
    raw: dict, filing_label: str, section_label: str, section_conf: str
) -> CovenantBreach | None:
    """
    Validate one raw breach/waiver dict. Enforces the present-state contract:
      - evidence_quote required (non-empty);
      - breach_or_waiver must be truthy (the model declined → drop);
      - the quote MUST contain a present-state breach/waiver token
        (_BREACH_PRESENT_RE) — the confirmatory gate. The LLM makes the semantic
        term-vs-present call; this only rejects a finding whose quote carries no
        such language, so it cannot create a false positive;
      - status normalized to the controlled vocabulary, else None.
    Returns None (dropped) on any failure. (quote_in_text against the section
    excerpt is applied by the caller, like every other pass.)
    """
    if not isinstance(raw, dict):
        return None
    if not _to_bool(raw.get("breach_or_waiver", False)):
        return None
    evidence_quote = str(raw.get("evidence_quote", "") or "").strip()
    if not evidence_quote:
        return None
    # Confirmatory gate: present-state breach/waiver language must be in the quote.
    if not _BREACH_PRESENT_RE.search(evidence_quote):
        return None

    status = str(raw.get("status", "") or "").strip().lower()
    status = status if status in _BREACH_STATUSES else None

    return CovenantBreach(
        breach_or_waiver=True,
        status=status,
        covenant_reference=(str(raw.get("covenant_reference", "") or "").strip() or None),
        evidence_quote=evidence_quote[:2000],
        description=(str(raw.get("description", "") or "").strip() or None),
        section=section_label or None,
        section_confidence=section_conf,
        source=filing_label,
        null_reason=(str(raw.get("null_reason", "") or "").strip() or None),
    )


def extract_covenant_breach(
    section_text: str,
    filing_label: str,
    client: anthropic.Anthropic | None = None,
    *,
    section_label: str = "",
    section_conf: str = "low",
    company_name: str = "",
    period_end: str = "",
    filing_type: str = "10-K",
    max_chars: int = _BREACH_MAX_SECTION_CHARS,
    model: str = MODEL,
) -> list[CovenantBreach]:
    """
    Extract present-state covenant breach / waiver findings from one located
    section slice (debt footnote or MD&A — NOT risk factors, per the 2c-iii scope
    decision). Haiku, temperature=0, single-shot.

    Grounding: every kept finding's evidence_quote must (a) contain a present-state
    breach/waiver token (_validate_covenant_breach) AND (b) pass quote_in_text
    against the excerpt the model saw — same contract as every other pass. Returns
    [] on any failure so a broken call never blocks the pipeline.

    NET-NEW and not yet wired to near_limit / score: validated in isolation first.
    """
    excerpt = (section_text or "")[:max_chars]
    if not excerpt.strip():
        return []
    if client is None:
        # anthropic.Anthropic() honours ANTHROPIC_BASE_URL (the APIYI relay) from env.
        client = anthropic.Anthropic()

    user_prompt = (
        COVENANT_BREACH_USER
        .replace("{COMPANY_NAME}", company_name or "(issuer)")
        .replace("{FILING_TYPE}", filing_type)
        .replace("{PERIOD_END}", period_end or "")
        .replace("{SECTION_LABEL}", section_label or "(section)")
        .replace("{SECTION_CONFIDENCE}", section_conf)
        .replace("{SECTION_TEXT}", excerpt)
    )
    message = client.messages.create(
        model=model,
        max_tokens=16000,
        temperature=0,
        system=COVENANT_BREACH_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
    )
    warn_if_truncated(message, filing_label)
    raw = parse_json_array(message.content[0].text)
    out = [_validate_covenant_breach(r, filing_label, section_label, section_conf) for r in raw]
    return [b for b in out if b is not None and quote_in_text(b.evidence_quote, excerpt)]


# Keyword → covenant_type map for matching a breach's free-text covenant_reference
# to an extracted covenant. Ordered most-specific first ("fixed charge coverage"
# before "coverage"); first hit wins.
_BREACH_REF_TYPE_MAP = (
    ("fixed charge", "min_fixed_charge_coverage"),
    ("leverage", "max_leverage"),
    ("interest coverage", "min_coverage"),
    ("coverage", "min_coverage"),
    ("net worth", "min_net_worth"),
    ("liquidity", "min_liquidity"),
    ("availability", "min_liquidity"),
    ("minimum cash", "min_liquidity"),
    ("capital expenditure", "max_capex"),
    ("capex", "max_capex"),
    ("cross-default", "cross_default"),
    ("cross default", "cross_default"),
)


def _ref_to_type(ref: str) -> str | None:
    """Map a breach's covenant_reference free text to a covenant_type, else None."""
    for kw, ct in _BREACH_REF_TYPE_MAP:
        if kw in ref:
            return ct
    return None


def _match_covenant_for_breach(breach: CovenantBreach, covenants: list[Covenant]) -> Covenant | None:
    """
    Pick the single covenant a footnote-level breach/waiver finding refers to, or
    None (an "orphan" breach with no matching extracted covenant).

    Match priority (each picks AT MOST one covenant — a single disclosed breach is
    one unit of signal, never fanned out across all covenants):
      1. quote overlap — the breach quote contains/overlaps a covenant's quote
         (rare: a waiver usually sits in a different sentence than the covenant);
      2. ratio_name overlap with covenant_reference;
      3. covenant_type from covenant_reference keyword; among that type prefer a
         maintenance covenant, then one with a reported_actual, then the first.
    No reference text and no quote overlap → None (orphan), never a guessed match.
    """
    if not covenants:
        return None
    for c in covenants:
        if _quotes_overlap(breach.evidence_quote, c.evidence_quote):
            return c
    ref = (breach.covenant_reference or "").strip().lower()
    if not ref:
        return None
    for c in covenants:
        rn = (c.ratio_name or "").strip().lower()
        if rn and (rn in ref or ref in rn):
            return c
    mapped = _ref_to_type(ref)
    if mapped:
        typed = [c for c in covenants if c.covenant_type == mapped]
        if typed:
            typed.sort(key=lambda c: (c.is_maintenance is not True, c.reported_actual is None))
            return typed[0]
    return None


def _apply_breach_findings(
    covenants: list[Covenant], breaches: list[CovenantBreach]
) -> list[CovenantBreach]:
    """
    Map footnote-level breach/waiver findings onto covenant rows (Stage 2c-iii —
    restores LLM_COVENANT §7.2 condition 3). For each finding that maps to an
    existing covenant, set near_limit=True and stamp the three audit fields:
    near_limit_reason="waiver/breach disclosed", near_limit_evidence_quote (the
    verbatim breach sentence — distinct from the covenant's own evidence_quote),
    and near_limit_section (where it was disclosed).

    Returns the ORPHAN breaches (no matching covenant). Per the Stage-1 decision:
    never fabricate a covenant row, never silently drop a breach — orphans are
    surfaced to the caller (and logged) for human review; REVIEW_FLAGS persistence
    is deferred. Mutates the matched covenants in place.
    """
    orphans: list[CovenantBreach] = []
    for b in breaches:
        match = _match_covenant_for_breach(b, covenants)
        if match is None:
            orphans.append(b)
            continue
        match.near_limit = True
        match.near_limit_reason = "waiver/breach disclosed"
        match.near_limit_evidence_quote = b.evidence_quote
        match.near_limit_section = b.section
    return orphans


def review_filing(
    cik: str,
    period: str,
    filings: list[dict],
    client: anthropic.Anthropic | None = None,
) -> tuple[list[Finding], list[Covenant], list[LossProvision], list[GoingConcern], list[CovenantBreach]]:
    """
    End-to-end LLM review for one period's 10-K: fetch → locate → extract.

    Shared by the CLI (track.py) and the API (api/main.py) so the locate-then-LLM
    flow lives in one place. The filing is fetched once and locate_sections runs
    once; five extraction passes follow:
      1. MD&A             → qualitative Findings (llm_review.review_text)
      2. debt footnote (+ MD&A/risk recall) → Covenants (Stage A ∪ B, deduped)
      3. debt footnote + MD&A → covenant breach/waiver (Stage 2c-iii): sets
         near_limit on the matched covenant; the ORPHANS (a disclosed breach with
         no matching covenant row) are RETURNED as the 5th element for review.
      4. contingencies    → LossProvisions
      5. auditor report + GC footnote + MD&A + risk factors → GoingConcern
         (Tier-1 / Tier-2; unioned across sections then collapsed)

    The MD&A pass runs ONLY on the located Item 7 section. If the section can't
    be found there is no fallback to the head of the raw HTML — that was the old
    pipeline's bug (the first chars of an inline-XBRL filing are markup, not
    MD&A), and a silent off-target review is worse than none.

    The filing is matched to the period via ingest.find_filing_for_period
    (exact fiscal-period reportDate first), not the old calendar-year heuristic
    that picked the wrong 10-K for off-calendar fiscal years.

    Imports ingest/sections lazily to avoid pulling the HTTP/parsing stack into
    modules that only need the dataclasses.

    Returns ([], [], [], [], []) if no matching filing or no sections are found.
    """
    from src.ingest import filing_doc_url, find_filing_for_period, get_filing_text
    from src.sections import locate_sections, section_confidence

    filing = find_filing_for_period(filings, period)
    if filing is None:
        return [], [], [], [], []

    # One retry-aware client, reused across all five passes below (CLI and API
    # both call review_filing without a client). max_retries lets the SDK ride out
    # 429 rate-limits and 5xx with exponential backoff honoring retry-after, so a
    # rate-limited pass retries instead of erroring out and being skipped. It is a
    # no-op on the healthy path (only acts on 429/5xx) — no sleep(), no slowdown
    # when not throttled. Built only after the filing is located, so the no-match
    # early-return above never constructs a client. Callers that pass their own
    # client (e.g. tests' MagicMock) keep it; the per-pass fallbacks are untouched.
    if client is None:
        client = anthropic.Anthropic(max_retries=8)

    text = get_filing_text(cik, filing["accessionNumber"], filing["primaryDocument"])
    # Public EDGAR URL of this document, so qualitative findings can deep-link
    # back to the source 10-K (the UI appends a #:~:text= quote fragment).
    doc_url = filing_doc_url(cik, filing["accessionNumber"], filing["primaryDocument"])
    sections = locate_sections(text)

    findings: list[Finding] = []
    covenants: list[Covenant] = []
    provisions: list[LossProvision] = []
    going_concern: list[GoingConcern] = []
    if sections["mdna"] is not None:
        findings = review_text(
            sections["mdna"].text, f"10-K {period}, MD&A", client, source_url=doc_url
        )
    # Covenants: Stage A (debt footnote, precise) ∪ Stage B (MD&A + risk-factors
    # recall sweep, 2c-ii), deduped to one row per covenant. cushion/near_limit
    # are recomputed in code after the merge (2c-i logic, unchanged).
    covenants_a: list[Covenant] = []
    if sections["debt"] is not None:
        covenants_a = extract_debt_footnote(
            sections["debt"].text, f"10-K {period}, Debt", client,
            section_conf=section_confidence(sections["debt"]), period_end=period,
        )
    covenants_b: list[Covenant] = []
    for _key, _label in (("mdna", "MD&A"), ("risk_factors", "Risk Factors")):
        _sec = sections.get(_key)
        if _sec is not None:
            covenants_b += extract_covenants_broad(
                _sec.text, f"10-K {period}, {_label} (covenants)", client,
                section_label=_label, section_conf=section_confidence(_sec),
                period_end=period,
            )
    covenants = _dedupe_covenants(covenants_a, covenants_b)

    # ── Covenant breach / waiver (Stage 2c-iii): footnote-level grounded pass ──
    # Restores the language-based near_limit signal (LLM_COVENANT §7.2 cond. 3).
    # Runs over the debt footnote + MD&A ONLY (risk-factors EXCLUDED — hypothetical-
    # heavy, exactly the conditional class we must not fire on). Runs AFTER dedupe,
    # so the numeric near_limit recompute in _merge_into can't clobber a language
    # flag. Maps each finding to one covenant (near_limit + reason + verbatim quote
    # + section); orphan breaches (no matching covenant) are logged for review,
    # never fabricated into rows, never silently dropped.
    breach_findings: list[CovenantBreach] = []
    for _key, _label in (("debt", "Debt footnote"), ("mdna", "MD&A")):
        _sec = sections.get(_key)
        if _sec is not None:
            breach_findings += extract_covenant_breach(
                _sec.text, f"10-K {period}, {_label} (breach/waiver)", client,
                section_label=_label, section_conf=section_confidence(_sec),
                period_end=period,
            )
    orphan_breaches = _apply_breach_findings(covenants, breach_findings)
    for _orphan in orphan_breaches:
        logger.warning(
            "Covenant breach/waiver disclosed but no matching covenant extracted "
            "(orphan — surfaced for review): cik=%s period=%s section=%s status=%s quote=%r",
            cik, period, _orphan.section, _orphan.status, _orphan.evidence_quote[:200],
        )

    if sections["contingencies"] is not None:
        provisions = extract_loss_provisions(
            sections["contingencies"].text, f"10-K {period}, Contingencies", client
        )

    # ── Going-concern (Stage 2b): Tier-1 from auditor report + GC footnote;
    # Tier-2 from MD&A + risk factors. Union across sections, then collapse
    # (dedupe; Tier-1 present → drop Tier-2). Each finding carries the section's
    # locator confidence. The model labels the tier from the quote.
    gc_sources = [
        ("auditor_report", "Auditor's Report"),
        ("going_concern_footnote", "Going-Concern Footnote"),
        ("mdna", "MD&A"),
        ("risk_factors", "Risk Factors"),
    ]
    gc_raw: list[GoingConcern] = []
    for key, label in gc_sources:
        sec = sections.get(key)
        if sec is None:
            continue
        gc_raw += extract_going_concern(
            sec.text, f"10-K {period}, {label}", client,
            section_label=label, section_conf=section_confidence(sec),
            period_end=period,
        )
    going_concern = _collapse_going_concern(gc_raw)

    return findings, covenants, provisions, going_concern, orphan_breaches
