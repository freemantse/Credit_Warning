"""Tests for src/llm_review.py — Anthropic SDK is mocked."""

import json
import pytest
from unittest.mock import MagicMock, patch

from src.llm_review import review_text, Finding, _validate_finding, quote_in_text


# --- _validate_finding unit tests (no mocking needed) ---

def test_valid_finding():
    raw = {
        "concern": "covenant proximity warning",
        "severity": "high",
        "evidence_quote": "we may be close to violating our leverage covenant",
        "source": "10-K 2023-12-31, Note 8",
    }
    finding = _validate_finding(raw)
    assert isinstance(finding, Finding)
    assert finding.severity == "high"


def test_finding_with_number_in_concern_rejected():
    raw = {
        "concern": "leverage at 4.5x",  # contains number — must be rejected
        "severity": "high",
        "evidence_quote": "our leverage ratio is 4.5 times",
        "source": "10-K 2023-12-31",
    }
    assert _validate_finding(raw) is None


def test_finding_missing_evidence_quote_rejected():
    raw = {
        "concern": "going concern risk",
        "severity": "medium",
        "evidence_quote": "",  # blank
        "source": "10-K 2023-12-31",
    }
    assert _validate_finding(raw) is None


def test_finding_invalid_severity_rejected():
    raw = {
        "concern": "litigation risk",
        "severity": "critical",  # not low/medium/high
        "evidence_quote": "we face significant litigation exposure",
        "source": "10-K 2023-12-31",
    }
    assert _validate_finding(raw) is None


# --- quote_in_text unit tests (no mocking needed) ---

def test_quote_in_text_exact_match():
    source = "Management believes there is substantial doubt about our ability to continue."
    assert quote_in_text("substantial doubt about our ability", source)


def test_quote_in_text_normalizes_typography_and_whitespace():
    # Curly quotes / em-dash / collapsed whitespace in the source must still match
    # an ASCII-straight quote from the LLM, and vice versa.
    source = "the Company’s lenders — subject to  certain\nconditions — waived the covenant"
    assert quote_in_text("the company's lenders - subject to certain conditions", source)


def test_quote_in_text_tolerates_truncated_tail():
    source = (
        "We may be unable to refinance our senior notes on acceptable terms, "
        "which could materially affect our liquidity position going forward."
    )
    # Long quote whose tail was cut/ellipsised by the model: prefix still verifies.
    truncated = (
        "We may be unable to refinance our senior notes on acceptable terms, "
        "which could materially aff…"
    )
    assert quote_in_text(truncated, source)


def test_quote_in_text_rejects_fabricated_quote():
    source = "Revenues grew across all segments and liquidity remains strong."
    assert not quote_in_text("substantial doubt about going concern", source)


def test_quote_in_text_rejects_empty_quote():
    assert not quote_in_text("", "any source text")


# --- review_text integration tests with mocked Anthropic client ---

def _make_mock_client(response_text: str) -> MagicMock:
    mock_content = MagicMock()
    mock_content.text = response_text
    mock_message = MagicMock()
    mock_message.content = [mock_content]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message
    return mock_client


# Source text that backs the quotes used in the mocked LLM responses below —
# review_text now verifies every evidence_quote against its input text.
_SOURCE_TEXT = (
    "In our MD&A we note that we are approaching our maximum leverage covenant. "
    "Further, we cannot guarantee we will achieve our targets. Our auditors state "
    "there is substantial doubt about our ability to continue. Leverage is 5 times. "
    "Separately, we face material litigation risk."
)


def test_review_text_returns_findings():
    response = json.dumps([
        {
            "concern": "covenant proximity warning",
            "severity": "high",
            "evidence_quote": "we are approaching our maximum leverage covenant",
            "source": "10-K 2023-12-31, Note 8",
        },
        {
            "concern": "management uncertainty in guidance",
            "severity": "medium",
            "evidence_quote": "we cannot guarantee we will achieve our targets",
            "source": "10-K 2023-12-31, MD&A",
        },
    ])
    client = _make_mock_client(response)
    findings = review_text(_SOURCE_TEXT, "10-K 2023-12-31", client=client)
    assert len(findings) == 2
    assert all(isinstance(f, Finding) for f in findings)
    assert findings[0].severity == "high"


def test_review_text_filters_invalid_findings():
    response = json.dumps([
        {
            "concern": "leverage at 5x exceeds limit",  # has number → rejected
            "severity": "high",
            "evidence_quote": "leverage is 5 times",
            "source": "10-K 2023-12-31",
        },
        {
            "concern": "going concern language",
            "severity": "high",
            "evidence_quote": "there is substantial doubt about our ability to continue",
            "source": "10-K 2023-12-31",
        },
    ])
    client = _make_mock_client(response)
    findings = review_text(_SOURCE_TEXT, "10-K 2023-12-31", client=client)
    assert len(findings) == 1
    assert findings[0].concern == "going concern language"


def test_review_text_drops_finding_with_unverifiable_quote():
    response = json.dumps([
        {
            "concern": "going concern language",
            "severity": "high",
            # Plausible-sounding but NOT present in the source text → dropped.
            "evidence_quote": "our lenders have declared an event of default",
            "source": "10-K 2023-12-31",
        },
    ])
    client = _make_mock_client(response)
    findings = review_text(_SOURCE_TEXT, "10-K 2023-12-31", client=client)
    assert findings == []


def test_review_text_handles_json_parse_error():
    client = _make_mock_client("Sorry, I cannot help with that.")
    findings = review_text("text", "10-K 2023-12-31", client=client)
    assert findings == []


def test_review_text_handles_empty_array():
    client = _make_mock_client("[]")
    findings = review_text("text", "10-K 2023-12-31", client=client)
    assert findings == []


def test_review_text_handles_markdown_fences():
    response = "```json\n" + json.dumps([
        {
            "concern": "litigation exposure",
            "severity": "medium",
            "evidence_quote": "we face material litigation risk",
            "source": "10-K 2023-12-31",
        }
    ]) + "\n```"
    client = _make_mock_client(response)
    findings = review_text(_SOURCE_TEXT, "10-K 2023-12-31", client=client)
    assert len(findings) == 1
