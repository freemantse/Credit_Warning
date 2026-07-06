"""
Central construction point for the Anthropic LLM client + the active model string.

Why this module exists
----------------------
Historically the Anthropic client was built inline (`anthropic.Anthropic(...)`) at
~9 call sites and the model string was a hard-coded `MODEL` constant. This module
gives ONE place that decides *how* the client is built and *which* model string is
used, so an optional OpenRouter route can be toggled without scattering `if` checks
through the extractors.

The toggle: `USE_OPENROUTER`
---------------------------
When `USE_OPENROUTER` is set (truthy, read from the environment — the app loads
`.env.local` before importing the extractors), the client is pointed at OpenRouter's
Anthropic-compatible "Anthropic Skin" endpoint and the OpenRouter model slug is used:

    base_url = https://openrouter.ai/api   (the Anthropic SDK appends /v1/messages)
    api_key  = OPENROUTER_API_KEY
    model    = OPENROUTER_MODEL  (default "anthropic/claude-haiku-4.5")

This serves the SAME model (claude-haiku-4.5, via Amazon Bedrock behind OpenRouter)
through the SAME anthropic SDK and the SAME `client.messages.create(...)` call sites —
only the base_url, key, and model string differ.

Default OFF — byte-for-byte unchanged
-------------------------------------
When `USE_OPENROUTER` is unset:
  * build_client(max_retries=n) is EXACTLY `anthropic.Anthropic(max_retries=n)`
    (and build_client() with no arg is EXACTLY `anthropic.Anthropic()`), so it still
    honours ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY from the env as before.
  * resolve_model() returns the original "claude-haiku-4-5-20251001".
  * OPENROUTER_API_KEY is never read.
No behavioural delta, no new required env var when the flag is off.
"""

from __future__ import annotations

import os

import anthropic

# The direct-Anthropic model string — unchanged from the historical MODEL constant.
_DIRECT_MODEL = "claude-haiku-4-5-20251001"

# OpenRouter's Anthropic-compatible endpoint. The anthropic SDK appends /v1/messages,
# so this must be the bare "…/api" root (NOT "…/api/v1").
_OPENROUTER_BASE_URL = "https://openrouter.ai/api"

# Default OpenRouter slug for the same Haiku 4.5 model (override via OPENROUTER_MODEL).
_OPENROUTER_MODEL_DEFAULT = "anthropic/claude-haiku-4.5"


def use_openrouter() -> bool:
    """True when the OpenRouter toggle is enabled. Read live from the env each call
    so tests / scripts can set it after import. Accepts 1/true/yes/on (any case)."""
    return os.getenv("USE_OPENROUTER", "").strip().lower() in ("1", "true", "yes", "on")


def resolve_model() -> str:
    """The model string for the active route: the OpenRouter slug when the toggle is
    on, else the original direct-Anthropic model string (byte-for-byte)."""
    if use_openrouter():
        return os.getenv("OPENROUTER_MODEL", _OPENROUTER_MODEL_DEFAULT)
    return _DIRECT_MODEL


def build_client(max_retries: int | None = None) -> anthropic.Anthropic:
    """
    Build the Anthropic SDK client for the active route.

    OFF (default): identical to the historical inline construction —
        build_client()        -> anthropic.Anthropic()
        build_client(8)        -> anthropic.Anthropic(max_retries=8)
    ON (USE_OPENROUTER): same SDK, pointed at OpenRouter with OPENROUTER_API_KEY.
    """
    if use_openrouter():
        kwargs: dict = {
            "base_url": _OPENROUTER_BASE_URL,
            "api_key": os.environ["OPENROUTER_API_KEY"],  # KeyError surfaces a missing key loudly
        }
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        return anthropic.Anthropic(**kwargs)

    # OFF: preserve the exact historical constructions (no base_url/api_key kwargs,
    # so ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY from the env are honoured as before).
    if max_retries is not None:
        return anthropic.Anthropic(max_retries=max_retries)
    return anthropic.Anthropic()
