"""
LLM structured extraction of debt-footnote and loss-provision details.

Why a separate module from llm_review.py?
  llm_review.py extracts QUALITATIVE findings and enforces a hard "no digits"
  rule — numbers are forbidden there because they belong to the deterministic
  XBRL path (extract.py). This module is the deliberate exception: it extracts
  HYBRID output — structured numbers (covenant thresholds, accrued provision
  amounts) WHERE they can be reliably parsed, plus a qualitative flag and a
  verbatim quote for everything else.

The anti-hallucination contract (critical):
  An LLM asked for numbers will sometimes invent them. So every non-null numeric
  field returned by the model is validated against its own evidence_quote: if the
  number does not actually appear in the quote (allowing for unit scaling like
  "$500 million" → 500000000), it is dropped to None. The qualitative flag and
  the verbatim quote always survive. This keeps the structured numbers honest and
  is why these signals are only allowed a small, capped contribution to the
  stress score (see score.py).

Input:
  These functions receive a pre-located section slice from sections.py — the
  debt footnote or the commitments-and-contingencies footnote — NOT the whole
  filing. That keeps each LLM call small and on-target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import anthropic

from src.llm_review import Finding, parse_json_array, quote_in_text, review_text

# Same fast/cheap model used for the qualitative review — structured extraction
# from a short, focused excerpt is well within Haiku's capability.
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
        near_limit=bool(raw.get("near_limit", False)),
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
        is_material=bool(raw.get("is_material", False)),
        qualitative_flag=str(raw.get("qualitative_flag", "")).strip(),
        evidence_quote=evidence_quote[:240],
        source=str(raw.get("source", "") or fallback_source).strip(),
    )


# Max section characters sent per footnote LLM call. The located sections are
# capped at 20k chars by sections.py; this is the single trim applied here.
MAX_SECTION_CHARS = 20_000


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
        max_tokens=1500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
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


def review_filing(
    cik: str,
    period: str,
    filings: list[dict],
    client: anthropic.Anthropic | None = None,
) -> tuple[list[Finding], list[Covenant], list[LossProvision]]:
    """
    End-to-end LLM review for one period's 10-K: fetch → locate → extract.

    Shared by the CLI (track.py) and the API (api/main.py) so the locate-then-LLM
    flow lives in one place. The filing is fetched once and locate_sections runs
    once; three extraction passes follow:
      1. MD&A          → qualitative Findings (llm_review.review_text)
      2. debt footnote → Covenants
      3. contingencies → LossProvisions

    The MD&A pass runs ONLY on the located Item 7 section. If the section can't
    be found there is no fallback to the head of the raw HTML — that was the old
    pipeline's bug (the first chars of an inline-XBRL filing are markup, not
    MD&A), and a silent off-target review is worse than none.

    The filing is matched to the period via ingest.find_filing_for_period
    (exact fiscal-period reportDate first), not the old calendar-year heuristic
    that picked the wrong 10-K for off-calendar fiscal years.

    Imports ingest/sections lazily to avoid pulling the HTTP/parsing stack into
    modules that only need the dataclasses.

    Returns ([], [], []) if no matching filing or no sections are found.
    """
    from src.ingest import find_filing_for_period, get_filing_text
    from src.sections import locate_sections

    filing = find_filing_for_period(filings, period)
    if filing is None:
        return [], [], []

    text = get_filing_text(cik, filing["accessionNumber"], filing["primaryDocument"])
    sections = locate_sections(text)

    findings: list[Finding] = []
    covenants: list[Covenant] = []
    provisions: list[LossProvision] = []
    if sections["mdna"] is not None:
        findings = review_text(
            sections["mdna"].text, f"10-K {period}, MD&A", client
        )
    if sections["debt"] is not None:
        covenants = extract_debt_footnote(
            sections["debt"].text, f"10-K {period}, Debt", client
        )
    if sections["contingencies"] is not None:
        provisions = extract_loss_provisions(
            sections["contingencies"].text, f"10-K {period}, Contingencies", client
        )
    return findings, covenants, provisions
