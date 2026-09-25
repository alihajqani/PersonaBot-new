"""Gemini backend (google-genai v2 SDK)."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from personabot.errors import (
    ConfigError,
    LLMError,
    LLMKeysExhausted,
    LLMQuotaError,
    LLMResponseError,
    LLMTransientError,
)
from personabot.infrastructure.llm.client import LLMClient, backoff_delay, classify_error
from personabot.infrastructure.llm.keypool import AsyncKeyPool
from personabot.logging_setup import get_logger
from personabot.settings import Settings

log = get_logger(__name__)


def build_http_options(settings: Settings) -> Any:
    """``HttpOptions`` for a Gemini client, carrying ``GEMINI_PROXY`` when set.

    ``GEMINI_PROXY`` was a declared setting that nothing read: the client was
    built with a timeout and nothing else, so every call went direct however it
    was configured. Where the Gemini API is geo-blocked that is the difference
    between the pipeline running and every key coming back
    ``FAILED_PRECONDITION: User location is not supported`` — which reads like
    a credential problem and is not one.

    Deliberately scoped to this client rather than exported into ``os.environ``:
    Playwright talks to the survey host, and phase 4 must keep going out over
    the connection (or the Tor circuit) the browser settings choose.
    """
    from google.genai import types

    options = types.HttpOptions(timeout=settings.llm_timeout_seconds * 1000)

    proxy = settings.gemini_proxy
    if not proxy:
        return options

    require_proxy_support(proxy)

    import httpx

    # google-genai passes these straight to httpx.Client / httpx.AsyncClient.
    options.client_args = {"proxy": proxy}
    options.async_client_args = {
        "proxy": proxy,
        # Pins the httpx path. google-genai switches async calls to aiohttp
        # whenever aiohttp is importable and no transport was supplied, and
        # aiohttp would never see `proxy` — the call would silently go direct
        # and fail with the geo-block this setting exists to route around.
        "transport": httpx.AsyncHTTPTransport(proxy=proxy),
    }
    return options


def require_proxy_support(proxy: str) -> None:
    """Fail early, and by name, on a proxy httpx cannot open.

    httpx only speaks SOCKS when `socksio` is installed, and the error it
    raises otherwise surfaces mid-generation as a transient failure that the
    retry loop dutifully repeats against every key in the pool.
    """
    scheme = urlsplit(proxy).scheme.lower()

    if scheme in ("socks5", "socks5h", "socks4"):
        try:
            import socksio  # noqa: F401
        except ImportError:
            raise ConfigError(
                f"GEMINI_PROXY={proxy} is a SOCKS proxy, but httpx cannot open "
                f"one without the `socksio` package. Install it with "
                f"`.venv/bin/python -m pip install 'httpx[socks]'`, or point "
                f"GEMINI_PROXY at an http:// proxy instead."
            ) from None
        return

    if scheme not in ("http", "https"):
        raise ConfigError(
            f"GEMINI_PROXY={proxy} has no scheme httpx understands. Use "
            f"http://host:port, https://host:port or socks5://host:port."
        )

# A per-day quota does not clear by waiting a minute. Without a server-supplied
# retryDelay there is no way to know when it resets, so the key is set aside for
# long enough that the pool stops hammering a project that is done for the day.
_DAILY_QUOTA_COOLDOWN = 900.0

# Gemini also answers a bare
# ``{"message": "Resource has been exhausted (e.g. check quota)."}`` — no
# QuotaFailure, no RetryInfo, nothing to say which limit tripped. It is tempting
# to read that as momentary load and back off for a few seconds. Measured
# against the live API, that is wrong: shortening the wait to 2-25s took 429s
# from ~12/min to ~55/min and turned zero construct failures into ten, because
# a call cycles through all N keys inside one cooldown and gives up. Treat an
# unexplained 429 exactly like a spent per-minute budget — wait it out.


class GeminiClient(LLMClient):
    """Gemini with leased key rotation and separated retry budgets.

    Quota retries and transient retries are counted separately. The predecessor
    shared one loop and called ``get_next_key()`` at the top of every iteration,
    so a transient network blip consumed a key rotation; ``keys_tried`` was never
    reset after a success either. Combined with the substring bug (A1), a single
    flaky connection could exhaust the whole key pool and report a quota failure.

    Measuring a real run turned up the reverse failure too. A transient 500 was
    retried 1.4 seconds later, and because the pool preferred the lowest-indexed
    free key that retry landed on the *same* project — three attempts putting
    ~17,000 input tokens through one 16,000-per-minute budget in five seconds,
    turning a flaky connection into a genuine quota cascade. Transient retries
    now park the key for the length of their own backoff, so the next attempt is
    guaranteed to be a different credential.
    """

    def __init__(self, settings: Settings, pool: AsyncKeyPool) -> None:
        self._settings = settings
        self._pool = pool
        # One SDK client per key, not one per request. Rebuilding it every call
        # meant a fresh TLS handshake and a fresh connection pool each time, and
        # nothing ever closed them.
        self._clients: dict[str, object] = {}

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
                    return await self._call(
                        lease, system_prompt, user_prompt, temperature
                    )

                except Exception as raw_error:
                    error = classify_error(raw_error)

                    if isinstance(error, LLMQuotaError):
                        lease.penalize(self._cooldown_for(error, quota_failures))
                        quota_failures += 1
                        last_error = error
                        log.warning(
                            "Key %s hit its quota (%d/%d, scope=%s). Rotating. %s",
                            lease.tail, quota_failures, max_quota_failures,
                            error.scope, error,
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
                        # The prompt reached the model even though the reply did
                        # not come back, so the input tokens were spent. Charge
                        # them, or the retry storm becomes a quota storm.
                        lease.record_input_tokens(self._pool.expected_input_tokens)
                        lease.penalize(delay)
                        log.warning(
                            "Transient failure (%s). Retry %d/%d on another key in %.1fs.",
                            error, transient_failures, max_transient_failures, delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    # Auth failure, malformed request — retrying cannot help.
                    raise error from raw_error

    def _cooldown_for(self, error: LLMQuotaError, attempt: int) -> float:
        """How long to set a key aside, preferring the server's own answer."""
        if error.retry_after is not None:
            return max(1.0, error.retry_after)
        if error.scope == "day":
            return _DAILY_QUOTA_COOLDOWN
        # Both a named per-minute quota and an unexplained exhaustion: wait out
        # a full window rather than cycling the pool.
        return self._pool.default_cooldown

    def _client_for(self, api_key: str):
        from google import genai

        client = self._clients.get(api_key)
        if client is None:
            client = genai.Client(
                api_key=api_key,
                http_options=build_http_options(self._settings),
            )
            self._clients[api_key] = client
        return client

    async def _call(
        self, lease, system_prompt: str, user_prompt: str, temperature: float
    ) -> str:
        from google.genai import types

        client = self._client_for(lease.value)

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

        # The quota that binds on the free tier is measured in *input* tokens,
        # so this is the number the pool paces on — recorded before any
        # validation, because the tokens are spent either way.
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            lease.record_input_tokens(getattr(usage, "prompt_token_count", 0) or 0)

        if not response.candidates:
            raise LLMResponseError(
                f"Gemini returned no candidates; prompt_feedback={response.prompt_feedback}"
            )

        text = response.text
        if not text or not text.strip():
            finish_reason = getattr(response.candidates[0], "finish_reason", "unknown")
            raise LLMResponseError(f"Gemini returned empty text (finish_reason={finish_reason})")

        return text

    async def aclose(self) -> None:
        for client in self._clients.values():
            try:
                await client.aio.aclose()
            except Exception as exc:  # a failed close must not sink a finished phase
                log.debug("Closing a Gemini client failed: %s", exc)
        self._clients.clear()
