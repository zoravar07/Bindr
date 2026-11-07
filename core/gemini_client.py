"""Thin async wrapper over the Gemini Interactions API.

Rules enforced here (see CLAUDE.md, rule 8):
- Only `client.interactions.create` is used; multi-turn state goes through
  `previous_interaction_id`.
- Reasoning is set with `generation_config={"thinking_level": ...}`.
- `temperature`, `top_p`, `top_k` and `thinking_budget` are refused.
- Model IDs come from `shared.config.MODEL_IDS` via a role name.

Every failure (missing key, network, API status, timeout, bad JSON) surfaces
as `GeminiError`, so callers can fall back to deterministic code paths.

Verified live on 2026-11-07 with google-genai 3.0.0: `gemini-4-argon`,
`gemini-3.8-flash` and `gemini-3.5-flash-lite` are accepted as-is, and
`response_format={"type": "text", "mime_type": "application/json", "schema": ...}`
returns schema-shaped JSON in `interaction.output_text`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from shared.config import GEMINI_API_KEY, THINKING_LEVELS, model_id

log = logging.getLogger(__name__)

FORBIDDEN_GENERATION_KEYS = frozenset({"temperature", "top_p", "top_k", "thinking_budget"})


class GeminiError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeminiReply:
    text: str
    interaction_id: str | None
    model: str
    data: Any = None  # parsed JSON when a schema was requested


class GeminiClient:
    def __init__(self, api_key: str | None = None, *, client: Any = None):
        self._api_key = api_key if api_key is not None else GEMINI_API_KEY
        self._client = client

    @property
    def available(self) -> bool:
        return self._client is not None or bool(self._api_key)

    def _sdk(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise GeminiError("GEMINI_API_KEY is not set")
            from google import genai  # imported lazily so tests without the SDK still import this module

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def generate(
        self,
        input: str,
        *,
        role: str = "lead_reasoning",
        thinking_level: str = "medium",
        system_instruction: str | None = None,
        json_schema: dict[str, Any] | None = None,
        previous_interaction_id: str | None = None,
        timeout_s: float = 60.0,
        max_output_tokens: int | None = None,
    ) -> GeminiReply:
        if thinking_level not in THINKING_LEVELS:
            raise ValueError(f"thinking_level must be one of {THINKING_LEVELS}, got {thinking_level!r}")
        model = model_id(role)
        generation_config: dict[str, Any] = {"thinking_level": thinking_level}
        if max_output_tokens is not None:
            generation_config["max_output_tokens"] = max_output_tokens
        _assert_allowed(generation_config)

        body: dict[str, Any] = {"model": model, "input": input, "generation_config": generation_config}
        if system_instruction:
            body["system_instruction"] = system_instruction
        if previous_interaction_id:
            body["previous_interaction_id"] = previous_interaction_id
        if json_schema is not None:
            body["response_format"] = {"type": "text", "mime_type": "application/json", "schema": json_schema}

        try:
            sdk = self._sdk()
            interaction = await asyncio.wait_for(sdk.aio.interactions.create(**body), timeout=timeout_s)
        except GeminiError:
            raise
        except asyncio.TimeoutError as exc:
            raise GeminiError(f"{model} timed out after {timeout_s:g}s") from exc
        except Exception as exc:  # SDK APIError subclasses, httpx errors, auth errors
            raise GeminiError(f"{model} call failed: {type(exc).__name__}: {exc}") from exc

        status = getattr(interaction, "status", None)
        if status not in (None, "completed"):
            raise GeminiError(f"{model} interaction ended with status {status!r}")
        text = getattr(interaction, "output_text", None) or ""
        data = None
        if json_schema is not None:
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise GeminiError(f"{model} returned invalid JSON: {text[:200]!r}") from exc
        return GeminiReply(text=text, interaction_id=getattr(interaction, "id", None), model=model, data=data)


def _assert_allowed(generation_config: dict[str, Any]) -> None:
    bad = FORBIDDEN_GENERATION_KEYS & set(generation_config)
    if bad:
        raise ValueError(f"Deprecated generation params are not allowed: {sorted(bad)}")


_default: GeminiClient | None = None


def get_gemini() -> GeminiClient:
    global _default
    if _default is None:
        _default = GeminiClient()
    return _default


def set_gemini(client: GeminiClient | None) -> None:
    """Swap the process-wide client (tests, or disabling LLM calls with GeminiClient(api_key=""))."""
    global _default
    _default = client
