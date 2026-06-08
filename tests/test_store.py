"""Tests for src/store.py — mocks the Supabase client; no live network."""

from unittest.mock import MagicMock, patch

import pytest

from src.concepts import MissingDataError
from src.extract import RatioResult
import src.store as store


def make_result(name, value, period="2023-12-31"):
    return RatioResult(
        name=name,
        value=value,
        inputs={"a": 1.0, "b": 2.0},
        source_tags={"a": "us-gaap/TagA", "b": "us-gaap/TagB"},
        period_end=period,
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


def test_save_skips_missing_errors():
    results = {
        "leverage": make_result("leverage", 2.0),
        "interest_coverage": MissingDataError("no data"),
    }
    mock_client = make_mock_client()
    with patch("src.store._client", return_value=mock_client):
        store.save_ratios("AAPL", "2023-12-31", results)

    rows = mock_client.table.return_value.upsert.call_args[0][0]
    assert len(rows) == 1
    assert rows[0]["ratio_name"] == "leverage"


def test_save_skips_upsert_when_all_missing():
    results = {"leverage": MissingDataError("no data")}
    mock_client = make_mock_client()
    with patch("src.store._client", return_value=mock_client):
        store.save_ratios("AAPL", "2023-12-31", results)

    # No valid rows → upsert must NOT be called
    mock_client.table.return_value.upsert.assert_not_called()


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
