"""Tests for src/llm_review.py — Anthropic SDK is mocked."""

import json
import pytest
from unittest.mock import MagicMock, patch

from src.llm_review import review_text, Finding, _validate_finding


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


# --- review_text integration tests with mocked Anthropic client ---

def _make_mock_client(response_text: str) -> MagicMock:
    mock_content = MagicMock()
    mock_content.text = response_text
    mock_message = MagicMock()
    mock_message.content = [mock_content]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message
    return mock_client


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
    findings = review_text("some filing text", "10-K 2023-12-31", client=client)
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
    findings = review_text("text", "10-K 2023-12-31", client=client)
    assert len(findings) == 1
    assert findings[0].concern == "going concern language"


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
    findings = review_text("text", "10-K 2023-12-31", client=client)
    assert len(findings) == 1
