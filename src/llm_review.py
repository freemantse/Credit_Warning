"""
LLM qualitative review of 10-K filing text.

Design constraints:
  - The LLM returns qualitative labels only — no numbers, no scores.
  - Numbers are the job of the deterministic XBRL extraction (extract.py).
  - This separation keeps the scoring model fully auditable: you can always
    trace a score back to specific XBRL tags and verbatim filing quotes.

Invariant: any finding whose concern field contains a digit is rejected.
  This enforces the qualitative-only rule post-generation. For example,
  "revenue declined 15%" would be rejected; "management expressed doubt
  about revenue growth" would be kept.

Input: callers pass the LOCATED, tag-stripped MD&A section text (from
  sections.locate_sections), not raw filing HTML. review_text() trims it to
  MAX_REVIEW_CHARS in one place — the only truncation point.

Quote verification: each finding's evidence_quote is checked against the source
  text after whitespace/quote-mark normalization; findings whose quote does not
  appear are dropped. This is the qualitative counterpart of footnote_review's
  number-in-quote guard.

Contribution to the stress score:
  High-severity findings add up to 10 points to the score (see score.py).
  They cannot independently push an issuer past the 50-point stress threshold.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import anthropic

logger = logging.getLogger(__name__)

# claude-haiku is fast and cheap — appropriate for structured extraction tasks
# where we send a constrained prompt and expect JSON output.
MODEL = "claude-haiku-4-5-20251001"

# The single truncation point for review input. 100k chars ≈ 25k tokens — covers
# nearly all MD&A sections in full (including Liquidity and Capital Resources,
# which sits in the latter half) and is small against the model's 200k-token
# context window.
MAX_REVIEW_CHARS = 100_000

# The system prompt is carefully worded to:
#   1. Focus the LLM on qualitative language signals only (not repeating numbers).
#   2. Require verbatim evidence quotes (not paraphrases) so findings are verifiable.
#   3. Demand structured JSON output so we can parse it reliably.
#   4. Instruct the model to return [] rather than inventing findings.
SYSTEM_PROMPT = """You are a credit analyst assistant reviewing SEC filing text.
Your job is to identify qualitative risk signals that a parser cannot detect:
- Management tone: hedging, uncertainty, defensiveness
- Covenant proximity warnings or waivers mentioned
- Litigation or regulatory concerns that could affect cash flows
- Going-concern language or auditor qualifications
- Unusual disclosures about liquidity or financing

Return a JSON array of findings. Each finding must be:
{
  "concern": "<short label — NO numbers, percentages, or dollar amounts>",
  "severity": "low" | "medium" | "high",
  "evidence_quote": "<verbatim excerpt from the filing text, max 200 chars>",
  "source": "<e.g. 10-K 2023-12-31, MD&A>"
}

Rules:
- concern must describe a qualitative issue only — never include numbers
- evidence_quote must be a direct quote, not a paraphrase
- omit findings where you cannot quote evidence
- return [] if no concerns found
"""

# Compiled once at import time (not inside the function) for efficiency.
# Used to reject any finding whose concern contains a digit.
_NUMBER_IN_CONCERN = re.compile(r"\d")

# Normalization for quote verification: typographic characters the LLM (or the
# HTML-to-text step) may render differently from each other.
_QUOTE_CHAR_MAP = str.maketrans({
    "‘": "'", "’": "'",   # curly single quotes
    "“": '"', "”": '"',   # curly double quotes
    "–": "-", "—": "-",   # en/em dashes
    " ": " ",                  # non-breaking space
})
_WS_RE = re.compile(r"\s+")

# When a quote was truncated by the model (max-length instruction), its tail may
# be cut mid-word or end in an ellipsis. A prefix of this many normalized chars
# is enough to confirm the quote is genuine.
_QUOTE_PREFIX_CHARS = 80


def _normalize_for_match(s: str) -> str:
    """Lowercase, map typographic quotes/dashes to ASCII, collapse whitespace."""
    return _WS_RE.sub(" ", s.translate(_QUOTE_CHAR_MAP).lower()).strip()


def quote_in_text(quote: str, source_text: str) -> bool:
    """
    Anti-hallucination guard: does `quote` actually appear in `source_text`?

    Both sides are normalized (case, curly quotes, dashes, whitespace) before a
    substring check. Long quotes also pass on their first _QUOTE_PREFIX_CHARS
    characters, tolerating model-side truncation/ellipsis at the tail.

    The qualitative counterpart of footnote_review._number_in_text — shared by
    both modules so every stored evidence_quote is verified against its source.
    """
    norm_quote = _normalize_for_match(quote).rstrip(". …")
    if not norm_quote:
        return False
    norm_source = _normalize_for_match(source_text)
    if norm_quote in norm_source:
        return True
    return (
        len(norm_quote) > _QUOTE_PREFIX_CHARS
        and norm_quote[:_QUOTE_PREFIX_CHARS] in norm_source
    )


def warn_if_truncated(message, filing_label: str) -> None:
    """
    Log a warning when the model stopped because it hit max_tokens.

    A truncated response is cut off mid-JSON, so parse_json_array returns [] and
    the period silently stores zero findings — indistinguishable from "no
    concerns found". The caps are sized generously (~100 items per call), so
    this firing means something unusual; surface it instead of hiding it.
    """
    if getattr(message, "stop_reason", None) == "max_tokens":
        logger.warning(
            "LLM response for %s hit max_tokens and was truncated; "
            "findings for this call are likely incomplete or empty",
            filing_label,
        )


def parse_json_array(raw_text: str) -> list:
    """
    Strip markdown code fences from an LLM response and parse it as a JSON array.

    Shared by llm_review and footnote_review: both prompt the model for a JSON
    array and must tolerate the model wrapping it in ```json fences. Returns []
    on any parse failure or non-list result, so a malformed LLM response degrades
    to "no findings" rather than crashing the pipeline.
    """
    raw_text = raw_text.strip()

    # Strip markdown code fences if the model wrapped the JSON.
    # e.g. ```json\n[...]\n``` → [...]
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text)
        raw_text = re.sub(r"\n?```$", "", raw_text)

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return []

    # Guard against the model returning a non-list value (e.g. a dict or a string).
    return parsed if isinstance(parsed, list) else []


@dataclass
class Finding:
    """
    One qualitative risk signal extracted from a filing.

    All four fields are required — findings without a concern label or
    evidence quote are rejected by _validate_finding().

    Attributes:
        concern:        Short qualitative label, e.g. "Going-concern language"
        severity:       "low" | "medium" | "high"
        evidence_quote: Verbatim excerpt from the filing (max ~200 chars)
        source:         e.g. "10-K 2023-12-31, MD&A"
        source_url:     Public SEC EDGAR URL of the filing document the quote
                        came from, so the UI can deep-link back to it. Pipeline-
                        supplied (never from the LLM); defaults to "" for older
                        findings persisted before this field existed.
    """
    concern: str
    severity: str
    evidence_quote: str
    source: str
    source_url: str = ""


def _validate_finding(raw: dict) -> Finding | None:
    """
    Parse and validate one raw finding dict from the LLM JSON response.

    Validation rules:
      1. concern and evidence_quote must both be non-empty strings.
      2. severity must be exactly "low", "medium", or "high" (case-insensitive).
      3. concern must contain NO digits — enforces the qualitative-only invariant.
         Example rejection: "revenue declined 15%" contains "15" → rejected.
         Example acceptance: "management expressed uncertainty about growth" → kept.

    Returns None (silently drops the finding) if any rule fails.
    This is intentional — we'd rather drop a bad finding than store garbage data.
    """
    # The model occasionally returns array elements that aren't objects at all
    # (e.g. bare strings); drop them rather than crashing the whole review.
    if not isinstance(raw, dict):
        return None

    # str() + strip: coerce non-string values and handle extra spaces, like the
    # footnote_review validators do.
    concern = str(raw.get("concern", "") or "").strip()
    severity = str(raw.get("severity", "") or "").strip().lower()
    evidence_quote = str(raw.get("evidence_quote", "") or "").strip()
    source = str(raw.get("source", "") or "").strip()

    # Rule 1: required fields must be present.
    if not concern or not evidence_quote:
        return None

    # Rule 2: severity must be one of three allowed values.
    if severity not in ("low", "medium", "high"):
        return None

    # Rule 3: no numbers in the concern label.
    # The LLM is instructed to avoid numbers, but we validate here as a hard guard.
    if _NUMBER_IN_CONCERN.search(concern):
        return None

    return Finding(
        concern=concern,
        severity=severity,
        evidence_quote=evidence_quote,
        source=source,
    )


def review_text(
    text: str,
    filing_label: str,
    client: anthropic.Anthropic | None = None,
    source_url: str = "",
) -> list[Finding]:
    """
    Send filing section text to Claude and return validated qualitative findings.

    Flow:
      1. Build the user prompt: filing label + the text (trimmed to MAX_REVIEW_CHARS).
      2. Call the Claude API with the system prompt and user message.
      3. Strip any markdown code fences from the response (some models add them).
      4. Parse the JSON array.
      5. Validate each finding individually; drop invalid ones.
      6. Drop findings whose evidence_quote does not appear in the input text.
      7. Return the surviving findings (may be empty).

    Args:
        text:          The LOCATED, tag-stripped MD&A section text from
                       sections.locate_sections — pass it untrimmed; the single
                       truncation to MAX_REVIEW_CHARS happens here.
        filing_label:  Human-readable label used as source context for findings,
                       e.g. "10-K 2023-12-31, MD&A".
        client:        Optional pre-created Anthropic client. If None, a new
                       client is created using the ANTHROPIC_API_KEY env var.
        source_url:    Public SEC EDGAR URL of the filing document, stamped onto
                       every returned finding for UI traceability. Optional.

    Returns:
        List of validated Finding objects. May be empty if:
          - The LLM found no concerns ("return [] if no concerns found").
          - The LLM response couldn't be parsed as JSON.
          - All findings failed validation or quote verification.
        All failures are swallowed — a broken LLM call never blocks ratio storage.
    """
    if client is None:
        # Creates a client from the ANTHROPIC_API_KEY environment variable.
        client = anthropic.Anthropic()

    excerpt = text[:MAX_REVIEW_CHARS]
    user_prompt = (
        f"Filing: {filing_label}\n\n"
        f"Text excerpt:\n{excerpt}\n\n"
        "Return your findings as a JSON array only — no other text."
    )

    message = client.messages.create(
        model=MODEL,
        # A finding with a max-length quote is ~120 output tokens, so this covers
        # well over 100 findings — far beyond any real filing. Generous on purpose:
        # max_tokens is a ceiling, not a spend (only generated tokens are billed),
        # while hitting the cap truncates the JSON mid-array and the whole call
        # degrades to zero findings. ~16k is also the non-streaming limit before
        # SDK HTTP timeouts become a concern.
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    warn_if_truncated(message, filing_label)

    # Extract the response text, strip any code fences, and parse the JSON array.
    # Returns [] on any malformed output rather than crashing the pipeline.
    raw_findings = parse_json_array(message.content[0].text)

    # Validate each raw dict individually. Invalid ones are None and filtered out.
    # Quote verification runs against the exact excerpt the model saw, so a
    # genuine quote can never fail because of our own truncation.
    findings = []
    for raw in raw_findings:
        validated = _validate_finding(raw)
        if validated is not None and quote_in_text(validated.evidence_quote, excerpt):
            # source_url is pipeline-supplied, not from the LLM — stamp it on
            # each surviving finding so the UI can deep-link back to the filing.
            validated.source_url = source_url
            findings.append(validated)

    return findings
