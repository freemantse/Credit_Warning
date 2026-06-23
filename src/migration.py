"""
Rating-migration / downgrade-trend detector (Tier-1, FLAT / non-sector-adjusted).

Detects whether a company's credit quality is on a TRAJECTORY toward a rating
change — measuring the *motion* of the credit-stress score, not its level. A high
score is stress that already arrived; a rising score from a healthy level is
stress that is *coming*. This module measures the latter.

Implements MIGRATION_DETECTOR.md:
  • §3.1 MANDATORY preprocessing — collapse the 40 raw ~90-day backtest snapshots
    to ONE observation per distinct `period_end` (oldest→newest, has_data only),
    so velocity/acceleration are computed across distinct annual filings, NOT the
    90-day re-evaluation snapshots (which repeat the same score 3–4× then jump).
  • §4 four components: velocity, acceleration, component-sequence, distance-to-boundary.
  • §5 decision: persistence + breadth + one-off-event filter (incl. the
    leverage-spike-without-coverage-decline / debt-funded-acquisition suppression).
  • §6 output schema via detect_migration().

╔══════════════════════════════════════════════════════════════════════════╗
║ TIER-1 LIMITATION — FLAT trend thresholds (no sector adjustment).          ║
║ Deterioration velocity that is normal for a cyclical (autos, energy) would ║
║ be alarming for a utility. This detector uses one global set of trend      ║
║ thresholds. It will be superseded by sector-adjusted thresholds once the   ║
║ benchmark layer (Tier 2) exists — see detect_migration_sector_adjusted().  ║
╚══════════════════════════════════════════════════════════════════════════╝

Additive analysis module: does NOT modify score.py / rating.py / calibrate.py /
the config / the live scoring path.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from datetime import date

from src.score import DEFAULT_CONFIG, _CORE_RULE_KEYS, _ADDITIONAL_RULE_RATIOS, _ramp
from src.rating import score_to_rating, BUCKET_ORDER

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT = pathlib.Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
RESULTS_PATH = DATA_DIR / "backtest_results.json"
CALIBRATION_PATH = DATA_DIR / "rating_calibration.json"
VALIDATION_PATH = DATA_DIR / "migration_validation.json"

# ── Rule → ratio mapping (the real keys, confirmed in Stage 1) ─────────────────
# Core rules read these ratios (hard-coded in score.compute_score); additional
# rules come straight from score._ADDITIONAL_RULE_RATIOS.
_CORE_RULE_TO_RATIO = {
    "profitability": "ebitda_margin",
    "leverage>5x": "leverage",
    "coverage<2x": "interest_coverage",
    "cash_flow_to_debt<30%": "cash_flow_to_debt",
    "fcf_negative": "fcf_margin",          # size-normalized; raw free_cash_flow is informational only
    "liquidity<1x": "liquidity",
    "current_ratio<1.5x": "current_ratio",
    "debt_to_assets>40%": "debt_to_assets",
    "maturity_wall": "maturity_near_term_pct",
}
RULE_TO_RATIO: dict[str, str] = {**_CORE_RULE_TO_RATIO, **dict(_ADDITIONAL_RULE_RATIOS)}

# ── Component groups in DETERIORATION ORDER (§4.3) ─────────────────────────────
# liquidity moves first, qualitative/profitability moves last. Grouped by the 19
# scoring RULES (each polarity-normalized via _ramp), not raw ratio values — so
# "group deteriorating" == the group's summed stress-points rising, regardless of
# whether the underlying ratio is higher-worse or lower-worse.
GROUP_ORDER = ["liquidity", "coverage", "leverage", "qualitative"]
GROUPS: dict[str, list[str]] = {
    "liquidity":   ["liquidity<1x", "current_ratio<1.5x", "quick_ratio<1x"],
    "coverage":    ["coverage<2x", "cash_flow_to_debt<30%", "fcf_negative",
                    "ocf_ebitda_conversion<0.7x", "moody_adjusted_fcf_negative", "rcf_net_debt<15%"],
    "leverage":    ["leverage>5x", "debt_to_assets>40%", "maturity_wall", "debt_to_equity>2x",
                    "asset_coverage<1.5x", "tangible_asset_coverage<1x",
                    "liquidation_asset_coverage<0.7x", "maturity_coverage_near_term<1x"],
    "qualitative": ["profitability", "revenue_yoy_growth<-5%"],
}
# Sanity: every scored rule is assigned to exactly one group.
assert {r for rs in GROUPS.values() for r in rs} == set(RULE_TO_RATIO), "group/rule mismatch"

# Analyst-annotated "structurally invisible" defaults: the fatal deterioration
# post-dates the last available filing (Whiting, Oasis — the 2020 COVID/commodity
# collapse hit after the FY2019 10-K) or has no ratio footprint at all (PG&E —
# wildfire-liability bankruptcy; score never exceeded ~38). No filing-cadence,
# ratio-based detector can catch these — they are the inherent LIMIT of the data
# source, not detector misses, and are reported separately so the catch-rate
# denominator is honest. NOT exhaustive: other 2020-shock oil names may also
# qualify but are only listed here once diagnosed by hand (CIKs, 10-digit).
STRUCTURALLY_INVISIBLE = {
    "0001255474": "Whiting Petroleum",   # 2020 oil/COVID shock post-dates FY2019 10-K
    "0001486159": "Oasis Petroleum",     # 2020 oil/COVID shock post-dates FY2019 10-K
    "0001004980": "PG&E Corp",           # wildfire-liability default; no ratio footprint
}


@dataclass
class MigrationParams:
    """Tunable parameters (§9). Defaults are the spec defaults; surfaced for tuning."""
    velocity_window: int = 4            # N trailing distinct filings for the slope
    min_history: int = 4                # < this → insufficient_history
    min_persistence: int = 3            # consecutive rising filings to call a trend
    min_breadth: int = 2                # of 4 component groups deteriorating
    stable_velocity_band: float = 1.0   # |velocity| <= band → stable (motion≈0)
    group_slope_eps: float = 0.5        # group pts/yr slope above which a group "deteriorates"
    single_spike_frac: float = 0.80     # one step holding ≥ this share of the rise → one-off
    plateau_eps: float = 1.0            # post-spike rise (pts) below this counts as a plateau
    strong_vel: float = 8.0             # |velocity| ≥ this → strong (velocity alone)
    strong_combo_vel: float = 6.0       # combined-path floor: breadth+accel can only lift to "strong" at/above this velocity (Change 1)
    moderate_vel: float = 3.0           # |velocity| ≥ this → moderate
    ceiling_level: float = 90.0         # plateau at/above this = score-cap saturation, not a one-off (Fix #2)
    chronic_stress_floor: float = 60.0  # "improving" with window-min ≥ this = saturation mean-reversion, not recovery (Fix #4)


@dataclass
class Obs:
    period_end: str
    score: float
    ratios: dict


# ── §3.1 preprocessing: collapse to distinct-filing series ─────────────────────

def build_series(case: dict) -> list[Obs]:
    """
    Collapse a case's raw `trajectory[]` to ONE observation per distinct
    `period_end` (has_data only), ordered oldest → newest. The score taken is the
    one as of each new filing. THIS is the series every component runs on.
    """
    seen: dict[str, Obs] = {}
    for snap in case.get("trajectory", []):
        if not snap.get("has_data"):
            continue
        pe = snap.get("period_end")
        if not pe or pe in seen:
            continue
        seen[pe] = Obs(period_end=pe, score=float(snap["score"]), ratios=snap.get("ratios") or {})
    return [seen[pe] for pe in sorted(seen)]  # oldest → newest by ISO date


# ── small numeric helpers ───────────────────────────────────────────────────────

def _year(period_end: str) -> float:
    return date.fromisoformat(period_end).toordinal() / 365.25


def _ols_slope(xs: list[float], ys: list[float]) -> float:
    """Ordinary-least-squares slope dy/dx. 0.0 when undefined (<2 pts or zero spread)."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return num / den


# ── §4.1 velocity ───────────────────────────────────────────────────────────────

def velocity(series: list[Obs], p: MigrationParams) -> float:
    """Score slope (pts/year) over the trailing N distinct filings. + = deteriorating."""
    w = series[-p.velocity_window:]
    return _ols_slope([_year(o.period_end) for o in w], [o.score for o in w])


# ── §4.2 acceleration ─────────────────────────────────────────────────────────

def acceleration(series: list[Obs], p: MigrationParams) -> float | None:
    """recent_velocity − prior_velocity over the window halves (pts/yr²). None if <3 obs."""
    w = series[-p.velocity_window:]
    n = len(w)
    if n < 3:
        return None
    mid = n // 2
    prior = w[:mid + 1]
    recent = w[mid:]
    sv = lambda part: _ols_slope([_year(o.period_end) for o in part], [o.score for o in part])
    return sv(recent) - sv(prior)


# ── §4.3 component sequence ─────────────────────────────────────────────────────

def _group_points(ratios: dict) -> dict[str, float]:
    """Polarity-normalized stress points per component group for one observation."""
    out = {g: 0.0 for g in GROUP_ORDER}
    for g, rules in GROUPS.items():
        for rule in rules:
            r = DEFAULT_CONFIG["rules"][rule]
            val = ratios.get(RULE_TO_RATIO[rule])  # None → _ramp returns 0.0
            out[g] += _ramp(val, r["healthy"], r["severe"], r["weight"])
    return out


def component_sequence(series: list[Obs], p: MigrationParams) -> dict:
    """
    Per group, OLS slope of its stress-points over the trailing window; a group is
    "deteriorating" when that slope exceeds group_slope_eps. Reports the
    deteriorating groups (in deterioration order), the earliest-stage group moving,
    and the early-stage liquidity-stress flag (liquidity moving while leverage isn't).
    """
    w = series[-p.velocity_window:]
    xs = [_year(o.period_end) for o in w]
    gp = [_group_points(o.ratios) for o in w]
    slopes = {g: _ols_slope(xs, [pts[g] for pts in gp]) for g in GROUP_ORDER}
    deteriorating = [g for g in GROUP_ORDER if slopes[g] > p.group_slope_eps]
    improving = [g for g in GROUP_ORDER if slopes[g] < -p.group_slope_eps]   # symmetric (upgrade side)
    earliest = next((g for g in GROUP_ORDER if g in deteriorating), None)
    early_liq = ("liquidity" in deteriorating) and ("leverage" not in deteriorating)
    return {
        "deteriorating_groups": deteriorating,
        "improving_groups": improving,
        "earliest_stage_moving": earliest,
        "early_stage_liquidity_stress": early_liq,
        "group_slopes": {g: round(slopes[g], 2) for g in GROUP_ORDER},
    }


# ── §4.4 distance-to-boundary ───────────────────────────────────────────────────

def _load_boundaries() -> list[dict]:
    return json.loads(CALIBRATION_PATH.read_text())["boundaries"]


def boundary_projection(current_score: float, vel: float, trend: str,
                        boundaries: list[dict]) -> dict:
    """
    Symmetric distance-to-boundary in BOTH directions (§4.4 + Change 2):

      • Downgrade: gap up to the next-WORSE bucket boundary; projected years at
        positive velocity. Nulled when the trend is improving (moving away).
      • Upgrade: gap down to the next-BETTER bucket boundary; projected years at
        negative velocity. Nulled when the trend is deteriorating.

    `boundary_confidence` reports the ACTIVE-direction boundary's confidence
    (upgrade boundary when improving, else the downgrade boundary) so interpolated
    (BBB↓) projections are flagged; the IG boundaries (AA|A, A|BBB) are the
    trustworthy ones. Separate down_/up_boundary_confidence are also included.
    """
    rating = score_to_rating(current_score)["rating"]
    k = BUCKET_ORDER.index(rating)

    # Downgrade — boundary between current bucket k and the next-worse (k → k+1).
    if k < len(boundaries):
        nb = boundaries[k]
        d_dist = round(nb["threshold"] - current_score, 2)
        d_conf = nb["confidence"]
        d_proj = round(d_dist / vel, 1) if vel > 0 and d_dist > 0 else None
    else:  # already 'D'
        d_dist = d_proj = d_conf = None

    # Upgrade — boundary between the next-better bucket (k-1) and current k.
    if k >= 1:
        bb = boundaries[k - 1]
        u_dist = round(current_score - bb["threshold"], 2)  # pts the score must FALL
        u_conf = bb["confidence"]
        u_proj = round(u_dist / abs(vel), 1) if vel < 0 and u_dist > 0 else None
    else:  # already 'AA'
        u_dist = u_proj = u_conf = None

    active_conf = u_conf if trend == "improving" else d_conf
    return {
        "distance_to_downgrade": None if trend == "improving" else d_dist,
        "projected_years_to_downgrade": d_proj,
        "distance_to_upgrade": u_dist if trend == "improving" else None,
        "projected_years_to_upgrade": u_proj,
        "boundary_confidence": active_conf,
        "down_boundary_confidence": d_conf,
        "up_boundary_confidence": u_conf,
    }


# ── §5 decision logic ───────────────────────────────────────────────────────────

def _persistence_run(series: list[Obs]) -> int:
    """
    Length of the trailing NON-DECREASING (deteriorating-or-flat) run, in observations.

    Fix #2: uses >= (not strictly >), so a series that rose into the 100 score-cap
    and plateaus there (…→100→100) does not have its persistence run reset by the
    ceiling. A genuine reversal (a real decrease) still breaks the run. Safe because
    the velocity>band gate already excludes flat-but-high series from "deteriorating"
    (§8), so non-decreasing persistence cannot, on its own, manufacture a trend.
    """
    run = 1
    for i in range(len(series) - 1, 0, -1):
        if series[i].score >= series[i - 1].score - 1e-9:
            run += 1
        else:
            break
    return run


def _persistence_run_down(series: list[Obs]) -> int:
    """
    Symmetric upgrade-side analogue: trailing NON-INCREASING (improving-or-flat)
    run, in observations. A genuine reversal (a real increase) breaks the run.
    Used to give the upgrade direction the same persistence rigor as the downgrade.
    """
    run = 1
    for i in range(len(series) - 1, 0, -1):
        if series[i].score <= series[i - 1].score + 1e-9:
            run += 1
        else:
            break
    return run


def _one_off_flags(series: list[Obs], deteriorating_groups: list[str], breadth: int,
                   p: MigrationParams) -> tuple[bool, str | None]:
    """
    §5.3 one-off-event filter. Returns (likely_one_off, suppressed_reason).
    `suppressed_reason` is also set for breadth failures (not a one-off, but suppresses).
    """
    w = series[-p.velocity_window:]
    ys = [o.score for o in w]
    deltas = [ys[i] - ys[i - 1] for i in range(1, len(ys))]
    total_rise = ys[-1] - ys[0]

    # Single-spike test: nearly all the rise in one step, then a plateau.
    #   Fix #1: a spike on the FINAL step (the jump into default) has no subsequent
    #           observation, so the "then plateaus" condition is unverifiable — it is
    #           terminal deterioration, NOT a one-off. Require ≥1 observation after.
    #   Fix #2: a plateau at/above the score-cap ceiling is SATURATION (maxed-out /
    #           defaulted), not a transient bump that resolved — do not suppress.
    single_spike = False
    if deltas and total_rise > 0:
        spike_i = max(range(len(deltas)), key=lambda i: deltas[i])
        is_terminal = spike_i >= len(deltas) - 1            # Fix #1
        post_levels = ys[spike_i + 1:]
        post_rise = sum(deltas[spike_i + 1:])
        plateau_level = max(post_levels) if post_levels else None
        if (not is_terminal
                and deltas[spike_i] >= p.single_spike_frac * total_rise
                and post_rise <= p.plateau_eps
                and plateau_level is not None
                and plateau_level < p.ceiling_level):       # Fix #2
            single_spike = True

    # Leverage-spike-without-coverage-decline = debt-funded-acquisition signature.
    #   Fix #3: only when the leverage-group rise is a SINGLE-STEP jump (the
    #           acquisition signature). Sustained leverage build with no coverage/
    #           liquidity decline is genuine deterioration (e.g. a bankruptcy whose
    #           coverage/liquidity ratios are sparse at the group level) and must NOT
    #           be suppressed. NOTE: correctness on real acquisitions is logic-trusted
    #           but UNTESTED until the Phase-5b annotated acquisition cases are added.
    leverage_only = False
    if ("leverage" in deteriorating_groups
            and "coverage" not in deteriorating_groups
            and "liquidity" not in deteriorating_groups):
        lev = [_group_points(o.ratios)["leverage"] for o in w]
        ldeltas = [lev[i] - lev[i - 1] for i in range(1, len(lev))]
        lrise = lev[-1] - lev[0]
        if ldeltas and lrise > 0 and max(ldeltas) >= p.single_spike_frac * lrise:
            leverage_only = True

    likely_one_off = single_spike or leverage_only
    if leverage_only:
        return True, "leverage_spike_without_coverage_decline (single-step; likely debt-funded acquisition)"
    if single_spike:
        return True, "single_period_spike_then_plateau (non-terminal, below ceiling)"
    if breadth < p.min_breadth:
        return False, f"breadth<{p.min_breadth} (only {breadth} group(s) deteriorating)"
    return False, None


def _strength(vel_abs: float, accel_in_dir: float | None, breadth: int, p: MigrationParams) -> str:
    """
    Strength from velocity + acceleration + breadth (§5.4), uniform across all names.

    Change 1: the combined (breadth+acceleration) path now requires velocity ≥
    strong_combo_vel, not merely ≥ moderate_vel — so a moderate-velocity-but-broad
    move (e.g. ~4.6 pts/yr with breadth 4) reads "moderate", while genuine
    high-velocity ramps (≥ strong_vel) stay "strong" on velocity alone.

    `accel_in_dir` is acceleration in the trend's own direction (positive = the
    trend is speeding up), so the same formula grades upgrades and downgrades.
    """
    a = accel_in_dir or 0.0
    if vel_abs >= p.strong_vel or (vel_abs >= p.strong_combo_vel and a > 0 and breadth >= 3):
        return "strong"
    if vel_abs >= p.moderate_vel:
        return "moderate"
    return "weak"


def evaluate_series(series: list[Obs], p: MigrationParams) -> dict:
    """
    Run the four trend components + §5 decision on a distinct-filing series.
    Returns the trend fields (no company identity / no distance-to-boundary).
    """
    if len(series) < p.min_history:
        return {"trend": "insufficient_history", "observations_used": len(series)}

    vel = velocity(series, p)
    accel = acceleration(series, p)
    seq = component_sequence(series, p)
    # Symmetric persistence + breadth for both directions (Change 2).
    persist_up = _persistence_run(series)        # trailing non-DECREASING (deterioration)
    persist_down = _persistence_run_down(series)  # trailing non-INCREASING (improvement)
    breadth_det = len(seq["deteriorating_groups"])
    breadth_imp = len(seq["improving_groups"])
    likely_one_off, suppressed_reason = _one_off_flags(series, seq["deteriorating_groups"], breadth_det, p)

    if vel > p.stable_velocity_band:
        direction = "deteriorating"
    elif vel < -p.stable_velocity_band:
        direction = "improving"
    else:
        direction = "stable"  # motion ≈ 0 → stable regardless of level (§8)

    # default reported persistence/breadth = downgrade side; overridden for improving.
    persistence, breadth = persist_up, breadth_det

    if direction == "deteriorating":
        confirmed = (persist_up >= p.min_persistence and breadth_det >= p.min_breadth
                     and not likely_one_off)
        if confirmed:
            trend = "deteriorating"
        else:
            trend = "stable"  # rising but not a confirmed trend
            if suppressed_reason is None:
                if persist_up < p.min_persistence:
                    suppressed_reason = f"persistence<{p.min_persistence} (run={persist_up})"
                else:
                    suppressed_reason = "not confirmed"
    elif direction == "improving":
        # Fix #4: "improving requires the window not to be pinned high." A negative
        # slope whose window MINIMUM is still ≥ chronic_stress_floor is saturation
        # mean-reversion off the score ceiling (e.g. drifting 100→90), NOT genuine
        # recovery — the company remains deeply distressed. Reclassify as chronic
        # stress (a stable subtype). Mirror of §8's flat-but-high guard.
        w = series[-p.velocity_window:]
        win_min = min(o.score for o in w)
        persistence, breadth = persist_down, breadth_imp  # report upgrade-side figures
        if win_min >= p.chronic_stress_floor:
            trend = "stable_chronic_stress"
            suppressed_reason = (f"declining off ceiling but window min score {win_min:.0f} "
                                 f"≥ {p.chronic_stress_floor:.0f} — saturation mean-reversion, not recovery")
        else:
            # Change 2: the UPGRADE side carries the same rigor as the downgrade
            # side — sustained negative velocity needs persistence AND breadth.
            confirmed_up = persist_down >= p.min_persistence and breadth_imp >= p.min_breadth
            if confirmed_up:
                trend = "improving"
            else:
                trend = "stable"  # falling but not a confirmed upgrade trend
                if persist_down < p.min_persistence:
                    suppressed_reason = f"upgrade persistence<{p.min_persistence} (run={persist_down})"
                else:
                    suppressed_reason = f"upgrade breadth<{p.min_breadth} (only {breadth_imp} group(s) improving)"
    else:
        trend = "stable"

    if trend == "deteriorating":
        strength = _strength(abs(vel), accel, breadth_det, p)
    elif trend == "improving":
        # acceleration in the improving direction: negative accel = improving faster.
        strength = _strength(abs(vel), (-accel if accel is not None else None), breadth_imp, p)
    else:
        strength = "weak"

    return {
        "observations_used": len(series),
        "trend": trend,
        "strength": strength,
        "velocity_pts_per_year": round(vel, 2),
        "acceleration": round(accel, 2) if accel is not None else None,
        "component_sequence": {
            "deteriorating_groups": seq["deteriorating_groups"],
            "improving_groups": seq["improving_groups"],
            "earliest_stage_moving": seq["earliest_stage_moving"],
            "early_stage_liquidity_stress": seq["early_stage_liquidity_stress"],
        },
        "persistence_quarters": persistence,   # consecutive filings in the trend's direction
        "breadth_groups": breadth,             # groups moving in the trend's direction
        "likely_one_off": likely_one_off,
        "suppressed_reason": suppressed_reason,
        "_group_slopes": seq["group_slopes"],  # diagnostic
    }


# ── §6 public entry point ───────────────────────────────────────────────────────

def detect_migration(case: dict, p: MigrationParams | None = None,
                     boundaries: list[dict] | None = None) -> dict:
    """
    Detect the rating-migration trend for one backtest `case` dict.
    Returns the §6 schema. FLAT Tier-1 (see module docstring / the sector stub).
    """
    p = p or MigrationParams()
    boundaries = boundaries if boundaries is not None else _load_boundaries()
    series = build_series(case)

    base = {
        "cik": case.get("cik"),
        "company_name": case.get("company_name"),
        "current_score": round(series[-1].score, 1) if series else None,
        "current_rating": score_to_rating(series[-1].score)["rating"] if series else None,
    }

    if len(series) < p.min_history:
        return {**base, "observations_used": len(series), "trend": "insufficient_history",
                "strength": None, "velocity_pts_per_year": None, "acceleration": None,
                "component_sequence": None, "distance_to_downgrade": None,
                "projected_years_to_downgrade": None, "distance_to_upgrade": None,
                "projected_years_to_upgrade": None, "boundary_confidence": None,
                "persistence_quarters": None, "breadth_groups": None,
                "likely_one_off": False, "suppressed_reason": "insufficient_history"}

    ev = evaluate_series(series, p)
    proj = boundary_projection(series[-1].score, ev["velocity_pts_per_year"], ev["trend"], boundaries)
    out = {**base, **ev, **proj}
    out.pop("_group_slopes", None)
    return out


# ── Tier 2 extension point (NOT built) ──────────────────────────────────────────

def detect_migration_sector_adjusted(case: dict, sector_group: str,
                                     p: MigrationParams | None = None) -> dict:
    """
    TIER 2 (NOT IMPLEMENTED) — sector-adjusted trend thresholds.

    Would scale the velocity / strength / breadth thresholds per `sector_group`
    using the benchmark layer (peer-group score-velocity distributions), because a
    deterioration rate that is normal for a cyclical is alarming for a utility.
    Depends on the benchmark layer, which does not exist yet.
    """
    raise NotImplementedError(
        "Tier 2 sector-adjusted migration thresholds require the benchmark layer "
        "(not built). Scale the Tier-1 velocity/strength thresholds per sector_group here."
    )


# ── §7 validation ───────────────────────────────────────────────────────────────

def _first_deteriorating(series: list[Obs], p: MigrationParams) -> int | None:
    """Smallest prefix length (≥min_history) whose evaluate_series() == deteriorating."""
    for k in range(p.min_history, len(series) + 1):
        if evaluate_series(series[:k], p)["trend"] == "deteriorating":
            return k
    return None


def run_validation(results_path: pathlib.Path | str = RESULTS_PATH,
                   p: MigrationParams | None = None) -> dict:
    p = p or MigrationParams()
    R = json.loads(pathlib.Path(results_path).read_text())
    boundaries = _load_boundaries()

    rows = []
    for case in R["cases"]:
        series = build_series(case)
        res = detect_migration(case, p, boundaries)
        # lead: years between first-deteriorating filing and event_date
        lead_years = lead_obs = None
        if len(series) >= p.min_history and case.get("event_date"):
            k = _first_deteriorating(series, p)
            if k is not None:
                first_pe = series[k - 1].period_end
                ev = date.fromisoformat(case["event_date"])
                lead_years = round((ev - date.fromisoformat(first_pe)).days / 365.25, 2)
                lead_obs = len(series) - (k - 1) - 1
        rows.append({**res, "label": case.get("label"), "event_date": case.get("event_date"),
                     "distinct_filings": len(series),
                     "first_deteriorating_lead_years": lead_years,
                     "first_deteriorating_lead_obs": lead_obs})
    return {"rows": rows, "params": p.__dict__}


def _median(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def main() -> int:
    p = MigrationParams()
    val = run_validation(p=p)
    rows = val["rows"]

    from collections import Counter
    distressed = [r for r in rows if r["label"] == "distressed"]
    healthy = [r for r in rows if r["label"] == "healthy"]
    d_scoreable = [r for r in distressed if r["trend"] != "insufficient_history"]
    d_deteriorating = [r for r in d_scoreable if r["trend"] == "deteriorating"]
    leads = [r["first_deteriorating_lead_years"] for r in d_deteriorating]

    # Structurally-invisible: scoreable distressed whose fatal deterioration is not
    # in any filing (annotated). Excluded from the HONEST catch-rate denominator.
    d_invisible = [r for r in d_scoreable if r["cik"] in STRUCTURALLY_INVISIBLE]
    d_honest_denom = [r for r in d_scoreable if r["cik"] not in STRUCTURALLY_INVISIBLE]

    print("\n" + "=" * 74)
    print("MIGRATION DETECTOR — VALIDATION (§7)   [Fix #4 applied]")
    print("=" * 74)
    print(f"params: {val['params']}")

    print("\n1) DISTRESSED deterioration-catch (trend analogue of catch-rate):")
    print(f"   scoreable distressed (≥{p.min_history} distinct filings): {len(d_scoreable)}/{len(distressed)}")
    print(f"   distressed trend breakdown: {dict(Counter(r['trend'] for r in d_scoreable))}")
    print(f"   raw catch:    {len(d_deteriorating)}/{len(d_scoreable)} "
          f"({100*len(d_deteriorating)/len(d_scoreable):.0f}%)  (all scoreable)")
    print(f"   HONEST catch: {len(d_deteriorating)}/{len(d_honest_denom)} "
          f"({100*len(d_deteriorating)/len(d_honest_denom):.0f}%)  (excludes {len(d_invisible)} structurally-invisible)")
    print(f"   median lead (first-deteriorating → event): {_median(leads)} years  "
          f"(median obs lead: {_median([r['first_deteriorating_lead_obs'] for r in d_deteriorating])})")
    chronic = [r for r in d_scoreable if r["trend"] == "stable_chronic_stress"]
    print(f"   stable_chronic_stress (Fix #4 — pinned high, off-ceiling drift): {len(chronic)} -> "
          f"{[r['company_name'] for r in chronic]}")
    not_flagged = [r for r in d_scoreable if r["trend"] != "deteriorating"]
    if not_flagged:
        print(f"   distressed NOT flagged deteriorating ({len(not_flagged)}):")
        for r in not_flagged:
            inv = "  [STRUCTURALLY-INVISIBLE]" if r["cik"] in STRUCTURALLY_INVISIBLE else ""
            print(f"      {r['company_name']}: trend={r['trend']} vel={r['velocity_pts_per_year']} "
                  f"breadth={r['breadth_groups']} reason={r['suppressed_reason']}{inv}")

    print("\n2) HEALTHY controls — should be mostly stable/improving:")
    from collections import Counter
    hc = Counter(r["trend"] for r in healthy)
    print(f"   trend mix: {dict(hc)}")
    h_det = [r for r in healthy if r["trend"] == "deteriorating"]
    WEAKENING = {"UPS"}  # S&P outlook → negative; mild deterioration is legitimate
    POSITIVE_OUTLOOK = {"ITW", "LLY", "CAT"}  # must NOT show strong deterioration
    print(f"   healthy flagged 'deteriorating': {len(h_det)}")
    for r in h_det:
        tk = (r.get("company_name") or "")
        note = ""
        # match by company name keywords to outlook sets
        legit = any(w.lower() in tk.lower() for w in ("ups", "united parcel"))
        note = "  [legit — S&P negative outlook]" if legit else ""
        print(f"      {r['company_name']}: strength={r['strength']} vel={r['velocity_pts_per_year']} "
              f"breadth={r['breadth_groups']} groups={r['component_sequence']['deteriorating_groups']}{note}")
    # explicit check on the positive-outlook names
    print("   positive-outlook names (ITW / Eli Lilly / Caterpillar) — must NOT be strongly deteriorating:")
    for r in healthy:
        nm = (r.get("company_name") or "").lower()
        if any(w in nm for w in ("illinois tool", "lilly", "caterpillar")):
            flag = "  <-- WARNING strong deterioration" if (r["trend"] == "deteriorating" and r["strength"] == "strong") else ""
            print(f"      {r['company_name']}: trend={r['trend']} strength={r['strength']} "
                  f"vel={r['velocity_pts_per_year']}{flag}")

    print("\n3) One-off filter test cases (Waste Management / Emerson / Becton Dickinson / Air Products):")
    names = " ".join((r.get("company_name") or "").lower() for r in rows)
    present = [n for n in ("waste management", "emerson", "becton", "air products") if n in names]
    if present:
        print(f"   present: {present} — checking suppression…")
        for r in rows:
            nm = (r.get("company_name") or "").lower()
            if any(n in nm for n in present):
                print(f"      {r['company_name']}: trend={r['trend']} likely_one_off={r['likely_one_off']} "
                      f"reason={r['suppressed_reason']}")
    else:
        print("   NONE present in the current 95-case set (held for Phase 5b). "
              "The leverage-spike-without-coverage one-off filter is therefore UNTESTED on "
              "real acquisition cases until those controls are added.")
    # report any one-off suppressions that did fire
    fired = [r for r in rows if r["likely_one_off"]]
    print(f"   one-off suppressions that fired anywhere: {len(fired)}")
    for r in fired:
        print(f"      {r['company_name']}: {r['suppressed_reason']}")

    print("\n4) Surprises (healthy strongly-deteriorating, or distressed not flagged):")
    surprises = []
    for r in h_det:
        if r["strength"] in ("moderate", "strong"):
            surprises.append(f"{r['company_name']} (healthy) → {r['trend']}/{r['strength']} vel={r['velocity_pts_per_year']}")
    for r in not_flagged:
        surprises.append(f"{r['company_name']} (distressed) → {r['trend']} (not deteriorating)")
    if surprises:
        for s in surprises:
            print(f"   - {s}")
    else:
        print("   none")

    print("\n5) STRUCTURALLY-INVISIBLE defaults (inherent limit of filing-based detection,")
    print("   NOT detector misses — excluded from the honest catch-rate denominator):")
    for r in d_invisible:
        print(f"   - {r['company_name']}: trend={r['trend']} (fatal deterioration post-dates last "
              f"filing or has no ratio footprint)")
    print(f"   → honest catch-rate excludes these {len(d_invisible)}; raw denominator {len(d_scoreable)} "
          f"→ honest denominator {len(d_honest_denom)}")

    # ── Change 1: strength grading distribution ──────────────────────────────
    print("\nCHANGE 1 — strength grading (uniform; combo path now needs vel ≥ "
          f"{p.strong_combo_vel}):")
    graded = [r for r in rows if r["trend"] in ("deteriorating", "improving")]
    print(f"   strength distribution (deteriorating+improving, n={len(graded)}): "
          f"{dict(Counter(r['strength'] for r in graded))}")
    def _row(nm_key):
        return next((r for r in rows if nm_key.lower() in (r.get('company_name') or '').lower()), None)
    gm = _row("general mills")
    print(f"   General Mills: strength={gm['strength']} (vel {gm['velocity_pts_per_year']}, "
          f"breadth {gm['breadth_groups']})  -> {'OK moderate' if gm['strength']=='moderate' else 'CHECK'}")
    print("   genuine high-vel ramps must STAY strong:")
    for nm in ("Akorn", "Pier 1", "Briggs"):
        r = _row(nm)
        print(f"      {r['company_name']}: strength={r['strength']} vel={r['velocity_pts_per_year']} "
              f"-> {'OK strong' if r['strength']=='strong' else 'CHECK'}")

    # ── Change 2: UPGRADE direction (first-class) ─────────────────────────────
    improving = [r for r in rows if r["trend"] == "improving"]
    print(f"\nCHANGE 2 — UPGRADE direction (genuine improving trends): {len(improving)}")
    print(f"   {'company':<24}{'label':<11}{'vel':>7}{'br':>4}  proj_yrs_to_upgrade  bound_conf")
    for r in sorted(improving, key=lambda x: x["velocity_pts_per_year"]):
        print(f"   {(r['company_name'] or '')[:23]:<24}{r['label']:<11}{r['velocity_pts_per_year']:>7}"
              f"{r['breadth_groups']:>4}  {str(r.get('projected_years_to_upgrade')):>17}  "
              f"{r.get('up_boundary_confidence')}")
    print("   agency upgrade-side test cases (Eli Lilly, Caterpillar, ITW):")
    for nm in ("lilly", "caterpillar", "illinois tool"):
        r = _row(nm)
        print(f"      {r['company_name']}: trend={r['trend']} vel={r['velocity_pts_per_year']} "
              f"strength={r['strength']} (proj_yrs_to_upgrade={r.get('projected_years_to_upgrade')})")
    print("   NOTE: upgrade side is LOGIC-SOUND-BUT-LIGHTLY-TESTED — the library is")
    print("   mostly distressed/down names; few genuine improvers exist to validate against.")

    # §9 parameter-effect summary
    print("\n§9 parameter-effect summary on the validation set:")
    allv = [r["velocity_pts_per_year"] for r in rows if r.get("velocity_pts_per_year") is not None]
    print(f"   trend mix (all 95): {dict(Counter(r['trend'] for r in rows))}")
    print(f"   velocity pts/yr: min={min(allv):.1f} median={_median(allv):.1f} max={max(allv):.1f}")
    print(f"   breadth distribution: {dict(Counter(r['breadth_groups'] for r in rows if r['breadth_groups'] is not None))}")
    print(f"   insufficient_history: {sum(1 for r in rows if r['trend']=='insufficient_history')}")

    # ── write artifact ──────────────────────────────────────────────────────
    summary = {
        "params": val["params"],
        "distressed_scoreable": len(d_scoreable),
        "distressed_total": len(distressed),
        "distressed_trend_breakdown": dict(Counter(r["trend"] for r in d_scoreable)),
        "distressed_deteriorating": len(d_deteriorating),
        "distressed_catch_pct_raw": round(100 * len(d_deteriorating) / len(d_scoreable), 1) if d_scoreable else None,
        "distressed_catch_pct_honest": round(100 * len(d_deteriorating) / len(d_honest_denom), 1) if d_honest_denom else None,
        "stable_chronic_stress": [r["company_name"] for r in chronic],
        "structurally_invisible": {
            "note": "fatal deterioration post-dates the last filing (2020 commodity/COVID shock) "
                    "or has no ratio footprint (wildfire liability). Inherent limit of filing-based "
                    "detection, NOT detector misses. Excluded from honest catch-rate. Not exhaustive.",
            "names": [r["company_name"] for r in d_invisible],
        },
        "median_lead_years": _median(leads),
        "healthy_trend_mix": dict(hc),
        "healthy_false_trend": len(h_det),
        "one_off_cases_present": present,
        "surprises": surprises,
        "strength_distribution": dict(Counter(r["strength"] for r in graded)),
        "upgrade_side": {
            "note": "logic-sound-but-lightly-tested — library is mostly distressed names; "
                    "few genuine improvers to validate against.",
            "improving_count": len(improving),
            "improving": [{"company_name": r["company_name"], "label": r["label"],
                           "velocity": r["velocity_pts_per_year"],
                           "projected_years_to_upgrade": r.get("projected_years_to_upgrade"),
                           "up_boundary_confidence": r.get("up_boundary_confidence")}
                          for r in improving],
        },
        "known_limitations": {
            "eli_lilly_model_vs_agency_divergence": {
                "company_name": "Eli Lilly",
                "detector_trend": "stable",
                "velocity_pts_per_year": next((r["velocity_pts_per_year"] for r in rows
                                               if "lilly" in (r.get("company_name") or "").lower()), None),
                "velocity_direction": "deteriorating (positive velocity)",
                "agency_view": "Moody's upgraded A1->Aa3 (positive outlook)",
                "description": "Lilly's score rises (velocity ~+15.1, deteriorating direction) while "
                               "Moody's UPGRADED it A1->Aa3 with a positive outlook. The detector reads "
                               "buyback / investment-heavy balance-sheet leverage as deterioration — the "
                               "SAME blind spot as Amgen in the rating calibration. This is a FLAT Tier-1 "
                               "limitation that sector adjustment (Tier 2) is expected to fix, NOT a "
                               "migration-detector defect: the trend math is correct on the score it is "
                               "given; the underlying score over-penalizes capital-return-heavy issuers.",
                "tier2_expected_fix": "sector-adjusted thresholds / benchmark layer (not built)",
            }
        },
    }
    VALIDATION_PATH.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print(f"\nArtifact written: {VALIDATION_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
