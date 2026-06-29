"""
Off-Vercel LLM extraction worker.

Why this exists
  The LLM passes (review_filing) take minutes per filing — far past Vercel's 60 s
  serverless function limit. So instead of running them in a request, the fast
  /api/jobs endpoints enqueue work into the `llm_jobs` table, and this always-on
  worker drains the queue from a long-lived process (local now; a small host like
  Railway later). There is NO request timeout here, so a slow, rate-limited run
  can take as long as it needs.

Run it
  python -m src.worker

What it does each loop
  1. reset_stuck() — re-queue jobs left 'running' by a crashed/killed worker.
  2. claim_job()   — atomically take the oldest pending job (status -> 'running').
  3. run it        — for part='full', the whole review_filing pipeline, saved via
                     the existing save_* functions (this worker is just a new
                     caller; review_filing and save_* are unchanged).
  4. mark_done / mark_failed — on success, done; on error, increment attempts and
                     either re-queue (< MAX_ATTEMPTS) or mark 'failed' with the error.
  Idle (empty queue) -> sleep IDLE_SLECONDS and poll again.

Rate limits
  review_filing builds an Anthropic client with max_retries, so 429s are ridden
  out inside a single run (slow, not a failure). A job only consumes an attempt
  on a real exception, not on transient throttling the SDK already retried.
"""

from __future__ import annotations

import logging
import time

from src.store import claim_job, mark_done, mark_failed, reset_stuck

logger = logging.getLogger("worker")

# Tunables. MAX_ATTEMPTS bounds retries of a genuinely failing job; STUCK_MINUTES
# must exceed a legitimate full run on the low rate tier (which can exceed 15 min)
# so reset_stuck never re-queues a job that is still running — hence 30, not 15.
MAX_ATTEMPTS = 3
STUCK_MINUTES = 30
IDLE_SLECONDS = 5


def _run_job(job: dict) -> None:
    """
    Execute one claimed job. Wired parts:
      - 'full'      → the whole review_filing pipeline (all five passes).
      - 'covenants' → covenant-only pipeline (Stage 2c A∪B + breach/waiver) via
                      run_covenants; saves to the covenants table only.
    'breach' (and any other value) is reserved and raises NotImplementedError.

    footnote_review is imported lazily inside each branch (not at module top) so
    `import src.worker` stays free of the anthropic/HTTP stack — matching the
    deferred imports in api/main.py and track.py.
    """
    part = job.get("part", "full")
    cik = job["cik"]
    period_end = job["period_end"]

    if part == "full":
        from src.ingest import get_filings
        from src.footnote_review import review_filing
        from src.store import (
            save_covenants,
            save_findings,
            save_going_concern,
            save_loss_provisions,
        )

        # Mirror track.py / _run_llm_review_task exactly: fetch the 10-K list once,
        # run the 5-pass pipeline (retry-aware client built inside review_filing),
        # and persist via the existing savers. The 5th return (orphan breaches) is
        # logged inside review_filing; not persisted here (REVIEW_FLAGS deferred).
        filings = get_filings(cik, ["10-K"])
        findings, covenants, provisions, going_concern, _orphans = review_filing(
            cik, period_end, filings
        )
        save_findings(cik, period_end, findings)
        save_covenants(cik, period_end, covenants)
        save_loss_provisions(cik, period_end, provisions)
        save_going_concern(cik, period_end, going_concern)
        logger.info(
            "job %s done: cik=%s period=%s findings=%d covenants=%d provisions=%d gc=%d",
            job["id"], cik, period_end,
            len(findings), len(covenants), len(provisions), len(going_concern),
        )
        return

    if part == "covenants":
        # Covenant-only pipeline (Stage 2c A∪B + breach/waiver) — NO going-concern /
        # contingencies / qualitative passes. run_covenants builds the same
        # retry-aware client and yields byte-for-byte the same covenant rows as a
        # full run for this filing. Only save_covenants is needed: the covenants
        # table holds both the 2c-i covenant fields and the 2c-iii breach/near-limit
        # fields. Orphan breaches are logged inside run_covenants (REVIEW_FLAGS
        # deferred). Lazy-imported here to keep `import src.worker` light.
        from src.ingest import get_filings
        from src.footnote_review import run_covenants
        from src.store import save_covenants

        filings = get_filings(cik, ["10-K"])
        covenants, orphans = run_covenants(cik, period_end, filings)
        save_covenants(cik, period_end, covenants)
        logger.info(
            "job %s done (covenants): cik=%s period=%s covenants=%d orphans=%d",
            job["id"], cik, period_end, len(covenants), len(orphans),
        )
        return

    # 'breach' and any other part values are reserved/unwired for now.
    raise NotImplementedError(f"job part {part!r} not implemented yet")


def run_forever(idle_seconds: int = IDLE_SLECONDS) -> None:
    """Drain the llm_jobs queue forever. Ctrl+C to stop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("worker started (MAX_ATTEMPTS=%d, STUCK_MINUTES=%d, idle=%ds)",
                MAX_ATTEMPTS, STUCK_MINUTES, idle_seconds)
    while True:
        # Recover jobs orphaned by a crashed worker. Cheap (indexed) and also
        # catches a mid-run kill from a previous process on startup.
        try:
            n_reset = reset_stuck(STUCK_MINUTES)
            if n_reset:
                logger.warning("reset %d stuck job(s) back to pending", n_reset)
        except Exception:
            logger.exception("reset_stuck failed; continuing")

        try:
            job = claim_job()
        except Exception:
            logger.exception("claim_job failed; backing off")
            time.sleep(idle_seconds)
            continue

        if job is None:
            time.sleep(idle_seconds)          # queue empty
            continue

        logger.info("claimed job %s: cik=%s period=%s part=%s (attempt %d)",
                    job["id"], job["cik"], job["period_end"],
                    job.get("part", "full"), job.get("attempts", 0) + 1)
        try:
            _run_job(job)
            mark_done(job["id"])
        except Exception as e:
            attempts = job.get("attempts", 0) + 1
            logger.exception("job %s failed (attempt %d/%d)", job["id"], attempts, MAX_ATTEMPTS)
            try:
                mark_failed(job["id"], str(e), attempts, MAX_ATTEMPTS)
            except Exception:
                logger.exception("mark_failed also failed for job %s", job["id"])


if __name__ == "__main__":
    run_forever()
