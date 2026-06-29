"""
Supabase-backed time-series store for ratio results and LLM findings.

Why Supabase?
  The previous SQLite implementation didn't persist across Vercel serverless
  function invocations (each cold start gets a fresh /tmp). Supabase (hosted
  PostgreSQL) persists across deployments and is accessible from both the
  local dev environment and Vercel serverless functions.

Identity model (see supabase/schema.sql):
  Every row is keyed on the company's CIK — SEC's permanent identifier — not
  its ticker. Tickers and names change over time; the CIK never does. The
  human-facing ticker/name live on the `companies` table as refreshable
  attributes and are joined back in for display.

Database schema:
  companies table:
    cik          TEXT  — zero-padded 10-digit CIK (primary key)
    name         TEXT  — current legal/display name
    tickers      JSONB — current ticker symbol(s)
    exchanges    JSONB — exchanges the tickers trade on
    former_names JSONB — [{name, from, to}] prior names

  ratios table:
    cik             TEXT        — canonical company key
    period_end      DATE        — fiscal year-end, e.g. "2023-09-30"
    ratio_name      TEXT        — e.g. "leverage", "free_cash_flow"
    value           FLOAT       — computed ratio value
    inputs_json     JSONB       — raw dollar inputs used in the formula
    source_tags_json JSONB      — winning XBRL tags per input
    PRIMARY KEY (cik, period_end, ratio_name)

  llm_findings table:
    cik             TEXT
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

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from supabase import Client, create_client

from src.extract import RatioResult, MissingRatio


# ── Supabase client factory ──────────────────────────────────────────────────

# Module-level cached client. Reused across all calls within a warm process.
_cached_client: Client | None = None


def _client() -> Client:
    """
    Return a cached Supabase client, creating it on first use.

    Why cache at module level?
      Building a client per call cost ~1.1s of TLS/connection setup *per query* —
      with the N+1 read pattern that dominated request latency (a detail page
      made ~39 queries). supabase-py talks to the REST API over a pooled httpx
      session; there is no long-lived Postgres connection to go stale, so reusing
      the client is safe and reuses the underlying HTTP connection pool.

    Why the service-role key (not the anon key)?
      The service-role key bypasses Supabase Row Level Security (RLS). Since
      this code only runs server-side (never in the browser), bypassing RLS is
      appropriate and avoids needing to configure RLS policies.

    Raises RuntimeError with setup instructions if credentials are missing,
    rather than a cryptic AttributeError or None-related crash later.
    """
    global _cached_client
    if _cached_client is not None:
        return _cached_client

    # Load .env.local (gitignored) so CLI entry points — scripts.seed_cases, the
    # backtest, track — pick up Supabase creds without each calling load_dotenv.
    # Mirrors the explicit load in api/main.py and is idempotent: load_dotenv does
    # NOT override env vars already set (e.g. Vercel's platform env), and silently
    # no-ops when the file is absent.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env.local")

    url = os.environ.get("SUPABASE_URL")
    # Accept either key name for backward compatibility with older .env.local files.
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set. "
            "Copy .env.local.example to .env.local and fill in your Supabase credentials."
        )
    _cached_client = create_client(url, key)
    return _cached_client


def _fetch_all(build_query, *, page_size: int = 1000) -> list[dict[str, Any]]:
    """
    Return EVERY row matched by a PostgREST select, paging past the 1000-row cap.

    PostgREST silently truncates every response to 1000 rows unless you request
    explicit ranges. An unfiltered portfolio-wide read of a table that has grown
    past 1000 rows therefore drops data with NO error — e.g. `ratios` holds
    ~9 ratios × ~15 periods per issuer, so a handful of issuers already exceeds
    the cap and whole companies silently vanish from get_ratios_grouped(). We
    page in fixed windows until a short page signals the end.

    `build_query` must be a zero-arg callable returning a FRESH, *ordered* query
    builder. Ordering is required: without a stable sort, successive .range()
    windows can skip or repeat rows. The builder is rebuilt per page because a
    PostgREST builder is single-use once executed.
    """
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        batch = build_query().range(start, start + page_size - 1).execute().data
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        start += page_size


# ── Company identity ─────────────────────────────────────────────────────────

def save_company(info: dict[str, Any], **_) -> None:
    """
    Upsert a company's identity snapshot into the companies table.

    `info` is the dict returned by src.ingest.get_company_info — it must contain
    the canonical `cik` plus the mutable display attributes (name, tickers,
    exchanges, formerNames). The CIK is the conflict target, so re-tracking a
    company refreshes its name/ticker rather than inserting a duplicate.
    """
    cik = info["cik"].zfill(10)
    row = {
        "cik": cik,
        "name": info.get("name", ""),
        "tickers": info.get("tickers", []),
        "exchanges": info.get("exchanges", []),
        "former_names": info.get("formerNames", []),
    }
    _client().table("companies").upsert(row).execute()


def get_company(cik: str, **_) -> dict[str, Any] | None:
    """
    Return the stored identity row for a CIK, or None if not tracked.

    Used by the API to attach a human-facing ticker/name to responses that are
    otherwise keyed purely on CIK.
    """
    cik = cik.zfill(10)
    res = _client().table("companies").select("*").eq("cik", cik).limit(1).execute()
    return res.data[0] if res.data else None


def get_cik_by_ticker(ticker: str, **_) -> str | None:
    """
    Resolve a ticker to a CIK using only the local companies table (no EDGAR call).

    Lets read endpoints accept a friendly ticker in the URL without depending on
    SEC EDGAR for every page load. Returns None if no tracked company currently
    carries that ticker — the caller can then fall back to an EDGAR lookup.

    Matching is case-insensitive: tickers are stored uppercase (the API
    uppercases before saving), so we uppercase the query to match.
    """
    res = (
        _client()
        .table("companies")
        .select("cik")
        # JSONB containment: companies whose `tickers` array includes this symbol.
        # The value must be a JSON *string* (e.g. '["AAPL"]'), not a Python list —
        # passing a list makes postgrest emit a PostgreSQL array literal ({AAPL}),
        # which Postgres rejects as invalid JSON for a JSONB column.
        .contains("tickers", json.dumps([ticker.upper()]))
        .limit(1)
        .execute()
    )
    return res.data[0]["cik"] if res.data else None


def _companies_map(ciks: list[str]) -> dict[str, dict]:
    """
    Fetch identity rows for a set of CIKs and return them keyed by CIK.

    Batched into one `in_` query so list_issuers doesn't issue a round-trip per
    company. CIKs absent from the companies table simply won't appear in the map.
    """
    if not ciks:
        return {}
    res = _client().table("companies").select("*").in_("cik", ciks).execute()
    return {row["cik"]: row for row in res.data}


# ── Case library (backtest roster) ───────────────────────────────────────────
# The `cases` table is the editable roster the point-in-time backtest evaluates
# (migrated from data/cases.csv). list_cases returns CSV-compatible dicts so the
# backtest and /api/backtest/cases stay unchanged.

# The canonical case columns, in the order the CSV used.
_CASE_COLUMNS = ("case_id", "company_name", "ticker", "cik", "label", "event_date", "notes")


def _case_row(row: dict[str, Any]) -> dict[str, Any]:
    """
    Shape one DB row into the CSV-compatible dict (every value a string, never
    None) that csv.DictReader produced — the invariant load_cases relies on.
    """
    return {col: (row.get(col) or "") for col in _CASE_COLUMNS}


def list_cases(**_) -> list[dict[str, Any]]:
    """
    Return every backtest case as a CSV-compatible dict (keys: case_id,
    company_name, ticker, cik, label, event_date, notes — all strings).

    Sorted by label then case_id so the order is stable across runs.
    """
    res = (
        _client()
        .table("cases")
        .select(",".join(_CASE_COLUMNS))
        .order("label")
        .order("case_id")
        .execute()
    )
    return [_case_row(row) for row in res.data]


def get_case(case_id: str, **_) -> dict[str, Any] | None:
    """Return one case row (CSV-shaped) or None — used to 409 on a duplicate slug."""
    res = (
        _client()
        .table("cases")
        .select(",".join(_CASE_COLUMNS))
        .eq("case_id", case_id)
        .limit(1)
        .execute()
    )
    return _case_row(res.data[0]) if res.data else None


def add_case(row: dict[str, Any], **_) -> dict[str, Any]:
    """
    Insert (upsert on case_id) one case row; returns it in CSV-compatible shape.

    Upsert-on-case_id keeps the seed script and re-adds idempotent. The caller
    (API) resolves the CIK/name and generates the case_id; cik is zero-padded
    here defensively.
    """
    record = {col: (row.get(col) or "") for col in _CASE_COLUMNS}
    record["cik"] = record["cik"].zfill(10)
    _client().table("cases").upsert(record).execute()
    return record


def delete_case(case_id: str, **_) -> bool:
    """
    Hard-delete one case by case_id. Returns True if a row existed (so the API
    can 404 an unknown case_id). Checks existence first to stay correct
    regardless of whether the client returns the deleted rows.
    """
    existed = get_case(case_id) is not None
    _client().table("cases").delete().eq("case_id", case_id).execute()
    return existed


# ── Scoring config ───────────────────────────────────────────────────────────
# A single active stress-score parameter set (one row, id='active'). Absent row
# → src.score.DEFAULT_CONFIG (reproduces the original hard-coded behavior).

def get_score_config(**_) -> dict[str, Any]:
    """Return the active score-config dict, or DEFAULT_CONFIG when no row exists."""
    res = (
        _client()
        .table("score_config")
        .select("config")
        .eq("id", "active")
        .limit(1)
        .execute()
    )
    if res.data and res.data[0].get("config"):
        return res.data[0]["config"]
    from src.score import DEFAULT_CONFIG  # lazy import avoids any import-order coupling
    return DEFAULT_CONFIG


def save_score_config(cfg: dict[str, Any], **_) -> None:
    """Upsert the single active score-config row with `cfg`."""
    from datetime import datetime, timezone
    _client().table("score_config").upsert({
        "id": "active",
        "config": cfg,
        # DEFAULT NOW() only fires on insert, so set updated_at explicitly so it
        # refreshes on every apply.
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


# ── Write operations ─────────────────────────────────────────────────────────

def _ratio_rows(cik: str, period_end: str, results: dict[str, RatioResult | MissingRatio]) -> list[dict]:
    """
    Build the list of upsert rows for one (cik, period_end).

    Both computed (RatioResult) and missing (MissingRatio) ratios produce a row:
      - RatioResult → value set, missing_json null.
      - MissingRatio → value null, inputs/source_tags hold whichever inputs DID
        resolve, and missing_json records which inputs are missing + the reason so
        the source-audit panel can explain exactly what's absent.
    """
    rows: list[dict] = []
    for name, result in results.items():
        if isinstance(result, RatioResult):
            rows.append({
                "cik": cik,
                "period_end": period_end,
                "ratio_name": name,
                "value": result.value,
                "inputs_json": result.inputs,           # Python dict → JSONB
                "source_tags_json": result.source_tags,  # Python dict → JSONB
                "missing_json": None,
            })
        elif isinstance(result, MissingRatio):
            rows.append({
                "cik": cik,
                "period_end": period_end,
                "ratio_name": name,
                "value": None,
                "inputs_json": result.inputs,            # the subset that resolved
                "source_tags_json": result.source_tags,
                "missing_json": {
                    "missing_inputs": result.missing_inputs,
                    "reason": result.reason,
                },
            })
    return rows


def save_ratios(
    cik: str,
    period_end: str,
    results: dict[str, RatioResult | MissingRatio],
    **_,   # accept and ignore extra kwargs for forward compatibility
) -> None:
    """
    Persist ratio results for one (cik, period_end) to the ratios table.

    Upsert semantics (insert or update on conflict):
      If a row with the same (cik, period_end, ratio_name) already exists,
      it is overwritten. This makes re-running track() for the same period safe.

    inputs_json and source_tags_json are stored as JSONB columns.
    Supabase automatically serialises the Python dicts to JSON on write
    and deserialises back to Python dicts on read.

    Note: to persist many periods at once, prefer save_ratios_bulk — it does a
    single round-trip instead of one per period.
    """
    rows = _ratio_rows(cik.zfill(10), period_end, results)
    # Only make the DB call if there's something to store.
    if rows:
        _client().table("ratios").upsert(rows).execute()


def save_ratios_bulk(
    cik: str,
    results_by_period: dict[str, dict[str, RatioResult | MissingRatio]],
    **_,
) -> None:
    """
    Persist ratio results for MANY periods in a single upsert round-trip.

    `results_by_period` maps period_end → the results dict from extract_all().
    Tracking an issuer produces ~18 periods × ~5 ratios; writing them one period
    at a time was ~15 round-trips (the dominant cost of a track). Flattening every
    period's rows into one upsert turns that into a single request.

    Same upsert semantics as save_ratios (overwrite on PK conflict).
    """
    cik = cik.zfill(10)
    rows = [
        row
        for period_end, results in results_by_period.items()
        for row in _ratio_rows(cik, period_end, results)
    ]
    if rows:
        _client().table("ratios").upsert(rows).execute()


def save_findings(
    cik: str,
    period_end: str,
    findings: list[Any],
    **_,
) -> None:
    """
    Persist LLM qualitative findings for one (cik, period_end).

    on_conflict on the natural key (cik, period_end, concern, evidence_quote):
      Re-running the LLM review on the same period does NOT multiply findings —
      a row matching the unique key is UPDATED in place rather than inserted
      again. Crucially this refreshes the mutable columns (severity, source,
      source_url) on the existing row, so a re-run backfills/updates source_url
      instead of silently skipping it (the old ignore_duplicates=True behaviour
      left stale source_url='' rows untouched). Two findings with the same
      concern but different evidence_quote remain separate rows.
    """
    cik = cik.zfill(10)
    rows = [
        {
            "cik": cik,
            "period_end": period_end,
            "concern": f.concern,
            "severity": f.severity,
            "evidence_quote": f.evidence_quote,
            "source": f.source,
            # getattr default keeps this resilient to finding-like objects that
            # predate the source_url field.
            "source_url": getattr(f, "source_url", "") or "",
        }
        for f in findings
    ]
    if rows:
        # on_conflict matches UNIQUE (cik, period_end, concern, evidence_quote);
        # ignore_duplicates defaults to False, so a conflict does an UPDATE
        # (merge), keeping source_url/severity/source fresh on re-runs.
        _client().table("llm_findings").upsert(
            rows, on_conflict="cik,period_end,concern,evidence_quote"
        ).execute()


def save_maturities_bulk(
    cik: str,
    schedules_by_period: dict[str, Any],
    **_,
) -> None:
    """
    Persist debt maturity schedules for MANY periods in one upsert round-trip.

    `schedules_by_period` maps period_end → MaturitySchedule. One row is written
    per resolved bucket (schedules with no buckets contribute nothing). Same
    overwrite-on-PK-conflict semantics as save_ratios_bulk.
    """
    cik = cik.zfill(10)
    rows = [
        {
            "cik": cik,
            "period_end": period_end,
            "bucket": bucket,
            "value": value,
            "source_tag": schedule.source_tags.get(bucket, ""),
        }
        for period_end, schedule in schedules_by_period.items()
        for bucket, value in schedule.buckets.items()
    ]
    if rows:
        _client().table("debt_maturities").upsert(rows).execute()


def save_covenants(
    cik: str,
    period_end: str,
    covenants: list[Any],
    **_,
) -> None:
    """
    Persist LLM-extracted covenants for one (cik, period_end).

    ignore_duplicates=True (like save_findings): re-running the footnote review on
    the same period won't multiply rows, since the UNIQUE constraint covers
    (cik, period_end, covenant_type, evidence_quote).
    """
    cik = cik.zfill(10)
    rows = [
        {
            "cik": cik,
            "period_end": period_end,
            "covenant_type": c.covenant_type,
            "threshold": c.threshold,
            "direction": c.direction,
            "reported_actual": c.reported_actual,
            "near_limit": c.near_limit,
            "evidence_quote": c.evidence_quote,
            "source": c.source,
            # ── Stage 2c-i additive columns (default-tolerant via getattr so
            # older Covenant objects without these fields still persist) ──
            "covenant_subtype": getattr(c, "covenant_subtype", None),
            "ratio_name": getattr(c, "ratio_name", None),
            "unit": getattr(c, "unit", None),
            "testing_frequency": getattr(c, "testing_frequency", None),
            "is_springing": getattr(c, "is_springing", None),
            "springing_trigger": getattr(c, "springing_trigger", None),
            "step_down": getattr(c, "step_down", None),
            "is_maintenance": getattr(c, "is_maintenance", None),
            "cushion": getattr(c, "cushion", None),
            "cushion_pct": getattr(c, "cushion_pct", None),
            "section_confidence": getattr(c, "section_confidence", None),
            "null_reason": getattr(c, "null_reason", None),
            # ── Stage 2c-iii: why near_limit is set + the breach/waiver evidence
            # (verbatim quote + section). Data foundation for the on-demand
            # evidence button. getattr-tolerant for older Covenant objects. ──
            "near_limit_reason": getattr(c, "near_limit_reason", None),
            "near_limit_evidence_quote": getattr(c, "near_limit_evidence_quote", None),
            "near_limit_section": getattr(c, "near_limit_section", None),
        }
        for c in covenants
    ]
    if rows:
        _client().table("covenants").upsert(rows, ignore_duplicates=True).execute()


def save_loss_provisions(
    cik: str,
    period_end: str,
    provisions: list[Any],
    **_,
) -> None:
    """
    Persist LLM-extracted loss provisions for one (cik, period_end).

    Same ignore_duplicates semantics as save_covenants; UNIQUE constraint covers
    (cik, period_end, matter, evidence_quote).
    """
    cik = cik.zfill(10)
    rows = [
        {
            "cik": cik,
            "period_end": period_end,
            "matter": p.matter,
            "provision_amount": p.provision_amount,
            "is_material": p.is_material,
            "qualitative_flag": p.qualitative_flag,
            "evidence_quote": p.evidence_quote,
            "source": p.source,
        }
        for p in provisions
    ]
    if rows:
        _client().table("loss_provisions").upsert(rows, ignore_duplicates=True).execute()


def save_going_concern(
    cik: str,
    period_end: str,
    findings: list[Any],
    **_,
) -> None:
    """
    Persist LLM-extracted going-concern findings for one (cik, period_end).

    Same ignore_duplicates semantics as save_covenants; the UNIQUE constraint
    covers (cik, period_end, tier, evidence_quote). adverse_conditions is written
    as a list → JSONB. `null_reason` is intentionally NOT persisted (no column in
    the going_concern schema — it is carried on the dataclass for audit only).
    """
    cik = cik.zfill(10)
    rows = [
        {
            "cik": cik,
            "period_end": period_end,
            "tier": g.tier,
            "confidence": g.confidence,
            "status": g.status,
            "going_concern_flag": g.going_concern_flag,
            "source_party": g.source_party,
            "doubt_alleviated": g.doubt_alleviated,
            "adverse_conditions": g.adverse_conditions,
            "description": g.description,
            "evidence_quote": g.evidence_quote,
            "section": g.section,
            "section_confidence": g.section_confidence,
            "source": g.source,
        }
        for g in findings
    ]
    if rows:
        _client().table("going_concern").upsert(rows, ignore_duplicates=True).execute()


# ── Read operations ──────────────────────────────────────────────────────────

def get_issuers(**_) -> list[dict[str, Any]]:
    """
    Return one identity row per tracked company (all rows in the companies table).

    Each row is {cik, name, ticker} where `ticker` is the first current ticker
    (or "" if unknown). Querying companies directly — rather than deriving CIKs
    from the ratios table — ensures issuers whose ratio extraction failed (e.g.
    banks with non-standard XBRL) are still visible in the portfolio list.
    """
    res = _client().table("companies").select("cik, name, tickers").execute()
    issuers = []
    for row in sorted(res.data, key=lambda r: r["cik"]):
        tickers = row.get("tickers") or []
        issuers.append({
            "cik": row["cik"],
            "name": row.get("name", ""),
            "ticker": tickers[0] if tickers else "",
        })
    return issuers


def get_periods(cik: str, **_) -> list[str]:
    """
    Return all period_end dates stored for a CIK, sorted ascending.

    Ascending order is important:
      - The API (get_issuer in api/main.py) reverses it to newest-first for
        the frontend, which shows the most recent period at the top.
      - The backtest uses the ascending order to walk forward in time.

    set() deduplicates because the ratios table has one row per ratio name,
    so each period_end appears once per ratio (5 times if all ratios computed).
    """
    cik = cik.zfill(10)
    res = (
        _client()
        .table("ratios")
        .select("period_end")
        .eq("cik", cik)             # filter to this company only
        .order("period_end")        # ascending in DB; we deduplicate and re-sort below
        .execute()
    )
    # set() deduplicates, sorted() ensures ascending order is guaranteed.
    return sorted(set(r["period_end"] for r in res.data))


def _ratio_data_from_row(row: dict) -> dict:
    """
    Shape one ratios-table row into the {value, inputs, source_tags, ...} dict the
    API/frontend expect. For a missing ratio (value null), unpack missing_json into
    `missing_inputs` and `reason` so the source-audit panel can show what's absent.
    """
    data = {
        "value": row["value"],                   # None for a missing ratio
        "inputs": row["inputs_json"],            # JSONB → already a Python dict
        "source_tags": row["source_tags_json"],  # JSONB → already a Python dict
    }
    missing = row.get("missing_json")
    if missing:
        data["missing_inputs"] = missing.get("missing_inputs", [])
        data["reason"] = missing.get("reason", "")
    return data


def get_full_ratios(cik: str, period_end: str, **_) -> dict[str, dict]:
    """
    Return all ratio data for one (cik, period_end), including audit info.

    The returned structure mirrors what the frontend expects:
      {
        "leverage": {
          "value": 3.2,
          "inputs": {"total_debt": 8e9, "cash": 2e9, ...},
          "source_tags": {"total_debt": "us-gaap/LongTermDebt", ...}
        },
        # a missing ratio additionally carries value=None plus:
        #   "missing_inputs": [{"field": "total_debt", "tags_tried": [...]}],
        #   "reason": "..."
        ...
      }

    inputs_json and source_tags_json are JSONB columns in Supabase.
    Supabase returns them as Python dicts automatically — no json.loads() needed.
    """
    cik = cik.zfill(10)
    res = (
        _client()
        .table("ratios")
        .select("ratio_name, value, inputs_json, source_tags_json, missing_json")
        .eq("cik", cik)
        .eq("period_end", period_end)
        .execute()
    )
    return {row["ratio_name"]: _ratio_data_from_row(row) for row in res.data}


def get_all_ratios(cik: str, period_end: str, **_) -> dict[str, float]:
    """
    Return a flat {ratio_name: value} dict for one (cik, period_end).

    Convenience wrapper over get_full_ratios() for callers that only need the
    numeric values (e.g. quick CLI display) and don't need the full audit trail.
    """
    return {name: data["value"] for name, data in get_full_ratios(cik, period_end).items()}


def get_findings(cik: str, period_end: str, **_) -> list[dict]:
    """
    Return LLM findings for one (cik, period_end) as plain dicts.

    Returns an empty list if:
      - no_llm=True was used during track (LLM review skipped), or
      - the LLM review ran but found no concerning signals.

    The API (api/main.py) passes these dicts to _finding_objects() which
    converts them back into Finding dataclass instances for compute_score().
    """
    cik = cik.zfill(10)
    res = (
        _client()
        .table("llm_findings")
        .select("concern, severity, evidence_quote, source, source_url")
        .eq("cik", cik)
        .eq("period_end", period_end)
        .execute()
    )
    return list(res.data)


def get_ratios_grouped(cik: str | None = None, **_) -> dict[str, dict[str, dict[str, dict]]]:
    """
    Fetch ratios in ONE query and group them as cik → period_end → ratio_name → data.

    This replaces the per-period get_full_ratios() loop that caused an N+1 query
    storm (a detail page issued ~18×2 round-trips). Pass a cik to scope to one
    company (detail page); omit it to fetch every issuer at once (portfolio list).

    `data` is {value, inputs, source_tags} — the same shape get_full_ratios returns.
    """
    def build():
        q = (
            _client()
            .table("ratios")
            .select("cik, period_end, ratio_name, value, inputs_json, source_tags_json, missing_json")
            .order("cik").order("period_end").order("ratio_name")  # stable order for paging
        )
        return q.eq("cik", cik.zfill(10)) if cik is not None else q

    out: dict[str, dict[str, dict[str, dict]]] = {}
    for row in _fetch_all(build):
        out.setdefault(row["cik"], {}).setdefault(row["period_end"], {})[row["ratio_name"]] = (
            _ratio_data_from_row(row)
        )
    return out


def get_findings_grouped(cik: str | None = None, **_) -> dict[str, dict[str, list[dict]]]:
    """
    Fetch findings in ONE query and group them as cik → period_end → [findings].

    Companion to get_ratios_grouped — lets a request read all of a company's
    findings (or every company's) without a per-period round-trip. The findings
    table is usually empty (LLM review is off by default), so this is one cheap
    query rather than 18.
    """
    def build():
        q = (
            _client()
            .table("llm_findings")
            .select("cik, period_end, concern, severity, evidence_quote, source, source_url")
            .order("id")  # stable order for paging (BIGSERIAL primary key)
        )
        return q.eq("cik", cik.zfill(10)) if cik is not None else q

    out: dict[str, dict[str, list[dict]]] = {}
    for row in _fetch_all(build):
        out.setdefault(row["cik"], {}).setdefault(row["period_end"], []).append({
            "concern": row["concern"],
            "severity": row["severity"],
            "evidence_quote": row["evidence_quote"],
            "source": row["source"],
            "source_url": row.get("source_url", "") or "",
        })
    return out


def get_maturities_grouped(cik: str | None = None, **_) -> dict[str, dict[str, dict]]:
    """
    Fetch maturity rows in ONE query, grouped as cik → period_end → schedule dict.

    Each schedule dict is {buckets, source_tags, total_scheduled, near_term_pct,
    wall_year} — the derived metrics are recomputed here from the stored buckets
    so the read shape matches extract.MaturitySchedule without storing redundant
    summary rows.
    """
    def build():
        q = (
            _client()
            .table("debt_maturities")
            .select("cik, period_end, bucket, value, source_tag")
            .order("cik").order("period_end").order("bucket")  # stable order for paging
        )
        return q.eq("cik", cik.zfill(10)) if cik is not None else q

    # First gather raw buckets per (cik, period_end).
    raw: dict[str, dict[str, dict]] = {}
    for row in _fetch_all(build):
        period = raw.setdefault(row["cik"], {}).setdefault(
            row["period_end"], {"buckets": {}, "source_tags": {}}
        )
        period["buckets"][row["bucket"]] = row["value"]
        period["source_tags"][row["bucket"]] = row["source_tag"]

    # Then derive the same metrics MaturitySchedule computes.
    out: dict[str, dict[str, dict]] = {}
    for c, periods in raw.items():
        for period_end, data in periods.items():
            buckets = data["buckets"]
            total = sum(buckets.values())
            near_term = buckets.get("y1", 0.0) + buckets.get("y2", 0.0)
            out.setdefault(c, {})[period_end] = {
                "buckets": buckets,
                "source_tags": data["source_tags"],
                "total_scheduled": total,
                "near_term_pct": (near_term / total) if total else None,
                "wall_year": max(buckets, key=buckets.get) if buckets else None,
            }
    return out


def get_covenants_grouped(cik: str | None = None, **_) -> dict[str, dict[str, list[dict]]]:
    """Fetch covenants in ONE query, grouped as cik → period_end → [covenants]."""
    def build():
        q = (
            _client()
            .table("covenants")
            .select(
                "cik, period_end, covenant_type, threshold, direction, "
                "reported_actual, near_limit, evidence_quote, source"
            )
            .order("id")  # stable order for paging (BIGSERIAL primary key)
        )
        return q.eq("cik", cik.zfill(10)) if cik is not None else q

    out: dict[str, dict[str, list[dict]]] = {}
    for row in _fetch_all(build):
        out.setdefault(row["cik"], {}).setdefault(row["period_end"], []).append({
            "covenant_type": row["covenant_type"],
            "threshold": row["threshold"],
            "direction": row["direction"],
            "reported_actual": row["reported_actual"],
            "near_limit": row["near_limit"],
            "evidence_quote": row["evidence_quote"],
            "source": row["source"],
        })
    return out


def get_loss_provisions_grouped(cik: str | None = None, **_) -> dict[str, dict[str, list[dict]]]:
    """Fetch loss provisions in ONE query, grouped as cik → period_end → [provisions]."""
    def build():
        q = (
            _client()
            .table("loss_provisions")
            .select(
                "cik, period_end, matter, provision_amount, is_material, "
                "qualitative_flag, evidence_quote, source"
            )
            .order("id")  # stable order for paging (BIGSERIAL primary key)
        )
        return q.eq("cik", cik.zfill(10)) if cik is not None else q

    out: dict[str, dict[str, list[dict]]] = {}
    for row in _fetch_all(build):
        out.setdefault(row["cik"], {}).setdefault(row["period_end"], []).append({
            "matter": row["matter"],
            "provision_amount": row["provision_amount"],
            "is_material": row["is_material"],
            "qualitative_flag": row["qualitative_flag"],
            "evidence_quote": row["evidence_quote"],
            "source": row["source"],
        })
    return out


def delete_issuer(cik: str, **_) -> None:
    """
    Hard-delete all stored data for a company from all three tables.

    Called when the user clicks "Remove" in the portfolio dashboard.
    All per-company rows (ratios, findings, maturities, covenants, loss
    provisions) and the companies identity row are cleared so the issuer no
    longer appears anywhere.
    """
    cik = cik.zfill(10)
    client = _client()
    client.table("ratios").delete().eq("cik", cik).execute()
    client.table("llm_findings").delete().eq("cik", cik).execute()
    client.table("debt_maturities").delete().eq("cik", cik).execute()
    client.table("covenants").delete().eq("cik", cik).execute()
    client.table("loss_provisions").delete().eq("cik", cik).execute()
    resp = client.table("companies").delete().eq("cik", cik).execute()
    if not resp.data:
        raise ValueError(
            f"No company row deleted for CIK {cik} — "
            "verify SUPABASE_SERVICE_ROLE_KEY is set (anon key cannot DELETE with RLS enabled)"
        )


# ── LLM job queue (llm_jobs) ─────────────────────────────────────────────────
# Durable work queue drained by an off-Vercel worker (src/worker.py). All access
# goes through the shared _client() (service-role key, bypasses RLS), matching
# every other write here. PostgREST cannot express UPDATE ... WHERE id =
# (SELECT ... LIMIT 1), so claim_job uses an optimistic guarded UPDATE instead
# (see claim_job). Timestamps are written from Python as ISO-8601 UTC strings,
# since PostgREST does not evaluate now() inside a sent value.

def _utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string for TIMESTAMPTZ columns."""
    return datetime.now(timezone.utc).isoformat()


def insert_job(cik: str, period_end: str, part: str = "full", **_) -> dict[str, Any]:
    """
    Insert a new pending job and return the row. Assumes the caller has already
    handled dedupe (see get_job); a UNIQUE (cik, period_end, part) violation here
    means a concurrent insert raced us — the caller should re-read with get_job.
    """
    cik = cik.zfill(10)
    resp = (
        _client()
        .table("llm_jobs")
        .insert({"cik": cik, "period_end": period_end, "part": part})
        .execute()
    )
    return resp.data[0]


def get_job(
    job_id: int | None = None,
    *,
    cik: str | None = None,
    period_end: str | None = None,
    part: str = "full",
    **_,
) -> dict[str, Any] | None:
    """
    Fetch one job by id, or by its natural key (cik, period_end, part). Returns
    the row dict or None. Used by GET /api/jobs and by the POST dedupe path.
    """
    q = _client().table("llm_jobs").select("*")
    if job_id is not None:
        q = q.eq("id", job_id)
    else:
        if cik is None or period_end is None:
            raise ValueError("get_job needs either job_id or (cik, period_end)")
        q = q.eq("cik", cik.zfill(10)).eq("period_end", period_end).eq("part", part)
    res = q.limit(1).execute()
    return res.data[0] if res.data else None


def requeue_job(job_id: int, **_) -> dict[str, Any] | None:
    """
    Reset a job to pending (used to re-queue a previously 'failed' job from the
    POST endpoint). Clears error and the run timestamps; leaves attempts as-is so
    the failure history is visible. Returns the updated row.
    """
    res = (
        _client()
        .table("llm_jobs")
        .update({"status": "pending", "error": None, "started_at": None, "finished_at": None})
        .eq("id", job_id)
        .execute()
    )
    return res.data[0] if res.data else None


def claim_job(**_) -> dict[str, Any] | None:
    """
    Atomically claim the oldest pending job (B1 — optimistic guarded update, no DB
    function). Returns the claimed row (now status='running'), or None if the
    queue is empty.

    Two-step, safe under concurrency:
      1. SELECT the oldest pending id (ORDER BY requested_at).
      2. UPDATE ... SET status='running', started_at=now()
         WHERE id=<that id> AND status='pending'      ← the guard
    Postgres row-locking serializes step 2 across workers: only the winner's
    UPDATE matches (status still 'pending') and returns a row; a loser matches 0
    rows and we retry the select. Bounded retry loop avoids spinning forever if
    the queue drains under us.
    """
    client = _client()
    for _attempt in range(10):
        sel = (
            client.table("llm_jobs")
            .select("id")
            .eq("status", "pending")
            .order("requested_at")
            .limit(1)
            .execute()
        )
        if not sel.data:
            return None  # queue empty
        job_id = sel.data[0]["id"]
        upd = (
            client.table("llm_jobs")
            .update({"status": "running", "started_at": _utcnow_iso()})
            .eq("id", job_id)
            .eq("status", "pending")          # guard: only flip if still pending
            .execute()
        )
        if upd.data:
            return upd.data[0]                # we won the row
        # else another worker claimed it between our SELECT and UPDATE — retry
    return None


def mark_done(job_id: int, **_) -> None:
    """Mark a job complete: status='done', finished_at=now(), error cleared."""
    (
        _client()
        .table("llm_jobs")
        .update({"status": "done", "finished_at": _utcnow_iso(), "error": None})
        .eq("id", job_id)
        .execute()
    )


def mark_failed(job_id: int, error: str, attempts: int, max_attempts: int = 3, **_) -> None:
    """
    Record a failed run. `attempts` is the NEW count (already incremented by the
    caller). If attempts >= max_attempts the job is terminal ('failed' with the
    error and finished_at); otherwise it is re-queued ('pending') so the worker
    retries it later. The error is stored either way for visibility.
    """
    row: dict[str, Any] = {"attempts": attempts, "error": error[:2000]}
    if attempts >= max_attempts:
        row["status"] = "failed"
        row["finished_at"] = _utcnow_iso()
    else:
        row["status"] = "pending"            # re-queue for another attempt
        row["started_at"] = None
    _client().table("llm_jobs").update(row).eq("id", job_id).execute()


def reset_stuck(minutes: int = 30, **_) -> int:
    """
    Re-queue jobs stuck in 'running' longer than `minutes` (a worker died mid-run,
    or was killed). Returns how many were reset. The threshold must exceed a
    legitimate full run on the low rate tier (which can exceed 15 min), so it
    defaults to 30 — high enough not to falsely reset a still-running job.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    res = (
        _client()
        .table("llm_jobs")
        .update({"status": "pending", "started_at": None})
        .eq("status", "running")
        .lt("started_at", cutoff)
        .execute()
    )
    return len(res.data or [])
