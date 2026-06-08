"""
LLM qualitative review of 10-K filing text.

Why LLM review?
  XBRL data is structured and precise, but it only captures what companies
  explicitly tag. Qualitative risk signals — hedging language, covenant waiver
  mentions, going-concern hints — live in the prose of the MD&A section and
  footnotes. An LLM can read that prose and identify patterns a regex cannot.

Design constraints:
  - The LLM returns qualitative labels only — no numbers, no scores.
  - Numbers are the job of the deterministic XBRL extraction (extract.py).
  - This separation keeps the scoring model fully auditable: you can always
    trace a score back to specific XBRL tags and verbatim filing quotes.

Invariant: any finding whose concern field contains a digit is rejected.
  This enforces the qualitative-only rule post-generation. For example,
  "revenue declined 15%" would be rejected; "management expressed doubt
  about revenue growth" would be kept.

Contribution to the stress score:
  High-severity findings add up to 10 points to the score (see score.py).
  They cannot independently push an issuer past the 50-point stress threshold.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import anthropic

# claude-haiku is fast and cheap — appropriate for structured extraction tasks
# where we send a constrained prompt and expect JSON output.
MODEL = "claude-haiku-4-5-20251001"

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
    """
    concern: str
    severity: str
    evidence_quote: str
    source: str


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
    # Strip whitespace from all fields to handle extra spaces in LLM output.
    concern = raw.get("concern", "").strip()
    severity = raw.get("severity", "").strip().lower()
    evidence_quote = raw.get("evidence_quote", "").strip()
    source = raw.get("source", "").strip()

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
) -> list[Finding]:
    """
    Send a slice of filing text to Claude and return validated qualitative findings.

    Flow:
      1. Build the user prompt: filing label + first 8000 chars of the text.
      2. Call the Claude API with the system prompt and user message.
      3. Strip any markdown code fences from the response (some models add them).
      4. Parse the JSON array.
      5. Validate each finding individually; drop invalid ones.
      6. Return the surviving findings (may be empty).

    Args:
        text:          Filing text, typically the MD&A section. Callers should
                       pre-slice to ~12 000 chars; we trim again to 8000 here
                       to give the model room to think.
        filing_label:  Human-readable label used as source context for findings,
                       e.g. "10-K 2023-12-31".
        client:        Optional pre-created Anthropic client. If None, a new
                       client is created using the ANTHROPIC_API_KEY env var.

    Returns:
        List of validated Finding objects. May be empty if:
          - The LLM found no concerns ("return [] if no concerns found").
          - The LLM response couldn't be parsed as JSON.
          - All findings failed validation.
        All failures are swallowed — a broken LLM call never blocks ratio storage.
    """
    if client is None:
        # Creates a client from the ANTHROPIC_API_KEY environment variable.
        client = anthropic.Anthropic()

    user_prompt = (
        f"Filing: {filing_label}\n\n"
        f"Text excerpt:\n{text[:8000]}\n\n"   # second trim inside the 12 000-char slice
        "Return your findings as a JSON array only — no other text."
    )

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,  # enough for ~10 findings; LLM findings are short
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Extract the text content from the first content block in the response.
    raw_text = message.content[0].text.strip()

    # Strip markdown code fences if the model wrapped the JSON.
    # e.g. ```json\n[...]\n``` → [...]
    # The regex handles optional language specifiers like ```json or just ```.
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```[a-z]*\n?", "", raw_text)
        raw_text = re.sub(r"\n?```$", "", raw_text)

    # Attempt to parse the cleaned response as JSON.
    try:
        raw_findings = json.loads(raw_text)
    except json.JSONDecodeError:
        # The model returned non-JSON output. Return empty rather than crashing.
        return []

    # Guard against the model returning a non-list value (e.g. a dict or a string).
    if not isinstance(raw_findings, list):
        return []

    # Validate each raw dict individually. Invalid ones are None and filtered out.
    findings = []
    for raw in raw_findings:
        validated = _validate_finding(raw)
        if validated is not None:
            findings.append(validated)

    return findings
