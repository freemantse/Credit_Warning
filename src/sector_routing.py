"""
Production sector router — the SINGLE place src/ reads the materialized sector table.

`data/sector_routing.csv` is produced by experiments/build_sector_routing.py, which
runs Freeman's methodology classifier (deterministic SIC-table path) over the tracked
universe. Every sector-CONDITIONAL rule in the scorer consumes the table through
`get_sector()` here — one loader, one missing-row/confidence policy — so the gates
stay consistent. The first consumer is the intangibles-capex gate in extract.py;
the revenue tag-ordering and utility construction-capex fixes will import the same
two functions.

Policy (deliberate, applied uniformly):
  • MISSING ROW → get_sector returns None → the caller MUST fall back to current
    (ungated) behavior. Never guess a sector.
  • CONFIDENCE/SOURCE → sector_trusted() gates whether a bucket-conditional rule may
    fire: only high/medium confidence from a deterministic SIC hit (`sic_table`) or an
    LLM confirm/override of that prior (`llm_hybrid`). Low confidence, `llm_fallback`
    (LLM alone on an ambiguous SIC), and `default` are NOT trusted for driving a rule —
    they degrade to current behavior. `is_financial` is exempt: it's pure SIC 60–67,
    trustworthy regardless of the bucket confidence, so scope-gates may use it directly.
"""
from __future__ import annotations

import csv
import pathlib
from dataclasses import dataclass

_ROUTING_CSV = pathlib.Path(__file__).resolve().parent.parent / "data" / "sector_routing.csv"


@dataclass(frozen=True)
class Sector:
    """One issuer's routing row (see data/sector_routing.csv)."""
    cik: str                     # zero-padded 10-digit
    moodys_methodology: str      # a MOODYS_* bucket constant from the classifier
    sp_sector: str
    is_financial: bool           # pure SIC 60–67 (independent of bucket confidence)
    confidence: str              # high | medium | low
    source: str                  # sic_table | llm_hybrid | llm_fallback | default
    classifier_commit: str       # provenance: classifier version that produced the row


_TABLE: dict[str, Sector] | None = None


def _load() -> dict[str, Sector]:
    out: dict[str, Sector] = {}
    if not _ROUTING_CSV.exists():
        return out
    with open(_ROUTING_CSV, newline="") as f:
        for r in csv.DictReader(f):
            cik = (r.get("cik") or "").strip().zfill(10)
            if not cik or cik == "0000000000":
                continue
            out[cik] = Sector(
                cik=cik,
                moodys_methodology=(r.get("moodys_methodology") or "").strip(),
                sp_sector=(r.get("sp_sector") or "").strip(),
                is_financial=(r.get("is_financial") or "").strip() == "True",
                confidence=(r.get("methodology_confidence") or "").strip(),
                source=(r.get("methodology_source") or "").strip(),
                classifier_commit=(r.get("classifier_commit") or "").strip(),
            )
    return out


def get_sector(cik) -> Sector | None:
    """
    Return the routing Sector for a CIK (any int/str form; zero-padded internally), or
    None when the CIK is absent from the table. None means "no sector known" — callers
    MUST fall back to ungated/current behavior, never guess.

    The table is loaded once and cached at module level (immutable per process).
    """
    global _TABLE
    if _TABLE is None:
        _TABLE = _load()
    if not cik:
        return None
    return _TABLE.get(str(cik).strip().zfill(10))


_TRUSTED_SOURCES = frozenset({"sic_table", "llm_hybrid"})


def sector_trusted(sec: Sector | None) -> bool:
    """
    Whether `sec`'s bucket is trustworthy enough to DRIVE a sector-conditional rule.

    True only for high/medium confidence from a deterministic SIC hit or an LLM
    confirm/override (llm_hybrid). False for None, low confidence, llm_fallback (LLM
    alone on an ambiguous SIC), and default — those degrade the caller to current
    behavior. (is_financial is a separate, pure-SIC signal and does NOT need this gate.)
    """
    return (
        sec is not None
        and sec.confidence in ("high", "medium")
        and sec.source in _TRUSTED_SOURCES
    )


def _reset_cache_for_tests() -> None:
    """Test hook: force the next get_sector() to reload the CSV."""
    global _TABLE
    _TABLE = None
