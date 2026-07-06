"""
Methodology classifier — route each issuer to its Moody's and S&P sector methodology.

Moody's and S&P each rate a non-financial corporate under exactly ONE sector
methodology (its own scorecard). They choose it by the issuer's dominant business,
but neither agency publishes a mechanical classifier or an official crosswalk from a
standard code (SIC/NAICS/GICS) to their buckets — an analyst assigns it by judgment.
What they publish is only the LIST of buckets:

  - Moody's: ~30 standalone corporate sector methodologies, PLUS a separate
    "Infrastructure & Project Finance" group where regulated utilities/power live
    (an asymmetry vs S&P, encoded below).
  - S&P: one general "Corporate Methodology" plus the April-2024 "Sector-Specific
    Corporate Methodology" with 40 numbered sections (37 corporate/infra + 3 nonbank
    financial). Our target is the section NAME. S&P keeps Regulated Utilities and
    Transportation Infrastructure INSIDE the corporate document.

So we build the mapping ourselves. The primary, auditable path is a hand-built
SIC-prefix → bucket table matched LONGEST-prefix-first (a 4-digit key overrides its
2-digit major group). A US public issuer's SIC is published by SEC EDGAR, so this
input is available for essentially every registrant.

For SICs that are structurally ambiguous (holding companies, blank-check shells,
non-classifiable establishments) there is no credible deterministic answer, so we
fall back to an LLM that picks a bucket from each agency's list given the issuer's
name and business description. The deterministic table handles the common case with
no LLM call.

TRBC (LSEG "Economic Sector Name"), when available for an issuer, is used only as a
CROSS-CHECK: if it disagrees with the SIC result at the top-level group, we downgrade
confidence and record the disagreement. SIC stays the primary signal.

Financial-sector issuers (SIC 60–67) are not rated under the industrial corporate
cash-flow framework; we still classify them to each agency's financial-institution
bucket and flag (is_financial=True) that the corporate scorecard doesn't apply, so a
downstream consumer routes them to the right (separate) framework.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# ── Canonical bucket names (single source of truth) ──────────────────────────
# Downstream code should reference these constants, not free strings, so a rename
# happens in exactly one place.

# Moody's non-financial corporate sector methodologies (~30).
MOODYS_METHODOLOGIES: tuple[str, ...] = (
    "Aerospace and Defense",
    "Automobile Manufacturers",
    "Automotive Suppliers",
    "Building Materials",
    "Business and Consumer Services",
    "Chemical Industry",
    "Consumer Packaged Goods",
    "Distribution and Supply Chain Services",
    "Environmental Services and Waste Management",
    "Equipment and Transportation Rental",
    "Gaming",
    "Global Communications Infrastructure",
    "Global Steel",
    "Homebuilding and Property Development",
    "Independent Exploration and Production",
    "Integrated Oil and Gas",
    "Manufacturing",
    "Media",
    "Medical Products and Devices",
    "Midstream Energy",
    "Mining",
    "Natural Gas Pipelines",
    "Oilfield Services",
    "Paper and Forest Products",
    "Passenger Airlines",
    "Pharmaceutical Industry",
    "Protein and Agriculture",
    "Publishing",
    "REITs and Other Commercial Real Estate Firms",
    "Restaurant Industry",
    "Retail and Apparel",
    "Software and Diversified Technology",
    "Surface Transportation and Logistics",
    "Telecommunications Service Providers",
)

# Moody's Infrastructure & Project Finance group — utilities/power sit HERE, not in
# Corporates. Kept in a separate tuple so the asymmetry with S&P stays visible.
MOODYS_INFRASTRUCTURE: tuple[str, ...] = (
    "Regulated Electric and Gas Utilities",
    "Regulated Water Utilities",
    "Unregulated Utilities and Power Companies",
    "Regulated Electric and Gas Networks",
)

# Moody's financial-institution groups (Banks/FinCos/Securities/Insurance/Funds).
# The industrial corporate scorecard does NOT apply to these — teammates route them
# to the relevant FIG methodology instead.
MOODYS_FINANCIAL: tuple[str, ...] = (
    "Banks",
    "Finance Companies",
    "Securities Industry Service Providers",
    "Insurers",
    "Asset Management",
)

# S&P Sector-Specific Corporate Methodology — the 40 sections (Apr 2024).
SP_SECTORS: tuple[str, ...] = (
    # Corporate & Infrastructure (37)
    "Aerospace And Defense",
    "Agribusiness, Commodity Foods, And Agricultural Cooperatives",
    "Auto And Commercial Vehicle Manufacturing",
    "Auto Suppliers",
    "Building Materials",
    "Business And Consumer Services",
    "Capital Goods",
    "Commodity Chemicals",
    "Consumer Durables",
    "Consumer Staples And Branded Nondurables",
    "Containers And Packaging",
    "Contract Drilling",
    "Engineering And Construction",
    "Environmental Services",
    "Forest And Paper Products",
    "Health Care Equipment",
    "Health Care Services",
    "Homebuilders And Real Estate Developers",
    "Leisure And Sports",
    "Media And Entertainment",
    "Metals Production And Processing",
    "Midstream Energy",
    "Mining",
    "Oil And Gas Exploration And Production",
    "Oilfield Services And Equipment",
    "Pharmaceuticals",
    "Railroad, Package Express, And Logistics",
    "Refining And Marketing",
    "Regulated Utilities",
    "Retail And Restaurants",
    "Specialty Chemicals",
    "Technology Hardware And Semiconductors",
    "Technology Software And Services",
    "Telecommunications",
    "Transportation Cyclical",
    "Transportation Infrastructure",
    "Unregulated Power And Gas",
    # Nonbank Financial Services (3)
    "Asset Managers",
    "Financial Market Infrastructure",
    "Financial Services Finance Companies",
)

# Depository banks and insurers are rated under S&P's separate Financial Institutions
# / Insurance criteria, NOT the corporate sector doc — so these labels are not in the
# 40-section list. We still emit them (flagged) so the routing is explicit.
SP_FINANCIAL_EXTERNAL: tuple[str, ...] = (
    "Banks (Financial Institutions criteria)",
    "Insurers (Insurance criteria)",
)

# Generic fallbacks when nothing matches (both are legitimate catch-all buckets).
MOODYS_DEFAULT = "Business and Consumer Services"
SP_DEFAULT = "Business And Consumer Services"

# ── SIC prefix → (Moody's bucket, S&P bucket) ─────────────────────────────────
# Keyed by SIC prefix, matched LONGEST-prefix-first (a 4-digit key overrides its
# 2-digit major group), mirroring rating._industry_risk. Values are (moodys, sp).
# A single table (rather than two parallel dicts) keeps the two agencies' prefixes
# from drifting apart.
_SIC_METHODOLOGY: dict[str, tuple[str, str]] = {
    # ── Agriculture, forestry, fishing (01–09) ──
    "01": ("Protein and Agriculture", "Agribusiness, Commodity Foods, And Agricultural Cooperatives"),
    "02": ("Protein and Agriculture", "Agribusiness, Commodity Foods, And Agricultural Cooperatives"),
    "07": ("Protein and Agriculture", "Agribusiness, Commodity Foods, And Agricultural Cooperatives"),
    "08": ("Paper and Forest Products", "Forest And Paper Products"),
    "09": ("Protein and Agriculture", "Agribusiness, Commodity Foods, And Agricultural Cooperatives"),
    # ── Mining & extraction (10–14) ──
    "10": ("Mining", "Mining"),
    "12": ("Mining", "Mining"),
    "13": ("Independent Exploration and Production", "Oil And Gas Exploration And Production"),
    "1311": ("Independent Exploration and Production", "Oil And Gas Exploration And Production"),
    "1381": ("Oilfield Services", "Contract Drilling"),
    "1382": ("Oilfield Services", "Oilfield Services And Equipment"),
    "1389": ("Oilfield Services", "Oilfield Services And Equipment"),
    "14": ("Mining", "Mining"),
    # ── Construction (15–17) ──
    "15": ("Homebuilding and Property Development", "Engineering And Construction"),
    "1531": ("Homebuilding and Property Development", "Homebuilders And Real Estate Developers"),
    "16": ("Business and Consumer Services", "Engineering And Construction"),
    "17": ("Business and Consumer Services", "Engineering And Construction"),
    # ── Manufacturing (20–39) ──
    "20": ("Consumer Packaged Goods", "Consumer Staples And Branded Nondurables"),
    "201": ("Protein and Agriculture", "Agribusiness, Commodity Foods, And Agricultural Cooperatives"),
    "208": ("Consumer Packaged Goods", "Consumer Staples And Branded Nondurables"),  # beverages
    "21": ("Consumer Packaged Goods", "Consumer Staples And Branded Nondurables"),
    "22": ("Retail and Apparel", "Consumer Staples And Branded Nondurables"),
    "23": ("Retail and Apparel", "Consumer Staples And Branded Nondurables"),
    "24": ("Paper and Forest Products", "Forest And Paper Products"),
    "25": ("Manufacturing", "Consumer Durables"),
    "26": ("Paper and Forest Products", "Forest And Paper Products"),
    "265": ("Paper and Forest Products", "Containers And Packaging"),  # paperboard containers
    "27": ("Publishing", "Media And Entertainment"),
    "28": ("Chemical Industry", "Commodity Chemicals"),
    "283": ("Pharmaceutical Industry", "Pharmaceuticals"),
    "2833": ("Pharmaceutical Industry", "Pharmaceuticals"),
    "2834": ("Pharmaceutical Industry", "Pharmaceuticals"),
    "2835": ("Medical Products and Devices", "Health Care Equipment"),  # in-vitro/diagnostic
    "2836": ("Pharmaceutical Industry", "Pharmaceuticals"),
    "284": ("Consumer Packaged Goods", "Consumer Staples And Branded Nondurables"),  # soap/cosmetics
    "285": ("Chemical Industry", "Specialty Chemicals"),  # paints/coatings
    "286": ("Chemical Industry", "Commodity Chemicals"),  # industrial organic
    "289": ("Chemical Industry", "Specialty Chemicals"),
    "29": ("Integrated Oil and Gas", "Refining And Marketing"),
    "2911": ("Integrated Oil and Gas", "Refining And Marketing"),
    "30": ("Manufacturing", "Consumer Durables"),
    "3011": ("Automotive Suppliers", "Auto Suppliers"),  # tires
    "308": ("Chemical Industry", "Specialty Chemicals"),  # misc plastics
    "31": ("Retail and Apparel", "Consumer Durables"),
    "32": ("Building Materials", "Building Materials"),
    "33": ("Global Steel", "Metals Production And Processing"),
    "331": ("Global Steel", "Metals Production And Processing"),
    "333": ("Mining", "Metals Production And Processing"),  # nonferrous
    "34": ("Manufacturing", "Capital Goods"),
    "35": ("Manufacturing", "Capital Goods"),
    "357": ("Software and Diversified Technology", "Technology Hardware And Semiconductors"),
    "36": ("Software and Diversified Technology", "Technology Hardware And Semiconductors"),
    "3674": ("Software and Diversified Technology", "Technology Hardware And Semiconductors"),  # semis
    "37": ("Manufacturing", "Capital Goods"),
    "371": ("Automobile Manufacturers", "Auto And Commercial Vehicle Manufacturing"),
    "3711": ("Automobile Manufacturers", "Auto And Commercial Vehicle Manufacturing"),
    "3714": ("Automotive Suppliers", "Auto Suppliers"),
    "372": ("Aerospace and Defense", "Aerospace And Defense"),
    "376": ("Aerospace and Defense", "Aerospace And Defense"),
    "38": ("Medical Products and Devices", "Health Care Equipment"),
    "382": ("Software and Diversified Technology", "Technology Hardware And Semiconductors"),
    "384": ("Medical Products and Devices", "Health Care Equipment"),
    "39": ("Consumer Packaged Goods", "Consumer Durables"),
    # ── Transportation, communications, utilities (40–49) ──
    "40": ("Surface Transportation and Logistics", "Railroad, Package Express, And Logistics"),
    "41": ("Surface Transportation and Logistics", "Transportation Cyclical"),
    "42": ("Surface Transportation and Logistics", "Railroad, Package Express, And Logistics"),
    "44": ("Surface Transportation and Logistics", "Transportation Cyclical"),
    "45": ("Passenger Airlines", "Transportation Cyclical"),
    "4581": ("Business and Consumer Services", "Transportation Infrastructure"),  # airports/terminals
    "46": ("Midstream Energy", "Midstream Energy"),
    "47": ("Surface Transportation and Logistics", "Railroad, Package Express, And Logistics"),
    "48": ("Telecommunications Service Providers", "Telecommunications"),
    "481": ("Telecommunications Service Providers", "Telecommunications"),
    "483": ("Media", "Media And Entertainment"),
    "484": ("Media", "Media And Entertainment"),  # cable
    "489": ("Global Communications Infrastructure", "Telecommunications"),
    "49": ("Regulated Electric and Gas Utilities", "Regulated Utilities"),
    "4911": ("Regulated Electric and Gas Utilities", "Regulated Utilities"),
    "4922": ("Natural Gas Pipelines", "Midstream Energy"),
    "4923": ("Natural Gas Pipelines", "Midstream Energy"),
    "4924": ("Regulated Electric and Gas Utilities", "Regulated Utilities"),  # gas distribution
    "4931": ("Regulated Electric and Gas Utilities", "Regulated Utilities"),
    "4932": ("Regulated Electric and Gas Utilities", "Regulated Utilities"),
    "4941": ("Regulated Water Utilities", "Regulated Utilities"),
    "4953": ("Environmental Services and Waste Management", "Environmental Services"),
    "4959": ("Environmental Services and Waste Management", "Environmental Services"),
    # ── Wholesale & retail trade (50–59) ──
    "50": ("Distribution and Supply Chain Services", "Business And Consumer Services"),
    "51": ("Distribution and Supply Chain Services", "Business And Consumer Services"),
    "52": ("Retail and Apparel", "Retail And Restaurants"),
    "53": ("Retail and Apparel", "Retail And Restaurants"),
    "54": ("Retail and Apparel", "Retail And Restaurants"),
    "55": ("Retail and Apparel", "Retail And Restaurants"),
    "56": ("Retail and Apparel", "Retail And Restaurants"),
    "57": ("Retail and Apparel", "Retail And Restaurants"),
    "58": ("Restaurant Industry", "Retail And Restaurants"),
    "5812": ("Restaurant Industry", "Retail And Restaurants"),
    "59": ("Retail and Apparel", "Retail And Restaurants"),
    # ── Finance, insurance, real estate (60–67) — financial-institution frameworks ──
    "60": ("Banks", "Banks (Financial Institutions criteria)"),
    "61": ("Finance Companies", "Financial Services Finance Companies"),
    "62": ("Securities Industry Service Providers", "Financial Market Infrastructure"),
    "63": ("Insurers", "Insurers (Insurance criteria)"),
    "64": ("Insurers", "Insurers (Insurance criteria)"),
    "65": ("REITs and Other Commercial Real Estate Firms", "Homebuilders And Real Estate Developers"),
    "6512": ("REITs and Other Commercial Real Estate Firms", "Homebuilders And Real Estate Developers"),
    "6726": ("Asset Management", "Asset Managers"),
    "6798": ("REITs and Other Commercial Real Estate Firms", "Homebuilders And Real Estate Developers"),  # REITs
    # ── Services (70–89) ──
    "70": ("Business and Consumer Services", "Leisure And Sports"),
    "72": ("Business and Consumer Services", "Business And Consumer Services"),
    "73": ("Business and Consumer Services", "Business And Consumer Services"),
    "7370": ("Software and Diversified Technology", "Technology Software And Services"),
    "7371": ("Software and Diversified Technology", "Technology Software And Services"),
    "7372": ("Software and Diversified Technology", "Technology Software And Services"),
    "7374": ("Software and Diversified Technology", "Technology Software And Services"),
    "7375": ("Software and Diversified Technology", "Technology Software And Services"),
    "7379": ("Software and Diversified Technology", "Technology Software And Services"),
    "75": ("Business and Consumer Services", "Business And Consumer Services"),
    "78": ("Media", "Media And Entertainment"),
    "79": ("Gaming", "Leisure And Sports"),
    "7993": ("Gaming", "Leisure And Sports"),
    "7999": ("Gaming", "Leisure And Sports"),
    "80": ("Business and Consumer Services", "Health Care Services"),
    "82": ("Business and Consumer Services", "Business And Consumer Services"),
    "83": ("Business and Consumer Services", "Business And Consumer Services"),
    "87": ("Business and Consumer Services", "Business And Consumer Services"),
    "89": ("Business and Consumer Services", "Business And Consumer Services"),
}

# SICs with no credible deterministic answer → route to the LLM fallback. These are
# holding companies, blank-check shells, and non-classifiable establishments whose
# SIC says nothing about the operating business.
_AMBIGUOUS_SIC = {
    "6770",  # blank checks
    "6199",  # finance services (grab-bag)
    "9995",  # non-classifiable establishments
    "9997",
    "9999",
}
# `67` holding/investment offices, absent a more specific 4-digit code, is ambiguous
# too (a holdco's methodology depends on the operating subsidiaries). Handled in code
# because it's a prefix rule, not an exact code.

# ── Top-level groups (for the TRBC cross-check) ──────────────────────────────
# Every Moody's bucket maps to a coarse economic group; TRBC "Economic Sector Name"
# maps to the same enum, so we can compare the two at a level both taxonomies share.
_MOODYS_GROUP: dict[str, str] = {
    "Aerospace and Defense": "industrials",
    "Automobile Manufacturers": "consumer_cyclical",
    "Automotive Suppliers": "consumer_cyclical",
    "Building Materials": "industrials",
    "Business and Consumer Services": "industrials",
    "Chemical Industry": "materials",
    "Consumer Packaged Goods": "consumer_defensive",
    "Distribution and Supply Chain Services": "industrials",
    "Environmental Services and Waste Management": "industrials",
    "Equipment and Transportation Rental": "industrials",
    "Gaming": "consumer_cyclical",
    "Global Communications Infrastructure": "telecom",
    "Global Steel": "materials",
    "Homebuilding and Property Development": "consumer_cyclical",
    "Independent Exploration and Production": "energy",
    "Integrated Oil and Gas": "energy",
    "Manufacturing": "industrials",
    "Media": "consumer_cyclical",
    "Medical Products and Devices": "healthcare",
    "Midstream Energy": "energy",
    "Mining": "materials",
    "Natural Gas Pipelines": "energy",
    "Oilfield Services": "energy",
    "Paper and Forest Products": "materials",
    "Passenger Airlines": "industrials",
    "Pharmaceutical Industry": "healthcare",
    "Protein and Agriculture": "consumer_defensive",
    "Publishing": "consumer_cyclical",
    "REITs and Other Commercial Real Estate Firms": "real_estate",
    "Restaurant Industry": "consumer_cyclical",
    "Retail and Apparel": "consumer_cyclical",
    "Software and Diversified Technology": "technology",
    "Surface Transportation and Logistics": "industrials",
    "Telecommunications Service Providers": "telecom",
    "Regulated Electric and Gas Utilities": "utilities",
    "Regulated Water Utilities": "utilities",
    "Unregulated Utilities and Power Companies": "utilities",
    "Regulated Electric and Gas Networks": "utilities",
    "Banks": "financials",
    "Finance Companies": "financials",
    "Securities Industry Service Providers": "financials",
    "Insurers": "financials",
    "Asset Management": "financials",
}

# TRBC Economic Sector Name (lowercased) → the same coarse group enum.
_TRBC_GROUP: dict[str, str] = {
    "energy": "energy",
    "basic materials": "materials",
    "industrials": "industrials",
    "consumer cyclicals": "consumer_cyclical",
    "consumer non-cyclicals": "consumer_defensive",
    "financials": "financials",
    "healthcare": "healthcare",
    "technology": "technology",
    "telecommunications services": "telecom",
    "utilities": "utilities",
    "real estate": "real_estate",
}

# SIC major groups 60–67 are the financial-institution zone (see rating.financial_sector_note).
_FINANCIAL_SIC_PREFIXES = {"60", "61", "62", "63", "64", "65", "67"}

_CONFIDENCE_ORDER = ("high", "medium", "low")


@dataclass(frozen=True)
class MethodologyClass:
    """
    The methodology routing for one issuer.

    Attributes:
        moodys_methodology: Moody's sector methodology name (a MOODYS_* constant).
        sp_sector:          S&P sector-section name (an SP_SECTORS entry, or a
                            SP_FINANCIAL_EXTERNAL label for depository banks/insurers).
        confidence:         "high" (specific 3–4 digit SIC hit), "medium" (2-digit
                            major-group hit), or "low" (default / LLM fallback /
                            TRBC disagreement).
        source:             "sic_table" | "llm_fallback" | "default".
        is_financial:       True for SIC 60–67 — the industrial corporate scorecard
                            does NOT apply; route to the FIG/Insurance framework.
        notes:              Human-readable provenance / caveats (e.g. TRBC disagreement).
    """
    moodys_methodology: str
    sp_sector: str
    confidence: str
    source: str
    is_financial: bool = False
    notes: str = ""


def _digits(sic: str | None) -> str:
    """Strip a SIC to its digits (EDGAR sometimes stores it padded or as an int)."""
    return "".join(ch for ch in str(sic or "") if ch.isdigit())


def _longest_prefix_match(digits: str) -> tuple[str, tuple[str, str]] | None:
    """
    Return (matched_prefix, (moodys, sp)) for the LONGEST SIC prefix present in the
    table, or None. A 4-digit key overrides its 2-digit major group — the same
    longest-prefix rule as rating._industry_risk.
    """
    for length in range(len(digits), 0, -1):
        hit = _SIC_METHODOLOGY.get(digits[:length])
        if hit is not None:
            return digits[:length], hit
    return None


def _downgrade(confidence: str) -> str:
    """Move one step toward 'low' (high→medium→low)."""
    i = _CONFIDENCE_ORDER.index(confidence)
    return _CONFIDENCE_ORDER[min(i + 1, len(_CONFIDENCE_ORDER) - 1)]


def _trbc_note(moodys_bucket: str, trbc: str | None) -> str | None:
    """
    Compare the SIC-derived top-level group against TRBC's economic sector. Returns a
    disagreement note (which triggers a confidence downgrade) or None when they agree
    / TRBC is unmapped. Only groups both taxonomies express are compared.
    """
    if not trbc:
        return None
    trbc_group = _TRBC_GROUP.get(str(trbc).strip().lower())
    sic_group = _MOODYS_GROUP.get(moodys_bucket)
    if trbc_group and sic_group and trbc_group != sic_group:
        return f"TRBC sector '{trbc}' ({trbc_group}) disagrees with SIC group ({sic_group})"
    return None


def classify_methodology(
    sic: str | None,
    sic_description: str | None = None,
    name: str | None = None,
    trbc: str | None = None,
    business_description: str | None = None,
    llm_client=None,
) -> MethodologyClass:
    """
    Classify one issuer into its Moody's and S&P sector methodology.

    Primary path is the deterministic SIC-prefix table (auditable). For structurally
    ambiguous SICs (holding companies, blank-check shells, non-classifiable) the SIC
    carries no signal, so we fall back to an LLM over the name/description if one is
    available (else a flagged default). TRBC, when given, is a cross-check that can
    only LOWER confidence, never raise it — SIC is authoritative.

    Args:
        sic:                  EDGAR SIC code (string or int-like).
        sic_description:      EDGAR human label (fed to the LLM fallback).
        name:                 Issuer name (fed to the LLM fallback).
        trbc:                 LSEG TRBC "Economic Sector Name", if known (cross-check).
        business_description: Optional 10-K Item-1 text (fed to the LLM fallback).
        llm_client:           Optional anthropic.Anthropic (injected for tests/offline).

    Returns:
        MethodologyClass. Never raises for a bad/empty SIC — worst case is a flagged
        low-confidence default.
    """
    digits = _digits(sic)
    is_financial = len(digits) >= 2 and digits[:2] in _FINANCIAL_SIC_PREFIXES

    ambiguous = (
        not digits
        or digits in _AMBIGUOUS_SIC
        # bare "67xx" holding/investment offices with no recognised 4-digit code
        or (digits[:2] == "67" and _SIC_METHODOLOGY.get(digits[:4]) is None
            and _SIC_METHODOLOGY.get(digits[:3]) is None)
    )

    if ambiguous:
        result = _classify_via_llm(name, sic_description, business_description, llm_client)
        # Financial holdcos still flagged financial for downstream routing.
        return MethodologyClass(
            moodys_methodology=result[0],
            sp_sector=result[1],
            confidence="low",
            source=result[2],
            is_financial=is_financial,
            notes=result[3],
        )

    match = _longest_prefix_match(digits)
    if match is None:
        moodys, sp = MOODYS_DEFAULT, SP_DEFAULT
        confidence, source = "low", "default"
        notes = f"No SIC-table match for {digits}; generic default applied"
    else:
        prefix, (moodys, sp) = match
        # A specific 3–4 digit hit is high-confidence; a 2-digit major-group hit is medium.
        confidence = "high" if len(prefix) >= 3 else "medium"
        source = "sic_table"
        notes = ""

    # TRBC cross-check: disagreement can only lower confidence.
    disagreement = _trbc_note(moodys, trbc)
    if disagreement:
        confidence = _downgrade(confidence)
        notes = (notes + "; " + disagreement).lstrip("; ") if notes else disagreement

    if is_financial:
        fin_note = ("Financial-sector issuer (SIC 60–67): industrial corporate "
                    "scorecard does not apply; route to the FIG/Insurance framework.")
        notes = (notes + "; " + fin_note).lstrip("; ") if notes else fin_note

    return MethodologyClass(
        moodys_methodology=moodys,
        sp_sector=sp,
        confidence=confidence,
        source=source,
        is_financial=is_financial,
        notes=notes,
    )


# ── LLM fallback ─────────────────────────────────────────────────────────────

# Haiku is fast/cheap and this is a constrained pick-from-a-list task — same model
# the qualitative filing review uses (src/llm_review.py).
_LLM_MODEL = "claude-haiku-4-5-20251001"

_LLM_SYSTEM = (
    "You are a credit analyst assigning an issuer to the single best-fit rating "
    "methodology for each of two agencies. Pick by the issuer's DOMINANT business "
    "(largest revenue/EBITDA segment). Choose EXACTLY ONE label from each provided "
    "list, copied verbatim. Respond with a JSON object only: "
    '{"moodys": "<one Moody\'s label>", "sp": "<one S&P label>"}. No other text.'
)


def _parse_json_object(raw_text: str) -> dict:
    """Strip markdown fences and parse an LLM response as a JSON object (else {})."""
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```[a-zA-Z]*\n?", "", raw_text)
        raw_text = re.sub(r"\n?```$", "", raw_text)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _classify_via_llm(
    name: str | None,
    sic_description: str | None,
    business_description: str | None,
    client=None,
) -> tuple[str, str, str, str]:
    """
    Ask the LLM to pick one Moody's and one S&P bucket for an ambiguous issuer.

    Returns (moodys, sp, source, notes). Degrades to the generic defaults with
    source="default" when no client/API key is available or the response is
    unusable — the classifier never blocks on the LLM.
    """
    # Build a client lazily so the deterministic path never needs anthropic/an API key.
    # With no injected client AND no API key in the environment, skip the LLM entirely
    # (rather than attempt a call that will fail) and degrade to the generic default.
    if client is None:
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return (MOODYS_DEFAULT, SP_DEFAULT, "default",
                    "Ambiguous SIC and no LLM available; generic default applied")
        try:
            import anthropic
            client = anthropic.Anthropic()
        except Exception:
            return (MOODYS_DEFAULT, SP_DEFAULT, "default",
                    "Ambiguous SIC and no LLM available; generic default applied")

    context = "\n".join(filter(None, [
        f"Issuer name: {name}" if name else None,
        f"SIC description: {sic_description}" if sic_description else None,
        f"Business description: {business_description[:8000]}" if business_description else None,
    ])) or "No issuer context available."

    user_prompt = (
        f"{context}\n\n"
        f"Moody's methodologies (choose one):\n"
        + "\n".join(f"- {m}" for m in MOODYS_METHODOLOGIES + MOODYS_INFRASTRUCTURE + MOODYS_FINANCIAL)
        + "\n\nS&P sectors (choose one):\n"
        + "\n".join(f"- {s}" for s in SP_SECTORS + SP_FINANCIAL_EXTERNAL)
        + "\n\nReturn the JSON object only."
    )

    try:
        message = client.messages.create(
            model=_LLM_MODEL,
            max_tokens=200,
            system=_LLM_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        obj = _parse_json_object(message.content[0].text)
    except Exception:
        return (MOODYS_DEFAULT, SP_DEFAULT, "default",
                "Ambiguous SIC; LLM fallback errored, generic default applied")

    moodys = obj.get("moodys")
    sp = obj.get("sp")
    valid_moodys = set(MOODYS_METHODOLOGIES) | set(MOODYS_INFRASTRUCTURE) | set(MOODYS_FINANCIAL)
    valid_sp = set(SP_SECTORS) | set(SP_FINANCIAL_EXTERNAL)
    if moodys in valid_moodys and sp in valid_sp:
        return (moodys, sp, "llm_fallback",
                "Ambiguous SIC; classified by LLM from business description")
    return (MOODYS_DEFAULT, SP_DEFAULT, "default",
            "Ambiguous SIC; LLM returned an off-list label, generic default applied")
