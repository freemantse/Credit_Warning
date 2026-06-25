"""Tests for src/footnote_review.py — Anthropic SDK and EDGAR fetch are mocked.

review_filing now runs five LLM passes (qualitative MD&A, covenant Stage A∪B,
covenant breach/waiver [2c-iii], contingencies, going-concern) and returns a
5-tuple ending in the orphan breach/waiver findings. Because the number and order
of LLM calls now varies with which sections locate, the mock is CONTENT-AWARE:
it dispatches on each pass's distinctive system prompt rather than on a fixed
call-order list, so adding/removing a pass no longer breaks these tests.
"""

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

# Every evidence_quote is a verbatim substring of its section body so the
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


# Distinctive opening phrases of each pass's SYSTEM prompt. These are mutually
# exclusive, so the dispatcher can tell the passes apart even though the
# qualitative SYSTEM_PROMPT itself mentions "covenant" and "going-concern".
_QUALITATIVE = "assistant reviewing SEC filing text"
_COVENANT = "extracting financial covenants"
_PROVISION = "COMMITMENTS AND CONTINGENCIES"
_GOING_CONCERN = "ability to continue operating"
_BREACH = "CURRENTLY in breach"


def _make_client(*, mdna=MDNA_RESPONSE, covenant=COVENANT_RESPONSE,
                 provision=PROVISION_RESPONSE, breach="[]", going_concern="[]"):
    """A MagicMock client whose create() dispatches by pass (system prompt)."""
    client = MagicMock()

    def _respond(**kwargs):
        system = kwargs.get("system", "") or ""
        user = kwargs["messages"][0]["content"]
        if _COVENANT in system:
            # Stage A (debt, PRECISE) returns the covenant; Stage B (RECALL) []
            # so the same covenant isn't double-extracted from MD&A/risk factors.
            return _mock_message(covenant if "PASS: PRECISE" in user else "[]")
        if _PROVISION in system:
            return _mock_message(provision)
        if _GOING_CONCERN in system:
            return _mock_message(going_concern)
        if _BREACH in system:
            return _mock_message(breach)
        return _mock_message(mdna)  # qualitative MD&A review (the fallback)

    client.messages.create.side_effect = _respond
    return client


def test_review_filing_extracts_each_pass_on_one_fetch():
    client = _make_client()
    with patch("src.ingest.get_filing_text", return_value=_build_filing()) as fetch:
        findings, covenants, provisions, going_concern, orphans = review_filing(
            "0000000001", "2023-12-31", [FILING], client=client
        )

    assert fetch.call_count == 1  # the filing is fetched exactly once

    assert len(findings) == 1
    assert findings[0].severity == "high"
    assert len(covenants) == 1
    assert covenants[0].covenant_type == "max_leverage"
    assert covenants[0].threshold == 4.0  # number appears in its quote → kept
    # No numeric basis (actual None) and no breach finding → not near_limit.
    assert covenants[0].near_limit is False
    assert covenants[0].near_limit_reason is None
    assert len(provisions) == 1
    assert provisions[0].is_material
    assert going_concern == []
    assert orphans == []


def test_review_filing_sends_located_sections_not_raw_html():
    client = _make_client(mdna="[]", covenant="[]", provision="[]")
    with patch("src.ingest.get_filing_text", return_value=_build_filing()):
        review_filing("0000000001", "2023-12-31", [FILING], client=client)

    calls = client.messages.create.call_args_list
    # The MD&A qualitative pass must see the located Item 7 prose, not the head.
    mdna_prompt = next(
        c.kwargs["messages"][0]["content"] for c in calls
        if _QUALITATIVE in (c.kwargs.get("system") or "")
    )
    assert "<html>" not in mdna_prompt and "<head>" not in mdna_prompt
    assert MDNA_LIQUIDITY_SENTENCE.split(",")[0] in mdna_prompt
    # The Stage-A covenant pass must see the debt footnote.
    debt_prompt = next(
        c.kwargs["messages"][0]["content"] for c in calls
        if "PASS: PRECISE" in c.kwargs["messages"][0]["content"]
    )
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
    client = _make_client(covenant=fabricated_covenant)
    with patch("src.ingest.get_filing_text", return_value=_build_filing()):
        _, covenants, _, _, _ = review_filing(
            "0000000001", "2023-12-31", [FILING], client=client
        )
    assert covenants == []


def test_review_filing_no_matching_filing_returns_empty():
    client = MagicMock()
    with patch("src.ingest.get_filing_text") as fetch:
        result = review_filing("0000000001", "1999-12-31", [FILING], client=client)
    assert result == ([], [], [], [], [])  # 5-tuple, all empty
    assert fetch.call_count == 0
    assert client.messages.create.call_count == 0


# --- near_limit contract (2c-i: DERIVED in code; the LLM's value is ignored) ---

def test_llm_supplied_near_limit_is_ignored_without_numeric_basis():
    """near_limit is code-derived; the model's near_limit (string OR bool) is
    ignored. With no threshold/actual, cushion is uncomputable → near_limit False,
    near_limit_reason None — regardless of what the LLM claimed."""
    from src.footnote_review import _validate_covenant

    for llm_val in ("true", "false", True, False):
        raw = {
            "covenant_type": "max_leverage",
            "threshold": None,
            "direction": "max",
            "reported_actual": None,
            "near_limit": llm_val,
            "evidence_quote": "maintain a maximum leverage covenant",
            "source": "10-K 2023-12-31, Debt",
        }
        covenant = _validate_covenant(raw, "10-K 2023-12-31, Debt")
        assert covenant is not None
        assert covenant.near_limit is False, f"llm_val={llm_val!r}"
        assert covenant.near_limit_reason is None


def test_near_limit_reason_stamped_on_numeric_paths():
    """The numeric path records WHY near_limit fired: 'breach' (cushion<0) or
    'cushion' (<=10% headroom); None when there is ample headroom."""
    from src.footnote_review import _validate_covenant

    # Breach: max covenant, actual 5.0 exceeds limit 4.0 → cushion -1.0 < 0.
    breach = _validate_covenant({
        "covenant_type": "max_leverage", "threshold": 4.0, "direction": "max",
        "reported_actual": 5.0,
        "evidence_quote": "leverage ratio not to exceed 4.0; actual was 5.0",
        "source": "10-K, Debt",
    }, "10-K, Debt")
    assert breach.near_limit is True and breach.near_limit_reason == "breach"

    # Thin cushion: actual 3.8 vs limit 4.0 → 5% headroom (<=10%).
    thin = _validate_covenant({
        "covenant_type": "max_leverage", "threshold": 4.0, "direction": "max",
        "reported_actual": 3.8,
        "evidence_quote": "leverage ratio not to exceed 4.0; actual was 3.8",
        "source": "10-K, Debt",
    }, "10-K, Debt")
    assert thin.near_limit is True and thin.near_limit_reason == "cushion"

    # Ample headroom: actual 2.0 vs limit 4.0 → 50% cushion → not near_limit.
    healthy = _validate_covenant({
        "covenant_type": "max_leverage", "threshold": 4.0, "direction": "max",
        "reported_actual": 2.0,
        "evidence_quote": "leverage ratio not to exceed 4.0; actual was 2.0",
        "source": "10-K, Debt",
    }, "10-K, Debt")
    assert healthy.near_limit is False and healthy.near_limit_reason is None


# --- 2c-iii breach → covenant mapping (offline; no LLM) ---

def _cov(ct, ratio_name=None, direction="max", is_maintenance=True):
    from src.footnote_review import Covenant
    return Covenant(
        covenant_type=ct, threshold=None, direction=direction, reported_actual=None,
        near_limit=False, evidence_quote=f"the {ratio_name or ct} covenant",
        source="Debt", ratio_name=ratio_name, is_maintenance=is_maintenance,
    )


def _breach(ref, quote="the Company was not in compliance and obtained a waiver",
            section="Debt footnote"):
    from src.footnote_review import CovenantBreach
    return CovenantBreach(
        breach_or_waiver=True, status="waiver_obtained", covenant_reference=ref,
        evidence_quote=quote, description="x", section=section,
        section_confidence="high", source="Debt",
    )


def test_breach_flags_one_covenant_with_evidence_not_all():
    """A disclosed breach maps to ONE covenant (by type), stamping near_limit +
    reason + verbatim quote + section. It must NOT fan out to other covenants."""
    from src.footnote_review import _apply_breach_findings

    covs = [_cov("max_leverage", "consolidated leverage ratio"),
            _cov("min_coverage", "interest coverage ratio")]
    orphans = _apply_breach_findings(covs, [_breach("maximum leverage ratio")])

    lev = covs[0]
    assert lev.near_limit is True
    assert lev.near_limit_reason == "waiver/breach disclosed"
    assert lev.near_limit_evidence_quote  # the verbatim breach sentence is stored
    assert lev.near_limit_section == "Debt footnote"
    assert covs[1].near_limit is False     # the coverage covenant is untouched
    assert orphans == []


def test_orphan_breach_is_surfaced_not_fabricated_not_dropped():
    """A breach naming a covenant with no extracted row is returned as an orphan —
    no covenant is fabricated and none is wrongly flagged."""
    from src.footnote_review import _apply_breach_findings

    covs = [_cov("max_leverage", "leverage ratio")]
    orphans = _apply_breach_findings(covs, [_breach("minimum liquidity covenant")])

    assert covs[0].near_limit is False     # wrong-type covenant not flagged
    assert len(orphans) == 1               # the breach is surfaced, not lost
    assert orphans[0].covenant_reference == "minimum liquidity covenant"


def test_no_breach_findings_leaves_covenants_untouched():
    from src.footnote_review import _apply_breach_findings

    covs = [_cov("max_leverage", "leverage ratio"), _cov("min_liquidity", "min liquidity")]
    orphans = _apply_breach_findings(covs, [])
    assert all(c.near_limit is False for c in covs)
    assert orphans == []


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
