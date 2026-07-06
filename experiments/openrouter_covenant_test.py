#!/usr/bin/env python3
"""
experiments/openrouter_covenant_test.py — capstone: run RAD 2023-03-04 covenants
through the OpenRouter/Bedrock route (USE_OPENROUTER=1) via the real run_covenants
path, and report parity vs. the known direct-Claude 5-covenant baseline.

READ-ONLY: writes nothing to disk/DB. All output goes to the terminal.

What it prints, in order:
  1. Route proof: whether ANTHROPIC_BASE_URL is set in the env (tells you if the
     DIRECT baseline is Anthropic-direct or the APIYI relay), the RESOLVED base_url
     of the client we actually built, and the RESOLVED model string — so you can SEE
     it is hitting OpenRouter with anthropic/claude-haiku-4.5, not silently falling
     through to direct Anthropic.
  2. The covenants run_covenants returns (whole-section, no chunking).
  3. Whether the run completed without a 429 (any 429 the SDK retried is counted),
     plus wall-clock time.
  4. The OpenRouter cost of the run (credits delta across the run).

Run:
    USE_OPENROUTER=1 python experiments/openrouter_covenant_test.py
(The script also force-sets USE_OPENROUTER=1 itself so the capstone can't no-op.)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

# Load .env.local BEFORE importing anything under src (OPENROUTER_API_KEY etc.).
load_dotenv(_REPO_ROOT / ".env.local")

# Force the toggle ON for this capstone so it can't silently no-op if the caller
# forgot to export it. (resolve_model/build_client read the env live.)
os.environ["USE_OPENROUTER"] = "1"

import httpx  # noqa: E402  (transitive dep of the anthropic SDK)

from src.llm_client import build_client, resolve_model, use_openrouter  # noqa: E402
from src.footnote_review import run_covenants  # noqa: E402
from src.ingest import get_filings  # noqa: E402

RAD_CIK = "0000084129"
PERIOD = "2023-03-04"
_OR_BASE = "https://openrouter.ai/api"


def _openrouter_usage() -> float | None:
    """Cumulative credits used on this OpenRouter key (for a before/after delta).
    Returns None if the endpoint can't be reached / key rejected."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    try:
        r = httpx.get(
            f"{_OR_BASE}/v1/credits",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        r.raise_for_status()
        return float(r.json()["data"]["total_usage"])
    except Exception as e:  # noqa: BLE001 — diagnostics only, never fail the run
        print(f"  [credits endpoint unavailable: {e}]")
        return None


class _429Counter(logging.Handler):
    """Counts any log record mentioning a 429 (the anthropic SDK logs retries), so a
    429 the SDK rode out with backoff is still surfaced rather than hidden."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if "429" in record.getMessage() or "rate limit" in record.getMessage().lower():
                self.count += 1
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    # ── 1. Route proof ────────────────────────────────────────────────────────
    print("=" * 72)
    print("ROUTE PROOF")
    print("=" * 72)
    anthropic_base = os.getenv("ANTHROPIC_BASE_URL")
    print(f"  USE_OPENROUTER (resolved)   : {use_openrouter()}")
    print(f"  ANTHROPIC_BASE_URL in env   : {anthropic_base!r}")
    print(f"    -> direct baseline is     : "
          f"{'the APIYI relay' if anthropic_base else 'Anthropic DIRECT (no relay)'}")
    probe = build_client(max_retries=8)
    print(f"  built client base_url       : {probe.base_url}")
    print(f"  resolved model string       : {resolve_model()}")
    if str(probe.base_url).rstrip("/") != _OR_BASE:
        print("  !! base_url is NOT OpenRouter — aborting so we don't mislabel the run.")
        sys.exit(1)
    print()

    # ── 2 + 3. Run RAD covenants (whole-section, no chunking) ──────────────────
    print("=" * 72)
    print(f"RUNNING run_covenants(RAD {PERIOD}) — WHOLE-SECTION (run_covenants does")
    print("no chunking; each pass sends the full located section in one call)")
    print("=" * 72)

    counter = _429Counter()
    for name in ("anthropic", "httpx", "httpcore"):
        logging.getLogger(name).addHandler(counter)
        logging.getLogger(name).setLevel(logging.INFO)

    usage_before = _openrouter_usage()
    filings = get_filings(RAD_CIK, ["10-K"])

    t0 = time.monotonic()
    error = None
    covenants, orphans = [], []
    try:
        covenants, orphans = run_covenants(RAD_CIK, PERIOD, filings)
    except Exception as e:  # noqa: BLE001 — a hard 429/failure is a result to report
        error = e
    elapsed = time.monotonic() - t0
    usage_after = _openrouter_usage()

    print()
    if error is not None:
        print(f"  RUN FAILED: {type(error).__name__}: {error}")
    else:
        print(f"  Covenants found: {len(covenants)}")
        for c in covenants:
            print(f"    - type={c.covenant_type:<28} dir={c.direction:<3} "
                  f"threshold={c.threshold!r:<10} actual={c.reported_actual!r} "
                  f"near_limit={c.near_limit}")
        if orphans:
            print(f"  Orphan breach findings: {len(orphans)}")

    # ── whole-section / 429 verdict ────────────────────────────────────────────
    print()
    print("=" * 72)
    print("RATE-LIMIT / CHUNKING VERDICT")
    print("=" * 72)
    print(f"  Chunking used                : NO (run_covenants is whole-section)")
    print(f"  429s observed (incl. retried): {counter.count}")
    print(f"  Completed without hard error : {error is None}")
    print(f"  Wall-clock                   : {elapsed:.1f}s")
    if error is None and counter.count == 0:
        print("  -> Whole-section ran cleanly with NO 429 — chunking not needed on this route.")
    elif error is None:
        print("  -> Completed, but 429(s) were retried by the SDK — see count above.")

    # ── 4. Cost ────────────────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("COST (OpenRouter credits delta)")
    print("=" * 72)
    if usage_before is not None and usage_after is not None:
        print(f"  total_usage before : {usage_before:.6f} credits")
        print(f"  total_usage after  : {usage_after:.6f} credits")
        print(f"  run cost           : {usage_after - usage_before:.6f} credits (USD)")
    else:
        print("  Could not read credits delta from OpenRouter (see note above).")


if __name__ == "__main__":
    main()
