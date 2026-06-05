"""
Supabase-backed time-series store for ratio results and LLM findings.

Replaces the previous SQLite implementation. Data is stored in Supabase
(PostgreSQL) for persistence across deployments and serverless functions.

Required env vars: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
Run supabase/schema.sql in the Supabase SQL editor to create the tables.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
from supabase import Client, create_client

from src.concepts import MissingDataError
from src.extract import RatioResult


def _client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set. "
            "Copy .env.local.example to .env.local and fill in your Supabase credentials."
        )
    return create_client(url, key)


def save_ratios(
    ticker: str,
    period_end: str,
    results: dict[str, RatioResult | MissingDataError],
    **_,
) -> None:
    """Upsert ratio results for (ticker, period_end). Skips MissingDataError entries."""
    rows = [
        {
            "ticker": ticker,
            "period_end": period_end,
            "ratio_name": name,
            "value": result.value,
            "inputs_json": result.inputs,
            "source_tags_json": result.source_tags,
        }
        for name, result in results.items()
        if isinstance(result, RatioResult)
    ]
    if rows:
        _client().table("ratios").upsert(rows).execute()


def save_findings(
    ticker: str,
    period_end: str,
    findings: list[Any],
    **_,
) -> None:
    """Upsert LLM findings for (ticker, period_end). Ignores exact duplicates."""
    rows = [
        {
            "ticker": ticker,
            "period_end": period_end,
            "concern": f.concern,
            "severity": f.severity,
            "evidence_quote": f.evidence_quote,
            "source": f.source,
        }
        for f in findings
    ]
    if rows:
        _client().table("llm_findings").upsert(rows, ignore_duplicates=True).execute()


def get_issuers(**_) -> list[str]:
    """Return all tracked tickers, sorted."""
    res = _client().table("ratios").select("ticker").execute()
    return sorted(set(r["ticker"] for r in res.data))


def get_periods(ticker: str, **_) -> list[str]:
    """Return all period_end dates for a ticker, sorted ascending."""
    res = (
        _client()
        .table("ratios")
        .select("period_end")
        .eq("ticker", ticker)
        .order("period_end")
        .execute()
    )
    return sorted(set(r["period_end"] for r in res.data))


def get_full_ratios(ticker: str, period_end: str, **_) -> dict[str, dict]:
    """Return full ratio data including inputs and source_tags for a period."""
    res = (
        _client()
        .table("ratios")
        .select("ratio_name, value, inputs_json, source_tags_json")
        .eq("ticker", ticker)
        .eq("period_end", period_end)
        .execute()
    )
    return {
        row["ratio_name"]: {
            "value": row["value"],
            "inputs": row["inputs_json"],        # JSONB → already a dict
            "source_tags": row["source_tags_json"],
        }
        for row in res.data
    }


def get_all_ratios(ticker: str, period_end: str, **_) -> dict[str, float]:
    """Return {ratio_name: value} for a specific (ticker, period_end)."""
    return {name: data["value"] for name, data in get_full_ratios(ticker, period_end).items()}


def get_findings(ticker: str, period_end: str, **_) -> list[dict]:
    """Return LLM findings for (ticker, period_end)."""
    res = (
        _client()
        .table("llm_findings")
        .select("concern, severity, evidence_quote, source")
        .eq("ticker", ticker)
        .eq("period_end", period_end)
        .execute()
    )
    return list(res.data)


def delete_issuer(ticker: str, **_) -> None:
    """Remove all stored data for a ticker."""
    client = _client()
    client.table("ratios").delete().eq("ticker", ticker).execute()
    client.table("llm_findings").delete().eq("ticker", ticker).execute()


def get_ratio_history(ticker: str, ratio_name: str, **_) -> pd.DataFrame:
    """Return time-sorted DataFrame of a ratio's history for a ticker."""
    res = (
        _client()
        .table("ratios")
        .select("period_end, value, inputs_json, source_tags_json")
        .eq("ticker", ticker)
        .eq("ratio_name", ratio_name)
        .order("period_end")
        .execute()
    )
    return pd.DataFrame(res.data)
