"""Tests for src/sections.py — section location in synthetic 10-K HTML."""

from src.sections import _SECTION_MAX_CHARS, html_to_text, locate_sections


# Synthetic 10-K with the structures that matter for location:
#   - a table-of-contents whose Item 7 / 7A / 8 link lines must NOT anchor MD&A
#   - a real Item 7 containing SUBHEADINGS (Overview, Results of Operations,
#     Liquidity and Capital Resources) that the slice must span
#   - Item 7A and Item 8 as the MD&A end boundary
#   - debt and contingencies footnotes inside Item 8
MDNA_LIQUIDITY_SENTENCE = (
    "There is substantial doubt about our ability to continue as a going "
    "concern, and our liquidity depends on refinancing our revolving credit "
    "facility before it matures."
)
DEBT_BODY = (
    "Our senior notes bear interest at a fixed rate and mature in 2027. "
    "The credit agreement requires us to maintain a maximum leverage covenant "
    "of 4.0x and contains customary events of default. The principal amount "
    "outstanding under the senior notes was due in annual maturities, and the "
    "indenture permits us to redeem the notes at a premium. "
) * 4
CONTINGENCIES_BODY = (
    "We are subject to litigation in the ordinary course of business. "
    "A patent lawsuit was filed against the company and a loss is reasonably "
    "possible but not accrued; an unfavorable settlement of this legal "
    "proceeding could be material. The plaintiff alleges damages. "
) * 4


def _build_filing(liquidity_body: str = "") -> str:
    overview_body = (
        "We design and sell widgets worldwide. Our cash flow from operations "
        "funds our investments and our dividend. " * 8
    )
    results_body = (
        "Revenue increased due to higher unit volumes, partially offset by "
        "unfavorable currency movements affecting our cash flow. " * 8
    )
    liquidity = liquidity_body or (MDNA_LIQUIDITY_SENTENCE + " ") * 8
    return f"""<html>
<head><title>FORM 10-K</title><style>p {{ margin: 0; }}</style></head>
<body>
<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>
<p>FORM 10-K ANNUAL REPORT</p>
<table>
<tr><td>Item 1. Business</td><td>3</td></tr>
<tr><td>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations</td><td>25</td></tr>
<tr><td>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</td><td>48</td></tr>
<tr><td>Item 8. Financial Statements and Supplementary Data</td><td>50</td></tr>
</table>
<p>Item 1. Business</p>
<p>{"We make widgets and sell them through retail partners. " * 12}</p>
<p>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations</p>
<p>Overview</p>
<p>{overview_body}</p>
<p>Results of Operations</p>
<p>{results_body}</p>
<p>Liquidity and Capital Resources</p>
<p>{liquidity}</p>
<p>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</p>
<p>{"Our market risk relates to interest rate fluctuations on our short-term investments. " * 8}</p>
<p>Item 8. Financial Statements and Supplementary Data</p>
<p>NOTE 5. Long-Term Debt</p>
<p>{DEBT_BODY}</p>
<p>NOTE 9. Commitments and Contingencies</p>
<p>{CONTINGENCIES_BODY}</p>
<p>Signatures</p>
</body>
</html>"""


def test_html_to_text_strips_markup_and_keeps_headings_on_own_lines():
    text = html_to_text(_build_filing())
    assert "<p>" not in text and "<style>" not in text
    assert "p { margin: 0; }" not in text  # style block dropped entirely
    lines = [l.strip() for l in text.splitlines()]
    assert "NOTE 5. Long-Term Debt" in lines


def test_mdna_anchored_at_real_item_7_not_toc():
    sections = locate_sections(_build_filing())
    mdna = sections["mdna"]
    assert mdna is not None
    assert mdna.heading_matched is not None
    # The TOC Item 7 line is followed immediately by the TOC Item 7A line, so
    # its slice is too short to qualify — the anchor must be the real section.
    assert MDNA_LIQUIDITY_SENTENCE.split(",")[0] in mdna.text


def test_mdna_spans_subheadings_and_stops_at_item_7a():
    sections = locate_sections(_build_filing())
    mdna = sections["mdna"]
    assert mdna is not None
    # Spans past internal subheadings...
    assert "Results of Operations" in mdna.text
    assert "Liquidity and Capital Resources" in mdna.text
    # ...but ends at Item 7A: the market-risk body must not be included.
    assert "market risk relates to interest rate fluctuations" not in mdna.text


def test_mdna_respects_max_chars():
    huge_liquidity = (MDNA_LIQUIDITY_SENTENCE + " ") * 800  # ≫ 100k chars
    sections = locate_sections(_build_filing(liquidity_body=huge_liquidity))
    mdna = sections["mdna"]
    assert mdna is not None
    assert len(mdna.text) <= _SECTION_MAX_CHARS["mdna"]


def test_debt_and_contingencies_still_located():
    sections = locate_sections(_build_filing())
    debt = sections["debt"]
    assert debt is not None
    assert "maximum leverage covenant" in debt.text
    contingencies = sections["contingencies"]
    assert contingencies is not None
    assert "reasonably possible" in contingencies.text


def test_footnote_spans_internal_subheadings():
    # Real debt notes contain subheadings ("Commercial Paper", "Term Debt");
    # the slice must span them and stop at the NEXT numbered note instead.
    filing = _build_filing().replace(
        f"<p>NOTE 5. Long-Term Debt</p>\n<p>{DEBT_BODY}</p>",
        f"<p>NOTE 5. Long-Term Debt</p>\n<p>Commercial Paper</p>\n"
        f"<p>{'We issue commercial paper to fund short-term needs. ' * 8}</p>\n"
        f"<p>Term Debt</p>\n<p>{DEBT_BODY}</p>",
    )
    sections = locate_sections(filing)
    debt = sections["debt"]
    assert debt is not None
    assert "commercial paper" in debt.text.lower()
    assert "maximum leverage covenant" in debt.text  # past the Term Debt subheading
    assert "reasonably possible" not in debt.text  # stops at NOTE 9


def test_alternate_footnote_titles_anchor():
    # Some filers title the notes "Financing Arrangements" / "Legal Matters"
    # instead of the canonical "Long-Term Debt" / "Commitments and Contingencies".
    filing = _build_filing().replace(
        "NOTE 5. Long-Term Debt", "NOTE 5. Financing Arrangements"
    ).replace(
        "NOTE 9. Commitments and Contingencies", "NOTE 9. Legal Matters"
    )
    sections = locate_sections(filing)
    debt = sections["debt"]
    assert debt is not None
    assert debt.heading_matched is not None
    assert "maximum leverage covenant" in debt.text
    contingencies = sections["contingencies"]
    assert contingencies is not None
    assert contingencies.heading_matched is not None
    assert "reasonably possible" in contingencies.text
