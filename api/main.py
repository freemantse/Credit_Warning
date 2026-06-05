"""
FastAPI backend for the Credit Warning System.

All routes use the /api/ prefix so they work both:
  - Locally: uvicorn api.main:app --port 8000  →  http://localhost:8000/api/...
  - Vercel:  vercel.json routes /api/* → this function  →  /api/...

Run locally: python3 -m uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_backtest_status: dict[str, Any] = {
    "running": False,
    "result": None,
    "error": None,
}


class TrackRequest(BaseModel):
    ticker: str
    no_llm: bool = True
    periods: int = 15


def _to_ratio_results(full_ratios: dict, period_end: str) -> dict[str, RatioResult]:
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


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/issuers")
def list_issuers():
    tickers = get_issuers()
    result = []
    for ticker in tickers:
        periods = get_periods(ticker)
        if not periods:
            continue
        latest = periods[-1]
        full_ratios = get_full_ratios(ticker, latest)
        findings = get_findings(ticker, latest)
        ratio_results = _to_ratio_results(full_ratios, latest)
        finding_objs = _finding_objects(findings)
        score_result = compute_score(ratio_results, finding_objs)

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
    ticker = req.ticker.upper().strip()
    try:
        cik = get_cik(ticker)
    except ValueError:
        raise HTTPException(404, f"Ticker {ticker!r} not found in SEC EDGAR")
    except Exception as e:
        raise HTTPException(500, f"EDGAR lookup failed: {e}")

    try:
        facts = get_company_facts(cik)
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch company facts: {e}")

    available = _get_available_periods(facts)
    periods = available[: req.periods]
    if not periods:
        raise HTTPException(404, f"No annual XBRL periods found for {ticker}")

    saved = 0
    for period in periods:
        results = extract_all(facts, period)
        save_ratios(ticker, period, results)

        if not req.no_llm:
            try:
                filings = get_filings(cik, ["10-K"])
                matching = [f for f in filings if period[:4] in f["filingDate"]]
                if matching:
                    filing = matching[0]
                    text = get_filing_text(
                        cik, filing["accessionNumber"], filing["primaryDocument"]
                    )
                    from src.llm_review import review_text
                    findings_list = review_text(text[:12000], f"10-K {period}")
                    save_findings(ticker, period, findings_list)
            except Exception:
                pass
        saved += 1

    return {"ticker": ticker, "periods_saved": saved, "periods": periods}


@app.get("/api/issuer/{ticker}")
def get_issuer(ticker: str):
    ticker = ticker.upper()
    periods = get_periods(ticker)
    if not periods:
        raise HTTPException(404, f"{ticker} is not tracked. POST /api/track first.")

    period_data = []
    for period in reversed(periods):
        full_ratios = get_full_ratios(ticker, period)
        findings = get_findings(ticker, period)
        ratio_results = _to_ratio_results(full_ratios, period)
        finding_objs = _finding_objects(findings)
        score_result = compute_score(ratio_results, finding_objs)

        period_data.append({
            "period_end": period,
            "ratios": full_ratios,
            "score": score_result.score,
            "breakdown": score_result.breakdown,
            "alerts": score_result.alerts,
            "findings": findings,
        })

    return {"ticker": ticker, "periods": period_data}


@app.delete("/api/issuer/{ticker}")
def remove_issuer(ticker: str):
    ticker = ticker.upper()
    delete_issuer(ticker)
    return {"status": "deleted", "ticker": ticker}


def _run_backtest_task():
    try:
        from src.backtest import run_backtest
        result = run_backtest()
        _backtest_status["result"] = result
        _backtest_status["error"] = None
    except Exception as e:
        _backtest_status["error"] = str(e)
        _backtest_status["result"] = None
    finally:
        _backtest_status["running"] = False


@app.post("/api/backtest")
def start_backtest(background_tasks: BackgroundTasks):
    if _backtest_status["running"]:
        raise HTTPException(409, "Backtest already running")
    _backtest_status["running"] = True
    _backtest_status["result"] = None
    _backtest_status["error"] = None
    background_tasks.add_task(_run_backtest_task)
    return {"status": "started"}


@app.get("/api/backtest/status")
def backtest_status():
    return _backtest_status
