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
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
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
from src.rating import compute_implied_rating, rating_outlook, OUTLOOK_DEFAULT, RatingOutlookResult, RATING_SCALE
from src.ratings.labels import rating_asof
from src.store import (
    add_case,
    delete_case,
    delete_issuer,
    get_agency_ratings_grouped,
    get_case,
    get_cik_by_ticker,
    get_company,
    get_bond_instruments_grouped,
    get_covenants_grouped,
    get_findings_grouped,
    get_implied_ratings_grouped,
    get_issuers,
    get_loss_provisions_grouped,
    get_migration_predictions_grouped,
    get_maturities_grouped,
    get_model_registry,
    get_ratios_grouped,
    get_score_config,
    list_cases,
    save_model_registry,
    save_bond_instruments,
    save_company,
    save_covenants,
    save_findings,
    save_implied_ratings_bulk,
    save_loss_provisions,
    save_maturities_bulk,
    save_ratios_bulk,
    save_score_config,
    touch_last_refreshed,
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

def _active_config() -> ScoreConfig:
    """
    Return the active ScoreConfig — the model-LEARNED stress-score weights when a
    model has been trained (persisted to score_config by the trainer), else the
    built-in DEFAULT_CONFIG.

    Read once per request (no process cache): the trainer writes the config offline,
    so a cached value would hide newly learned weights until a redeploy. Resilient
    to Supabase being unavailable → DEFAULT_CONFIG.
    """
    try:
        return ScoreConfig.from_dict(get_score_config())
    except Exception:
        return ScoreConfig.from_dict(DEFAULT_CONFIG)


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


def _outlook_payload(res: RatingOutlookResult | None) -> dict | None:
    """Shape a RatingOutlookResult into the JSON dict the frontend consumes (or None)."""
    if res is None:
        return None
    return {
        "outlook": res.outlook,
        "trend_pressure": res.trend_pressure,
        "gap_pressure": res.gap_pressure,
        "gap": res.gap,
        "rating_change": res.rating_change,
        "score_change": res.score_change,
        "reasons": res.reasons,
        "periods_used": res.periods_used,
    }


# ── Prediction "why" summary (shared by portfolio + issuer detail) ───────────

# Friendly labels for the model's feature drivers / ratios.
_RATIO_LABEL = {
    "leverage": "leverage", "interest_coverage": "interest coverage",
    "free_cash_flow": "free cash flow", "fcf_margin": "FCF margin",
    "ebitda_margin": "EBITDA margin", "liquidity": "liquidity",
    "cash_flow_to_debt": "cash-flow-to-debt", "debt_to_assets": "debt/assets",
    "maturity_near_term_pct": "near-term maturities",
    "implied_rating_index": "implied rating", "stress_score": "stress score",
    "financial_risk_index": "financial-risk profile",
    "agency_rating_index": "agency rating", "implied_vs_agency_gap": "implied-vs-agency gap",
    "time_in_rating_months": "time in rating",
}


def _ratio_values(period_ratios: dict | None) -> dict[str, float]:
    """Flatten a stored {name: {value, …}} period dict to {name: value} (numbers only)."""
    out: dict[str, float] = {}
    for name, data in (period_ratios or {}).items():
        v = data.get("value") if isinstance(data, dict) else None
        if isinstance(v, (int, float)):
            out[name] = float(v)
    return out


def _driver_phrase(driver: dict, ratios_now: dict, ratios_prev: dict) -> str:
    """
    One human phrase for a model driver, preferring the actual YoY ratio move
    ("leverage rose 40% YoY") and falling back to the driver's direction.
    """
    feat = driver.get("feature", "")
    base = feat[:-4] if feat.endswith("_yoy") else feat
    label = _RATIO_LABEL.get(base, base.replace("_", " "))
    now, prev = ratios_now.get(base), ratios_prev.get(base)
    if isinstance(now, (int, float)) and isinstance(prev, (int, float)) and prev not in (0, None):
        pct = (now - prev) / abs(prev) * 100
        if abs(pct) >= 1:
            return f"{label} {'rose' if pct > 0 else 'fell'} {abs(pct):.0f}% YoY"
    return f"{'rising' if driver.get('contribution', 0) > 0 else 'easing'} {label}"


def _driver_pct_change(driver: dict, ratios_now: dict, ratios_prev: dict) -> float | None:
    """
    Year-over-year % move of a driver's underlying ratio (now vs the prior period),
    so the UI can show "(↑ 20%)" beside each driver. Mirrors _driver_phrase's move
    exactly, so the per-driver badge agrees with the reason text. None when the
    feature isn't a tracked ratio or the prior value is missing/zero.
    """
    feat = driver.get("feature", "")
    base = feat[:-4] if feat.endswith("_yoy") else feat
    now, prev = ratios_now.get(base), ratios_prev.get(base)
    if isinstance(now, (int, float)) and isinstance(prev, (int, float)) and prev not in (0, None):
        return round((now - prev) / abs(prev) * 100, 1)
    return None


def _prediction_summary(pred_row: dict | None, outlook, ratios_now: dict, ratios_prev: dict) -> dict | None:
    """
    Unify the directional "rating change" signal + a short "why" for one issuer.

    Prefers the trained model's calibrated prediction (direction from P(down) vs
    P(up) with a deadband; reason from the top drivers); falls back to the
    rule-based Rating Outlook (direction + its first reason sentence). None when
    neither exists.
    """
    if pred_row and (pred_row.get("p_downgrade") is not None or pred_row.get("p_upgrade") is not None):
        pd_ = pred_row.get("p_downgrade") or 0.0
        pu_ = pred_row.get("p_upgrade") or 0.0
        direction = "down" if pd_ - pu_ > 0.05 else "up" if pu_ - pd_ > 0.05 else "stable"
        phrases = [_driver_phrase(d, ratios_now, ratios_prev)
                   for d in (pred_row.get("drivers_json") or [])[:2]]
        return {
            "direction": direction,
            "p_downgrade": pred_row.get("p_downgrade"),
            "p_upgrade": pred_row.get("p_upgrade"),
            "source": "model",
            "reason": "; ".join(phrases) if phrases else "model prediction",
        }
    if outlook is not None:
        o = outlook.outlook
        direction = "down" if o == "Negative" else "up" if o == "Positive" else "stable"
        return {
            "direction": direction,
            "p_downgrade": None,
            "p_upgrade": None,
            "source": "outlook",
            "reason": outlook.reasons[0] if outlook.reasons else o,
        }
    return None


# ── Agency-rating helpers (issuer detail overlay + history) ──────────────────

# Display priority when an issuer carries more than one agency series, used only to
# break ties — the richest (most-events) series is preferred for the chart overlay.
_AGENCY_PRIORITY = {"MDY": 0, "SPI": 1, "FTC": 2, "EJR": 3}


def _rating_letter(idx: int | None) -> str | None:
    """Map a rating_index back to its notation (AAA…D), clamped; None when idx is None."""
    if idx is None:
        return None
    i = max(0, min(len(RATING_SCALE) - 1, int(round(idx))))
    return RATING_SCALE[i]


def _primary_agency(agency_grouped: dict[str, list[dict]]) -> str | None:
    """
    Pick the agency whose series to overlay on the implied-rating chart: the one with
    the most rating actions, ties broken by agency authority (Moody's → S&P → Fitch →
    Egan-Jones). None when the issuer has no agency history yet.
    """
    if not agency_grouped:
        return None
    return max(
        agency_grouped,
        key=lambda a: (len(agency_grouped[a]), -_AGENCY_PRIORITY.get(a, 9)),
    )


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
    ratings_by_cik = get_implied_ratings_grouped()     # 1 query for every issuer's implied ratings
    predictions_by_cik = get_migration_predictions_grouped()  # resilient → {} if untrained/undeployed
    active = _active_config()                           # model-learned (or default) weights, read once

    result = []
    for issuer in issuers:
        cik = issuer["cik"]
        by_period = ratios_by_cik.get(cik, {})
        if not by_period:
            # Company tracked but no ratios computed yet (e.g. bank with non-standard
            # XBRL, or tracking failed mid-way). Show it in the list so the user can
            # see it and delete it — just with all-null metrics.
            result.append({
                "cik": cik,
                "ticker": issuer["ticker"],
                "name": issuer["name"],
                "latest_period": None,
                "period_count": 0,
                "ebitda_margin": None,
                "leverage": None,
                "interest_coverage": None,
                "free_cash_flow": None,
                "fcf_margin": None,
                "liquidity": None,
                "cash_flow_to_debt": None,
                "debt_to_assets": None,
                "score": None,
                "alerts": [],
                "implied_rating": None,
                "rating_index": None,
                "outlook": None,
                "prediction": None,
            })
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
            config=active,
        )

        # Helper to safely pull a ratio value without KeyError on missing data.
        def _v(name: str) -> float | None:
            r = full_ratios.get(name)
            return r["value"] if r else None

        # Latest-period implied rating (None when it couldn't be computed).
        cik_ratings = ratings_by_cik.get(cik, {})
        rating = cik_ratings.get(latest)

        # Rating Outlook: build the recent (period → rating_index, score) series and
        # derive the directional signal. Scores for the window are recomputed from
        # already-loaded ratios (compute_score is pure/fast) so portfolio and detail
        # outlooks use identical inputs. Agency gap is None until Stage 1 data lands.
        outlook_series = []
        for per in periods[-OUTLOOK_DEFAULT.window:]:
            rr = _to_ratio_results(by_period[per], per)
            sc = compute_score(
                rr,
                findings_by_cik.get(cik, {}).get(per, []),
                maturities_by_cik.get(cik, {}).get(per),
                covenants_by_cik.get(cik, {}).get(per, []),
                provisions_by_cik.get(cik, {}).get(per, []),
                config=active,
            )
            outlook_series.append({
                "period_end": per,
                "rating_index": (cik_ratings.get(per) or {}).get("rating_index"),
                "score": sc.score,
            })
        outlook = rating_outlook(outlook_series)

        # Directional "rating change" prediction (model if trained, else outlook),
        # with a short "why" from the latest vs. prior period's ratios.
        prior_period = periods[-2] if len(periods) >= 2 else None
        prediction = _prediction_summary(
            predictions_by_cik.get(cik, {}).get(latest),
            outlook,
            _ratio_values(full_ratios),
            _ratio_values(by_period.get(prior_period) if prior_period else None),
        )

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
            "score": score_result.score,
            "alerts": score_result.alerts,
            "implied_rating": rating["implied_rating"] if rating else None,
            "rating_index": rating["rating_index"] if rating else None,
            "outlook": outlook.outlook if outlook else None,
            "prediction": prediction,
        })
    return result


def _track_one(identifier: str, *, no_llm: bool = True, periods: int | None = None) -> dict:
    """
    Core track pipeline: resolve a ticker-or-CIK to a CIK, fetch XBRL facts from
    EDGAR, extract ratios + debt maturities for each available annual period, and
    persist them to Supabase. Optionally runs the (slow) LLM qualitative review.

    Shared by the POST /api/track route and the auto-refresh cron so both go
    through exactly the same ingestion path. Raises HTTPException on failure.
    """
    identifier = identifier.upper().strip()

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
    periods = available if periods is None else available[: periods]
    if not periods:
        raise HTTPException(404, f"No annual XBRL periods found for {identifier}")

    # Step 5: Extract ratios for every period (pure compute, instant) and persist
    # them in a SINGLE bulk upsert. Writing one period at a time was ~18 separate
    # DB round-trips — the dominant cost of a track — so we batch them all here.
    results_by_period = {period: extract_all(facts, period) for period in periods}
    save_ratios_bulk(cik, results_by_period)

    # Step 5a2: Implied credit ratings are pure compute over the ratios just
    # extracted (no extra EDGAR round-trip), so derive and bulk-save them here.
    # Periods whose rating can't be computed (too few sub-factors) are omitted.
    ratings_by_period = {
        period: r
        for period in periods
        if (r := compute_implied_rating(results_by_period[period])) is not None
    }
    save_implied_ratings_bulk(cik, ratings_by_period)

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
    if not no_llm:
        filings = get_filings(cik, ["10-K"])
        for period in periods:
            try:
                from src.footnote_review import review_filing
                findings_list, covenants, provisions, instruments = review_filing(
                    cik, period, filings
                )
                save_findings(cik, period, findings_list)
                save_covenants(cik, period, covenants)
                save_loss_provisions(cik, period, provisions)
                save_bond_instruments(cik, period, instruments)
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


@app.post("/api/track")
def track_issuer(req: TrackRequest):
    """
    Resolve a ticker to a CIK, fetch XBRL facts from EDGAR, extract ratios for
    each available annual period, and persist them to Supabase.

    Optionally runs an LLM qualitative review of the 10-K MD&A text for each
    period if no_llm=False (slow; disabled by default in the UI). Thin wrapper
    around _track_one, which is also used by the auto-refresh cron.
    """
    return _track_one(req.ticker, no_llm=req.no_llm, periods=req.periods)


# Number of seconds into the run after which the cron stops *starting* new
# issuers. Kept under vercel.json's maxDuration (60s) with headroom for the
# in-flight issuer to finish. Leftover issuers roll to the next run (they sort
# last by last_refreshed), so coverage rotates across runs.
_CRON_TIME_BUDGET_SEC = 50


@app.get("/api/cron/refresh-all")
def cron_refresh_all(authorization: str = Header(default="")):
    """
    Re-track every portfolio issuer from EDGAR so newly-filed 10-Ks flow into
    history automatically. Invoked daily by a Vercel Cron job (see vercel.json).

    Auth: Vercel sends `Authorization: Bearer ${CRON_SECRET}` on cron calls when
    a CRON_SECRET env var is set. We require it so the endpoint isn't publicly
    abusable. If CRON_SECRET is unset (e.g. local dev without the var), auth is
    skipped — set it in production.

    Issuers are processed oldest-refreshed-first (NULLs, i.e. never-refreshed,
    first) within a wall-clock budget so a single run never trips the function
    timeout; any issuers not reached are picked up on the next run. The LLM pass
    is always skipped here (no_llm=True) — only deterministic ratio + maturity
    data is refreshed.
    """
    secret = os.environ.get("CRON_SECRET")
    if secret and authorization != f"Bearer {secret}":
        raise HTTPException(401, "Unauthorized")

    # Oldest-refreshed-first; None (never refreshed) sorts before any timestamp.
    issuers = sorted(
        get_issuers(), key=lambda i: (i.get("last_refreshed") is not None, i.get("last_refreshed") or "")
    )

    start = datetime.now()
    refreshed: list[dict] = []
    skipped_for_time: list[str] = []
    errors: list[dict] = []

    for issuer in issuers:
        cik = issuer["cik"]
        if (datetime.now() - start).total_seconds() >= _CRON_TIME_BUDGET_SEC:
            skipped_for_time.append(cik)
            continue
        try:
            result = _track_one(cik, no_llm=True)
            touch_last_refreshed(cik)
            refreshed.append({"cik": cik, "periods_saved": result["periods_saved"]})
        except Exception as e:
            # One bad issuer must not abort the whole run.
            errors.append({"cik": cik, "error": str(e)})
            logging.warning("Cron refresh failed for %s", cik, exc_info=True)

    summary = {
        "total": len(issuers),
        "refreshed": refreshed,
        "skipped_for_time": skipped_for_time,
        "errors": errors,
    }
    logging.info(
        "Cron refresh-all: %d refreshed, %d skipped (time), %d errors of %d total",
        len(refreshed), len(skipped_for_time), len(errors), len(issuers),
    )
    return summary


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
    ratings_by_period = get_implied_ratings_grouped(cik).get(cik, {})
    instruments_by_period = get_bond_instruments_grouped(cik).get(cik, {})
    # Migration predictions are optional (Stage 3 / may be undeployed) — the grouped
    # reader returns {} if the table is absent, so this never fails the issuer load.
    predictions_by_period = get_migration_predictions_grouped(cik).get(cik, {})

    # Agency-rating history (real Moody's/Fitch/Egan-Jones trajectories), grouped by
    # agency, each sorted ascending by effective_date. The "primary" agency (richest
    # series) is overlaid on the implied-rating chart and supplies the per-period
    # as-of agency rating; empty {} until Stage 1 ratings are ingested.
    agency_by_agency = get_agency_ratings_grouped(cik).get(cik, {})
    primary_agency = _primary_agency(agency_by_agency)
    primary_events = agency_by_agency.get(primary_agency, []) if primary_agency else []

    # Per-period source link: the public SEC EDGAR URL of the 10-K each period's
    # ratios were extracted from, so an analyst can trace any figure on the audit
    # card back to the filing. Best-effort — the 10-K list comes from the cached
    # submissions JSON; if EDGAR is unreachable on a cold instance we just omit
    # the link rather than fail the whole issuer load.
    try:
        filings = get_filings(cik, ["10-K"])
    except Exception:
        filings = []

    active = _active_config()  # model-learned (or default) weights, read once
    _periods_asc = sorted(ratios_by_period)  # for prior-period lookup (YoY "why")
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
            config=active,
        )

        # Match this period to its 10-K and build the EDGAR document URL (None if
        # no filing matches or the list couldn't be fetched).
        filing = find_filing_for_period(filings, period) if filings else None
        source_url = (
            filing_doc_url(cik, filing["accessionNumber"], filing["primaryDocument"])
            if filing
            else None
        )

        # Migration prediction (if any) enriched with a plain-language "why" derived
        # from this period's drivers vs. the prior period's ratios.
        migration = predictions_by_period.get(period)
        if migration:
            _i = _periods_asc.index(period)
            _prior = _periods_asc[_i - 1] if _i > 0 else None
            _rn = _ratio_values(full_ratios)
            _rp = _ratio_values(ratios_by_period.get(_prior) if _prior else None)
            summary = _prediction_summary(migration, None, _rn, _rp)
            # Tag each driver with its YoY % move (None when not a tracked ratio),
            # so the UI can show "(↑ 20%)" beside it and explain why it's a driver.
            _drivers = [{**d, "pct_change": _driver_pct_change(d, _rn, _rp)}
                        for d in (migration.get("drivers_json") or [])]
            migration = {**migration, "drivers_json": _drivers}
            if summary:
                migration = {**migration, "reason": summary["reason"],
                             "direction": summary["direction"], "source": summary["source"]}

        # As-of agency rating of the primary agency at this period_end — overlaid on
        # the implied-rating chart so the implied-vs-agency gap is visible. None until
        # agency ratings are ingested (or before the issuer's first rated date).
        ag_idx, _ag_status = rating_asof(primary_events, period) if primary_events else (None, None)

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
            "implied_rating": ratings_by_period.get(period),  # rating dict or None
            "bond_instruments": instruments_by_period.get(period, []),  # LLM-extracted (may be empty)
            "migration": migration,         # calibrated P(down)/P(up)/P(default) + drivers + reason, or None
            "agency_rating_index": ag_idx,            # primary agency's rating as-of this period (or None)
            "agency_rating": _rating_letter(ag_idx),  # …as a notation letter
        })

    # Rating Outlook: directional signal from the full (rating_index, score) history.
    # Agency gap is None until Stage 1 ratings data is ingested.
    outlook_series = [
        {
            "period_end": p["period_end"],
            "rating_index": (p["implied_rating"] or {}).get("rating_index"),
            "score": p["score"],
        }
        for p in period_data
    ]
    outlook = rating_outlook(outlook_series)

    # Issuer-level directional prediction (model when trained, else the rule-based
    # outlook) for the headline banner — mirrors the portfolio's per-row prediction.
    latest = _periods_asc[-1] if _periods_asc else None
    prior = _periods_asc[-2] if len(_periods_asc) >= 2 else None
    prediction = _prediction_summary(
        predictions_by_period.get(latest) if latest else None,
        outlook,
        _ratio_values(ratios_by_period.get(latest) if latest else None),
        _ratio_values(ratios_by_period.get(prior) if prior else None),
    )

    # Issuer-level agency-rating history (per agency: the dated rating actions), for
    # the Agency Rating History section. Empty {} when no agency data is ingested.
    agency_ratings = {
        ag: [
            {
                "effective_date": e["effective_date"],
                "rating_index": e.get("rating_index"),
                "rating_raw": e.get("rating_raw"),
                "rating_status": e.get("rating_status"),
                "rating_action": e.get("rating_action"),
            }
            for e in evs
        ]
        for ag, evs in agency_by_agency.items()
    }

    tickers = company.get("tickers") or []
    return {
        "cik": cik,
        "ticker": tickers[0] if tickers else ticker.upper(),
        "name": company.get("name", ""),
        "periods": period_data,
        "outlook": _outlook_payload(outlook),
        "prediction": prediction,            # headline directional signal (model/outlook)
        "agency_ratings": agency_ratings,    # per-agency dated rating actions
        "primary_agency": primary_agency,    # the agency overlaid on the chart (or None)
    }


@app.delete("/api/issuer/{ticker}")
def remove_issuer(ticker: str):
    """Delete all stored data for a company (resolved from ticker or CIK)."""
    cik = _resolve_cik_for_read(ticker)
    delete_issuer(cik)
    return {"status": "deleted", "cik": cik}


@app.get("/api/screen/senior-secured")
def screen_senior_secured_route(
    min_rating: str = "BBB-",
    exclude_negative: bool = True,
):
    """
    The senior-secured screen: senior-secured bond instruments of credit-healthy,
    not-deteriorating issuers, ranked best-first.

    Query params:
      - min_rating: issuer must be at least this healthy (default "BBB-", the IG floor).
      - exclude_negative: drop issuers whose Rating Outlook is Negative (default true).

    Returns {meta, rows}. Empty rows until issuers are tracked WITH the LLM pass
    (bond-instrument extraction runs in the LLM review, off by default), so the page
    explains how to populate it.
    """
    from src.screen import screen_senior_secured
    try:
        return screen_senior_secured(
            min_rating=min_rating, exclude_negative_outlook=exclude_negative
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


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
                findings_list, covenants, provisions, instruments = review_filing(cik, period, filings)
                save_findings(cik, period, findings_list)
                save_covenants(cik, period, covenants)
                save_loss_provisions(cik, period, provisions)
                save_bond_instruments(cik, period, instruments)
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

def _run_backtest_task(steps: int):
    """
    Worker function executed in a FastAPI BackgroundTask.
    Runs the full backtest and writes the result (or error) into _backtest_status.
    The import is deferred to avoid loading backtest deps on every server start.

    The run uses the ACTIVE stress-score config (the model-learned weights when a
    model has been trained, else DEFAULT_CONFIG) — the same config the dashboard
    scores with. There is no longer a transient/UI-tuned config.
    """
    try:
        from src.backtest import run_backtest
        result = run_backtest(steps=steps, config=_active_config())
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


@app.post("/api/backtest")
def start_backtest(background_tasks: BackgroundTasks, req: BacktestRequest | None = None):
    """
    Start the stress-score backtest as a background task and return immediately.
    The frontend polls /api/backtest/status every 3 s to detect completion.
    Returns 409 if a backtest is already running.

    Accepts an optional body: {"steps": N} — point-in-time history depth, clamped to
    [_MIN_STEPS, _MAX_STEPS]. The run always uses the active stress-score config.
    """
    if _backtest_status["running"]:
        raise HTTPException(409, "Backtest already running")

    from src.backtest import DEFAULT_STEPS
    steps = req.steps if (req and req.steps is not None) else DEFAULT_STEPS
    steps = max(_MIN_STEPS, min(_MAX_STEPS, steps))

    _backtest_status["running"] = True
    _backtest_status["result"] = None
    _backtest_status["error"] = None
    background_tasks.add_task(_run_backtest_task, steps)
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


_EVENT_TYPES = ("downgrade", "upgrade", "default", "control")


class AddCaseRequest(BaseModel):
    identifier: str                  # ticker (e.g. "BTU") or CIK (e.g. "1064728")
    # The rating-event type the backtest checks the model catches early. `label`
    # (distressed|healthy) is still accepted for back-compat and, when event_type is
    # omitted, mapped: distressed→default, healthy→control.
    event_type: str | None = None    # downgrade | upgrade | default | control
    label: str | None = None
    agency: str | None = None        # MDY|FTC|SPI|EJR for a rating-migration event (optional)
    event_date: str | None = None    # "YYYY-MM-DD"; required for non-control events
    notes: str | None = None
    case_id: str | None = None       # optional explicit slug; auto-generated when omitted


@app.post("/api/cases")
def create_case(req: AddCaseRequest):
    """
    Add a case to the unified rating-event backtest library.

    Resolves the identifier (ticker or CIK) to a canonical CIK + current name via
    EDGAR; an `event_type` of downgrade/upgrade/default needs an event_date, while a
    `control` defaults to a pinned anchor. The legacy `label` is derived from the
    event_type (control→healthy, else distressed) for back-compat. Returns the row.
    """
    event_type = (req.event_type or "").strip().lower()
    if not event_type:
        # Derive from the legacy label when only that was supplied.
        legacy = (req.label or "").strip().lower()
        event_type = "control" if legacy == "healthy" else "default" if legacy == "distressed" else ""
    if event_type not in _EVENT_TYPES:
        raise HTTPException(400, f"event_type must be one of {_EVENT_TYPES}")
    # `label` keeps the table's existing CHECK happy and the stress backtest working.
    # An upgrade is NOT a distress event, so it maps to healthy alongside controls;
    # only downgrades/defaults are "distressed" for the legacy stress backtest.
    label = "healthy" if event_type in ("control", "upgrade") else "distressed"

    agency = (req.agency or "").strip().upper() or None
    if agency and agency not in ("MDY", "FTC", "SPI", "EJR"):
        raise HTTPException(400, "agency must be MDY, FTC, SPI, or EJR")

    event_date = (req.event_date or "").strip()
    if event_type != "control" and not event_date:
        raise HTTPException(400, f"event_date (the {event_type} date) is required for a {event_type} case")
    if event_date:
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, f"Invalid event_date {event_date!r} — expected YYYY-MM-DD")
    # Control default: a stable far-future anchor so baselines stay reproducible.
    if event_type == "control" and not event_date:
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
        "event_type": event_type,
        "agency": agency,
        "event_date": event_date,
        "notes": (req.notes or "").strip(),
    })


@app.delete("/api/cases/{case_id}")
def remove_case(case_id: str):
    """Delete one case from the backtest library by case_id."""
    if not delete_case(case_id):
        raise HTTPException(404, f"case_id {case_id!r} not found")
    return {"status": "deleted", "case_id": case_id}


# ── Background model training ("trained in the back") ────────────────────────

@app.get("/api/cron/train-model")
def cron_train_model(authorization: str = Header(default="")):
    """
    Orchestrate a background (re)train of the rating-migration model: assemble the
    labeled matrix, train walk-forward vintages + the active model, persist the
    model-learned stress-score weights, write predictions, and run the walk-forward
    eval. Auth mirrors cron_refresh_all (Bearer CRON_SECRET when set).

    NOTE: a full walk-forward train can exceed a serverless function's time/memory
    budget. This endpoint is the trigger/orchestrator; in production the heavy run
    is expected to execute as a longer-lived scheduled job (external worker / CI /
    local cron) calling the same `src.model` pipeline. The app only ever READS the
    persisted artifacts (migration_predictions, score_config, model_registry,
    data/migration_eval.json). Inert until agency ratings have been ingested.
    """
    secret = os.environ.get("CRON_SECRET")
    if secret and authorization != f"Bearer {secret}":
        raise HTTPException(401, "Unauthorized")

    try:
        from datetime import timezone
        from src.model.features import load_training_matrix
        from src.model.train import train_all, save_model, derive_score_config, DEFAULT_ARTIFACT

        df = load_training_matrix()
        if df.empty:
            return {"status": "skipped", "reason": "no labeled training rows yet (ingest agency ratings first)"}

        version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # Single active model on the full history; vintages/eval are expected to run
        # in the heavier offline job.
        split = max(df["period_end"])
        bundle, metrics = train_all(df, split, version=version)
        path = save_model(bundle, DEFAULT_ARTIFACT)
        save_model_registry(
            version=version, artifact_path=path, feature_list=bundle["feature_columns"],
            train_window={"split_date": split, "n_train": metrics["n_train"], "n_test": metrics["n_test"]},
            metrics=metrics,
        )
        save_score_config(derive_score_config(bundle))
        return {"status": "trained", "version": version, "rows": int(len(df))}
    except Exception as e:
        logging.warning("cron train-model failed", exc_info=True)
        raise HTTPException(500, f"train-model failed: {e}")


# ── Rating-migration EVENT backtest (run the trained model over the case library) ─

_migration_bt_status: dict[str, Any] = {"running": False, "result": None, "error": None}


def _load_vintages() -> list[dict[str, Any]]:
    """Walk-forward vintages on disk: data/model_vintages/<cutoff>.joblib → [{cutoff, path}]."""
    d = _ROOT / "data" / "model_vintages"
    if not d.exists():
        return []
    return sorted(
        ({"cutoff": p.stem, "path": str(p)} for p in d.glob("*.joblib")),
        key=lambda v: v["cutoff"],
    )


def _run_migration_backtest_task(steps: int):
    try:
        from src.backtest import load_cases
        from src.model.features import build_scoring_matrix
        from src.migration_backtest import run_migration_backtest

        vintages = _load_vintages()
        cases = load_cases()
        scoring_by_cik: dict[str, list[dict]] = {}
        df = build_scoring_matrix()
        for rec in df.to_dict("records"):
            scoring_by_cik.setdefault(str(rec["cik"]).zfill(10), []).append(rec)

        result = run_migration_backtest(cases, scoring_by_cik, vintages, steps=steps)
        if not vintages:
            result["note"] = ("No trained model vintages found (data/model_vintages/). "
                              "Train the model once agency ratings are ingested.")
        from datetime import timezone
        result["run_at"] = datetime.now(timezone.utc).isoformat()
        _migration_bt_status["result"] = result
        _migration_bt_status["error"] = None
    except Exception as e:
        _migration_bt_status["error"] = str(e)
        _migration_bt_status["result"] = None
    finally:
        _migration_bt_status["running"] = False


@app.post("/api/migration/backtest")
def start_migration_backtest(background_tasks: BackgroundTasks, req: BacktestRequest | None = None):
    """
    Run the unified rating-EVENT backtest: replay the trained model point-in-time
    over the case library and report whether it flagged each issuer's upgrade /
    downgrade / default early. Background task; poll /api/migration/backtest/status.
    """
    if _migration_bt_status["running"]:
        raise HTTPException(409, "Migration backtest already running")
    from src.migration_backtest import DEFAULT_STEPS
    steps = req.steps if (req and req.steps is not None) else DEFAULT_STEPS
    steps = max(_MIN_STEPS, min(_MAX_STEPS, steps))
    _migration_bt_status["running"] = True
    _migration_bt_status["result"] = None
    _migration_bt_status["error"] = None
    background_tasks.add_task(_run_migration_backtest_task, steps)
    return {"status": "started"}


@app.get("/api/migration/backtest/status")
def migration_backtest_status():
    """Current state of the migration event backtest (running flag / result / error)."""
    return _migration_bt_status


@app.get("/api/migration/scorecard")
def migration_scorecard():
    """
    Read-only walk-forward scorecard: the `migration` block written by
    src.model.evaluate (data/migration_eval.json) plus the active model's
    provenance/metrics from model_registry. Either may be null until trained.
    """
    import json
    eval_block = None
    p = _ROOT / "data" / "migration_eval.json"
    if p.exists():
        try:
            eval_block = json.loads(p.read_text()).get("migration")
        except ValueError:
            eval_block = None
    try:
        model = get_model_registry()
    except Exception:
        model = None
    return {"migration": eval_block, "model": model}

