"""OpenAI-compatible backend — vLLM, Ollama, LM Studio, and friends."""

from __future__ import annotations

import asyncio

from personabot.errors import LLMError, LLMResponseError, LLMTransientError
from personabot.infrastructure.llm.client import LLMClient, backoff_delay, classify_error
from personabot.logging_setup import get_logger
from personabot.settings import Settings

log = get_logger(__name__)


class OpenAICompatClient(LLMClient):
    """Any OpenAI-compatible chat endpoint.

    The predecessor gave this backend no retry, no timeout and no error handling
    at all — a single blip propagated and killed the phase, while the Gemini path
    retried. Both backends now behave the same way.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=self._settings.vllm_base_url,
                api_key=self._settings.vllm_api_key.get_secret_value(),
                timeout=float(self._settings.llm_timeout_seconds),
                max_retries=0,  # retry policy lives here, not in the SDK
            )
        return self._client

    async def generate(
        self, system_prompt: str, user_prompt: str, temperature: float
    ) -> str:
        attempts = 0
        while True:
            try:
                return await self._call(system_prompt, user_prompt, temperature)
            except Exception as raw_error:
                error = classify_error(raw_error)
                if not isinstance(error, (LLMTransientError, LLMResponseError)):
                    raise error from raw_error

                attempts += 1
                if attempts > self._settings.llm_max_retries:
                    raise error from raw_error

                delay = backoff_delay(attempts)
                log.warning(
                    "Transient failure (%s). Retry %d/%d in %.1fs.",
                    error, attempts, self._settings.llm_max_retries, delay,
                )
                await asyncio.sleep(delay)

    async def _call(self, system_prompt: str, user_prompt: str, temperature: float) -> str:
        response = await self._ensure_client().chat.completions.create(
            model=self._settings.vllm_model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            top_p=0.95,
            response_format={"type": "json_object"},
        )

        if not response.choices:
            raise LLMResponseError("Endpoint returned no choices")

        content = response.choices[0].message.content
        if not content or not content.strip():
            raise LLMResponseError("Endpoint returned empty content")
        return content

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
