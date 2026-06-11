"""
FastAPI backend for the Credit Warning System.

All routes use the /api/ prefix so they work both:
  - Locally: uvicorn api.main:app --port 8000  →  http://localhost:8000/api/...
  - Vercel:  vercel.json routes /api/* → this function  →  /api/...

Run locally: python3 -m uvicorn api.main:app --reload --port 8000

Data flow per request:
  1. Ingest:  EDGAR XBRL JSON  →  get_company_facts()
  2. Extract: facts + period   →  extract_all()  →  RatioResult objects
  3. Store:   RatioResults     →  Supabase (ratios table)
  4. Score:   ratios + findings→  compute_score() → ScoreResult
  5. Serve:   ScoreResult      →  JSON response to frontend
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Load .env.local (Next.js convention) so the Python API sees the same
# SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / ANTHROPIC_API_KEY as the frontend.
_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env.local")

# Ensure the project root is on sys.path so `from src.x import y` works
# whether the server is started from the project root or the api/ subdirectory.
sys.path.insert(0, str(_ROOT))

from src.extract import (
    RatioResult,
    _get_available_periods,
    extract_all,
    debt_maturity_schedule,
)
from src.ingest import (
    filing_doc_url,
    find_filing_for_period,
    get_company_facts,
    get_company_info,
    get_filings,
    resolve_identifier,
)
from src.score import DEFAULT_CONFIG, ScoreConfig, compute_score
from src.store import (
    add_case,
    delete_case,
    delete_issuer,
    get_case,
    get_cik_by_ticker,
    get_company,
    get_covenants_grouped,
    get_findings_grouped,
    get_issuers,
    get_loss_provisions_grouped,
    get_maturities_grouped,
    get_ratios_grouped,
    get_score_config,
    list_cases,
    save_company,
    save_covenants,
    save_findings,
    save_loss_provisions,
    save_maturities_bulk,
    save_ratios_bulk,
    save_score_config,
)

app = FastAPI(title="Credit Warning API", version="1.0")

# Allow all origins in CORS so the Next.js dev server (port 3000) and Vercel
# preview URLs can reach the API without preflight failures.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-process state for the long-running backtest task.
# A single dict is fine here because FastAPI runs in one process on Vercel
# and locally. If scaled to multiple workers, move this to Redis or Supabase.
_backtest_status: dict[str, Any] = {
    "running": False,
    "result": None,
    "error": None,
}

# Per-process cache of the active (applied) scoring config, so the hot read
# endpoints (/api/issuers, /api/issuer) don't hit Supabase on every request.
# Same single-process caveat as _backtest_status; invalidated when the config is
# applied via PUT /api/score-config.
_active_config_cache: ScoreConfig | None = None


def _active_config() -> ScoreConfig:
    """Return the active ScoreConfig (the one applied to the live portfolio), cached."""
    global _active_config_cache
    if _active_config_cache is None:
        try:
            _active_config_cache = ScoreConfig.from_dict(get_score_config())
        except Exception:
            # Supabase unavailable → fall back to defaults rather than 500 the dashboard.
            _active_config_cache = ScoreConfig.from_dict(DEFAULT_CONFIG)
    return _active_config_cache


def _invalidate_config_cache() -> None:
    global _active_config_cache
    _active_config_cache = None


class TrackRequest(BaseModel):
    ticker: str
    no_llm: bool = True       # LLM pass is slow (~30 s/filing); UI always omits it for responsiveness
    periods: int | None = None  # cap on annual periods to fetch; None = full available history


# ── Helper: reconstruct typed objects from raw Supabase rows ────────────────

def _to_ratio_results(full_ratios: dict, period_end: str) -> dict[str, RatioResult]:
    # Reconstruct RatioResult objects from Supabase rows so compute_score can consume them.
    # Supabase returns plain dicts; compute_score expects dataclass instances.
    # Skip missing ratios (value is None): they must not be scored, matching the
    # pre-existing behaviour where an absent ratio contributes no stress points.
    return {
        name: RatioResult(
            name=name,
            value=data["value"],
            inputs=data.get("inputs", {}),
            source_tags=data.get("source_tags", {}),
            period_end=period_end,
        )
        for name, data in full_ratios.items()
        if data.get("value") is not None
    }


def _resolve_cik_for_read(identifier: str) -> str:
    """
    Resolve a ticker-or-CIK path param to a canonical CIK for read endpoints.

    The frontend still uses friendly /issuer/AAPL URLs, but the DB is keyed on
    CIK. We resolve without hitting EDGAR when possible:
      1. A bare CIK (all digits) is zero-padded and returned directly.
      2. A ticker is looked up in the local companies table (it was stored when
         the issuer was tracked) — no SEC round-trip.
      3. As a last resort (e.g. a ticker that was renamed since it was tracked),
         fall back to an EDGAR ticker→CIK lookup via resolve_identifier.
    """
    ident = identifier.strip()
    cand = ident[3:] if ident[:3].upper() == "CIK" else ident
    if cand.isdigit() and len(cand) <= 10:
        return cand.zfill(10)

    cik = get_cik_by_ticker(ident)
    if cik:
        return cik

    # Not tracked under that ticker locally — try EDGAR as a fallback.
    return resolve_identifier(ident)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    """Liveness probe used by Vercel and local dev to confirm the server is up."""
    return {"status": "ok"}


@app.get("/api/issuers")
def list_issuers():
    """
    Return a summary row for every tracked issuer, scored against their latest period.

    Each row includes the five key ratios and a stress score so the dashboard
    table can render without a separate per-issuer API call.
    """
    # Fetch identity, all ratios, and all findings in a fixed handful of queries
    # (instead of ~3 per issuer). Grouped dicts are keyed by cik → period → ….
    issuers = get_issuers()
    ratios_by_cik = get_ratios_grouped()       # 1 query for every issuer's ratios
    findings_by_cik = get_findings_grouped()   # 1 query for every issuer's findings
    maturities_by_cik = get_maturities_grouped()       # 1 query for every issuer's maturities
    covenants_by_cik = get_covenants_grouped()         # 1 query for every issuer's covenants
    provisions_by_cik = get_loss_provisions_grouped()  # 1 query for every issuer's provisions

    result = []
    for issuer in issuers:
        cik = issuer["cik"]
        by_period = ratios_by_cik.get(cik, {})
        if not by_period:
            # Company exists in DB but has no stored ratios — skip rather than error.
            continue

        # period_end strings sort chronologically; the newest is the max.
        periods = sorted(by_period)
        latest = periods[-1]
        full_ratios = by_period[latest]
        findings = findings_by_cik.get(cik, {}).get(latest, [])
        maturity = maturities_by_cik.get(cik, {}).get(latest)
        covenants = covenants_by_cik.get(cik, {}).get(latest, [])
        provisions = provisions_by_cik.get(cik, {}).get(latest, [])

        # Re-score from stored data so the summary is always consistent with
        # the detail page (both call compute_score with the same inputs).
        ratio_results = _to_ratio_results(full_ratios, latest)
        # compute_score reads findings/covenants/provisions through _attr, which
        # handles the stored dicts directly — no dataclass conversion needed.
        # config = the active (applied) parameters so the dashboard reflects them.
        score_result = compute_score(
            ratio_results, findings, maturity, covenants, provisions,
            config=_active_config(),
        )

        # Helper to safely pull a ratio value without KeyError on missing data.
        def _v(name: str) -> float | None:
            r = full_ratios.get(name)
            return r["value"] if r else None

        result.append({
            "cik": cik,
            "ticker": issuer["ticker"],
            "name": issuer["name"],
            "latest_period": latest,
            "period_count": len(periods),
            "ebitda_margin": _v("ebitda_margin"),
            "leverage": _v("leverage"),
            "interest_coverage": _v("interest_coverage"),
            "free_cash_flow": _v("free_cash_flow"),
            "fcf_margin": _v("fcf_margin"),
            "liquidity": _v("liquidity"),
            "cash_flow_to_debt": _v("cash_flow_to_debt"),
            "debt_to_assets": _v("debt_to_assets"),
            "current_ratio": _v("current_ratio"),
            "score": score_result.score,
            "alerts": score_result.alerts,
        })
    return result


@app.post("/api/track")
def track_issuer(req: TrackRequest):
    """
    Resolve a ticker to a CIK, fetch XBRL facts from EDGAR, extract ratios for
    each available annual period, and persist them to Supabase.

    Optionally runs an LLM qualitative review of the 10-K MD&A text for each
    period if no_llm=False (slow; disabled by default in the UI).
    """
    identifier = req.ticker.upper().strip()

    # Step 1: Resolve the input (ticker OR CIK) → canonical CIK.
    try:
        cik = resolve_identifier(identifier)
    except ValueError:
        raise HTTPException(404, f"{identifier!r} not found in SEC EDGAR")
    except Exception as e:
        raise HTTPException(500, f"EDGAR lookup failed: {e}")

    # Step 2: Persist the company's identity snapshot (name, current tickers,
    # former names) keyed on the permanent CIK. Best-effort: a failure here
    # shouldn't block ratio ingestion, but we still surface a clear error.
    try:
        info = get_company_info(cik)
        save_company(info)
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch/save company info: {e}")

    # Step 3: Fetch the full XBRL company facts JSON (cached to disk after first fetch).
    try:
        facts = get_company_facts(cik)
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch company facts: {e}")

    # Step 4: Determine which annual periods are available. Fetch the full
    # history by default (req.periods=None); a caller may still cap it to the
    # most recent N. XBRL data only goes back to ~2009, so "full" is ~15 years.
    available = _get_available_periods(facts)
    periods = available if req.periods is None else available[: req.periods]
    if not periods:
        raise HTTPException(404, f"No annual XBRL periods found for {identifier}")

    # Step 5: Extract ratios for every period (pure compute, instant) and persist
    # them in a SINGLE bulk upsert. Writing one period at a time was ~18 separate
    # DB round-trips — the dominant cost of a track — so we batch them all here.
    results_by_period = {period: extract_all(facts, period) for period in periods}
    save_ratios_bulk(cik, results_by_period)

    # Step 5b: Debt maturity schedules are pure XBRL compute (no LLM, no filing
    # fetch) — extract for every period and bulk-save, always (even when no_llm).
    maturities_by_period = {
        period: debt_maturity_schedule(facts, period) for period in periods
    }
    save_maturities_bulk(cik, maturities_by_period)

    # Step 6: Optional LLM review per period (slow; disabled by default).
    # review_filing fetches the 10-K once, locates the MD&A / debt /
    # contingencies sections, and runs the three LLM passes on the located
    # slices only.
    if not req.no_llm:
        filings = get_filings(cik, ["10-K"])
        for period in periods:
            try:
                from src.footnote_review import review_filing
                findings_list, covenants, provisions = review_filing(
                    cik, period, filings
                )
                save_findings(cik, period, findings_list)
                save_covenants(cik, period, covenants)
                save_loss_provisions(cik, period, provisions)
            except Exception:
                # LLM review is best-effort; ratio/maturity data has already been
                # saved. Log it — silent swallowing previously hid pipeline bugs.
                logging.warning(
                    "LLM review skipped for %s %s", cik, period, exc_info=True
                )

    return {
        "cik": cik,
        "ticker": info["tickers"][0] if info.get("tickers") else identifier,
        "name": info.get("name", ""),
        "periods_saved": len(periods),
        "periods": periods,
    }


@app.get("/api/issuer/{ticker}")
def get_issuer(ticker: str):
    """
    Return full ratio history and stress scores for a single issuer.

    Periods are returned newest-first so the frontend chart can show recent
    trends at the top without client-side sorting.
    """
    cik = _resolve_cik_for_read(ticker)

    # Fetch the whole history in two queries (ratios + findings), then build each
    # period in memory — instead of two queries *per period* (the old N+1 storm).
    company = get_company(cik) or {}
    ratios_by_period = get_ratios_grouped(cik).get(cik, {})
    if not ratios_by_period:
        raise HTTPException(404, f"{ticker} is not tracked. POST /api/track first.")
    findings_by_period = get_findings_grouped(cik).get(cik, {})
    maturities_by_period = get_maturities_grouped(cik).get(cik, {})
    covenants_by_period = get_covenants_grouped(cik).get(cik, {})
    provisions_by_period = get_loss_provisions_grouped(cik).get(cik, {})

    # Per-period source link: the public SEC EDGAR URL of the 10-K each period's
    # ratios were extracted from, so an analyst can trace any figure on the audit
    # card back to the filing. Best-effort — the 10-K list comes from the cached
    # submissions JSON; if EDGAR is unreachable on a cold instance we just omit
    # the link rather than fail the whole issuer load.
    try:
        filings = get_filings(cik, ["10-K"])
    except Exception:
        filings = []

    period_data = []
    # Newest period first so the frontend chart/table show recent data at the top.
    for period in sorted(ratios_by_period, reverse=True):
        full_ratios = ratios_by_period[period]
        findings = findings_by_period.get(period, [])
        maturity = maturities_by_period.get(period)          # dict or None
        covenants = covenants_by_period.get(period, [])      # list of dicts
        provisions = provisions_by_period.get(period, [])    # list of dicts
        ratio_results = _to_ratio_results(full_ratios, period)
        # compute_score accepts dicts for all LLM signals (see _attr).
        # config = the active (applied) parameters so detail scores match the dashboard.
        score_result = compute_score(
            ratio_results, findings, maturity, covenants, provisions,
            config=_active_config(),
        )

        # Match this period to its 10-K and build the EDGAR document URL (None if
        # no filing matches or the list couldn't be fetched).
        filing = find_filing_for_period(filings, period) if filings else None
        source_url = (
            filing_doc_url(cik, filing["accessionNumber"], filing["primaryDocument"])
            if filing
            else None
        )

        period_data.append({
            "period_end": period,
            "ratios": full_ratios,          # full dict with value + inputs + source_tags
            "score": score_result.score,
            "breakdown": score_result.breakdown,   # per-component point contributions
            "alerts": score_result.alerts,
            "findings": findings,           # LLM qualitative findings (may be empty)
            "maturities": maturity,         # XBRL maturity schedule (or None)
            "covenants": covenants,         # LLM-extracted covenants (may be empty)
            "loss_provisions": provisions,  # LLM-extracted provisions (may be empty)
            "source_url": source_url,       # public SEC EDGAR URL of the source 10-K
        })

    tickers = company.get("tickers") or []
    return {
        "cik": cik,
        "ticker": tickers[0] if tickers else ticker.upper(),
        "name": company.get("name", ""),
        "periods": period_data,
    }


@app.delete("/api/issuer/{ticker}")
def remove_issuer(ticker: str):
    """Delete all stored data for a company (resolved from ticker or CIK)."""
    cik = _resolve_cik_for_read(ticker)
    delete_issuer(cik)
    return {"status": "deleted", "cik": cik}


# ── On-demand LLM review (per-issuer background task) ────────────────────────
#
# Tracking deliberately skips the LLM pass (no_llm defaults to True) because it
# takes ~30 s/filing. These endpoints let the detail page run the pass on demand
# for an already-tracked issuer, mirroring the backtest start+poll pattern.
#
# Status is keyed by CIK so reviews of different issuers don't clobber each
# other. As with the backtest, a single in-process dict assumes one server
# process (true on Vercel and locally) — move to Redis/Supabase if scaled out.
_llm_review_status: dict[str, dict[str, Any]] = {}

# Default number of most-recent annual periods to review. The full history is
# ~15 filings (~7 min) — far past the 60 s Vercel function limit — so we cap to
# the most recent few, which is where credit concerns are most actionable.
_LLM_REVIEW_DEFAULT_PERIODS = 3


class LlmReviewRequest(BaseModel):
    # How many most-recent annual periods to review. None = all stored periods
    # (slow; only safe on a long-lived local server, not Vercel's 60 s limit).
    periods: int | None = _LLM_REVIEW_DEFAULT_PERIODS


def _run_llm_review_task(cik: str, periods: list[str]) -> None:
    """
    Worker executed in a FastAPI BackgroundTask: run the LLM review for each
    period and save findings/covenants/provisions. Updates _llm_review_status
    so the frontend can poll progress. Deferred import keeps the LLM/HTTP stack
    out of every server start.
    """
    status = _llm_review_status[cik]
    try:
        from src.footnote_review import review_filing

        filings = get_filings(cik, ["10-K"])
        for period in periods:
            try:
                findings_list, covenants, provisions = review_filing(cik, period, filings)
                save_findings(cik, period, findings_list)
                save_covenants(cik, period, covenants)
                save_loss_provisions(cik, period, provisions)
            except Exception:
                # Best-effort per period: one bad filing must not abort the rest.
                logging.warning(
                    "LLM review skipped for %s %s", cik, period, exc_info=True
                )
            status["periods_done"] += 1
        status["error"] = None
    except Exception as e:
        # A failure here (e.g. EDGAR fetch of the filing list) aborts the whole run.
        status["error"] = str(e)
    finally:
        status["running"] = False


@app.post("/api/issuer/{ticker}/llm-review")
def start_llm_review(
    ticker: str, req: LlmReviewRequest, background_tasks: BackgroundTasks
):
    """
    Run the LLM qualitative review for an already-tracked issuer in the
    background and return immediately. The frontend polls
    /api/issuer/{ticker}/llm-review/status to detect completion.

    Periods come from the issuer's stored ratios (newest-first), so no EDGAR
    facts round-trip is needed; the 10-K filing list is fetched in the worker.
    Returns 409 if a review is already running for this issuer.
    """
    cik = _resolve_cik_for_read(ticker)

    existing = _llm_review_status.get(cik)
    if existing and existing["running"]:
        raise HTTPException(409, "LLM review already running for this issuer")

    ratios_by_period = get_ratios_grouped(cik).get(cik, {})
    if not ratios_by_period:
        raise HTTPException(404, f"{ticker} is not tracked. POST /api/track first.")

    periods = sorted(ratios_by_period, reverse=True)  # newest-first
    if req.periods is not None:
        periods = periods[: req.periods]

    _llm_review_status[cik] = {
        "running": True,
        "error": None,
        "periods_done": 0,
        "periods_total": len(periods),
    }
    background_tasks.add_task(_run_llm_review_task, cik, periods)
    return {"status": "started", "cik": cik, "periods_total": len(periods)}


@app.get("/api/issuer/{ticker}/llm-review/status")
def llm_review_status(ticker: str):
    """
    Return the current LLM-review state for one issuer: running flag, error, and
    periods_done/periods_total progress. Returns an idle state (running=False,
    periods 0/0) if no review has been started for this issuer in this process.
    """
    cik = _resolve_cik_for_read(ticker)
    return _llm_review_status.get(
        cik, {"running": False, "error": None, "periods_done": 0, "periods_total": 0}
    )


# ── Backtest (long-running background task) ──────────────────────────────────

def _run_backtest_task(steps: int, config_dict: dict | None):
    """
    Worker function executed in a FastAPI BackgroundTask.
    Runs the full backtest and writes the result (or error) into _backtest_status.
    The import is deferred to avoid loading backtest deps on every server start.

    config_dict is the (transient) scoring parameters to TEST this run with —
    it is NOT persisted, so a backtest experiment never changes the live
    portfolio. None → the run uses the default parameters.
    """
    try:
        from src.backtest import run_backtest
        config = ScoreConfig.from_dict(config_dict) if config_dict else ScoreConfig.from_dict(DEFAULT_CONFIG)
        result = run_backtest(steps=steps, config=config)
        _backtest_status["result"] = result
        _backtest_status["error"] = None
    except Exception as e:
        _backtest_status["error"] = str(e)
        _backtest_status["result"] = None
    finally:
        # Always clear the running flag so the UI can re-trigger if needed.
        _backtest_status["running"] = False


# Bounds on the user-supplied step count: at least a few snapshots to be
# meaningful, capped so a stray large value can't hammer EDGAR for decades
# of history per company.
_MIN_STEPS, _MAX_STEPS = 4, 60


class BacktestRequest(BaseModel):
    # Snapshots per case (~90 days apart). Optional — omitted means the
    # backtest's own DEFAULT_STEPS is used.
    steps: int | None = None
    # Scoring parameters to TEST this run with (transient — NOT persisted, so a
    # backtest experiment never changes the live portfolio). Omitted → the active
    # (applied) config is used, so the backtest matches the dashboard.
    config: dict | None = None


@app.post("/api/backtest")
def start_backtest(background_tasks: BackgroundTasks, req: BacktestRequest | None = None):
    """
    Start the backtest as a background task and return immediately.
    The frontend polls /api/backtest/status every 3 s to detect completion.
    Returns 409 if a backtest is already running.

    Accepts an optional body:
      - {"steps": N}   point-in-time history depth, clamped to [_MIN_STEPS, _MAX_STEPS].
      - {"config": …}  scoring parameters to TEST with (transient — not saved).
                       Omitted → the active applied config (matches the dashboard).
    """
    if _backtest_status["running"]:
        raise HTTPException(409, "Backtest already running")

    from src.backtest import DEFAULT_STEPS
    steps = req.steps if (req and req.steps is not None) else DEFAULT_STEPS
    steps = max(_MIN_STEPS, min(_MAX_STEPS, steps))

    # A request config is the draft being TESTED; otherwise run with the active
    # (applied) config so the backtest reflects what the portfolio currently uses.
    if req and req.config is not None:
        config_dict = _validate_config(req.config)
    else:
        try:
            config_dict = get_score_config()
        except Exception:
            config_dict = DEFAULT_CONFIG

    _backtest_status["running"] = True
    _backtest_status["result"] = None
    _backtest_status["error"] = None
    background_tasks.add_task(_run_backtest_task, steps, config_dict)
    return {"status": "started"}


@app.get("/api/backtest/status")
def backtest_status():
    """
    Return the current backtest state: running flag, result dict, or error string.

    When no backtest has run in this server process, fall back to the last
    persisted run on disk (results from the most recent CLI/server run, then
    the committed baseline). This lets the page show the latest scorecard on
    load instead of an empty state — marked with saved=True so the UI can say
    the numbers come from a previous run.
    """
    if not _backtest_status["running"] and _backtest_status["result"] is None:
        import json
        from src.backtest import RESULTS_PATH, BASELINE_PATH

        for path in (RESULTS_PATH, BASELINE_PATH):
            if path.exists():
                try:
                    saved = json.loads(path.read_text())
                except ValueError:
                    continue  # corrupt/partial file — try the next fallback
                return {**_backtest_status, "result": saved, "saved": True}
    return _backtest_status


@app.get("/api/backtest/cases")
def backtest_cases():
    """
    Return the backtest case library: which companies are tested and counts.

    Reads the roster (Supabase `cases`, CSV fallback) via load_cases — not run
    results — so the library is visible before any backtest has been run.
    """
    from src.backtest import load_cases

    try:
        cases = load_cases()
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

    distressed = sum(1 for c in cases if (c.get("label") or "").strip() == "distressed")
    healthy = sum(1 for c in cases if (c.get("label") or "").strip() == "healthy")
    return {
        "total": len(cases),
        "distressed": distressed,
        "healthy": healthy,
        "cases": cases,
    }


# ── Case library CRUD ────────────────────────────────────────────────────────

def _slugify_case_id(name: str, ticker: str, event_date: str, label: str) -> str:
    """
    Build a stable case_id slug like "hertz-2020" (distressed) or "aapl" (healthy),
    mirroring the convention in data/cases.csv: distressed slugs append the event
    year; healthy controls are just the lowercased ticker (or name).
    """
    base = re.sub(r"[^a-z0-9]+", "-", (ticker or name).lower()).strip("-")
    if label == "distressed" and event_date:
        return f"{base}-{event_date[:4]}"
    return base


class AddCaseRequest(BaseModel):
    identifier: str                  # ticker (e.g. "BTU") or CIK (e.g. "1064728")
    label: str                       # "distressed" | "healthy"
    event_date: str | None = None    # "YYYY-MM-DD"; required for distressed
    notes: str | None = None
    case_id: str | None = None       # optional explicit slug; auto-generated when omitted


@app.post("/api/cases")
def create_case(req: AddCaseRequest):
    """
    Add a case to the backtest library.

    Resolves the identifier (ticker or CIK) to a canonical CIK + current name via
    EDGAR, validates the label, requires event_date for distressed cases (defaults
    healthy controls to a pinned anchor), auto-generates a case_id slug when
    omitted, and inserts the row. Returns the stored row (shape: BacktestCaseInfo).
    """
    label = req.label.strip().lower()
    if label not in ("distressed", "healthy"):
        raise HTTPException(400, "label must be 'distressed' or 'healthy'")

    event_date = (req.event_date or "").strip()
    if label == "distressed" and not event_date:
        raise HTTPException(
            400, "event_date (the Chapter 11 / credit-event date) is required for distressed cases"
        )
    if event_date:
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, f"Invalid event_date {event_date!r} — expected YYYY-MM-DD")
    # Healthy default: pin a stable far-future anchor so baselines stay reproducible
    # (matches the 2025-12-31 anchors already in cases.csv).
    if label == "healthy" and not event_date:
        event_date = "2025-12-31"

    identifier = req.identifier.strip()
    if not identifier:
        raise HTTPException(400, "identifier (ticker or CIK) is required")
    try:
        cik = resolve_identifier(identifier)
    except ValueError:
        raise HTTPException(404, f"{identifier!r} not found in SEC EDGAR")
    except Exception as e:
        raise HTTPException(500, f"EDGAR lookup failed: {e}")

    try:
        info = get_company_info(cik)
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch company info: {e}")

    company_name = info.get("name", "")
    ticker = info["tickers"][0] if info.get("tickers") else identifier.upper()

    case_id = (req.case_id or "").strip() or _slugify_case_id(company_name, ticker, event_date, label)
    if not case_id:
        raise HTTPException(400, "could not derive a case_id slug; pass case_id explicitly")

    # Explicit 409 rather than a silent upsert-overwrite, so the user knows the
    # slug/company is already in the library.
    if get_case(case_id) is not None:
        raise HTTPException(409, f"case_id {case_id!r} already exists in the library")

    return add_case({
        "case_id": case_id,
        "company_name": company_name,
        "ticker": ticker,
        "cik": cik,
        "label": label,
        "event_date": event_date,
        "notes": (req.notes or "").strip(),
    })


@app.delete("/api/cases/{case_id}")
def remove_case(case_id: str):
    """Delete one case from the backtest library by case_id."""
    if not delete_case(case_id):
        raise HTTPException(404, f"case_id {case_id!r} not found")
    return {"status": "deleted", "case_id": case_id}


# ── Scoring parameters (test in backtest vs. apply to portfolio) ─────────────

def _validate_config(raw: dict) -> dict:
    """
    Deep-merge `raw` over DEFAULT_CONFIG, enforce sane ranges, and return the
    normalized full config dict. Raises HTTPException(400) on bad input.

    The most important guard is healthy != severe per rule — _ramp divides by
    (severe - healthy), so equal values would be a division by zero.
    """
    if not isinstance(raw, dict):
        raise HTTPException(400, "config must be an object")

    unknown = set((raw.get("rules") or {}).keys()) - set(DEFAULT_CONFIG["rules"].keys())
    if unknown:
        raise HTTPException(400, f"unknown rule keys: {', '.join(sorted(unknown))}")

    try:
        cfg = ScoreConfig.from_dict(raw)
    except (TypeError, ValueError, KeyError) as e:
        raise HTTPException(400, f"invalid config: {e}")

    for key, r in cfg.rules.items():
        if not (0 <= r["weight"] <= 100):
            raise HTTPException(400, f"{key}.weight must be in [0, 100]")
        if r["healthy"] == r["severe"]:
            raise HTTPException(400, f"{key}: healthy and severe must differ (the ramp divides by their gap)")
    for key, v in cfg.ebitda_override.items():
        if not (0 <= v <= 100):
            raise HTTPException(400, f"ebitda_override.{key} must be in [0, 100]")
    for k in ("high_severity_per", "covenant_per", "provision_per"):
        if not (0 <= cfg.llm[k] <= 50):
            raise HTTPException(400, f"llm.{k} must be in [0, 50]")
    for k in ("high_severity_cap", "covenant_cap", "provision_cap", "combined_cap"):
        if not (0 <= cfg.llm[k] <= 100):
            raise HTTPException(400, f"llm.{k} must be in [0, 100]")
    if not (1 <= cfg.score_cap <= 100):
        raise HTTPException(400, "score_cap must be in [1, 100]")
    if not (1 <= cfg.escalation["min_severe"] <= 9):
        raise HTTPException(400, "escalation.min_severe must be an integer in [1, 9]")
    if not (0 < cfg.escalation["severe_frac"] <= 1):
        raise HTTPException(400, "escalation.severe_frac must be in (0, 1]")
    if not (0 <= cfg.escalation["floor"] <= 100):
        raise HTTPException(400, "escalation.floor must be in [0, 100]")
    if not (1 <= cfg.threshold <= 100):
        raise HTTPException(400, "threshold must be an integer in [1, 100]")

    return cfg.to_dict()


class ScoreConfigRequest(BaseModel):
    config: dict


@app.get("/api/score-config")
def read_score_config():
    """
    Return the active (applied) scoring parameters plus the built-in defaults.

    `active` drives the live portfolio/detail scores and seeds the editor;
    `defaults` powers the "Reset to defaults" button.
    """
    try:
        active = get_score_config()
    except Exception:
        active = DEFAULT_CONFIG
    return {
        "active": ScoreConfig.from_dict(active).to_dict(),
        "defaults": ScoreConfig.from_dict(DEFAULT_CONFIG).to_dict(),
    }


@app.put("/api/score-config")
def apply_score_config(req: ScoreConfigRequest):
    """
    Apply scoring parameters to the live portfolio: validate, persist as the
    active config, and invalidate the per-process cache. After this, the
    dashboard and detail pages recompute scores with these parameters (no
    re-track needed). This is the UI's "Apply to portfolio" action.
    """
    cfg = _validate_config(req.config)
    save_score_config(cfg)
    _invalidate_config_cache()
    return {"active": cfg}

