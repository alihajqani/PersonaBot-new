"""The ``check-keys`` command — verify every configured credential actually works.

Until now the first sign of a bad key was a phase dying half-way through a batch,
and the message was rarely about the key: a revoked credential and a
quota-limited one both surfaced as "generation failed", and with several keys in
rotation there was no way to tell *which* one was at fault. Personas already
written were fine, but the run was wasted.

This probes each key on its own, in parallel, and says what is wrong with each.
Two depths:

* default — ``count_tokens``, which authenticates the key and resolves the model
  name without generating anything, so it is free and instant;
* ``--live`` — a real (tiny) ``generate_content``, which additionally proves the
  key has generation quota left and that the model will actually answer.

The distinction matters because the free probe cannot see a spent free-tier
quota: ``count_tokens`` succeeds on a key whose generation budget is gone.

Nothing here goes through ``AsyncKeyPool`` or ``GeminiClient``: their job is to
rotate away from a failing key and retry, which is exactly what would hide the
per-key answer this command exists to produce.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from personabot.errors import (
    ConfigError,
    LLMQuotaError,
    LLMResponseError,
    LLMTransientError,
)
from personabot.infrastructure.llm.client import classify_error
from personabot.settings import Settings

# status -> (symbol, whether the credential is usable for a real run)
_STATUS = {
    "ok": ("✓", True),
    "quota": ("~", True),
    "invalid": ("✗", False),
    "unreachable": ("?", False),
}


@dataclass(slots=True)
class KeyCheck:
    """The verdict for one credential."""

    label: str
    status: str
    detail: str = ""
    latency_ms: float = 0.0

    @property
    def usable(self) -> bool:
        return _STATUS[self.status][1]

    @property
    def symbol(self) -> str:
        return _STATUS[self.status][0]


@dataclass(slots=True)
class KeyReport:
    provider: str
    model: str
    probe: str
    checks: list[KeyCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Facts about how the probe was made, as opposed to things that need fixing.
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every credential can be used for a run.

        A rate-limited key counts as usable — it is configured correctly and the
        pool will wait it out. A key nobody could reach does not: an unverified
        credential is not a verified one, and exiting 0 on "don't know" would
        make this command useless in CI.
        """
        return bool(self.checks) and all(check.usable for check in self.checks)

    def render(self) -> str:
        lines = ["", "PersonaBot credential check", "=" * 62]
        lines.append(f"  provider          : {self.provider}")
        lines.append(f"  model             : {self.model}")
        lines.append(f"  probe             : {self.probe}")
        lines.append("")
        lines.append(f"  {'credential':<28}{'result':<14}{'latency':>10}")
        lines.append("  " + "-" * 57)

        for check in self.checks:
            latency = f"{check.latency_ms:.0f} ms" if check.latency_ms else "-"
            lines.append(
                f"  {check.symbol} {check.label:<26}{check.status:<14}{latency:>10}"
            )

        lines.append("  " + "-" * 57)
        usable = sum(1 for check in self.checks if check.usable)
        lines.append(f"  {usable}/{len(self.checks)} usable")

        for check in self.checks:
            if check.detail:
                lines.append(f"\n  {check.label}: {check.detail}")

        for note in self.notes:
            lines.append(f"\n  · {note}")

        for warning in self.warnings:
            lines.append(f"\n  ! {warning}")

        lines.append("")
        return "\n".join(lines)


def _mask(key: str) -> str:
    """Identify a key without printing it. Never widen this."""
    return f"…{key[-4:]}" if len(key) >= 4 else "…"


def _verdict(raw_error: BaseException) -> tuple[str, str]:
    """Map a probe failure onto a status and a one-line explanation."""
    error = classify_error(raw_error)

    if isinstance(error, LLMQuotaError):
        return "quota", f"Rate-limited or out of quota right now. {error}"
    if isinstance(error, LLMTransientError):
        return "unreachable", f"Could not reach the API, so the key is unverified. {error}"
    if isinstance(error, LLMResponseError):
        # Authentication clearly worked; the model just would not answer.
        return "ok", f"Authenticated, but the reply was unusable: {error}"
    return "invalid", f"Rejected. {error}"


async def _probe_gemini_key(settings: Settings, key: str, *, live: bool) -> KeyCheck:
    from google import genai
    from google.genai import types

    from personabot.infrastructure.llm.gemini import build_http_options

    check = KeyCheck(label=_mask(key), status="ok")
    started = time.monotonic()

    try:
        client = genai.Client(
            api_key=key,
            # The same options the real client builds, so this check goes over
            # the same route — proxy included. When they were built separately
            # a proxied run could pass here and fail in phase 2, or vice versa.
            http_options=build_http_options(settings),
        )

        if live:
            response = await client.aio.models.generate_content(
                model=settings.gemini_model_name,
                contents="Reply with the JSON object {\"ok\": true} and nothing else.",
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            if not response.candidates:
                raise LLMResponseError(
                    f"no candidates; prompt_feedback={response.prompt_feedback}"
                )
        else:
            await client.aio.models.count_tokens(
                model=settings.gemini_model_name, contents="ping"
            )

    except Exception as raw_error:  # every backend failure is a result, not a crash
        check.status, check.detail = _verdict(raw_error)

    check.latency_ms = (time.monotonic() - started) * 1000
    return check


async def _check_gemini(settings: Settings, *, live: bool) -> KeyReport:
    from personabot.infrastructure.llm.gemini import require_proxy_support

    report = KeyReport(
        provider="gemini",
        model=settings.gemini_model_name,
        probe="generate_content (live)" if live else "count_tokens (free)",
    )

    keys = [key.get_secret_value() for key in settings.gemini_api_keys]

    # Raises ConfigError, which the CLI reports as one clean line. Left to the
    # probes it would come back as a dozen identical "Rejected" verdicts that
    # look like the keys are bad.
    if settings.gemini_proxy:
        require_proxy_support(settings.gemini_proxy)

    # One probe per key, concurrently: distinct credentials cannot rate-limit
    # each other, and a dozen keys checked serially at 180s timeout each is not
    # a command anyone would run.
    report.checks = list(
        await asyncio.gather(
            *(_probe_gemini_key(settings, key, live=live) for key in keys)
        )
    )

    duplicates = len(keys) - len(set(keys))
    if duplicates:
        report.warnings.append(
            f"{duplicates} GEMINI_API_KEY_* entr{'y is a' if duplicates == 1 else 'ies are'} "
            f"duplicate. The pool will treat them as independent keys and hit one "
            f"quota twice as fast."
        )

    if not live and report.ok:
        report.warnings.append(
            "count_tokens only proves the key authenticates and the model exists. "
            "Run with --live to also confirm generation quota is left."
        )

    if len(keys) < settings.concurrent_llm_tasks:
        report.warnings.append(
            f"CONCURRENT_LLM_TASKS={settings.concurrent_llm_tasks} exceeds the "
            f"{len(keys)} configured key(s); effective concurrency is {len(keys)}."
        )

    if settings.gemini_proxy:
        report.notes.append(
            f"GEMINI_PROXY={settings.gemini_proxy} is in use, so this check and "
            f"every generation call went through it. A pass here means a real run "
            f"reaches the API the same way. Phase 4 is unaffected: the browser "
            f"keeps its own route."
        )
    elif any(
        "location is not supported" in (check.detail or "").lower()
        for check in report.checks
    ):
        report.warnings.append(
            "Every key was rejected for its location, which is a geo-block on "
            "this network rather than a problem with the credentials. Set "
            "GEMINI_PROXY in .env to route generation through a proxy."
        )

    return report


async def _check_openai_compat(settings: Settings) -> KeyReport:
    """vLLM and friends have one endpoint, so there is one thing to check.

    Built here rather than through ``OpenAICompatClient`` for the same reason
    the Gemini probe bypasses ``GeminiClient``: that client retries transient
    failures, and a health check that quietly retries for a minute before
    reporting is worse than one that says "unreachable" immediately.
    """
    from openai import AsyncOpenAI

    report = KeyReport(
        provider=f"vllm ({settings.vllm_base_url})",
        model=settings.vllm_model_name,
        probe="chat.completions (live)",
    )

    check = KeyCheck(label=settings.vllm_base_url, status="ok")
    started = time.monotonic()
    client = AsyncOpenAI(
        base_url=settings.vllm_base_url,
        api_key=settings.vllm_api_key.get_secret_value(),
        timeout=float(settings.llm_timeout_seconds),
        max_retries=0,
    )
    try:
        response = await client.chat.completions.create(
            model=settings.vllm_model_name,
            messages=[{"role": "user", "content": 'Return {"ok": true} and nothing else.'}],
            temperature=0.0,
        )
        if not response.choices:
            raise LLMResponseError("endpoint returned no choices")
    except Exception as raw_error:
        check.status, check.detail = _verdict(raw_error)
    finally:
        await client.close()

    check.latency_ms = (time.monotonic() - started) * 1000
    report.checks = [check]
    return report


async def run(settings: Settings, *, live: bool = False) -> KeyReport:
    """Check whatever backend is configured. Raises ConfigError if none is."""
    settings.require_llm_ready()

    if settings.ai_provider == "gemini":
        return await _check_gemini(settings, live=live)
    if settings.ai_provider == "vllm":
        return await _check_openai_compat(settings)

    raise ConfigError(f"Unknown AI_PROVIDER={settings.ai_provider!r}")


__all__ = ["KeyCheck", "KeyReport", "run"]
