"""Tests for src/store.py — mocks the Supabase client; no live network."""

from unittest.mock import MagicMock, patch

import pytest

from src.extract import RatioResult, MissingRatio
import src.store as store


def make_result(name, value, period="2023-12-31"):
    return RatioResult(
        name=name,
        value=value,
        inputs={"a": 1.0, "b": 2.0},
        source_tags={"a": "us-gaap/TagA", "b": "us-gaap/TagB"},
        period_end=period,
    )


def make_missing(name, period="2023-12-31"):
    return MissingRatio(
        name=name,
        period_end=period,
        inputs={"a": 1.0},
        source_tags={"a": "us-gaap/TagA"},
        missing_inputs=[{"field": "b", "tags_tried": ["us-gaap/TagB"]}],
        reason="no data",
    )


def make_mock_client(data=None):
    """Build a MagicMock Supabase client with a chainable query builder."""
    qb = MagicMock()
    qb.execute.return_value = MagicMock(data=data or [])
    # Fluent chain: each builder method returns the same query builder
    qb.select.return_value = qb
    qb.eq.return_value = qb
    qb.order.return_value = qb
    qb.upsert.return_value = qb
    qb.delete.return_value = qb

    client = MagicMock()
    client.table.return_value = qb
    return client


def test_save_ratios_upserts_correct_rows():
    results = {"leverage": make_result("leverage", 3.5)}
    mock_client = make_mock_client()
    with patch("src.store._client", return_value=mock_client):
        store.save_ratios("AAPL", "2023-12-31", results)

    rows = mock_client.table.return_value.upsert.call_args[0][0]
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["period_end"] == "2023-12-31"
    assert rows[0]["ratio_name"] == "leverage"
    assert abs(rows[0]["value"] - 3.5) < 1e-9


def test_save_persists_missing_ratios():
    # Missing ratios now produce a row (value null) carrying which inputs are
    # missing, so the source-audit panel can show what's absent.
    results = {
        "leverage": make_result("leverage", 2.0),
        "interest_coverage": make_missing("interest_coverage"),
    }
    mock_client = make_mock_client()
    with patch("src.store._client", return_value=mock_client):
        store.save_ratios("AAPL", "2023-12-31", results)

    rows = mock_client.table.return_value.upsert.call_args[0][0]
    by_name = {r["ratio_name"]: r for r in rows}
    assert len(rows) == 2

    assert by_name["leverage"]["value"] == 2.0
    assert by_name["leverage"]["missing_json"] is None

    miss = by_name["interest_coverage"]
    assert miss["value"] is None
    assert miss["missing_json"]["missing_inputs"][0]["field"] == "b"
    assert miss["missing_json"]["reason"] == "no data"


def make_finding(concern, quote, severity="high", source="10-K 2023-12-31, MD&A", source_url=""):
    from types import SimpleNamespace
    return SimpleNamespace(
        concern=concern,
        severity=severity,
        evidence_quote=quote,
        source=source,
        source_url=source_url,
    )


def test_save_findings_upserts_with_conflict_target_and_source_url():
    # save_findings must (a) carry source_url into the row and (b) upsert with
    # on_conflict on the natural key so re-runs UPDATE (refreshing source_url)
    # rather than silently skipping the way ignore_duplicates=True did.
    findings = [make_finding("Going-concern doubt", "substantial doubt", source_url="http://x/10k")]
    mock_client = make_mock_client()
    with patch("src.store._client", return_value=mock_client):
        store.save_findings("AAPL", "2023-12-31", findings)

    upsert = mock_client.table.return_value.upsert
    rows = upsert.call_args[0][0]
    assert rows[0]["source_url"] == "http://x/10k"
    assert rows[0]["cik"] == "AAPL".zfill(10)  # cik zero-padded to width 10
    assert upsert.call_args[1]["on_conflict"] == "cik,period_end,concern,evidence_quote"
    assert "ignore_duplicates" not in upsert.call_args[1]


def test_get_issuers_deduplicates():
    raw = [{"ticker": "AAPL"}, {"ticker": "MSFT"}, {"ticker": "AAPL"}]
    mock_client = make_mock_client(data=raw)
    with patch("src.store._client", return_value=mock_client):
        issuers = store.get_issuers()

    assert set(issuers) == {"AAPL", "MSFT"}
    assert len(issuers) == 2


def test_get_periods_returns_sorted():
    raw = [{"period_end": "2023-12-31"}, {"period_end": "2021-12-31"}, {"period_end": "2022-12-31"}]
    mock_client = make_mock_client(data=raw)
    with patch("src.store._client", return_value=mock_client):
        periods = store.get_periods("AAPL")

    assert periods == ["2021-12-31", "2022-12-31", "2023-12-31"]
