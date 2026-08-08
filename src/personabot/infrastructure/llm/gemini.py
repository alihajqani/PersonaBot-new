"""Gemini backend (google-genai v2 SDK)."""

from __future__ import annotations

import asyncio

from personabot.errors import LLMError, LLMKeysExhausted, LLMQuotaError, LLMResponseError, LLMTransientError
from personabot.infrastructure.llm.client import LLMClient, backoff_delay, classify_error
from personabot.infrastructure.llm.keypool import AsyncKeyPool
from personabot.logging_setup import get_logger
from personabot.settings import Settings

log = get_logger(__name__)


class GeminiClient(LLMClient):
    """Gemini with leased key rotation and separated retry budgets.

    Quota retries and transient retries are counted separately. The predecessor
    shared one loop and called ``get_next_key()`` at the top of every iteration,
    so a transient network blip consumed a key rotation; ``keys_tried`` was never
    reset after a success either. Combined with the substring bug (A1), a single
    flaky connection could exhaust the whole key pool and report a quota failure.
    """

    def __init__(self, settings: Settings, pool: AsyncKeyPool) -> None:
        self._settings = settings
        self._pool = pool

    async def generate(
        self, system_prompt: str, user_prompt: str, temperature: float
    ) -> str:
        quota_failures = 0
        transient_failures = 0
        max_quota_failures = self._pool.size
        max_transient_failures = self._settings.llm_max_retries
        last_error: LLMError | None = None

        while True:
            async with await self._pool.acquire() as lease:
                try:
                    return await self._call(lease.value, system_prompt, user_prompt, temperature)

                except Exception as raw_error:
                    error = classify_error(raw_error)

                    if isinstance(error, LLMQuotaError):
                        lease.penalize(self._pool.default_cooldown)
                        quota_failures += 1
                        last_error = error
                        log.warning(
                            "Key %s hit its quota (%d/%d). Rotating.",
                            lease.tail, quota_failures, max_quota_failures,
                        )
                        if quota_failures >= max_quota_failures:
                            raise LLMKeysExhausted(
                                f"All {self._pool.size} Gemini key(s) are rate-limited. "
                                f"Last error: {last_error}"
                            ) from raw_error
                        continue

                    if isinstance(error, (LLMTransientError, LLMResponseError)):
                        transient_failures += 1
                        last_error = error
                        if transient_failures > max_transient_failures:
                            raise error from raw_error
                        delay = backoff_delay(transient_failures)
                        log.warning(
                            "Transient failure (%s). Retry %d/%d in %.1fs.",
                            error, transient_failures, max_transient_failures, delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    # Auth failure, malformed request — retrying cannot help.
                    raise error from raw_error

    async def _call(
        self, api_key: str, system_prompt: str, user_prompt: str, temperature: float
    ) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=self._settings.llm_timeout_seconds * 1000
            ),
        )

        response = await client.aio.models.generate_content(
            model=self._settings.gemini_model_name,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=temperature,
                top_p=0.95,
                response_mime_type="application/json",
                # Deliberate, and carried forward consciously rather than by
                # inheritance: personas cover sexual-health content that the
                # default filters refuse, which would bias the sample toward
                # respondents whose answers happen to be inoffensive.
                safety_settings=[
                    types.SafetySetting(category=category, threshold="BLOCK_NONE")
                    for category in (
                        "HARM_CATEGORY_HARASSMENT",
                        "HARM_CATEGORY_HATE_SPEECH",
                        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "HARM_CATEGORY_DANGEROUS_CONTENT",
                    )
                ],
            ),
        )

        if not response.candidates:
            raise LLMResponseError(
                f"Gemini returned no candidates; prompt_feedback={response.prompt_feedback}"
            )

        text = response.text
        if not text or not text.strip():
            finish_reason = getattr(response.candidates[0], "finish_reason", "unknown")
            raise LLMResponseError(f"Gemini returned empty text (finish_reason={finish_reason})")

        return text
