"""LLM backend protocol, error classification, and shared retry logic.

Audit finding A1 lives here. The predecessor classified errors by substring::

    def _is_quota_error(e):
        msg = str(e).lower()
        return any(k in msg for k in ("429", "quota", "rate", ...))

``"rate"`` is a substring of ``"gene-rate"``. Any exception whose text mentions
``generate_content`` — overwhelmingly the common case in google-genai
tracebacks — was therefore misread as a quota error. The call burned a key
rotation, then another, and finally surfaced as ``All N keys exhausted``, hiding
the real cause entirely. Root-causing a Gemini failure was near-impossible.

Classification now works from SDK exception types and HTTP status codes, with
whole-word matching only as a last resort.
"""

from __future__ import annotations

import asyncio
import random
import re
from abc import ABC, abstractmethod
from typing import Final

from personabot.errors import LLMError, LLMQuotaError, LLMResponseError, LLMTransientError
from personabot.logging_setup import get_logger

log = get_logger(__name__)

_QUOTA_STATUS: Final = frozenset({429})
_TRANSIENT_STATUS: Final = frozenset({500, 502, 503, 504, 408, 522, 524})

# Whole-word only, so "generate" can never match "rate".
_QUOTA_WORDS: Final = re.compile(
    r"\b(quota|rate[\s_-]?limit(?:ed)?|resource[\s_-]?exhausted|"
    r"too[\s_-]many[\s_-]requests|insufficient[\s_-]quota)\b",
    re.IGNORECASE,
)
_TRANSIENT_WORDS: Final = re.compile(
    r"\b(timeout|timed[\s_-]out|connection[\s_-](?:reset|refused|error|aborted)|"
    r"server[\s_-]disconnected|service[\s_-]unavailable|temporarily[\s_-]unavailable|"
    r"bad[\s_-]gateway|remote[\s_-]protocol[\s_-]error)\b",
    re.IGNORECASE,
)


# "41s", "1m30s", "0.5s" — the protobuf Duration spelling used by RetryInfo.
_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)m)?(\d+(?:\.\d+)?)s")
# Free-text fallback: "Please retry in 41.150357496s."
_RETRY_IN_TEXT = re.compile(r"retry in\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def _parse_duration(value: object) -> float | None:
    match = _DURATION.fullmatch(str(value).strip()) if value else None
    if not match:
        return None
    minutes, seconds = match.groups()
    return (float(minutes) * 60 if minutes else 0.0) + float(seconds)


def _quota_scope(quota_id: str) -> str:
    """Per-minute quotas heal by waiting; per-day ones do not."""
    lowered = quota_id.lower()
    if "perminute" in lowered:
        return "minute"
    if "perday" in lowered:
        return "day"
    return "unknown"


def _quota_details(error: BaseException) -> tuple[float | None, str]:
    """Pull ``retryDelay`` and the violated quota's period out of a 429.

    google-genai parks the decoded response body on ``APIError.details``, which
    carries a ``google.rpc.RetryInfo`` and a ``google.rpc.QuotaFailure``. Both
    say things no local heuristic can know: how long the server wants us to wait,
    and whether the limit that tripped resets in a minute or at midnight.
    Falls back to the human-readable message, which repeats the delay in prose.
    """
    retry_after: float | None = None
    scope = "unknown"

    payload = getattr(error, "details", None)
    if isinstance(payload, dict):
        entries = payload.get("error", {})
        entries = entries.get("details") if isinstance(entries, dict) else None
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("@type", ""))
            if kind.endswith("RetryInfo"):
                retry_after = _parse_duration(entry.get("retryDelay"))
            elif kind.endswith("QuotaFailure"):
                for violation in entry.get("violations") or []:
                    if isinstance(violation, dict):
                        scope = _quota_scope(str(violation.get("quotaId", "")))

    if retry_after is None:
        text_match = _RETRY_IN_TEXT.search(str(error))
        if text_match:
            retry_after = float(text_match.group(1))

    return retry_after, scope


def _as_quota_error(error: BaseException, message: str) -> LLMQuotaError:
    retry_after, scope = _quota_details(error)
    return LLMQuotaError(message, retry_after=retry_after, scope=scope)


# Exception class names (anywhere in the MRO) that mean "the transport failed,
# try again": google-api-core, the openai SDK, httpx, aiohttp and builtins.
_TRANSIENT_TYPES: Final = frozenset({
    "ServiceUnavailable", "DeadlineExceeded", "InternalServerError",
    "APIConnectionError", "APITimeoutError",
    "TransportError", "TimeoutException", "NetworkError", "ProtocolError",
    "ConnectError", "ReadError", "WriteError", "ConnectTimeout", "ReadTimeout",
    "PoolTimeout", "RemoteProtocolError",
    "ClientConnectionError", "ServerDisconnectedError", "ClientConnectorError",
    "ConnectionError", "TimeoutError",
})


def _status_code(error: BaseException) -> int | None:
    """Pull an HTTP status off an SDK exception, whatever it calls the field."""
    for attribute in ("status_code", "code", "http_status"):
        value = getattr(error, attribute, None)
        if isinstance(value, int) and 100 <= value < 600:
            return value
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def classify_error(error: BaseException) -> LLMError:
    """Map a backend exception onto our hierarchy.

    Order matters: exception *type* first, then status code, then — only if
    neither is conclusive — whole-word text matching.
    """
    if isinstance(error, LLMError):
        return error

    type_name = type(error).__name__
    if type_name in ("ResourceExhausted", "RateLimitError", "TooManyRequests"):
        return _as_quota_error(error, str(error))
    # Matched against the whole class hierarchy, not just the leaf name.
    # httpx raises `ConnectError`, `ReadError`, `PoolTimeout`… — all subclasses
    # of `TransportError` — and the leaf-name list missed `ConnectError`, so a
    # dropped connection was reported as a rejected key by `check-keys` and
    # treated as a permanent failure (no retry) by the real client.
    lineage = {cls.__name__ for cls in type(error).__mro__}
    if lineage & _TRANSIENT_TYPES:
        return LLMTransientError(f"{type_name}: {error}")

    status = _status_code(error)
    if status in _QUOTA_STATUS:
        return _as_quota_error(error, f"HTTP {status}: {error}")
    if status in _TRANSIENT_STATUS:
        return LLMTransientError(f"HTTP {status}: {error}")
    if status is not None and 400 <= status < 500:
        # A genuine client error — bad request, auth. Retrying cannot help.
        return LLMError(f"HTTP {status}: {error}")

    message = str(error)
    if _QUOTA_WORDS.search(message):
        return _as_quota_error(error, message)
    if _TRANSIENT_WORDS.search(message):
        return LLMTransientError(message)

    if isinstance(error, (asyncio.TimeoutError, ConnectionError, OSError)):
        return LLMTransientError(f"{type_name}: {error}")

    return LLMError(f"{type_name}: {error}")


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential backoff with full jitter.

    Jitter matters under concurrency: without it, N tasks that fail together
    retry together, reproducing the burst that caused the failure.
    """
    return random.uniform(0.0, min(cap, base * (2 ** attempt)))


class LLMClient(ABC):
    """Interface every backend implements."""

    @abstractmethod
    async def generate(
        self, system_prompt: str, user_prompt: str, temperature: float
    ) -> str:
        """Return raw text, requested as JSON.

        Raises:
            LLMQuotaError / LLMTransientError / LLMResponseError / LLMError
        """

    async def aclose(self) -> None:
        """Release any held resources."""
