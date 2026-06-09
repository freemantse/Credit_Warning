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

import sys
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
    get_company_facts,
    get_company_info,
    get_filings,
    get_filing_text,
    resolve_identifier,
)
from src.score import compute_score
from src.store import (
    delete_issuer,
    get_cik_by_ticker,
    get_company,
    get_covenants_grouped,
    get_findings_grouped,
    get_issuers,
    get_loss_provisions_grouped,
    get_maturities_grouped,
    get_ratios_grouped,
    save_company,
    save_covenants,
    save_findings,
    save_loss_provisions,
    save_maturities_bulk,
    save_ratios_bulk,
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


def _finding_objects(findings: list[dict]):
    # Convert raw dicts from Supabase into Finding dataclass instances
    # so compute_score can call getattr(f, "severity") on them.
    from src.llm_review import Finding
    return [
        Finding(
            concern=f["concern"],
            severity=f["severity"],
            evidence_quote=f["evidence_quote"],
            source=f["source"],
        )
        for f in findings
    ]


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
        finding_objs = _finding_objects(findings)
        score_result = compute_score(
            ratio_results, finding_objs, maturity, covenants, provisions
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

    # Step 6: Optional LLM review per period (slow; disabled by default). Runs the
    # MD&A qualitative pass plus the footnote pass (locates the debt &
    # contingencies sections and extracts covenants + loss provisions).
    if not req.no_llm:
        for period in periods:
            try:
                filings = get_filings(cik, ["10-K"])
                matching = [f for f in filings if period[:4] in f["filingDate"]]
                if matching:
                    filing = matching[0]
                    text = get_filing_text(
                        cik, filing["accessionNumber"], filing["primaryDocument"]
                    )
                    from src.llm_review import review_text
                    # Trim to 12 000 chars — enough for MD&A; avoids token-limit errors.
                    findings_list = review_text(text[:12000], f"10-K {period}")
                    save_findings(cik, period, findings_list)

                    from src.footnote_review import review_filing_footnotes
                    covenants, provisions = review_filing_footnotes(cik, period, filings)
                    save_covenants(cik, period, covenants)
                    save_loss_provisions(cik, period, provisions)
            except Exception:
                # LLM review is best-effort; ratio/maturity data has already been saved.
                pass

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

    period_data = []
    # Newest period first so the frontend chart/table show recent data at the top.
    for period in sorted(ratios_by_period, reverse=True):
        full_ratios = ratios_by_period[period]
        findings = findings_by_period.get(period, [])
        maturity = maturities_by_period.get(period)          # dict or None
        covenants = covenants_by_period.get(period, [])      # list of dicts
        provisions = provisions_by_period.get(period, [])    # list of dicts
        ratio_results = _to_ratio_results(full_ratios, period)
        finding_objs = _finding_objects(findings)
        # compute_score accepts dicts for maturity/covenants/provisions (see _attr).
        score_result = compute_score(
            ratio_results, finding_objs, maturity, covenants, provisions
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


# ── Backtest (long-running background task) ──────────────────────────────────

def _run_backtest_task():
    """
    Worker function executed in a FastAPI BackgroundTask.
    Runs the full backtest and writes the result (or error) into _backtest_status.
    The import is deferred to avoid loading backtest deps on every server start.
    """
    try:
        from src.backtest import run_backtest
        result = run_backtest()
        _backtest_status["result"] = result
        _backtest_status["error"] = None
    except Exception as e:
        _backtest_status["error"] = str(e)
        _backtest_status["result"] = None
    finally:
        # Always clear the running flag so the UI can re-trigger if needed.
        _backtest_status["running"] = False


@app.post("/api/backtest")
def start_backtest(background_tasks: BackgroundTasks):
    """
    Start the backtest as a background task and return immediately.
    The frontend polls /api/backtest/status every 3 s to detect completion.
    Returns 409 if a backtest is already running.
    """
    if _backtest_status["running"]:
        raise HTTPException(409, "Backtest already running")
    _backtest_status["running"] = True
    _backtest_status["result"] = None
    _backtest_status["error"] = None
    background_tasks.add_task(_run_backtest_task)
    return {"status": "started"}


@app.get("/api/backtest/status")
def backtest_status():
    """Return the current backtest state: running flag, result dict, or error string."""
    return _backtest_status
