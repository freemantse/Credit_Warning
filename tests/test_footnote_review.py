"""Tests for src/footnote_review.py — Anthropic SDK and EDGAR fetch are mocked."""

import json
from unittest.mock import MagicMock, patch

from src.footnote_review import review_filing
from tests.test_sections import (
    CONTINGENCIES_BODY,
    DEBT_BODY,
    MDNA_LIQUIDITY_SENTENCE,
    _build_filing,
)


def _mock_message(response_text: str) -> MagicMock:
    content = MagicMock()
    content.text = response_text
    message = MagicMock()
    message.content = [content]
    return message


FILING = {
    "form": "10-K",
    "filingDate": "2024-02-15",
    "reportDate": "2023-12-31",
    "accessionNumber": "0000000001-24-000001",
    "primaryDocument": "test-20231231.htm",
}

# One mocked response per LLM pass, in call order: MD&A → debt → contingencies.
# Every evidence_quote is a verbatim substring of its section's body so the
# quote-verification guard keeps the items.
MDNA_RESPONSE = json.dumps([
    {
        "concern": "going-concern language",
        "severity": "high",
        "evidence_quote": MDNA_LIQUIDITY_SENTENCE[:120],
        "source": "10-K 2023-12-31, MD&A",
    }
])
COVENANT_RESPONSE = json.dumps([
    {
        "covenant_type": "max_leverage",
        "threshold": 4.0,
        "direction": "max",
        "reported_actual": None,
        "near_limit": False,
        "evidence_quote": "maintain a maximum leverage covenant of 4.0x",
        "source": "10-K 2023-12-31, Debt",
    }
])
PROVISION_RESPONSE = json.dumps([
    {
        "matter": "patent litigation",
        "provision_amount": None,
        "is_material": True,
        "qualitative_flag": "reasonably possible loss, not accrued",
        "evidence_quote": "a loss is reasonably possible but not accrued",
        "source": "10-K 2023-12-31, Contingencies",
    }
])
SENIORITY_RESPONSE = json.dumps([
    {
        "instrument_name": "Senior Notes due 2027",
        "seniority": "senior_unsecured",
        "principal_amount": None,
        "coupon": None,
        "maturity_year": 2027,
        "evidence_quote": "Our senior notes bear interest at a fixed rate and mature in 2027",
        "source": "10-K 2023-12-31, Debt",
    }
])


def test_review_filing_runs_four_passes_on_one_fetch():
    client = MagicMock()
    # Call order: MD&A → debt(covenants) → debt(instruments) → contingencies.
    client.messages.create.side_effect = [
        _mock_message(MDNA_RESPONSE),
        _mock_message(COVENANT_RESPONSE),
        _mock_message(SENIORITY_RESPONSE),
        _mock_message(PROVISION_RESPONSE),
    ]
    with patch("src.ingest.get_filing_text", return_value=_build_filing()) as fetch:
        findings, covenants, provisions, instruments = review_filing(
            "0000000001", "2023-12-31", [FILING], client=client
        )

    assert fetch.call_count == 1
    assert client.messages.create.call_count == 4

    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert len(covenants) == 1
    assert covenants[0].covenant_type == "max_leverage"
    assert covenants[0].threshold == 4.0  # number appears in its quote → kept
    assert len(provisions) == 1
    assert provisions[0].is_material
    assert len(instruments) == 1
    assert instruments[0].seniority == "senior_unsecured"
    assert instruments[0].maturity_year == 2027  # appears in its quote → kept


def test_review_filing_sends_located_sections_not_raw_html():
    client = MagicMock()
    client.messages.create.side_effect = [
        _mock_message("[]"), _mock_message("[]"), _mock_message("[]"), _mock_message("[]"),
    ]
    with patch("src.ingest.get_filing_text", return_value=_build_filing()):
        review_filing("0000000001", "2023-12-31", [FILING], client=client)

    mdna_prompt = client.messages.create.call_args_list[0].kwargs["messages"][0]["content"]
    # The MD&A pass must see the located Item 7 prose, not the document head.
    assert "<html>" not in mdna_prompt and "<head>" not in mdna_prompt
    assert MDNA_LIQUIDITY_SENTENCE.split(",")[0] in mdna_prompt
    debt_prompt = client.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
    assert "maximum leverage covenant" in debt_prompt


def test_review_filing_drops_unverifiable_quotes():
    fabricated_covenant = json.dumps([
        {
            "covenant_type": "min_coverage",
            "threshold": 2.5,
            "direction": "min",
            "reported_actual": None,
            "near_limit": True,
            # Not present anywhere in the debt footnote → dropped entirely.
            "evidence_quote": "minimum coverage ratio of 2.5x under the indenture",
            "source": "10-K 2023-12-31, Debt",
        }
    ])
    client = MagicMock()
    client.messages.create.side_effect = [
        _mock_message("[]"),
        _mock_message(fabricated_covenant),
        _mock_message("[]"),
        _mock_message("[]"),
    ]
    with patch("src.ingest.get_filing_text", return_value=_build_filing()):
        _, covenants, _, _ = review_filing(
            "0000000001", "2023-12-31", [FILING], client=client
        )
    assert covenants == []


def test_review_filing_no_matching_filing_returns_empty():
    client = MagicMock()
    with patch("src.ingest.get_filing_text") as fetch:
        result = review_filing("0000000001", "1999-12-31", [FILING], client=client)
    assert result == ([], [], [], [])
    assert fetch.call_count == 0
    assert client.messages.create.call_count == 0


# --- boolean coercion: a string "false" must never count as True ---

def test_covenant_string_false_near_limit_is_false():
    from src.footnote_review import _validate_covenant

    raw = {
        "covenant_type": "max_leverage",
        "threshold": None,
        "direction": "max",
        "reported_actual": None,
        "near_limit": "false",  # string, not bool — bool("false") would be True
        "evidence_quote": "maintain a maximum leverage covenant",
        "source": "10-K 2023-12-31, Debt",
    }
    covenant = _validate_covenant(raw, "10-K 2023-12-31, Debt")
    assert covenant is not None
    assert covenant.near_limit is False


def test_covenant_string_true_near_limit_is_true():
    from src.footnote_review import _validate_covenant

    raw = {
        "covenant_type": "max_leverage",
        "threshold": None,
        "direction": "max",
        "reported_actual": None,
        "near_limit": "true",
        "evidence_quote": "maintain a maximum leverage covenant",
        "source": "10-K 2023-12-31, Debt",
    }
    covenant = _validate_covenant(raw, "10-K 2023-12-31, Debt")
    assert covenant is not None
    assert covenant.near_limit is True


def test_provision_string_false_is_material_is_false():
    from src.footnote_review import _validate_provision

    raw = {
        "matter": "patent litigation",
        "provision_amount": None,
        "is_material": "False",
        "qualitative_flag": "",
        "evidence_quote": "a loss is reasonably possible but not accrued",
        "source": "10-K 2023-12-31, Contingencies",
    }
    provision = _validate_provision(raw, "10-K 2023-12-31, Contingencies")
    assert provision is not None
    assert provision.is_material is False
