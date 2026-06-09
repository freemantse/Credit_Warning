"""
Locate specific footnote sections inside a raw 10-K filing document.

Why this module exists:
  A 10-K's primary HTML document is huge — often 100k–500k characters. The two
  sections we care about for credit risk live deep in the financial statements:
    - the long-term-DEBT footnote (maturities, interest, covenants), and
    - the COMMITMENTS AND CONTINGENCIES footnote (litigation, loss provisions).
  Naively sending the first N characters to an LLM (as the old pipeline did with
  text[:12000]) never reaches them. This module strips the HTML to text and
  slices out just the relevant section so only a small, on-target excerpt is sent
  to the LLM.

Strategy (deterministic, no LLM, no extra dependencies):
  1. Convert the HTML to plain text, preserving line breaks at block boundaries.
  2. Build an index of candidate heading lines (note titles).
  3. For each target section, find the first heading whose nearby text matches the
     section's keyword pattern, then slice from that heading to the next heading
     (bounded to a max window so token spend stays predictable).
  4. If no heading anchors, fall back to keyword-density chunk scoring and return
     the best chunk, flagged with heading_matched=None to signal lower confidence.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


# Max characters returned per section. ~20k chars ≈ 5k tokens — enough to cover a
# debt or contingencies footnote while keeping the LLM call cheap and bounded.
_MAX_SECTION_CHARS = 20_000

# Fallback chunking parameters (used only when heading anchoring fails).
_CHUNK_SIZE = 8_000
_CHUNK_OVERLAP = 1_000


@dataclass
class Section:
    """
    One located footnote section.

    Attributes:
        name:            "debt" or "contingencies".
        text:            The extracted plain-text slice (≤ _MAX_SECTION_CHARS).
        char_start:      Start offset in the stripped full text.
        char_end:        End offset in the stripped full text.
        heading_matched: The heading line that anchored the slice, or None when
                         the slice came from the chunk-density fallback (lower
                         confidence — downstream may treat it more cautiously).
    """
    name: str
    text: str
    char_start: int
    char_end: int
    heading_matched: str | None


# ── Section heading patterns ───────────────────────────────────────────────────
# Per section, an ORDERED list of heading regexes tried most-specific first. The
# canonical footnote title ("Commitments and contingencies", "Note N – Debt")
# must win over earlier weaker matches like a "Legal Proceedings" Item-3 stub or a
# table-of-contents line. Each is matched case-insensitively against heading lines.
_SECTION_HEADING_PATTERNS: dict[str, list[re.Pattern]] = {
    "debt": [
        re.compile(r"long[\s-]*term debt", re.IGNORECASE),
        re.compile(r"note\s+\d+\s*[–—:.\-]+.*\bdebt\b", re.IGNORECASE),
        re.compile(r"^\s*(term\s+)?debt\s*$", re.IGNORECASE),
        re.compile(r"\bborrowings\b|notes?\s+payable|credit facilit", re.IGNORECASE),
    ],
    "contingencies": [
        re.compile(r"commitments?\s+and\s+contingenc", re.IGNORECASE),
        re.compile(r"loss conting", re.IGNORECASE),
        re.compile(r"^\s*(other\s+)?legal proceedings\s*$", re.IGNORECASE),
        re.compile(r"\blitigation\b", re.IGNORECASE),
    ],
}

# Density-scoring pattern for the chunk fallback (no headings found): combines all
# of a section's keywords into one alternation.
_SECTION_DENSITY_PATTERNS: dict[str, re.Pattern] = {
    "debt": re.compile(
        r"long[\s-]*term debt|\bborrowings\b|notes?\s+payable|credit facilit|"
        r"maturit|covenant",
        re.IGNORECASE,
    ),
    "contingencies": re.compile(
        r"commitments?\s+and\s+contingenc|legal proceedings|\blitigation\b|"
        r"loss conting",
        re.IGNORECASE,
    ),
}

# A located section must have at least this many characters of body after its
# heading; shorter slices are table-of-contents entries (heading immediately
# followed by another heading) and are skipped.
_MIN_SECTION_BODY = 400

# Content keywords that distinguish the prose FOOTNOTE from a same-named
# balance-sheet line item (which appears earlier, inside a numeric table). Among
# all heading matches, the slice with the most content hits wins — so "Long-term
# debt" / "Commitments and contingencies" resolve to the discussion, not the
# one-line balance-sheet figure.
_SECTION_CONTENT_PATTERNS: dict[str, re.Pattern] = {
    "debt": re.compile(
        r"maturit|covenant|interest rate|due\s+(in|within|on)|"
        r"senior notes|principal amount|redeem|indenture|fixed[\s-]*rate",
        re.IGNORECASE,
    ),
    "contingencies": re.compile(
        r"litigation|lawsuit|reasonably possible|accrued|legal proceeding|"
        r"damages|settlement|alleg|class action|regulatory",
        re.IGNORECASE,
    ),
}

# A heading line either starts with a "NOTE n" / "n." prefix (optionally followed
# by a title) or is a short Title/UPPER-case line. Footnote titles in 10-Ks
# reliably take one of these shapes. The whole line must be ≤ 90 chars so we don't
# treat a long body sentence as a heading.
_HEADING_RE = re.compile(
    r"^\s*(?:(?:NOTE\s+\d+|Note\s+\d+|\d{1,2}\.)[\s.–—:-]*[A-Za-z][^\n]{0,80}"
    r"|[A-Z][A-Za-z0-9 ,&/'–—.-]{2,80})\s*$"
)

# Inline-XBRL wrapper tags (<ix:...>) carry no display text — strip the tags but
# keep their inner content.
_IX_TAG_RE = re.compile(r"</?ix:[^>]*>", re.IGNORECASE)
# Block-level tags whose close should become a newline so headings land on their
# own line after stripping.
_BLOCK_CLOSE_RE = re.compile(
    r"</(p|div|tr|td|th|li|h[1-6]|table)>|<br\s*/?>", re.IGNORECASE
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NEWLINE_RE = re.compile(r"\n{2,}")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+\n")


def html_to_text(raw: str) -> str:
    """
    Convert a filing's HTML to plain text, preserving heading line breaks.

    Steps:
      1. Drop <script>/<style> blocks entirely.
      2. Strip inline-XBRL <ix:...> wrappers (keep inner text).
      3. Turn block-tag closes and <br> into newlines so block content separates.
      4. Strip all remaining tags, unescape HTML entities, and tidy whitespace.

    Robust to the markup variation across filers; older plain-text filings pass
    through largely unchanged (they contain few/no tags).
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    text = _IX_TAG_RE.sub("", text)
    text = _BLOCK_CLOSE_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub("", text)
    text = html.unescape(text)
    # Collapse runs of blank lines and strip trailing spaces before newlines.
    text = _TRAILING_SPACE_RE.sub("\n", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text


def _heading_index(text: str) -> list[tuple[int, str]]:
    """
    Return (char_offset, heading_line) for every candidate heading in `text`.

    Offsets are positions in `text` so a section can be sliced heading→next-heading.
    """
    headings: list[tuple[int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and _HEADING_RE.match(stripped):
            headings.append((offset, stripped))
        offset += len(line)
    return headings


def _slice_to_next_heading(
    text: str, headings: list[tuple[int, str]], idx: int
) -> tuple[int, int]:
    """Return (start, end) char offsets from heading `idx` to the next heading."""
    start = headings[idx][0]
    end = headings[idx + 1][0] if idx + 1 < len(headings) else len(text)
    # Bound the window so token spend is predictable even if the next heading is
    # far away (or absent).
    end = min(end, start + _MAX_SECTION_CHARS)
    return start, end


def _anchor_section(
    text: str,
    headings: list[tuple[int, str]],
    patterns: list[re.Pattern],
    content: re.Pattern,
) -> Section | None:
    """
    Find a section by matching an ordered list of heading regexes, then picking
    the candidate slice that most looks like the real footnote.

    Algorithm:
      1. Collect every heading that matches ANY of the section's heading regexes
         and yields ≥ _MIN_SECTION_BODY chars of body (skips table-of-contents
         lines, where a heading is immediately followed by the next heading).
      2. Score each candidate by how many `content` keywords its slice contains.
         The footnote (prose: maturities, covenants / litigation, lawsuits) scores
         far higher than a same-named balance-sheet line item (a number in a table).
      3. Return the highest-scoring candidate. If every candidate scores 0, fall
         back to the earliest match in heading-pattern priority order.
    """
    candidates: list[tuple[int, int, int, str]] = []  # (score, start, end, heading)
    seen_offsets: set[int] = set()
    for pattern in patterns:
        for i, (off, heading) in enumerate(headings):
            if off in seen_offsets or not pattern.search(heading):
                continue
            start, end = _slice_to_next_heading(text, headings, i)
            if end - start < _MIN_SECTION_BODY:
                continue
            seen_offsets.add(off)
            score = len(content.findall(text[start:end]))
            candidates.append((score, start, end, heading))

    if not candidates:
        return None

    # Best by content score; ties broken by earliest position (stable, document order).
    best = max(candidates, key=lambda c: (c[0], -c[1]))
    _, start, end, heading = best
    return Section("", text[start:end], start, end, heading)


def _chunk_fallback(text: str, pattern: re.Pattern) -> Section | None:
    """
    Fallback when no heading anchors: score overlapping chunks by keyword density.

    Returns the highest-scoring chunk (most pattern matches), or None if no chunk
    contains the pattern at all. heading_matched is None to flag lower confidence.
    """
    best: tuple[int, int, int] | None = None  # (score, start, end)
    step = _CHUNK_SIZE - _CHUNK_OVERLAP
    for start in range(0, len(text), step):
        end = min(start + _CHUNK_SIZE, len(text))
        score = len(pattern.findall(text[start:end]))
        if score and (best is None or score > best[0]):
            best = (score, start, end)
        if end == len(text):
            break
    if best is None:
        return None
    _, start, end = best
    return Section(
        name="",
        text=text[start:end],
        char_start=start,
        char_end=end,
        heading_matched=None,
    )


def locate_sections(filing_html: str) -> dict[str, Section | None]:
    """
    Locate the debt and contingencies footnotes in a raw filing document.

    Args:
        filing_html: The raw filing text/HTML from ingest.get_filing_text().

    Returns:
        {"debt": Section|None, "contingencies": Section|None}. A value is None when
        neither heading anchoring nor the chunk fallback found the section.
    """
    text = html_to_text(filing_html)
    headings = _heading_index(text)

    out: dict[str, Section | None] = {}
    for name, patterns in _SECTION_HEADING_PATTERNS.items():
        section = _anchor_section(
            text, headings, patterns, _SECTION_CONTENT_PATTERNS[name]
        )
        if section is None:
            section = _chunk_fallback(text, _SECTION_DENSITY_PATTERNS[name])
        if section is not None:
            section.name = name
        out[name] = section
    return out
