"""Builds the configured LLM client.

One client is constructed per phase and shared by every concurrent task, so the
key pool and its cooldowns are shared too. The predecessor called
``get_llm_client()`` inside the per-persona loop, which rebuilt the client — and
logged a banner — on every single call.
"""

from __future__ import annotations

import json
import re

from personabot.errors import ConfigError, LLMResponseError
from personabot.infrastructure.llm.client import LLMClient
from personabot.infrastructure.llm.gemini import GeminiClient
from personabot.infrastructure.llm.keypool import AsyncKeyPool
from personabot.infrastructure.llm.openai_compat import OpenAICompatClient
from personabot.logging_setup import get_logger
from personabot.settings import Settings

log = get_logger(__name__)

_FENCED = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def build_llm_client(settings: Settings) -> LLMClient:
    settings.require_llm_ready()

    if settings.ai_provider == "gemini":
        pool = AsyncKeyPool(
            [key.get_secret_value() for key in settings.gemini_api_keys]
        )
        log.info(
            "LLM: Gemini | model=%s | %d key(s) | concurrency=%d",
            settings.gemini_model_name, pool.size, settings.concurrent_llm_tasks,
        )
        if pool.size < settings.concurrent_llm_tasks:
            log.warning(
                "CONCURRENT_LLM_TASKS=%d exceeds the %d available key(s). Tasks will "
                "queue for a free key, so effective concurrency is %d.",
                settings.concurrent_llm_tasks, pool.size, pool.size,
            )
        return GeminiClient(settings, pool)

    if settings.ai_provider == "vllm":
        log.info(
            "LLM: OpenAI-compatible | model=%s | endpoint=%s",
            settings.vllm_model_name, settings.vllm_base_url,
        )
        return OpenAICompatClient(settings)

    raise ConfigError(f"Unknown AI_PROVIDER={settings.ai_provider!r}")


def parse_json_response(text: str) -> dict | list:
    """Parse a model response that is supposed to be JSON.

    Tries the raw text first — with ``response_mime_type=application/json`` it
    usually is valid — then a fenced code block, then the outermost braces. The
    predecessor always sliced between the first ``{`` and last ``}``, which
    corrupts a response that legitimately begins with a JSON array, and it was
    applied in the answer generator but not the persona generator.
    """
    stripped = text.strip()
    if not stripped:
        raise LLMResponseError("Empty response")

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    fenced = _FENCED.search(stripped)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = stripped.find(opener), stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue

    preview = stripped[:200].replace("\n", " ")
    raise LLMResponseError(f"Response is not valid JSON. Starts with: {preview!r}")
