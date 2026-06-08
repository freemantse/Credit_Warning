"""
Supabase-backed time-series store for ratio results and LLM findings.

Why Supabase?
  The previous SQLite implementation didn't persist across Vercel serverless
  function invocations (each cold start gets a fresh /tmp). Supabase (hosted
  PostgreSQL) persists across deployments and is accessible from both the
  local dev environment and Vercel serverless functions.

Database schema (see supabase/schema.sql):
  ratios table:
    ticker          TEXT        — stock ticker, e.g. "AAPL"
    period_end      DATE        — fiscal year-end, e.g. "2023-09-30"
    ratio_name      TEXT        — e.g. "leverage", "free_cash_flow"
    value           FLOAT       — computed ratio value
    inputs_json     JSONB       — raw dollar inputs used in the formula
    source_tags_json JSONB      — winning XBRL tags per input
    PRIMARY KEY (ticker, period_end, ratio_name)

  llm_findings table:
    ticker          TEXT
    period_end      DATE
    concern         TEXT        — qualitative issue label
    severity        TEXT        — "low" | "medium" | "high"
    evidence_quote  TEXT        — verbatim quote from the filing
    source          TEXT        — e.g. "10-K 2023-12-31, MD&A"

Setup:
  Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env.local.
  Run supabase/schema.sql in the Supabase SQL editor to create the tables.
"""

from __future__ import annotations

import os
from typing import Any

from supabase import Client, create_client

from src.concepts import MissingDataError
from src.extract import RatioResult


# ── Supabase client factory ──────────────────────────────────────────────────

def _client() -> Client:
    """
    Create and return a Supabase client for one operation.

    Why not cache the client at module level?
      A module-level client would survive across multiple serverless invocations
      on Vercel, potentially holding a stale connection. Creating a fresh client
      per call is slightly slower but avoids connection-state issues in serverless.

    Why the service-role key (not the anon key)?
      The service-role key bypasses Supabase Row Level Security (RLS). Since
      this code only runs server-side (never in the browser), bypassing RLS is
      appropriate and avoids needing to configure RLS policies.

    Raises RuntimeError with setup instructions if credentials are missing,
    rather than a cryptic AttributeError or None-related crash later.
    """
    url = os.environ.get("SUPABASE_URL")
    # Accept either key name for backward compatibility with older .env.local files.
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set. "
            "Copy .env.local.example to .env.local and fill in your Supabase credentials."
        )
    return create_client(url, key)


# ── Write operations ─────────────────────────────────────────────────────────

def save_ratios(
    ticker: str,
    period_end: str,
    results: dict[str, RatioResult | MissingDataError],
    **_,   # accept and ignore extra kwargs for forward compatibility
) -> None:
    """
    Persist ratio results for one (ticker, period_end) to the ratios table.

    Only successful RatioResult objects are stored — MissingDataError entries
    are silently skipped because there is nothing meaningful to persist for a
    ratio that couldn't be computed.

    Upsert semantics (insert or update on conflict):
      If a row with the same (ticker, period_end, ratio_name) already exists,
      it is overwritten. This makes re-running track() for the same period safe.

    inputs_json and source_tags_json are stored as JSONB columns.
    Supabase automatically serialises the Python dicts to JSON on write
    and deserialises back to Python dicts on read.
    """
    # Build the list of rows to upsert, skipping any error entries.
    rows = [
        {
            "ticker": ticker,
            "period_end": period_end,
            "ratio_name": name,
            "value": result.value,
            "inputs_json": result.inputs,         # Python dict → JSONB
            "source_tags_json": result.source_tags, # Python dict → JSONB
        }
        for name, result in results.items()
        if isinstance(result, RatioResult)  # skip MissingDataError entries
    ]

    # Only make the DB call if there's something to store.
    if rows:
        _client().table("ratios").upsert(rows).execute()


def save_findings(
    ticker: str,
    period_end: str,
    findings: list[Any],
    **_,
) -> None:
    """
    Persist LLM qualitative findings for one (ticker, period_end).

    ignore_duplicates=True:
      If the exact same finding (all columns identical) already exists, the
      upsert silently ignores it rather than inserting a duplicate. This allows
      re-running the LLM review on the same period without multiplying findings.
      Two findings with the same concern but different evidence_quote are still
      kept as separate rows because they differ on at least one column.
    """
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
        # ignore_duplicates prevents findings from multiplying if the same period is re-ingested.
        _client().table("llm_findings").upsert(rows, ignore_duplicates=True).execute()


# ── Read operations ──────────────────────────────────────────────────────────

def get_issuers(**_) -> list[str]:
    """
    Return a sorted list of all tickers that have at least one stored ratio.

    Uses a SELECT with no WHERE clause — returns every distinct ticker.
    The set() deduplicates because the ratios table has one row per
    (ticker, period_end, ratio_name), so each ticker appears many times.
    """
    res = _client().table("ratios").select("ticker").execute()
    return sorted(set(r["ticker"] for r in res.data))


def get_periods(ticker: str, **_) -> list[str]:
    """
    Return all period_end dates stored for a ticker, sorted ascending.

    Ascending order is important:
      - The API (get_issuer in api/main.py) reverses it to newest-first for
        the frontend, which shows the most recent period at the top.
      - The backtest uses the ascending order to walk forward in time.

    set() deduplicates because the ratios table has one row per ratio name,
    so each period_end appears once per ratio (5 times if all ratios computed).
    """
    res = (
        _client()
        .table("ratios")
        .select("period_end")
        .eq("ticker", ticker)       # filter to this ticker only
        .order("period_end")        # ascending in DB; we deduplicate and re-sort below
        .execute()
    )
    # set() deduplicates, sorted() ensures ascending order is guaranteed.
    return sorted(set(r["period_end"] for r in res.data))


def get_full_ratios(ticker: str, period_end: str, **_) -> dict[str, dict]:
    """
    Return all ratio data for one (ticker, period_end), including audit info.

    The returned structure mirrors what the frontend expects:
      {
        "leverage": {
          "value": 3.2,
          "inputs": {"total_debt": 8e9, "cash": 2e9, ...},
          "source_tags": {"total_debt": "us-gaap/LongTermDebt", ...}
        },
        "free_cash_flow": { ... },
        ...
      }

    inputs_json and source_tags_json are JSONB columns in Supabase.
    Supabase returns them as Python dicts automatically — no json.loads() needed.
    """
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
            "inputs": row["inputs_json"],           # JSONB → already a Python dict
            "source_tags": row["source_tags_json"], # JSONB → already a Python dict
        }
        for row in res.data
    }


def get_all_ratios(ticker: str, period_end: str, **_) -> dict[str, float]:
    """
    Return a flat {ratio_name: value} dict for one (ticker, period_end).

    Convenience wrapper over get_full_ratios() for callers that only need the
    numeric values (e.g. quick CLI display) and don't need the full audit trail.
    """
    return {name: data["value"] for name, data in get_full_ratios(ticker, period_end).items()}


def get_findings(ticker: str, period_end: str, **_) -> list[dict]:
    """
    Return LLM findings for one (ticker, period_end) as plain dicts.

    Returns an empty list if:
      - no_llm=True was used during track (LLM review skipped), or
      - the LLM review ran but found no concerning signals.

    The API (api/main.py) passes these dicts to _finding_objects() which
    converts them back into Finding dataclass instances for compute_score().
    """
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
    """
    Hard-delete all stored data for a ticker from both tables.

    Called when the user clicks "Remove" in the portfolio dashboard.
    Both tables must be cleared so the issuer no longer appears anywhere.
    """
    client = _client()
    client.table("ratios").delete().eq("ticker", ticker).execute()
    client.table("llm_findings").delete().eq("ticker", ticker).execute()
