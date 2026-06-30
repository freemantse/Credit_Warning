#!/usr/bin/env python3
"""
experiments/finding_locality_measure.py — STANDALONE, READ-ONLY locality measurement.

Design question: do LLM findings CLUSTER in distinct regions of a section (MD&A
especially) or are they SPREAD across it? If they cluster, a layer-1 triage that
routes each extractor to only the paragraphs around its findings would save tokens;
if they're spread (union ≈ whole section), routing buys little. This script MEASURES
that on a real filing — it is not a feature.

For a filing+period it (live, or --from-db) collects each extractor's findings, locates
each evidence_quote's character span in the section it came from, and for the MULTI-READ
sections (MD&A read by 4 extractors; debt + risk-factors read by 2 each) reports:
  - per extractor: what fraction of the section its findings span / cover,
  - the UNION (±window paragraphs around all findings) — the fraction still read if
    you routed each extractor only to its finding regions (the headline metric),
  - the OVERLAP — fraction where >=2 extractors point at the same region.

Changes NOTHING and writes NOTHING (no DB, no disk). Imports originals read-only.

Run (AAPL harness check first, then RAD as the real measurement — LIVE):
    python experiments/finding_locality_measure.py AAPL --period 2024-09-28
    python experiments/finding_locality_measure.py RAD  --period 2023-03-04
Flags:
    --period YYYY-MM-DD       10-K reportDate (default: latest)
    --from-db                 read already-extracted rows (zero tokens) instead of
                              re-running the extractors live. NOTE: covenants.source is
                              unioned across sections post-dedupe, so DB attribution is
                              approximate; LIVE is authoritative.
    --window-paragraphs N     ±N-paragraph routing window (default 1)
    --cluster-gap N           merge spans closer than N chars into one band (default 1500)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env.local")

import anthropic  # noqa: E402

from src.footnote_review import (  # noqa: E402
    extract_covenant_breach,
    extract_covenants_broad,
    extract_debt_footnote,
    extract_going_concern,
    extract_loss_provisions,
)
from src.ingest import (  # noqa: E402
    find_filing_for_period,
    get_filings,
    get_filing_text,
    resolve_identifier,
)
from src.llm_review import (  # noqa: E402
    _QUOTE_PREFIX_CHARS,
    _normalize_for_match,
    review_text,
)
from src.sections import locate_sections, section_confidence  # noqa: E402

# Per-extractor read cap on each section (chars actually sent to the model), for the
# read-cap-skew caveat. Values mirror the live constants; the script reports them but
# uses the FULL located slice as the coverage denominator.
_READ_CAP = {
    ("mdna", "tone"): 100_000,        # llm_review.MAX_REVIEW_CHARS
    ("mdna", "cov_b"): 60_000,        # _COV_RECALL_MAX_CHARS
    ("mdna", "breach"): 60_000,       # _BREACH_MAX_SECTION_CHARS
    ("mdna", "gc"): 60_000,           # _GC_MAX_SECTION_CHARS
    ("debt", "cov_a"): 40_000,        # MAX_SECTION_CHARS
    ("debt", "breach"): 60_000,       # capped by debt locator (40k) in practice
    ("risk_factors", "cov_b"): 60_000,
    ("risk_factors", "gc"): 60_000,
}

# The MULTI-READ sections and which extractors read each (label → extractor key).
# Only these matter for the layer-1 dedup question.
_SECTION_READERS = {
    "mdna": [("tone", "MD&A"), ("cov_b", "MD&A"), ("breach", "MD&A"), ("gc", "MD&A")],
    "debt": [("cov_a", "Debt"), ("breach", "Debt footnote")],
    "risk_factors": [("cov_b", "Risk Factors"), ("gc", "Risk Factors")],
}
_EXTRACTOR_NAMES = {
    "tone": "tone/qualitative", "cov_a": "covenant Stage A", "cov_b": "covenant Stage B",
    "breach": "covenant breach", "gc": "going-concern",
}
_MAX_PARA_CHARS = 4_000  # sub-split paragraphs longer than this (older plain-text filings)


# ── Live extractor invocation (returns list of objects each with .evidence_quote) ──
def _run_extractor(key, section_text, label, period, conf, client):
    if key == "tone":
        return review_text(section_text, f"10-K {period}, {label}", client)
    if key == "cov_a":
        return extract_debt_footnote(section_text, f"10-K {period}, {label}", client,
                                     section_conf=conf, period_end=period)
    if key == "cov_b":
        return extract_covenants_broad(section_text, f"10-K {period}, {label} (covenants)", client,
                                       section_label=label, section_conf=conf, period_end=period)
    if key == "breach":
        return extract_covenant_breach(section_text, f"10-K {period}, {label} (breach/waiver)", client,
                                       section_label=label, section_conf=conf, period_end=period)
    if key == "gc":
        return extract_going_concern(section_text, f"10-K {period}, {label}", client,
                                     section_label=label, section_conf=conf, period_end=period)
    raise ValueError(key)


# ── Quote locating: whitespace/typography-tolerant search in RAW section text ──────
def _locate(quote: str, section_text: str) -> tuple[int, int] | None:
    """
    Return (start, end) char span of `quote` within raw `section_text`, mirroring
    quote_in_text's normalization (case, curly quotes/dashes, collapsed whitespace,
    80-char prefix tolerance). Returns None if not locatable.
    """
    norm = _normalize_for_match(quote).rstrip(". …")
    if not norm:
        return None
    tokens = norm.split(" ")

    def _pattern(toks: list[str]) -> re.Pattern:
        parts = []
        for t in toks:
            esc = re.escape(t)
            esc = esc.replace("'", "['’‘]").replace(re.escape("-"), "[-–—]")
            parts.append(esc)
        return re.compile(r"\s+".join(parts), re.IGNORECASE)

    m = _pattern(tokens).search(section_text)
    if m:
        return m.start(), m.end()
    # Prefix fallback (model-truncated quotes), same threshold as quote_in_text.
    if len(norm) > _QUOTE_PREFIX_CHARS:
        pre_tokens, acc = [], 0
        for t in tokens:
            pre_tokens.append(t)
            acc += len(t) + 1
            if acc >= _QUOTE_PREFIX_CHARS:
                break
        m = _pattern(pre_tokens).search(section_text)
        if m:
            return m.start(), m.end()
    return None


# ── Paragraph index of a raw section ───────────────────────────────────────────
def _paragraphs(text: str) -> list[tuple[int, int]]:
    """Paragraph [start,end] spans (split on blank lines; sub-split giant paragraphs)."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for piece in re.split(r"\n\s*\n", text):
        start = text.find(piece, pos) if piece else pos
        if start < 0:
            start = pos
        end = start + len(piece)
        if len(piece) > _MAX_PARA_CHARS:
            s = start
            while s < end:
                spans.append((s, min(s + _MAX_PARA_CHARS, end)))
                s += _MAX_PARA_CHARS
        else:
            spans.append((start, end))
        pos = end
    return [(s, e) for s, e in spans if e > s]


def _union_len(spans: list[tuple[int, int]]) -> int:
    """Total chars covered by a set of [start,end) spans (merged)."""
    if not spans:
        return 0
    merged = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return sum(e - s for s, e in merged)


def _window_spans(finding_spans, paras, n_window) -> list[tuple[int, int]]:
    """Expand each finding span to the paragraph(s) it touches, ±n_window neighbors."""
    out = []
    for fs, fe in finding_spans:
        touched = [i for i, (ps, pe) in enumerate(paras) if not (fe <= ps or fs >= pe)]
        if not touched:
            continue
        lo = max(0, min(touched) - n_window)
        hi = min(len(paras) - 1, max(touched) + n_window)
        out.append((paras[lo][0], paras[hi][1]))
    return out


def _clusters(spans, gap) -> list[tuple[int, int]]:
    """Merge spans whose gap is < `gap` into bands; return the band spans."""
    if not spans:
        return []
    bands = []
    for s, e in sorted(spans):
        if bands and s - bands[-1][1] < gap:
            bands[-1] = (bands[-1][0], max(bands[-1][1], e))
        else:
            bands.append((s, e))
    return bands


def _pct(n, d) -> float:
    return (100.0 * n / d) if d else 0.0


# ── --from-db loader (approximate attribution; LIVE is authoritative) ─────────────
def _quotes_from_db(cik, period):
    """Return {section_name: {extractor_key: [quotes]}} from stored rows (zero tokens)."""
    from src.store import (
        _client, get_covenants_grouped, get_findings_grouped, get_loss_provisions_grouped,
    )
    cik10 = cik.zfill(10)
    out: dict = {"mdna": {}, "debt": {}, "risk_factors": {}}

    def add(section, key, quote):
        if section in out and quote:
            out[section].setdefault(key, []).append(quote)

    # tone → llm_findings (source contains "MD&A")
    for row in get_findings_grouped(cik10).get(cik10, {}).get(period, []):
        if "MD&A" in (row.get("source") or ""):
            add("mdna", "tone", row.get("evidence_quote"))
    # provisions (single-read; collected but not in multi-read sections)
    _ = get_loss_provisions_grouped(cik10)
    # covenants → source is unioned; attribute to whichever multi-read section contains it (caller locates)
    for row in get_covenants_grouped(cik10).get(cik10, {}).get(period, []):
        q = row.get("evidence_quote")
        for section in ("debt", "mdna", "risk_factors"):
            add(section, "cov_db", q)  # ambiguous: tried against each section at locate time
    # going_concern → has explicit section column (no public grouped reader; direct query)
    gc = _client().table("going_concern").select("evidence_quote, section").eq(
        "cik", cik10).eq("period_end", period).execute()
    for row in (gc.data or []):
        sec = (row.get("section") or "").lower()
        if "md&a" in sec or "mdna" in sec:
            add("mdna", "gc", row.get("evidence_quote"))
        elif "risk" in sec:
            add("risk_factors", "gc", row.get("evidence_quote"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only finding-locality measurement (no DB/disk writes).")
    ap.add_argument("ticker_or_cik")
    ap.add_argument("--period", default=None)
    ap.add_argument("--from-db", action="store_true")
    ap.add_argument("--window-paragraphs", type=int, default=1)
    ap.add_argument("--cluster-gap", type=int, default=1500)
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

    mode = "FROM-DB (approx attribution)" if args.from_db else "LIVE (authoritative)"
    print(f"=== Finding-locality measurement: {args.ticker_or_cik} (cik={cik}) period={period} ===")
    print(f"    mode={mode}  window=±{args.window_paragraphs}¶  cluster_gap={args.cluster_gap} chars")

    text = get_filing_text(cik, filing["accessionNumber"], filing["primaryDocument"])
    sections = locate_sections(text)
    client = None if args.from_db else anthropic.Anthropic(max_retries=8)
    db_quotes = _quotes_from_db(cik, period) if args.from_db else None

    total_unlocated = 0
    for sec_name, readers in _SECTION_READERS.items():
        sec = sections.get(sec_name)
        print(f"\n{'='*78}\n## Section: {sec_name}")
        if sec is None:
            print("    NOT LOCATED — skipped.")
            continue
        body = sec.text
        L = len(body)
        conf = section_confidence(sec)
        paras = _paragraphs(body)
        print(f"    located: {L:,} chars, {len(paras)} paragraphs, confidence={conf}")

        per_extractor_spans: dict[str, list[tuple[int, int]]] = {}
        per_extractor_windows: dict[str, list[tuple[int, int]]] = {}
        unlocated_here = 0

        for key, label in readers:
            cap = _READ_CAP.get((sec_name, key))
            cap_note = f"reads first {cap:,} chars" + ("  (< section!)" if cap and cap < L else "")
            # gather quotes
            if args.from_db:
                if key == "cov_a" or key == "cov_b":
                    quotes = db_quotes.get(sec_name, {}).get("cov_db", [])
                else:
                    quotes = db_quotes.get(sec_name, {}).get(key, [])
            else:
                findings = _run_extractor(key, body, label, period, conf, client)
                quotes = [getattr(f, "evidence_quote", "") for f in findings]

            spans = []
            n_unloc = 0
            for q in quotes:
                loc = _locate(q, body) if q else None
                if loc:
                    spans.append(loc)
                else:
                    n_unloc += 1
            unlocated_here += n_unloc
            per_extractor_spans[key] = spans
            windows = _window_spans(spans, paras, args.window_paragraphs)
            per_extractor_windows[key] = windows

            envelope = _pct(max((e for _, e in spans), default=0) - min((s for s, _ in spans), default=0), L) if spans else 0.0
            covered = _pct(_union_len(spans), L)
            window_cov = _pct(_union_len(windows), L)
            print(f"    [{_EXTRACTOR_NAMES[key]:18}] {cap_note}")
            print(f"        findings located={len(spans)} unlocated={n_unloc} | "
                  f"span_envelope={envelope:.0f}%  covered={covered:.0f}%  ±{args.window_paragraphs}¶_window={window_cov:.0f}%")

        # Union across all extractors (the headline routing metric)
        all_windows = [w for ws in per_extractor_windows.values() for w in ws]
        all_spans = [s for ss in per_extractor_spans.values() for s in ss]
        union_pct = _pct(_union_len(all_windows), L)

        # Overlap: chars covered by >=2 extractors' windows
        overlap_spans = []
        keys = list(per_extractor_windows)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = per_extractor_windows[keys[i]], per_extractor_windows[keys[j]]
                for s1, e1 in a:
                    for s2, e2 in b:
                        lo, hi = max(s1, s2), min(e1, e2)
                        if hi > lo:
                            overlap_spans.append((lo, hi))
        overlap_pct = _pct(_union_len(overlap_spans), L)
        overlap_of_union = _pct(_union_len(overlap_spans), _union_len(all_windows)) if all_windows else 0.0

        bands = _clusters(all_spans, args.cluster_gap)
        bands_pct = _pct(_union_len(bands), L)

        # Headline line
        parts = [f"{_EXTRACTOR_NAMES[k]} {_pct(_union_len(per_extractor_windows[k]), L):.0f}%" for k, _ in readers]
        print(f"\n  >>> {sec_name}: " + "; ".join(parts))
        print(f"  >>> UNION (±{args.window_paragraphs}¶ around all findings) = {union_pct:.0f}% of section "
              f"| OVERLAP(>=2 extractors) = {overlap_pct:.0f}% of section ({overlap_of_union:.0f}% of union)")
        print(f"  >>> clustering: {len(bands)} band(s) occupying {bands_pct:.0f}% of section")
        if union_pct >= 80:
            print("  >>> READING: findings SPREAD — routing to finding-windows saves little here.")
        elif union_pct <= 40:
            print("  >>> READING: findings CLUSTER — routing could read far less than the whole section.")
        else:
            print("  >>> READING: PARTIAL clustering — moderate savings possible.")
        total_unlocated += unlocated_here

    print(f"\n{'='*78}")
    print("CAVEATS (read before trusting the numbers):")
    print(f"  - UNLOCATED quotes total: {total_unlocated}. Each is a quote the tolerant search")
    print("    could not place (model truncation / normalization edge) — it lowers confidence in")
    print("    the coverage %, since an unplaced finding contributes no span.")
    print("  - READ-CAP SKEW on MD&A: tone reads 100k, but covenant-B/breach/GC read only the first")
    print("    60k — so 3 of 4 MD&A extractors CANNOT have findings past 60k; interpret the union")
    print("    accordingly (the section tail is already unread by them today).")
    print("  - ONE FILING != A LAW: this measures actual findings for a single filing. AAPL is sparse")
    print("    (few/no findings → coverage may be ~0 or undefined); a covenant-rich distressed filing")
    print("    (e.g. RAD 2023-03-04, run LIVE) is needed to see whether findings truly cluster.")
    print("  - FROM-DB attribution is approximate (covenants.source is unioned post-dedupe); LIVE is")
    print("    authoritative for which section a covenant quote belongs to.")
    print("\n=== done (no DB/disk writes performed) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
