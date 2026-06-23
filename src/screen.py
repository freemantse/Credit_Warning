"""
Stage 4 — the senior-secured bond screen (the system's ultimate deliverable).

Surfaces SENIOR-SECURED bond instruments of CREDIT-HEALTHY, NOT-DETERIORATING
issuers, ranked best-first. It joins three signals already in the system:

  1. implied_ratings        — issuer credit health (rating_index, 0=AAA).
  2. Rating Outlook (Stage 0)— direction of travel; Negative outlooks are excluded.
  3. bond_instruments        — the LLM-extracted instruments + their seniority.

Each surfaced instrument is notched off its issuer's implied rating for seniority
(rating.notch_instrument): a senior-secured bond rates a notch ABOVE the issuer.

Downgrade-risk signal: until the migration model (Stage 3) writes calibrated
P(downgrade) to a migration_predictions table, the screen uses the deterministic
Rating Outlook as the forward-looking filter. Swapping in p_downgrade later is a
localized change in screen_senior_secured() — build_screen_rows is signal-agnostic.

Layered like the rest of the system: build_screen_rows() is a pure, unit-testable
ranking/filter over already-grouped data; screen_senior_secured() is the thin DB
orchestrator that fetches and computes the outlooks.
"""

from __future__ import annotations

from typing import Any

from src.rating import (
    RATING_SCALE,
    rating_index,
    notch_instrument,
    OUTLOOK_NEGATIVE,
    OUTLOOK_POSITIVE,
    OUTLOOK_STABLE,
)

DEFAULT_MIN_RATING = "BBB-"      # investment-grade floor by default
SENIOR_SECURED = "senior_secured"

# Outlook → rank for ordering (better outlook first).
_OUTLOOK_RANK = {OUTLOOK_POSITIVE: 0, OUTLOOK_STABLE: 1, OUTLOOK_NEGATIVE: 2}


def _latest_key(d: dict) -> str | None:
    """The newest period_end key in a {period_end: …} dict (period_ends sort chronologically)."""
    return max(d) if d else None


def build_screen_rows(
    *,
    issuers_map: dict[str, dict],
    implied_grouped: dict[str, dict[str, dict]],
    instruments_grouped: dict[str, dict[str, list[dict]]],
    outlook_by_cik: dict[str, Any],
    min_rating_index: int,
    predictions_by_cik: dict[str, dict] | None = None,
    exclude_negative_outlook: bool = True,
    max_p_downgrade: float = 0.30,
    seniority: str = SENIOR_SECURED,
) -> list[dict[str, Any]]:
    """
    Pure ranking/filter: produce the ranked screen rows from already-grouped data.

    Args:
        issuers_map:         cik → {ticker, name}.
        implied_grouped:     cik → period_end → implied-rating dict (rating_index, …).
        instruments_grouped: cik → period_end → [bond-instrument dicts].
        outlook_by_cik:      cik → RatingOutlookResult | None (the fallback filter).
        min_rating_index:    issuer must be at least this healthy (index ≤ this; lower=better).
        predictions_by_cik:  cik → latest migration prediction row (p_downgrade, …). When
                             present for an issuer it is the PREFERRED forward filter.
        exclude_negative_outlook: drop Negative-outlook issuers (used when no prediction).
        max_p_downgrade:     drop issuers whose calibrated P(downgrade) exceeds this.
        seniority:           which seniority tier to surface (default senior_secured).

    Returns one row per qualifying instrument, sorted by the notched instrument
    rating (best first), then issuer rating, then downgrade risk.
    """
    predictions_by_cik = predictions_by_cik or {}
    rows: list[dict[str, Any]] = []

    for cik, by_period in instruments_grouped.items():
        # Issuer health: latest implied rating must exist and be ≥ the floor.
        implied_periods = implied_grouped.get(cik, {})
        latest_implied_period = _latest_key(implied_periods)
        if latest_implied_period is None:
            continue
        issuer_idx = implied_periods[latest_implied_period].get("rating_index")
        if issuer_idx is None or issuer_idx > min_rating_index:
            continue

        outlook = outlook_by_cik.get(cik)
        outlook_label = outlook.outlook if outlook else None
        pred = predictions_by_cik.get(cik)
        p_down = pred.get("p_downgrade") if pred else None

        # Forward filter: prefer the calibrated downgrade probability when we have it,
        # else fall back to the deterministic Rating Outlook.
        if p_down is not None:
            if p_down > max_p_downgrade:
                continue
        elif exclude_negative_outlook and outlook_label == OUTLOOK_NEGATIVE:
            continue

        # Surface the issuer's most recent period of extracted instruments.
        latest_instr_period = _latest_key(by_period)
        if latest_instr_period is None:
            continue
        ident = issuers_map.get(cik, {})

        for inst in by_period[latest_instr_period]:
            if inst.get("seniority") != seniority:
                continue
            notched_idx = notch_instrument(issuer_idx, inst["seniority"])
            rows.append({
                "cik": cik,
                "ticker": ident.get("ticker", ""),
                "name": ident.get("name", ""),
                "instrument_name": inst.get("instrument_name"),
                "seniority": inst.get("seniority"),
                "principal_amount": inst.get("principal_amount"),
                "coupon": inst.get("coupon"),
                "maturity_year": inst.get("maturity_year"),
                "issuer_implied_rating": RATING_SCALE[issuer_idx],
                "issuer_rating_index": issuer_idx,
                "instrument_notched_rating": RATING_SCALE[notched_idx] if notched_idx is not None else None,
                "instrument_notched_index": notched_idx,
                "outlook": outlook_label,
                "p_downgrade": p_down,
                "period_end": latest_instr_period,
                "evidence_quote": inst.get("evidence_quote"),
                "source": inst.get("source"),
            })

    # Risk tiebreak: calibrated P(downgrade) when present, else the outlook rank.
    def _risk(r):
        return r["p_downgrade"] if r["p_downgrade"] is not None else 0.1 + 0.1 * _OUTLOOK_RANK.get(r["outlook"], 1)

    rows.sort(key=lambda r: (
        r["instrument_notched_index"] if r["instrument_notched_index"] is not None else 99,
        r["issuer_rating_index"] if r["issuer_rating_index"] is not None else 99,
        _risk(r),
    ))
    return rows


def screen_senior_secured(
    *,
    min_rating: str = DEFAULT_MIN_RATING,
    exclude_negative_outlook: bool = True,
    max_p_downgrade: float = 0.30,
    seniority: str = SENIOR_SECURED,
    config: dict | None = None,
) -> dict[str, Any]:
    """
    DB orchestrator: assemble the senior-secured screen as {meta, rows}.

    Fetches the grouped reads, computes each candidate issuer's Rating Outlook
    (the same score+implied-rating series the portfolio uses), and ranks the
    qualifying instruments via build_screen_rows.
    """
    from src.score import compute_score, ScoreConfig, DEFAULT_CONFIG
    from src.rating import rating_outlook, OUTLOOK_DEFAULT
    from src.model.features import _ratio_results_from_stored
    from src.store import (
        get_issuers, get_implied_ratings_grouped, get_bond_instruments_grouped,
        get_ratios_grouped, get_findings_grouped, get_maturities_grouped,
        get_covenants_grouped, get_loss_provisions_grouped, get_score_config,
        get_migration_predictions_grouped,
    )

    try:
        min_rating_index = rating_index(min_rating)
    except ValueError:
        raise ValueError(f"unknown min_rating {min_rating!r}")

    try:
        cfg = ScoreConfig.from_dict(config or get_score_config())
    except Exception:
        cfg = ScoreConfig.from_dict(DEFAULT_CONFIG)

    instruments_grouped = get_bond_instruments_grouped()
    implied_grouped = get_implied_ratings_grouped()
    issuers_map = {i["cik"]: {"ticker": i.get("ticker", ""), "name": i.get("name", "")}
                   for i in get_issuers()}

    # Only issuers that actually have extracted instruments are candidates.
    candidate_ciks = list(instruments_grouped)
    ratios = get_ratios_grouped()
    findings = get_findings_grouped()
    maturities = get_maturities_grouped()
    covenants = get_covenants_grouped()
    provisions = get_loss_provisions_grouped()

    outlook_by_cik: dict[str, Any] = {}
    for cik in candidate_ciks:
        periods = sorted(ratios.get(cik, {}))
        if not periods:
            outlook_by_cik[cik] = None
            continue
        series = []
        for per in periods[-OUTLOOK_DEFAULT.window:]:
            sc = compute_score(
                _ratio_results_from_stored(ratios[cik][per], per),
                findings.get(cik, {}).get(per, []),
                maturities.get(cik, {}).get(per),
                covenants.get(cik, {}).get(per, []),
                provisions.get(cik, {}).get(per, []),
                config=cfg,
            )
            series.append({
                "period_end": per,
                "rating_index": (implied_grouped.get(cik, {}).get(per) or {}).get("rating_index"),
                "score": sc.score,
            })
        outlook_by_cik[cik] = rating_outlook(series)

    # Calibrated migration predictions (Stage 3) when available — the preferred
    # forward filter. Resilient to the table being absent (returns {}).
    predictions = get_migration_predictions_grouped()
    predictions_by_cik = {
        cik: per[max(per)] for cik, per in predictions.items() if per
    }

    rows = build_screen_rows(
        issuers_map=issuers_map,
        implied_grouped=implied_grouped,
        instruments_grouped=instruments_grouped,
        outlook_by_cik=outlook_by_cik,
        predictions_by_cik=predictions_by_cik,
        min_rating_index=min_rating_index,
        exclude_negative_outlook=exclude_negative_outlook,
        max_p_downgrade=max_p_downgrade,
        seniority=seniority,
    )

    return {
        "meta": {
            "min_rating": min_rating,
            "exclude_negative_outlook": exclude_negative_outlook,
            "max_p_downgrade": max_p_downgrade,
            "seniority": seniority,
            "issuers_with_instruments": len(candidate_ciks),
            "matches": len(rows),
            # Prefer the learned, calibrated P(downgrade) when predictions exist;
            # otherwise the deterministic Rating Outlook is the forward filter.
            "downgrade_signal": "migration_model" if predictions_by_cik else "rating_outlook",
        },
        "rows": rows,
    }
