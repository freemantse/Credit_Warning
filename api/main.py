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

from src.concepts import MissingDataError
from src.extract import RatioResult, _get_available_periods, extract_all
from src.ingest import get_cik, get_company_facts, get_filings, get_filing_text
from src.score import STRESS_THRESHOLD, compute_score
from src.store import (
    delete_issuer,
    get_findings,
    get_full_ratios,
    get_issuers,
    get_periods,
    save_findings,
    save_ratios,
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
    no_llm: bool = True  # LLM pass is slow (~30 s/filing); UI always omits it for responsiveness
    periods: int = 15    # how many annual fiscal-year periods to fetch and store


# ── Helper: reconstruct typed objects from raw Supabase rows ────────────────

def _to_ratio_results(full_ratios: dict, period_end: str) -> dict[str, RatioResult]:
    # Reconstruct RatioResult objects from Supabase rows so compute_score can consume them.
    # Supabase returns plain dicts; compute_score expects dataclass instances.
    return {
        name: RatioResult(
            name=name,
            value=data["value"],
            inputs=data.get("inputs", {}),
            source_tags=data.get("source_tags", {}),
            period_end=period_end,
        )
        for name, data in full_ratios.items()
    }


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
    tickers = get_issuers()
    result = []
    for ticker in tickers:
        periods = get_periods(ticker)
        if not periods:
            # Ticker exists in DB but has no stored ratios — skip rather than error.
            continue

        # periods is sorted ascending; the latest period is the last element.
        latest = periods[-1]
        full_ratios = get_full_ratios(ticker, latest)
        findings = get_findings(ticker, latest)

        # Re-score from stored data so the summary is always consistent with
        # the detail page (both call compute_score with the same inputs).
        ratio_results = _to_ratio_results(full_ratios, latest)
        finding_objs = _finding_objects(findings)
        score_result = compute_score(ratio_results, finding_objs)

        # Helper to safely pull a ratio value without KeyError on missing data.
        def _v(name: str) -> float | None:
            r = full_ratios.get(name)
            return r["value"] if r else None

        result.append({
            "ticker": ticker,
            "latest_period": latest,
            "period_count": len(periods),
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
    ticker = req.ticker.upper().strip()

    # Step 1: Resolve ticker → CIK (SEC's internal company identifier).
    try:
        cik = get_cik(ticker)
    except ValueError:
        raise HTTPException(404, f"Ticker {ticker!r} not found in SEC EDGAR")
    except Exception as e:
        raise HTTPException(500, f"EDGAR lookup failed: {e}")

    # Step 2: Fetch the full XBRL company facts JSON (cached to disk after first fetch).
    try:
        facts = get_company_facts(cik)
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch company facts: {e}")

    # Step 3: Determine which annual periods are available and take the most recent N.
    available = _get_available_periods(facts)
    periods = available[: req.periods]
    if not periods:
        raise HTTPException(404, f"No annual XBRL periods found for {ticker}")

    # Step 4: Extract ratios and optionally run LLM review for each period.
    saved = 0
    for period in periods:
        results = extract_all(facts, period)
        save_ratios(ticker, period, results)

        if not req.no_llm:
            try:
                filings = get_filings(cik, ["10-K"])
                # Match by year only — filing dates don't align exactly with period_end.
                matching = [f for f in filings if period[:4] in f["filingDate"]]
                if matching:
                    filing = matching[0]
                    text = get_filing_text(
                        cik, filing["accessionNumber"], filing["primaryDocument"]
                    )
                    from src.llm_review import review_text
                    # Trim to 12 000 chars — enough for MD&A; avoids token-limit errors.
                    findings_list = review_text(text[:12000], f"10-K {period}")
                    save_findings(ticker, period, findings_list)
            except Exception:
                # LLM review is best-effort; ratio data has already been saved.
                pass
        saved += 1

    return {"ticker": ticker, "periods_saved": saved, "periods": periods}


@app.get("/api/issuer/{ticker}")
def get_issuer(ticker: str):
    """
    Return full ratio history and stress scores for a single issuer.

    Periods are returned newest-first so the frontend chart can show recent
    trends at the top without client-side sorting.
    """
    ticker = ticker.upper()
    periods = get_periods(ticker)
    if not periods:
        raise HTTPException(404, f"{ticker} is not tracked. POST /api/track first.")

    period_data = []
    # reversed() because get_periods() returns ascending; we want newest first.
    for period in reversed(periods):
        full_ratios = get_full_ratios(ticker, period)
        findings = get_findings(ticker, period)
        ratio_results = _to_ratio_results(full_ratios, period)
        finding_objs = _finding_objects(findings)
        score_result = compute_score(ratio_results, finding_objs)

        period_data.append({
            "period_end": period,
            "ratios": full_ratios,          # full dict with value + inputs + source_tags
            "score": score_result.score,
            "breakdown": score_result.breakdown,   # per-component point contributions
            "alerts": score_result.alerts,
            "findings": findings,           # LLM qualitative findings (may be empty)
        })

    return {"ticker": ticker, "periods": period_data}


@app.delete("/api/issuer/{ticker}")
def remove_issuer(ticker: str):
    """Delete all stored ratios and findings for a ticker."""
    ticker = ticker.upper()
    delete_issuer(ticker)
    return {"status": "deleted", "ticker": ticker}


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
