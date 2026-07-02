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


def _scope_ciks(q, cik: str | None, ciks: list[str] | None):
    """
    Apply a CIK filter to a grouped-read query builder.

    The grouped reads serve three callers with very different scope:
      • detail page  — one issuer        → `cik` set, `.eq`
      • portfolio    — the watchlist set → `ciks` set, `.in_`
      • cron/pipeline— everything        → neither, unfiltered

    `cik` and `ciks` are mutually exclusive; `cik` wins if both are passed. The
    `ciks` path is what keeps the dashboard from full-scanning a table that holds
    every issuer tracked solely to TRAIN the model — without it, get_ratios_grouped()
    alone pages through ~95k rows on one connection and the Supabase edge drops it
    mid-stream (RemoteProtocolError). Callers must avoid an empty `ciks` list (it
    would build `cik=in.()`); list_issuers short-circuits before reaching here.
    """
    if cik is not None:
        return q.eq("cik", cik.zfill(10))
    if ciks is not None:
        return q.in_("cik", [c.zfill(10) for c in ciks])
    return q


# ── Company identity ─────────────────────────────────────────────────────────

def save_company(info: dict[str, Any], **_) -> None:
    """
    Upsert a company's identity snapshot into the companies table.

    `info` is the dict returned by src.ingest.get_company_info — it must contain
    the canonical `cik` plus the mutable display attributes (name, tickers,
    exchanges, formerNames, sic, sic_description). The CIK is the conflict target,
    so re-tracking a company refreshes its name/ticker rather than inserting a
    duplicate.
    """
    cik = info["cik"].zfill(10)
    row = {
        "cik": cik,
        "name": info.get("name", ""),
        "tickers": info.get("tickers", []),
        "exchanges": info.get("exchanges", []),
        "former_names": info.get("formerNames", []),
        "sic": info.get("sic", ""),
        "sic_description": info.get("sic_description", ""),
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


# ── Case library (backtest roster) ───────────────────────────────────────────
# The `cases` table is the editable roster the point-in-time backtest evaluates
# (migrated from data/cases.csv). list_cases returns CSV-compatible dicts so the
# backtest and /api/backtest/cases stay unchanged.

# The canonical case columns, in the order the CSV used.
_CASE_COLUMNS = ("case_id", "company_name", "ticker", "cik", "label", "event_type", "agency", "event_date", "notes")


def _case_row(row: dict[str, Any]) -> dict[str, Any]:
    """
    Shape one DB row into the CSV-compatible dict (every value a string, never
    None) that csv.DictReader produced — the invariant load_cases relies on.
    """
    return {col: (row.get(col) or "") for col in _CASE_COLUMNS}


# Display/storage order for the roster: by credit-event severity the backtest reads
# top-down — distress/default, then downgrade, then upgrade, then control (matches
# scripts.rebuild_cases). This is the single source of order for the
# /api/backtest/cases roster, the backtest run, and the CSV export, so all three stay
# consistent (and any unknown event_type sorts last, deterministically).
_EVENT_ORDER = {"default": 0, "downgrade": 1, "upgrade": 2, "control": 3}


def list_cases(**_) -> list[dict[str, Any]]:
    """
    Return every backtest case as a CSV-compatible dict (all-string values).

    Ordered distress/default → downgrade → upgrade → control, then by event_date and
    ticker — stable across runs and shared by the UI roster, the backtest, and the
    CSV export.
    """
    res = _client().table("cases").select(",".join(_CASE_COLUMNS)).execute()
    rows = [_case_row(row) for row in res.data]
    rows.sort(key=lambda r: (
        _EVENT_ORDER.get((r.get("event_type") or "").strip(), 9),
        r.get("event_date") or "",
        r.get("ticker") or "",
    ))
    return rows


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
    # event_type has a CHECK constraint (4 values), so never send "" — derive it
    # from the label (healthy→control, else default) when the caller omitted it.
    if not record.get("event_type"):
        record["event_type"] = "control" if record.get("label") == "healthy" else "default"
    # agency is nullable; "" → NULL so the column stays clean for non-rating events.
    record["agency"] = record.get("agency") or None
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


def export_cases_to_csv(path: str | None = None) -> int:
    """Mirror the live `cases` table → data/cases.csv (DB → CSV export).

    The reverse of scripts.seed_cases (CSV → DB). Supabase is the source of truth;
    this keeps the committed CSV in sync after roster edits. Writes every case in
    list_cases() order (label, case_id — stable) using the canonical CSV schema.
    Returns the number of rows written.
    """
    import csv
    import pathlib

    dest = (pathlib.Path(path) if path
            else pathlib.Path(__file__).resolve().parent.parent / "data" / "cases.csv")
    rows = list_cases()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(_CASE_COLUMNS))
        w.writeheader()
        w.writerows(rows)
    return len(rows)


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
                    "not_applicable": result.not_applicable,
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


def save_implied_ratings_bulk(
    cik: str,
    results_by_period: dict[str, Any],
    **_,
) -> None:
    """
    Persist implied credit ratings for MANY periods in one upsert round-trip.

    `results_by_period` maps period_end → ImpliedRatingResult (see
    src.rating.compute_implied_ratings_series). One row per period; same
    overwrite-on-PK-conflict semantics as save_ratios_bulk, so re-tracking an
    issuer refreshes its ratings rather than duplicating them. Periods whose
    rating couldn't be computed (None) are simply absent from the dict.
    """
    cik = cik.zfill(10)
    rows = [
        {
            "cik": cik,
            "period_end": period_end,
            "implied_rating": r.implied_rating,
            "rating_index": r.rating_index,
            "financial_risk_profile": r.financial_risk_profile,
            "financial_risk_index": r.financial_risk_index,
            "business_risk_index": r.business_risk_index,
            "subscores_json": r.subscores,         # Python dict → JSONB
            "notes_json": r.notes,                 # Python list → JSONB
            "business_risk_json": r.business_risk, # business-risk proxy audit → JSONB
        }
        for period_end, r in results_by_period.items()
    ]
    if rows:
        _client().table("implied_ratings").upsert(rows).execute()


# ── Agency ratings + ML labels (ratings data workstream) ─────────────────────

# Columns persisted to agency_ratings / rating_labels, mirroring
# supabase/schema.sql. Listed explicitly so an event/label dict with
# extra keys (e.g. the transient "ric") is projected to exactly the table's columns.
_AGENCY_RATING_COLUMNS = (
    "cik", "agency", "effective_date", "rating_index", "rating_raw",
    "rating_status", "rating_action", "source_permid", "source_ric",
)
_RATING_LABEL_COLUMNS = (
    "cik", "period_end", "agency", "rating_index",
    "rating_index_3m", "rating_index_6m", "rating_index_12m",
    "label_3m", "label_6m", "label_12m", "notch_change_12m", "distress_12m",
)


def save_agency_ratings_bulk(events: list[dict[str, Any]], **_) -> None:
    """
    Upsert event-grain agency-rating actions (one row per cik+agency+effective_date).

    `events` are the crosswalked event dicts from src.ratings (after attach_cik). CIK
    is zero-padded; only the table's columns are sent. Same overwrite-on-PK-conflict
    semantics as the other bulk writers, so re-ingesting a drop is idempotent.
    """
    rows = [
        {**{c: e.get(c) for c in _AGENCY_RATING_COLUMNS}, "cik": str(e["cik"]).zfill(10)}
        for e in events
    ]
    if rows:
        _client().table("agency_ratings").upsert(rows).execute()


def _delete_all_rows(table: str) -> None:
    """Delete every row of `table` (needs the service-role key). supabase-py requires a
    filter on delete, so match a sentinel CIK no row can have — the same idiom as
    scripts.reset_training_tables."""
    _client().table(table).delete().neq("cik", "__none__").execute()


def clear_agency_ratings(**_) -> None:
    """Truncate agency_ratings so a reload MIRRORS the canonical CSV exactly — dropping
    rows the CSV no longer contains (e.g. a retired source like the old S&P/Kaggle drop).
    A plain upsert can't do this: keys absent from the CSV would survive and feed stale
    labels downstream. Used by load_agency_ratings' default replace mode."""
    _delete_all_rows("agency_ratings")


def save_rating_labels_bulk(labels: list[dict[str, Any]], **_) -> None:
    """
    Upsert ML label rows (one per cik+period_end+agency) from build_rating_labels.
    CIK zero-padded; projected to the rating_labels columns.
    """
    rows = [
        {**{c: lab.get(c) for c in _RATING_LABEL_COLUMNS}, "cik": str(lab["cik"]).zfill(10)}
        for lab in labels
    ]
    if rows:
        _client().table("rating_labels").upsert(rows).execute()


def clear_rating_labels(**_) -> None:
    """Truncate rating_labels so a rebuild MIRRORS the freshly built set — dropping stale
    rows for (cik, period_end, agency) combinations no longer produced (e.g. an agency
    dropped from the universe). Used by build_labels' default replace mode."""
    _delete_all_rows("rating_labels")


# ── Migration predictions + model registry (Stage 3) ─────────────────────────

_MIGRATION_PREDICTION_COLUMNS = (
    "cik", "period_end", "horizon_months", "p_downgrade", "p_upgrade",
    "p_distress", "drivers_json", "model_version",
)


def save_migration_predictions_bulk(rows: list[dict[str, Any]], **_) -> None:
    """
    Upsert calibrated migration predictions (one row per cik+period_end+horizon).
    Written offline by src.model.predict; read by the API/screen. CIK zero-padded;
    projected to the migration_predictions columns.
    """
    out = [
        {**{c: r.get(c) for c in _MIGRATION_PREDICTION_COLUMNS},
         "cik": str(r["cik"]).zfill(10),
         "horizon_months": int(r.get("horizon_months") or 12)}
        for r in rows
    ]
    if out:
        _client().table("migration_predictions").upsert(out).execute()


def clear_migration_predictions(**_) -> None:
    """Truncate migration_predictions (full manual wipe). NOTE: src.model.predict no
    longer clears up-front — it batch-upserts and prunes other-version rows at the END
    (see prune_migration_predictions_except_version), so a killed run never empties the
    table. Kept for an explicit hard reset."""
    _delete_all_rows("migration_predictions")


def get_predicted_keys(version: str) -> set[tuple[str, str]]:
    """
    (cik, period_end) pairs already scored for `version`, so src.model.predict can
    RESUME a killed run by skipping issuer-periods it already wrote. cik is the stored
    zero-padded form. Empty set if the table is missing/unreachable (treat all as unscored).
    """
    def build():
        return (
            _client()
            .table("migration_predictions")
            .select("cik,period_end")
            .eq("model_version", version)
            .order("cik").order("period_end")
        )
    try:
        rows = _fetch_all(build)
    except Exception:
        return set()
    return {(r["cik"], r["period_end"]) for r in rows}


def prune_migration_predictions_except_version(version: str) -> None:
    """
    Delete rows left from any OTHER model version — replace semantics with no
    empty-table window. After a full re-score with `version`, every current
    issuer-period has a fresh row, so anything tagged with a different version is
    stale (old model, or an issuer-period that dropped out of the universe).
    """
    _client().table("migration_predictions").delete().neq("model_version", version).execute()


def get_migration_predictions_grouped(
    cik: str | None = None, ciks: list[str] | None = None, **_
) -> dict[str, dict[str, dict]]:
    """
    Fetch migration predictions in ONE query, grouped as cik → period_end → row.

    Resilient to the table not existing yet (Stage 3 not deployed): returns {} on
    any query error, so the API/screen degrade gracefully to the rule-based outlook.
    """
    def build():
        q = (
            _client()
            .table("migration_predictions")
            .select(",".join(_MIGRATION_PREDICTION_COLUMNS))
            .order("cik").order("period_end")
        )
        return _scope_ciks(q, cik, ciks)

    out: dict[str, dict[str, dict]] = {}
    try:
        rows = _fetch_all(build)
    except Exception:
        return {}
    for row in rows:
        out.setdefault(row["cik"], {})[row["period_end"]] = row
    return out


def save_model_registry(*, version: str, artifact_path: str, feature_list: list,
                        train_window: dict, metrics: dict, **_) -> None:
    """Upsert the single active model_registry row (id='active')."""
    from datetime import datetime, timezone
    _client().table("model_registry").upsert({
        "id": "active",
        "version": version,
        "artifact_path": artifact_path,
        "feature_list": feature_list,
        "train_window": train_window,
        "metrics_json": metrics,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def get_model_registry(**_) -> dict[str, Any] | None:
    """Return the active model_registry row, or None (incl. when the table is absent)."""
    try:
        res = _client().table("model_registry").select("*").eq("id", "active").limit(1).execute()
    except Exception:
        return None
    return res.data[0] if res.data else None


def get_agency_ratings_grouped(cik: str | None = None, **_) -> dict[str, dict[str, list[dict]]]:
    """
    Fetch agency-rating events in ONE query, grouped as cik → agency → [events],
    each agency's events sorted by effective_date ascending.

    Used to derive point-in-time features such as time-in-rating (months since the
    last action). Pass a cik to scope to one issuer; omit it to read everything.
    """
    def build():
        q = (
            _client()
            .table("agency_ratings")
            .select(",".join(_AGENCY_RATING_COLUMNS))
            .order("cik").order("agency").order("effective_date")  # stable + chronological
        )
        return q.eq("cik", cik.zfill(10)) if cik is not None else q

    out: dict[str, dict[str, list[dict]]] = {}
    for row in _fetch_all(build):
        out.setdefault(row["cik"], {}).setdefault(row["agency"], []).append(row)
    return out


def get_rating_labels_grouped(cik: str | None = None, **_) -> dict[str, dict[str, dict[str, dict]]]:
    """
    Fetch rating labels in ONE query, grouped as cik → period_end → agency → row.

    Each row is the label dict the ML trainer consumes. Pass a cik to scope to one
    issuer; omit it to read the whole training set.
    """
    def build():
        q = (
            _client()
            .table("rating_labels")
            .select(",".join(_RATING_LABEL_COLUMNS))
            .order("cik").order("period_end").order("agency")  # stable order for paging
        )
        return q.eq("cik", cik.zfill(10)) if cik is not None else q

    out: dict[str, dict[str, dict[str, dict]]] = {}
    for row in _fetch_all(build):
        out.setdefault(row["cik"], {}).setdefault(row["period_end"], {})[row["agency"]] = row
    return out


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


def save_bond_instruments(
    cik: str,
    period_end: str,
    instruments: list[Any],
    **_,
) -> None:
    """
    Persist LLM-extracted bond instruments (with seniority) for one (cik, period_end).

    Same ignore_duplicates semantics as save_covenants; UNIQUE constraint covers
    (cik, period_end, instrument_name, evidence_quote), so re-running the review on
    a period won't multiply rows.
    """
    cik = cik.zfill(10)
    rows = [
        {
            "cik": cik,
            "period_end": period_end,
            "instrument_name": b.instrument_name,
            "seniority": b.seniority,
            "principal_amount": b.principal_amount,
            "coupon": b.coupon,
            "maturity_year": b.maturity_year,
            "evidence_quote": b.evidence_quote,
            "source": b.source,
        }
        for b in instruments
    ]
    if rows:
        _client().table("bond_instruments").upsert(rows, ignore_duplicates=True).execute()


# ── Read operations ──────────────────────────────────────────────────────────

def get_issuers(portfolio_only: bool = False, **_) -> list[dict[str, Any]]:
    """
    Return one identity row per tracked company.

    Each row is {cik, name, ticker, last_refreshed, sic} where `ticker` is the
    first current ticker (or "" if unknown), `last_refreshed` is when the issuer
    was last re-tracked from EDGAR (None = never), and `sic` is the industry code
    (used to flag unrated financial-sector issuers). Querying companies directly —
    rather than deriving CIKs from the ratios table — ensures issuers whose ratio
    extraction failed (e.g. banks with non-standard XBRL) are still visible.

    `portfolio_only=True` restricts to the curated watchlist (in_portfolio = TRUE) —
    used by the dashboard so issuers tracked solely to TRAIN the model don't appear.
    The default (all tracked) is what the refresh cron and model pipeline want.
    """
    q = _client().table("companies").select("cik, name, tickers, last_refreshed, sic")
    if portfolio_only:
        q = q.eq("in_portfolio", True)
    res = q.execute()
    issuers = []
    for row in sorted(res.data, key=lambda r: r["cik"]):
        tickers = row.get("tickers") or []
        issuers.append({
            "cik": row["cik"],
            "name": row.get("name", ""),
            "ticker": tickers[0] if tickers else "",
            "last_refreshed": row.get("last_refreshed"),
            "sic": row.get("sic"),
        })
    return issuers


def set_portfolio(cik: str, member: bool = True, **_) -> bool:
    """
    Flip companies.in_portfolio for one issuer (the curated-watchlist flag the
    dashboard filters on). Returns True if a row was updated, False if the CIK
    isn't tracked yet (track it first — features must exist to score it). Keyed on
    the permanent CIK; never touches ratios/labels, so training is unaffected.
    """
    res = (
        _client()
        .table("companies")
        .update({"in_portfolio": bool(member)})
        .eq("cik", cik.zfill(10))
        .execute()
    )
    return bool(res.data)


def touch_last_refreshed(cik: str, **_) -> None:
    """
    Stamp companies.last_refreshed = now() for one issuer.

    Called by the auto-refresh cron after a successful re-track so the next run
    can process issuers oldest-refreshed-first (NULLs — i.e. never-refreshed —
    sort first). Keyed on the permanent CIK.
    """
    from datetime import datetime, timezone
    _client().table("companies").update(
        {"last_refreshed": datetime.now(timezone.utc).isoformat()}
    ).eq("cik", cik.zfill(10)).execute()


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
        data["not_applicable"] = missing.get("not_applicable", False)
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


def get_ratios_grouped(
    cik: str | None = None, ciks: list[str] | None = None, **_
) -> dict[str, dict[str, dict[str, dict]]]:
    """
    Fetch ratios in ONE query and group them as cik → period_end → ratio_name → data.

    This replaces the per-period get_full_ratios() loop that caused an N+1 query
    storm (a detail page issued ~18×2 round-trips). Pass a cik to scope to one
    company (detail page), `ciks` to scope to the portfolio watchlist (the list
    endpoint — avoids scanning every model-training issuer), or neither to fetch
    everything (cron/pipeline).

    `data` is {value, inputs, source_tags} — the same shape get_full_ratios returns.
    """
    def build():
        q = (
            _client()
            .table("ratios")
            .select("cik, period_end, ratio_name, value, inputs_json, source_tags_json, missing_json")
            .order("cik").order("period_end").order("ratio_name")  # stable order for paging
        )
        return _scope_ciks(q, cik, ciks)

    out: dict[str, dict[str, dict[str, dict]]] = {}
    for row in _fetch_all(build):
        out.setdefault(row["cik"], {}).setdefault(row["period_end"], {})[row["ratio_name"]] = (
            _ratio_data_from_row(row)
        )
    return out


def get_findings_grouped(
    cik: str | None = None, ciks: list[str] | None = None, **_
) -> dict[str, dict[str, list[dict]]]:
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
        return _scope_ciks(q, cik, ciks)

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


def get_maturities_grouped(
    cik: str | None = None, ciks: list[str] | None = None, **_
) -> dict[str, dict[str, dict]]:
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
        return _scope_ciks(q, cik, ciks)

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


def get_implied_ratings_grouped(
    cik: str | None = None, ciks: list[str] | None = None, **_
) -> dict[str, dict[str, dict]]:
    """
    Fetch implied ratings in ONE query, grouped as cik → period_end → rating dict.

    Each rating dict is {implied_rating, rating_index, financial_risk_profile,
    financial_risk_index, business_risk_index, business_risk, subscores, notes} —
    the same shape the API attaches to each period. Pass a cik to scope to one
    issuer (detail page); omit it to fetch every issuer at once (portfolio list).
    """
    def build():
        q = (
            _client()
            .table("implied_ratings")
            .select(
                "cik, period_end, implied_rating, rating_index, financial_risk_profile, "
                "financial_risk_index, business_risk_index, subscores_json, notes_json, "
                "business_risk_json"
            )
            .order("cik").order("period_end")  # stable order for paging
        )
        return _scope_ciks(q, cik, ciks)

    out: dict[str, dict[str, dict]] = {}
    for row in _fetch_all(build):
        out.setdefault(row["cik"], {})[row["period_end"]] = {
            "implied_rating": row["implied_rating"],
            "rating_index": row["rating_index"],
            "financial_risk_profile": row["financial_risk_profile"],
            "financial_risk_index": row["financial_risk_index"],
            "business_risk_index": row["business_risk_index"],
            "business_risk": row.get("business_risk_json") or {},
            "subscores": row.get("subscores_json") or {},
            "notes": row.get("notes_json") or [],
        }
    return out


def get_covenants_grouped(
    cik: str | None = None, ciks: list[str] | None = None, **_
) -> dict[str, dict[str, list[dict]]]:
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
        return _scope_ciks(q, cik, ciks)

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


def get_loss_provisions_grouped(
    cik: str | None = None, ciks: list[str] | None = None, **_
) -> dict[str, dict[str, list[dict]]]:
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
        return _scope_ciks(q, cik, ciks)

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


def get_bond_instruments_grouped(cik: str | None = None, **_) -> dict[str, dict[str, list[dict]]]:
    """Fetch bond instruments in ONE query, grouped as cik → period_end → [instruments]."""
    def build():
        q = (
            _client()
            .table("bond_instruments")
            .select(
                "cik, period_end, instrument_name, seniority, principal_amount, "
                "coupon, maturity_year, evidence_quote, source"
            )
            .order("id")  # stable order for paging (BIGSERIAL primary key)
        )
        return q.eq("cik", cik.zfill(10)) if cik is not None else q

    out: dict[str, dict[str, list[dict]]] = {}
    for row in _fetch_all(build):
        out.setdefault(row["cik"], {}).setdefault(row["period_end"], []).append({
            "instrument_name": row["instrument_name"],
            "seniority": row["seniority"],
            "principal_amount": row["principal_amount"],
            "coupon": row["coupon"],
            "maturity_year": row["maturity_year"],
            "evidence_quote": row["evidence_quote"],
            "source": row["source"],
        })
    return out


def delete_issuer(cik: str, **_) -> None:
    """
    Hard-delete all stored data for a company from every table.

    Called when the user clicks "Remove" in the portfolio dashboard.
    All per-company rows (ratios, implied ratings, findings, maturities,
    covenants, loss provisions) and the companies identity row are cleared so the
    issuer no longer appears anywhere.
    """
    cik = cik.zfill(10)
    client = _client()
    client.table("ratios").delete().eq("cik", cik).execute()
    client.table("implied_ratings").delete().eq("cik", cik).execute()
    client.table("agency_ratings").delete().eq("cik", cik).execute()
    client.table("rating_labels").delete().eq("cik", cik).execute()
    client.table("migration_predictions").delete().eq("cik", cik).execute()
    client.table("llm_findings").delete().eq("cik", cik).execute()
    client.table("debt_maturities").delete().eq("cik", cik).execute()
    client.table("covenants").delete().eq("cik", cik).execute()
    client.table("loss_provisions").delete().eq("cik", cik).execute()
    client.table("bond_instruments").delete().eq("cik", cik).execute()
    resp = client.table("companies").delete().eq("cik", cik).execute()
    if not resp.data:
        raise ValueError(
            f"No company row deleted for CIK {cik} — "
            "verify SUPABASE_SERVICE_ROLE_KEY is set (anon key cannot DELETE with RLS enabled)"
        )
