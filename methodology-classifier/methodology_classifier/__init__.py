"""
methodology-classifier — route a corporate issuer to its Moody's and S&P sector
rating methodology from a SIC code (deterministic table + optional LLM fallback),
with an optional TRBC cross-check.

Public API:
    classify_methodology(sic, sic_description=None, name=None, trbc=None,
                         business_description=None, llm_client=None) -> MethodologyClass

    MethodologyClass — frozen dataclass:
        .moodys_methodology  .sp_sector  .confidence  .source  .is_financial  .notes

Bucket lists (single source of truth):
    MOODYS_METHODOLOGIES, MOODYS_INFRASTRUCTURE, MOODYS_FINANCIAL,
    SP_SECTORS, SP_FINANCIAL_EXTERNAL
"""

from .classifier import (  # noqa: F401
    MethodologyClass,
    classify_methodology,
    MOODYS_METHODOLOGIES,
    MOODYS_INFRASTRUCTURE,
    MOODYS_FINANCIAL,
    SP_SECTORS,
    SP_FINANCIAL_EXTERNAL,
    MOODYS_DEFAULT,
    SP_DEFAULT,
)

__version__ = "0.1.0"
