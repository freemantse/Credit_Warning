"""
Locate specific footnote sections inside a raw 10-K filing document.

Strategy (deterministic, no LLM, no extra dependencies):
  1. Convert the HTML to plain text, preserving line breaks at block boundaries.
  2. Build an index of candidate heading lines (note titles).
  3. For each target section, find the first heading whose nearby text matches the
     section's keyword pattern, then slice from that heading to the section's end
     boundary (bounded to a max window so token spend stays predictable).
  4. If no heading anchors, fall back to keyword-density chunk scoring and return the best chunk, flagged with heading_matched=None to signal lower confidence.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


# Max characters returned per section. ~40k chars ≈ 10k tokens — covers a debt
# or contingencies footnote even for large issuers with many instruments.
# The MD&A gets a larger window: it averages ~7,000 words (~45k chars) and the
# credit-relevant prose (liquidity, capital resources) sits well past its
# opening, so 100k chars ≈ 25k tokens covers nearly all MD&As in full — still
# trivial against the review model's context window.
_MAX_SECTION_CHARS = 40_000
_SECTION_MAX_CHARS: dict[str, int] = {
    "mdna": 100_000,
    "debt": 40_000,
    "contingencies": 40_000,
    # ── Stage 2a additions (LLM_EXTRACTOR_PORT §5) ─────────────────────────────
    # risk_factors can be very large (Item 1A often runs 50k–150k chars); window
    # it generously but bounded so token spend stays predictable. The auditor's
    # report and the going-concern footnote are short — the 40k convention covers
    # them (the substantial-doubt / explanatory paragraph sits near their start).
    "risk_factors": 80_000,
    "auditor_report": 40_000,
    "going_concern_footnote": 40_000,
}

# Fallback chunking parameters (used only when heading anchoring fails).
_CHUNK_SIZE = 8_000
_CHUNK_OVERLAP = 1_000


@dataclass
class Section:
    """
    One located filing section.

    Attributes:
        name:            "mdna", "debt", or "contingencies".
        text:            The extracted plain-text slice (≤ the section's max,
                         see _SECTION_MAX_CHARS).
        char_start:      Start offset in the stripped full text.
        char_end:        End offset in the stripped full text.
        heading_matched: The heading line that anchored the slice, or None when the slice came from the chunk-density fallback (lower confidence — downstream may treat it more cautiously).
    """
    name: str
    text: str
    char_start: int
    char_end: int
    heading_matched: str | None


# ── Section heading patterns ───────────────────────────────────────────────────
# Per section, an ORDERED list of heading regexes tried most-specific first. The
# canonical footnote title ("Commitments and contingencies", "Note N – Debt")
# must win over earlier weaker matches like a "Legal Proceedings" Item-3 stub or a table-of-contents line. Each is matched case-insensitively against heading lines.
_SECTION_HEADING_PATTERNS: dict[str, list[re.Pattern]] = {
    "mdna": [
        re.compile(r"management'?s discussion and analysis", re.IGNORECASE),
        re.compile(r"^\s*item\s*7[\s.:–—-]", re.IGNORECASE),
    ],
    "debt": [
        re.compile(r"long[\s-]*term debt", re.IGNORECASE),
        re.compile(r"note\s+\d+\s*[–—:.\-]+.*\bdebt\b", re.IGNORECASE),
        re.compile(r"financing arrangements|borrowing arrangements", re.IGNORECASE),
        re.compile(r"\bindebtedness\b", re.IGNORECASE),
        re.compile(r"credit agreements?|senior notes", re.IGNORECASE),
        re.compile(r"^\s*(term\s+)?debt\s*$", re.IGNORECASE),
        re.compile(r"\bborrowings\b|notes?\s+payable|credit facilit", re.IGNORECASE),
    ],
    "contingencies": [
        # Optional comma covers titles like "Commitments, Contingencies and
        # Guarantees" / "... and Supply Concentrations".
        re.compile(r"commitments?,?\s+(and\s+)?contingenc", re.IGNORECASE),
        re.compile(r"loss conting", re.IGNORECASE),
        # Anchored to the full line (modulo an optional "NOTE n" prefix) so a
        # heading like "Risks Related to Legal Proceedings" doesn't match.
        re.compile(
            r"^\s*(note\s+\d+[\s.–—:-]*)?(other\s+)?legal (proceedings|matters)\s*$",
            re.IGNORECASE,
        ),
        re.compile(r"^\s*(note\s+\d+[\s.–—:-]*)?contingenc(ies|y)\s*$", re.IGNORECASE),
        # Requires "matters/proceedings" so a Risk-Factors heading like
        # "Legal and Regulatory Compliance Risks" doesn't match.
        re.compile(r"legal and regulatory (matters|proceedings)", re.IGNORECASE),
        re.compile(r"\blitigation\b", re.IGNORECASE),
    ],
    # ── Stage 2a additions (LLM_EXTRACTOR_PORT §5) — purely additive ───────────
    # risk_factors: Item 1A. Source for Stage-B covenant recall and Tier-2
    # going-concern precursors.
    "risk_factors": [
        re.compile(r"^\s*item\s*1a[\s.:–—-]", re.IGNORECASE),
        re.compile(r"^\s*risk factors\s*$", re.IGNORECASE),
    ],
    # auditor_report: report of independent registered public accounting firm —
    # the Tier-1 going-concern explanatory / emphasis-of-matter paragraph lives here.
    "auditor_report": [
        re.compile(r"report of independent registered public accounting firm", re.IGNORECASE),
        re.compile(r"^\s*report of independent (auditors?|registered)", re.IGNORECASE),
        re.compile(r"opinion on the financial statements", re.IGNORECASE),
    ],
    # going_concern_footnote: management's ASC 205-40 evaluation / basis-of-
    # presentation note carrying the formal substantial-doubt language (Tier-1).
    "going_concern_footnote": [
        re.compile(r"^\s*(note\s+\d+[\s.–—:-]*)?(liquidity and )?going concern\s*$", re.IGNORECASE),
        re.compile(r"\bgoing concern\b", re.IGNORECASE),
        re.compile(r"^\s*(note\s+\d+[\s.–—:-]*)?basis of presentation\s*$", re.IGNORECASE),
        re.compile(r"substantial doubt", re.IGNORECASE),
    ],
    # pension_footnote: the defined-benefit pension / retirement-benefits note
    # carrying the "Funded Status" table (PBO, fair value of plan assets). Source
    # for the LLM pension-fallback flag when the XBRL funded-status tags are absent
    # (~81% of filers). Most-specific note titles first, then table-anchor phrases.
    "pension_footnote": [
        re.compile(r"^\s*(note\s+\d+[\s.–—:-]*)?pension and other post[\s-]?retirement benefit", re.IGNORECASE),
        # Real note titles that end in "... Plans" carry the funded-status table
        # but no "benefit" word — e.g. Flowers Foods' "Note 21. Postretirement
        # Plans", or "Pension and Postretirement Plans" / "Retirement Plans".
        # Without this the locator anchored on a later subheading ("Pension
        # Benefits") sitting BELOW the table and truncated the slice.
        re.compile(
            r"^\s*(note\s+\d+[\s.–—:-]*)?(defined benefit\s+)?(employee\s+)?"
            r"(pension|post[\s-]?retirement|retirement)"
            r"( and (other )?post[\s-]?retirement)?( benefit)? plans?\s*$",
            re.IGNORECASE,
        ),
        re.compile(r"^\s*(note\s+\d+[\s.–—:-]*)?(employee\s+)?(retirement|pension)( and other post[\s-]?retirement)? benefit(s| plans)", re.IGNORECASE),
        re.compile(r"^\s*(note\s+\d+[\s.–—:-]*)?employee benefit plans?\s*$", re.IGNORECASE),
        re.compile(r"defined benefit (pension )?plans?", re.IGNORECASE),
        re.compile(r"projected benefit obligation", re.IGNORECASE),  # table anchor
        re.compile(r"\bfunded status\b", re.IGNORECASE),             # table anchor
    ],
}

# Density-scoring pattern for the chunk fallback (no headings found): combines all
# of a section's keywords into one alternation.
_SECTION_DENSITY_PATTERNS: dict[str, re.Pattern] = {
    "mdna": re.compile(
        r"liquidity|capital resources|going concern|covenant|cash flow|"
        r"refinanc|material weakness|substantial doubt",
        re.IGNORECASE,
    ),
    "debt": re.compile(
        r"long[\s-]*term debt|\bborrowings\b|notes?\s+payable|credit facilit|"
        r"financing arrangements|\bindebtedness\b|senior notes|"
        r"maturit|covenant",
        re.IGNORECASE,
    ),
    "contingencies": re.compile(
        r"commitments?,?\s+(and\s+)?contingenc|legal (proceedings|matters)|"
        r"\blitigation\b|loss conting",
        re.IGNORECASE,
    ),
    # ── Stage 2a additions ─────────────────────────────────────────────────────
    "risk_factors": re.compile(
        r"risk factors|adversely affect|could (harm|materially)|substantial doubt|"
        r"our indebtedness|covenant",
        re.IGNORECASE,
    ),
    "auditor_report": re.compile(
        r"report of independent|we have audited|critical audit matter|"
        r"substantial doubt|going concern|basis for opinion",
        re.IGNORECASE,
    ),
    "going_concern_footnote": re.compile(
        r"going concern|substantial doubt|ability to continue|recurring losses|"
        r"basis of presentation",
        re.IGNORECASE,
    ),
    "pension_footnote": re.compile(
        r"projected benefit obligation|fair value of plan assets|funded status|"
        r"net periodic benefit cost|defined benefit|pension",
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
    "mdna": re.compile(
        r"liquidity|capital resources|going concern|covenant|cash flow|"
        r"refinanc|material weakness|substantial doubt",
        re.IGNORECASE,
    ),
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
    # ── Stage 2a additions ─────────────────────────────────────────────────────
    # Content keywords that distinguish the real prose section from a same-named
    # table-of-contents stub (the slice with the most hits wins among candidates).
    "risk_factors": re.compile(
        r"adversely|could (harm|affect|result)|may (not|be unable)|"
        r"our (business|ability|indebtedness)|covenant|substantial doubt|liquidity",
        re.IGNORECASE,
    ),
    "auditor_report": re.compile(
        r"we have audited|basis for opinion|critical audit matter|"
        r"substantial doubt|going concern|opinion on the financial",
        re.IGNORECASE,
    ),
    "going_concern_footnote": re.compile(
        r"substantial doubt|ability to continue|recurring losses|"
        r"negative (working capital|cash flows)|sufficient liquidity|"
        r"obtain additional financing",
        re.IGNORECASE,
    ),
    "pension_footnote": re.compile(
        r"projected benefit obligation|fair value of plan assets|funded status|"
        r"net periodic benefit cost|defined benefit",
        re.IGNORECASE,
    ),
}

# A heading line either starts with a "NOTE n" / "n." / "Item n" prefix
# (optionally followed by a title) or is a short Title/UPPER-case line. Footnote
# titles in 10-Ks reliably take one of these shapes. The whole line must be short
# so we don't treat a long body sentence as a heading. "Item N." lines get a
# longer tail allowance (120 chars) because full Item-7 titles run long, e.g.
# "Item 7. Management's Discussion and Analysis of Financial Condition and
# Results of Operations" — the unambiguous "Item N" prefix keeps this safe.
_HEADING_RE = re.compile(
    r"^\s*(?:(?:NOTE\s+\d+|Note\s+\d+|\d{1,2}\.)[\s.–—:-]*[A-Za-z][^\n]{0,80}"
    r"|(?:ITEM|Item)\s+\d{1,2}[A-Ba-b]?[\s.–—:-][^\n]{0,120}"
    r"|[A-Z][A-Za-z0-9 ,&/'–—.-]{2,80})\s*$"
)

# Per-section END patterns. Sections CONTAIN their own subheadings, so slicing
# to the very next heading would cut them off after a few hundred chars. The
# MD&A ("Overview", "Results of Operations", ...) runs until the heading that
# starts the NEXT Item (7A or 8). Footnotes ("Term Debt", "U.S. Cell Phone
# Litigation", ...) run until the heading that starts the NEXT numbered note or
# Item. Filings whose notes carry no "NOTE n" / "n." prefix find no boundary and
# run to the max-chars cap instead — the relevant note still leads the slice.
_NOTE_OR_ITEM_BOUNDARY_RE = re.compile(
    r"^\s*(note\s+\d+\b|item\s+\d{1,2}[ab]?\b|\d{1,2}\.\s)", re.IGNORECASE
)
# Risk factors (Item 1A) ends at the next Item — 1B (unresolved staff comments),
# 1C (cybersecurity, post-2023), or 2 (properties). Dedicated so a "1." numbered
# bullet inside the risk prose can't prematurely close the slice.
_RISK_FACTORS_END_RE = re.compile(
    r"^\s*item\s*1[bc][\s.:–—-]|^\s*item\s*2[\s.:–—-]|unresolved staff comments",
    re.IGNORECASE,
)
# Auditor's report ends where the financial statements begin (the report precedes
# them). The going-concern explanatory paragraph sits near the report's start, so
# the 40k cap also bounds it safely if no boundary heading is found.
_AUDITOR_END_RE = re.compile(
    r"consolidated balance sheets?|consolidated statements of "
    r"(operations|income|comprehensive|cash flows|stockholders|changes)|"
    r"notes to (the )?(consolidated )?financial statements",
    re.IGNORECASE,
)
_SECTION_END_PATTERNS: dict[str, re.Pattern] = {
    "mdna": re.compile(
        r"item\s*7a[\s.:–—-]|quantitative and qualitative disclosures|"
        r"item\s*8[\s.:–—-]|financial statements and supplementary",
        re.IGNORECASE,
    ),
    "debt": _NOTE_OR_ITEM_BOUNDARY_RE,
    "contingencies": _NOTE_OR_ITEM_BOUNDARY_RE,
    # ── Stage 2a additions ─────────────────────────────────────────────────────
    "risk_factors": _RISK_FACTORS_END_RE,
    "auditor_report": _AUDITOR_END_RE,
    "going_concern_footnote": _NOTE_OR_ITEM_BOUNDARY_RE,
    # The pension note CONTAINS its own subheadings ("Pension Benefits", "Plan
    # Assets", the funded-status rollforward). Slicing to the next heading cut it
    # off before the funded-status table; run it to the next numbered note/Item
    # instead so the whole note — PBO, plan assets, funded status — is in scope.
    "pension_footnote": _NOTE_OR_ITEM_BOUNDARY_RE,
}

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


def _slice_section(
    text: str,
    headings: list[tuple[int, str]],
    idx: int,
    max_chars: int,
    end_pattern: re.Pattern | None = None,
) -> tuple[int, int]:
    """
    Return (start, end) char offsets for the section anchored at heading `idx`.

    Without an end_pattern the section runs to the next heading. With one it
    runs to the first SUBSEQUENT heading matching the pattern (the next Item for
    MD&A, the next numbered note/Item for footnotes), skipping over the
    section's own subheadings.
    """
    start = headings[idx][0]
    if end_pattern is not None:
        end = len(text)
        for off, heading in headings[idx + 1:]:
            if end_pattern.search(heading):
                end = off
                break
    else:
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(text)
    # Bound the window so token spend is predictable even if the end boundary is
    # far away (or absent).
    return start, min(end, start + max_chars)


def _anchor_section(
    text: str,
    headings: list[tuple[int, str]],
    patterns: list[re.Pattern],
    content: re.Pattern,
    max_chars: int,
    end_pattern: re.Pattern | None = None,
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
            start, end = _slice_section(text, headings, i, max_chars, end_pattern)
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
    Locate the credit-relevant sections in a raw filing document.

    Args:
        filing_html: The raw filing text/HTML from ingest.get_filing_text().

    Returns (each value is None when neither heading anchoring nor the chunk
    fallback found the section):
        {
          "mdna":                   Section|None,  # MD&A (Item 7)
          "debt":                   Section|None,  # debt / long-term-obligations footnote
          "contingencies":          Section|None,  # commitments & contingencies footnote
          # ── Stage 2a additions (LLM_EXTRACTOR_PORT §5) ──
          "risk_factors":           Section|None,  # Item 1A
          "auditor_report":         Section|None,  # report of independent registered public accounting firm
          "going_concern_footnote": Section|None,  # ASC 205-40 / basis-of-presentation going-concern note
        }

    The three original sections (mdna/debt/contingencies) resolve EXACTLY as
    before — the new keys are computed by the same heading-anchor → chunk-fallback
    machinery, independently, so they are purely additive.

    Table-of-contents safety: a TOC "Item 7 ..." link line also matches the MD&A
    heading patterns, but its slice ends at the TOC's own next "Item 7A" line —
    shorter than _MIN_SECTION_BODY — so it is skipped, and content scoring favors
    the real section over any other stub.
    """
    text = html_to_text(filing_html)
    headings = _heading_index(text)

    out: dict[str, Section | None] = {}
    for name, patterns in _SECTION_HEADING_PATTERNS.items():
        section = _anchor_section(
            text,
            headings,
            patterns,
            _SECTION_CONTENT_PATTERNS[name],
            _SECTION_MAX_CHARS.get(name, _MAX_SECTION_CHARS),
            _SECTION_END_PATTERNS.get(name),
        )
        if section is None:
            section = _chunk_fallback(text, _SECTION_DENSITY_PATTERNS[name])
        if section is not None:
            section.name = name
        out[name] = section
    return out


def section_confidence(section: "Section | None") -> str:
    """
    Map a located Section to its finding-level confidence (LLM_EXTRACTOR_PORT §3).

    "high" when the slice was heading-anchored (`heading_matched` is set);
    "low" when it came from the keyword-density chunk fallback (`heading_matched`
    is None), or when the section was not found at all. The downstream covenant
    (Stage 2c) and going-concern (Stage 2b) passes stamp this onto every finding
    so an extraction pulled from an unanchored slice is flagged less reliable.

    Stage 2a adds only this mechanism; the passes that consume it are built later,
    so no existing finding's behavior changes here.
    """
    return "high" if (section is not None and section.heading_matched is not None) else "low"
