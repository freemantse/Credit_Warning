#!/usr/bin/env python3
"""
experiments/covenant_chunk_test.py — STANDALONE, READ-ONLY covenant-chunking experiment.

Goal: run all 5 covenant passes (Stage A debt, Stage B MD&A, Stage B risk-factors,
Breach debt, Breach MD&A) with the section text split into OVERLAPPING CHUNKS sized
so each LLM call's total input (chunk + fixed prompt) stays under ~8,000 tokens —
proving large filings (e.g. Rite Aid) can run without tripping a 10k-tokens/min
limit, while producing the same covenants as the original whole-section extraction.

This file changes NOTHING in the app. It imports the original extraction functions
and prompts read-only and reuses them UNCHANGED by feeding them chunked section_text
(plus max_chars=len(chunk) so the chunk is never re-truncated). It writes nothing to
disk or the database — all output goes to the terminal.

Run (test AAPL first as a harness check, then RAD):
    python experiments/covenant_chunk_test.py AAPL --period 2024-09-28
    python experiments/covenant_chunk_test.py RAD  --period 2023-03-04 --compare
Flags:
    --period YYYY-MM-DD     fiscal period end (10-K reportDate). If omitted, the
                            latest available 10-K period is used.
    --compare               also run the ORIGINAL whole-section passes and diff the
                            covenant sets (the whole-section run may itself rate-limit
                            on a large filing — that is reported, not crashed).
    --match-original-caps   cap the TOTAL chunked input per pass to the same per-pass
                            max_chars the original uses (40k/60k), for a strict
                            apples-to-apples parity test (isolates "did chunking lose
                            anything" from "did chunking read more of the section").
    --chunk-tokens N        target tokens per call (default 8000).
    --overlap N             chunk overlap in chars (default 1000).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `src...` importable when run as a plain script from anywhere, and load the
# same .env.local the app uses (Supabase creds are unused here; ANTHROPIC_API_KEY
# is what matters). No second client style — we build one anthropic client below.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env.local")

import anthropic  # noqa: E402

# ── Original functions + prompts, imported READ-ONLY (never modified here) ──────
from src.footnote_review import (  # noqa: E402
    COVENANT_BREACH_SYSTEM,
    COVENANT_BREACH_USER,
    COVENANT_PASS_PRECISE,
    COVENANT_PASS_RECALL,
    COVENANT_SYSTEM,
    COVENANT_USER,
    _apply_breach_findings,
    _dedupe_covenants,
    extract_covenant_breach,
    extract_covenants_broad,
    extract_debt_footnote,
)
from src.ingest import (  # noqa: E402
    find_filing_for_period,
    get_filings,
    get_filing_text,
    resolve_identifier,
)
from src.sections import locate_sections, section_confidence  # noqa: E402

# Per-pass per-pass per-call max_chars the ORIGINAL uses (for --match-original-caps
# and the whole-section --compare run). Mirrors footnote_review's constants without
# importing private ones we don't need; values are the documented caps.
_ORIG_MAX_CHARS = {"stage_a": 40_000, "stage_b": 60_000, "breach": 60_000}
_SECTION_TEXT_PLACEHOLDER = "{SECTION_TEXT}"
_PASS_MODE_PLACEHOLDER = "{PASS_MODE}"


# ── Fixed-prompt overhead (computed at runtime from the imported prompt strings, so
# it stays accurate if the prompts ever change). This is the chars sent on EVERY
# call regardless of section text — system prompt + user template minus the
# {SECTION_TEXT} placeholder, plus the pass-mode block for Stage A/B. ────────────
def _fixed_prompt_chars(pass_name: str) -> int:
    if pass_name == "breach":
        return len(COVENANT_BREACH_SYSTEM) + len(COVENANT_BREACH_USER) - len(_SECTION_TEXT_PLACEHOLDER)
    pass_mode = COVENANT_PASS_PRECISE if pass_name == "stage_a" else COVENANT_PASS_RECALL
    return (
        len(COVENANT_SYSTEM)
        + len(COVENANT_USER)
        - len(_SECTION_TEXT_PLACEHOLDER)
        - len(_PASS_MODE_PLACEHOLDER)
        + len(pass_mode)
    )


def _chunk_size_chars(pass_name: str, target_tokens: int, safety_frac: float = 0.10) -> int:
    """
    chunk_chars = target_tokens*4 - fixed_prompt_chars(pass) - safety_margin.
    The safety margin (default 10%) absorbs the chars/4 approximation being optimistic
    on number/symbol-dense SEC text. Never returns < 4000 chars (a sane floor).
    """
    budget = target_tokens * 4 - _fixed_prompt_chars(pass_name)
    sized = int(budget * (1.0 - safety_frac))
    return max(4_000, sized)


def _chunks(text: str, size: int, overlap: int) -> list[tuple[int, str]]:
    """Overlapping windows of `text`. Returns [(char_start, chunk_text), ...]."""
    text = text or ""
    if len(text) <= size:
        return [(0, text)] if text.strip() else []
    out: list[tuple[int, str]] = []
    step = max(1, size - overlap)
    start = 0
    while start < len(text):
        out.append((start, text[start : start + size]))
        if start + size >= len(text):
            break
        start += step
    return out


def _approx_tokens(n_chars: int) -> int:
    return n_chars // 4


def _cov_brief(c) -> str:
    thr = getattr(c, "threshold", None)
    act = getattr(c, "reported_actual", None)
    q = (getattr(c, "evidence_quote", "") or "")[:120].replace("\n", " ")
    return (
        f"{getattr(c, 'covenant_type', '?')}/{getattr(c, 'direction', '?')} "
        f"thr={thr} actual={act} near_limit={getattr(c, 'near_limit', None)} "
        f"reason={getattr(c, 'near_limit_reason', None)!r} | {q!r}"
    )


def _cov_key(c) -> tuple:
    """Identity for diffing two covenant sets (type+direction+rounded threshold+quote head)."""
    thr = getattr(c, "threshold", None)
    thr_r = round(thr, 2) if isinstance(thr, (int, float)) else None
    q = (getattr(c, "evidence_quote", "") or "").strip().lower()[:80]
    return (getattr(c, "covenant_type", None), getattr(c, "direction", None), thr_r, q)


# ── Chunked runner for one pass over one section ────────────────────────────────
def _run_pass_chunked(
    pass_name: str,
    section,            # Section | None
    section_label: str,
    filing_label_base: str,
    period: str,
    client: anthropic.Anthropic,
    target_tokens: int,
    overlap: int,
    match_original_caps: bool,
) -> list:
    """
    Split section.text into overlapping chunks and call the ORIGINAL pass function
    on each chunk unchanged (max_chars=len(chunk) so it isn't re-truncated). Prints
    per-chunk token estimate + finds. Returns the flat list of findings (NOT yet
    deduped — caller dedupes).
    """
    if section is None:
        print(f"    [{section_label}] section not located — skipped")
        return []

    text = section.text
    if match_original_caps:
        text = text[: _ORIG_MAX_CHARS[pass_name]]  # strict parity: same input ceiling as original

    conf = section_confidence(section)
    size = _chunk_size_chars(pass_name, target_tokens)
    fixed = _fixed_prompt_chars(pass_name)
    chunks = _chunks(text, size, overlap)
    print(
        f"    [{section_label}] {len(text):,} chars → {len(chunks)} chunk(s) "
        f"(size≈{size:,}, overlap={overlap:,}, fixed_prompt≈{fixed:,} chars/~{_approx_tokens(fixed):,} tok)"
    )

    findings: list = []
    for i, (start, chunk) in enumerate(chunks, 1):
        approx_in = _approx_tokens(len(chunk) + fixed)
        flag = "OK" if approx_in < target_tokens else "OVER!"
        if pass_name == "stage_a":
            res = extract_debt_footnote(
                chunk, f"{filing_label_base} [chunk {i}/{len(chunks)}]", client,
                section_conf=conf, period_end=period, max_chars=len(chunk),
            )
        elif pass_name == "stage_b":
            res = extract_covenants_broad(
                chunk, f"{filing_label_base} [chunk {i}/{len(chunks)}]", client,
                section_label=section_label, section_conf=conf, period_end=period,
                max_chars=len(chunk),
            )
        elif pass_name == "breach":
            res = extract_covenant_breach(
                chunk, f"{filing_label_base} [chunk {i}/{len(chunks)}]", client,
                section_label=section_label, section_conf=conf, period_end=period,
                max_chars=len(chunk),
            )
        else:
            raise ValueError(pass_name)
        print(
            f"      chunk {i}/{len(chunks)} @char {start:,}: chunk={len(chunk):,} chars, "
            f"~input_tokens={approx_in:,} [{flag}<{target_tokens}] → {len(res)} found"
        )
        for item in res:
            label = _cov_brief(item) if pass_name != "breach" else (
                f"breach status={getattr(item,'status',None)} ref={getattr(item,'covenant_reference',None)!r} "
                f"| {(getattr(item,'evidence_quote','') or '')[:120]!r}"
            )
            print(f"          - {label}")
        findings.extend(res)
    return findings


# ── Whole-section runner (the ORIGINAL behavior) for --compare ──────────────────
def _run_pass_whole(pass_name, section, section_label, filing_label, period, client):
    if section is None:
        return []
    conf = section_confidence(section)
    if pass_name == "stage_a":
        return extract_debt_footnote(section.text, filing_label, client, section_conf=conf, period_end=period)
    if pass_name == "stage_b":
        return extract_covenants_broad(
            section.text, filing_label, client, section_label=section_label, section_conf=conf, period_end=period
        )
    if pass_name == "breach":
        return extract_covenant_breach(
            section.text, filing_label, client, section_label=section_label, section_conf=conf, period_end=period
        )
    raise ValueError(pass_name)


def _print_covenant_set(title: str, covs: list) -> None:
    print(f"\n  {title}: {len(covs)} covenant(s)")
    for c in covs:
        print(f"    - {_cov_brief(c)}")


def _diff_sets(chunked: list, whole: list) -> None:
    ck = {_cov_key(c): c for c in chunked}
    wk = {_cov_key(c): c for c in whole}
    only_chunked = [ck[k] for k in ck.keys() - wk.keys()]
    only_whole = [wk[k] for k in wk.keys() - ck.keys()]
    both = ck.keys() & wk.keys()
    print("\n  ── DIFF (chunked vs whole-section original) ──")
    print(f"    in BOTH: {len(both)}")
    print(f"    chunked-ONLY: {len(only_chunked)}")
    for c in only_chunked:
        print(f"        + {_cov_brief(c)}")
    print(f"    whole-ONLY: {len(only_whole)}")
    for c in only_whole:
        print(f"        - {_cov_brief(c)}")
    if not only_chunked and not only_whole:
        print("    ✓ identical covenant set — chunking lost nothing and added nothing")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only covenant-chunking experiment (no DB/disk writes).")
    ap.add_argument("ticker_or_cik")
    ap.add_argument("--period", default=None, help="10-K reportDate YYYY-MM-DD (default: latest)")
    ap.add_argument("--compare", action="store_true", help="also run the whole-section original and diff")
    ap.add_argument("--match-original-caps", action="store_true",
                    help="cap chunked input per pass to the original max_chars (strict parity)")
    ap.add_argument("--chunk-tokens", type=int, default=8000)
    ap.add_argument("--overlap", type=int, default=1000)
    args = ap.parse_args(argv)

    cik = resolve_identifier(args.ticker_or_cik)
    filings = get_filings(cik, ["10-K"])
    if args.period:
        period = args.period
    else:
        dated = sorted((f for f in filings if f.get("reportDate")),
                       key=lambda f: f["reportDate"], reverse=True)
        if not dated:
            print("No dated 10-K filings found.")
            return 1
        period = dated[0]["reportDate"]

    filing = find_filing_for_period(filings, period)
    if filing is None:
        print(f"No 10-K matching period {period}.")
        return 1

    print(f"=== Covenant chunk experiment: {args.ticker_or_cik} (cik={cik}) period={period} ===")
    print(f"    chunk_tokens={args.chunk_tokens} overlap={args.overlap} "
          f"match_original_caps={args.match_original_caps} compare={args.compare}")

    text = get_filing_text(cik, filing["accessionNumber"], filing["primaryDocument"])
    sections = locate_sections(text)
    # One retry-aware client (mirrors run_covenants); absorbs stray 429s even though
    # chunks are sized under the limit.
    client = anthropic.Anthropic(max_retries=8)

    # ── CHUNKED RUN ────────────────────────────────────────────────────────────
    print("\n# ===== CHUNKED RUN =====")
    print("\n## Stage A (debt footnote, precise)")
    a_raw = _run_pass_chunked("stage_a", sections.get("debt"), "Debt",
                              f"10-K {period}, Debt", period, client,
                              args.chunk_tokens, args.overlap, args.match_original_caps)
    cov_a = _dedupe_covenants(a_raw, [])

    print("\n## Stage B (MD&A + risk factors, recall sweep)")
    b_raw: list = []
    b_raw += _run_pass_chunked("stage_b", sections.get("mdna"), "MD&A",
                               f"10-K {period}, MD&A (covenants)", period, client,
                               args.chunk_tokens, args.overlap, args.match_original_caps)
    b_raw += _run_pass_chunked("stage_b", sections.get("risk_factors"), "Risk Factors",
                               f"10-K {period}, Risk Factors (covenants)", period, client,
                               args.chunk_tokens, args.overlap, args.match_original_caps)
    cov_b = _dedupe_covenants(b_raw, [])

    covenants = _dedupe_covenants(cov_a, cov_b)

    print("\n## Breach / waiver (debt footnote + MD&A)")
    breaches: list = []
    breaches += _run_pass_chunked("breach", sections.get("debt"), "Debt footnote",
                                  f"10-K {period}, Debt footnote (breach/waiver)", period, client,
                                  args.chunk_tokens, args.overlap, args.match_original_caps)
    breaches += _run_pass_chunked("breach", sections.get("mdna"), "MD&A",
                                  f"10-K {period}, MD&A (breach/waiver)", period, client,
                                  args.chunk_tokens, args.overlap, args.match_original_caps)
    # Dedupe breaches by (status, quote) so overlap-region duplicates don't double-map.
    seen: set = set()
    uniq_breaches: list = []
    for b in breaches:
        k = (getattr(b, "status", None), (getattr(b, "evidence_quote", "") or "").strip().lower()[:80])
        if k in seen:
            continue
        seen.add(k)
        uniq_breaches.append(b)
    orphans = _apply_breach_findings(covenants, uniq_breaches)

    _print_covenant_set("Stage A merged", cov_a)
    _print_covenant_set("Stage B merged", cov_b)
    _print_covenant_set("FINAL covenants (A ∪ B, after breach near_limit mapping)", covenants)
    print(f"\n  breach findings: {len(uniq_breaches)} unique; "
          f"orphans (no matching covenant): {len(orphans)}")
    for o in orphans:
        print(f"    [ORPHAN] status={getattr(o,'status',None)} ref={getattr(o,'covenant_reference',None)!r} "
              f"| {(getattr(o,'evidence_quote','') or '')[:120]!r}")

    # ── COMPARE (whole-section original) ────────────────────────────────────────
    if args.compare:
        print("\n# ===== WHOLE-SECTION ORIGINAL (for --compare) =====")
        print("    (this is the path that can rate-limit on large filings)")
        try:
            wa = _run_pass_whole("stage_a", sections.get("debt"), "Debt",
                                 f"10-K {period}, Debt", period, client)
            wb: list = []
            wb += _run_pass_whole("stage_b", sections.get("mdna"), "MD&A",
                                  f"10-K {period}, MD&A (covenants)", period, client)
            wb += _run_pass_whole("stage_b", sections.get("risk_factors"), "Risk Factors",
                                  f"10-K {period}, Risk Factors (covenants)", period, client)
            whole_cov = _dedupe_covenants(wa, wb)
            wbr: list = []
            wbr += _run_pass_whole("breach", sections.get("debt"), "Debt footnote",
                                   f"10-K {period}, Debt footnote (breach/waiver)", period, client)
            wbr += _run_pass_whole("breach", sections.get("mdna"), "MD&A",
                                   f"10-K {period}, MD&A (breach/waiver)", period, client)
            _apply_breach_findings(whole_cov, wbr)
            _print_covenant_set("WHOLE-SECTION FINAL covenants", whole_cov)
            _diff_sets(covenants, whole_cov)
        except Exception as e:  # noqa: BLE001 — report, don't crash; whole-section may 429/timeout
            print(f"\n  WHOLE-SECTION run did NOT complete: {type(e).__name__}: {e}")
            print("  → This is the expected failure on large filings that motivated chunking;")
            print("    the chunked run above completed, which is the result of interest.")

    print("\n=== done (no DB/disk writes performed) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
