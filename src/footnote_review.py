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

# Same fast/cheap model used for the qualitative review — structured extraction
# from a focused excerpt is well within Haiku's capability.
MODEL = "claude-haiku-4-5-20251001"

_COVENANT_TYPES = ("max_leverage", "min_coverage", "min_net_worth", "other")
_DIRECTIONS = ("max", "min")

# Matches numeric tokens like "4.0", "58,744", "500", "0.5" in an evidence quote.
_NUM_RE = re.compile(r"\d[\d,]*\.?\d*")


COVENANT_PROMPT = """You are a credit analyst reading the DEBT footnote of a SEC 10-K filing.
Identify any MAINTENANCE COVENANTS the company must comply with — financial tests
in its debt or credit agreements. Common types:
- maximum leverage ratio (debt / EBITDA must stay BELOW a limit)
- minimum interest-coverage ratio (EBITDA / interest must stay ABOVE a limit)
- minimum net worth / tangible net worth (must stay ABOVE a limit)

Return a JSON array. Each covenant must be:
{
  "covenant_type": "max_leverage" | "min_coverage" | "min_net_worth" | "other",
  "threshold": <the numeric limit if stated, else null>,
  "direction": "max" | "min",
  "reported_actual": <the company's current level if disclosed, else null>,
  "near_limit": <true if the company is described as close to, or at risk of breaching, the limit>,
  "evidence_quote": "<verbatim excerpt, max 240 chars, containing any number you report>",
  "source": "<the filing label provided>"
}

Rules:
- Every number you put in threshold or reported_actual MUST appear verbatim in evidence_quote.
- If a number is not explicitly stated, use null — never estimate or infer a figure.
- evidence_quote must be a direct quote, not a paraphrase.
- Omit covenants you cannot quote. Return [] if no maintenance covenants are described.
"""

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
    One maintenance covenant extracted from the debt footnote.

    Numeric fields are nullable: kept only when the figure appears verbatim in
    evidence_quote (see _number_in_text). A covenant may therefore carry just a
    qualitative near_limit flag plus its quote.
    """
    covenant_type: str          # max_leverage | min_coverage | min_net_worth | other
    threshold: float | None     # the limit, if reliably parsed
    direction: str              # "max" | "min"
    reported_actual: float | None  # current level, if disclosed
    near_limit: bool            # sits close to / at risk of breaching the limit
    evidence_quote: str         # verbatim quote (required)
    source: str


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


def _validate_covenant(raw: dict, fallback_source: str) -> Covenant | None:
    """Validate one raw covenant dict; drop hallucinated numbers to None."""
    if not isinstance(raw, dict):
        return None
    covenant_type = str(raw.get("covenant_type", "")).strip().lower()
    direction = str(raw.get("direction", "")).strip().lower()
    evidence_quote = str(raw.get("evidence_quote", "")).strip()

    if covenant_type not in _COVENANT_TYPES or not evidence_quote:
        return None
    if direction not in _DIRECTIONS:
        # Infer a sensible default from the covenant type rather than dropping it.
        direction = "max" if covenant_type == "max_leverage" else "min"

    threshold = _to_float(raw.get("threshold"))
    reported_actual = _to_float(raw.get("reported_actual"))
    # Drop any number not backed verbatim by the quote.
    if not _number_in_text(threshold, evidence_quote):
        threshold = None
    if not _number_in_text(reported_actual, evidence_quote):
        reported_actual = None

    return Covenant(
        covenant_type=covenant_type,
        threshold=threshold,
        direction=direction,
        reported_actual=reported_actual,
        near_limit=_to_bool(raw.get("near_limit", False)),
        evidence_quote=evidence_quote[:240],
        source=str(raw.get("source", "") or fallback_source).strip(),
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
) -> list[Covenant]:
    """
    Extract maintenance covenants from a located debt-footnote slice.

    Args:
        section_text:  The debt-footnote text from sections.locate_sections.
        filing_label:  e.g. "10-K 2023-09-30, Debt" — used as the finding source.
        client:        Optional pre-created Anthropic client (else built from env).

    Returns validated Covenant objects (may be empty). Hallucinated numbers are
    nulled out; covenants without a quotable basis — or whose quote does not
    actually appear in the section text — are dropped entirely.
    """
    raw, excerpt = _extract(section_text, filing_label, COVENANT_PROMPT, client)
    out = [_validate_covenant(r, filing_label) for r in raw]
    return [c for c in out if c is not None and quote_in_text(c.evidence_quote, excerpt)]


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


def review_filing(
    cik: str,
    period: str,
    filings: list[dict],
    client: anthropic.Anthropic | None = None,
) -> tuple[list[Finding], list[Covenant], list[LossProvision], list[GoingConcern]]:
    """
    End-to-end LLM review for one period's 10-K: fetch → locate → extract.

    Shared by the CLI (track.py) and the API (api/main.py) so the locate-then-LLM
    flow lives in one place. The filing is fetched once and locate_sections runs
    once; four extraction passes follow:
      1. MD&A             → qualitative Findings (llm_review.review_text)
      2. debt footnote    → Covenants
      3. contingencies    → LossProvisions
      4. auditor report + GC footnote + MD&A + risk factors → GoingConcern
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

    Returns ([], [], [], []) if no matching filing or no sections are found.
    """
    from src.ingest import filing_doc_url, find_filing_for_period, get_filing_text
    from src.sections import locate_sections, section_confidence

    filing = find_filing_for_period(filings, period)
    if filing is None:
        return [], [], [], []

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
    if sections["debt"] is not None:
        covenants = extract_debt_footnote(
            sections["debt"].text, f"10-K {period}, Debt", client
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

    return findings, covenants, provisions, going_concern
