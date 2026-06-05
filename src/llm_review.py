"""
LLM qualitative review of filing text.

The LLM returns ONLY findings with evidence — never numbers, never scores.
Invariant: any finding whose concern field contains a digit is rejected.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import anthropic

MODEL = "claude-haiku-4-5-20251001"

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

_NUMBER_IN_CONCERN = re.compile(r"\d")


@dataclass
class Finding:
    concern: str
    severity: str
    evidence_quote: str
    source: str


def _validate_finding(raw: dict) -> Finding | None:
    """Parse and validate one raw finding dict. Returns None if invalid."""
    concern = raw.get("concern", "").strip()
    severity = raw.get("severity", "").strip().lower()
    evidence_quote = raw.get("evidence_quote", "").strip()
    source = raw.get("source", "").strip()

    if not concern or not evidence_quote:
        return None
    if severity not in ("low", "medium", "high"):
        return None
    # Invariant: concern must not contain numbers
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
    Send filing text to the LLM and return validated qualitative findings.

    Args:
        text: MD&A or footnote text from the filing.
        filing_label: e.g. "10-K 2023-12-31" — used as source label context.
        client: optional Anthropic client (uses default env API key if None).

    Returns:
        List of validated Finding objects. Empty list if none found or LLM fails.
    """
    if client is None:
        client = anthropic.Anthropic()

    user_prompt = (
        f"Filing: {filing_label}\n\n"
        f"Text excerpt:\n{text[:8000]}\n\n"  # trim to stay within token budget
        "Return your findings as a JSON array only — no other text."
    )

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw_text = message.content[0].text.strip()

    # Strip markdown code fences if present
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```[a-z]*\n?", "", raw_text)
        raw_text = re.sub(r"\n?```$", "", raw_text)

    try:
        raw_findings = json.loads(raw_text)
    except json.JSONDecodeError:
        return []

    if not isinstance(raw_findings, list):
        return []

    findings = []
    for raw in raw_findings:
        validated = _validate_finding(raw)
        if validated is not None:
            findings.append(validated)

    return findings
