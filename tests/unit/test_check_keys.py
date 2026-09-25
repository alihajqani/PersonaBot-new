"""The `check-keys` preflight.

The point of the command is to distinguish failures that used to look identical
once a phase was already running: a revoked key, a throttled key, and a network
outage all surfaced as "generation failed", with no indication of which key.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from pydantic import SecretStr

from personabot.application import check_keys
from personabot.application.check_keys import KeyCheck, KeyReport, _mask, _verdict
from personabot.errors import LLMQuotaError, LLMResponseError, LLMTransientError
from personabot.settings import Settings


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """Never let the developer's own keys leak into these tests.

    `_collect_api_keys` falls back to scanning the environment whenever the
    passed tuple is empty, so without this the "no keys configured" case picks
    up the real `.env` and fires live probes at the API.
    """
    for name in list(os.environ):
        if name.upper().startswith("GEMINI_API_KEY_"):
            monkeypatch.delenv(name)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "gemini_api_keys": (SecretStr("k" * 20 + "AAAA"),),
        "concurrent_llm_tasks": 1,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestVerdicts:
    def test_403_is_invalid_not_a_retryable_blip(self):
        """A revoked key must not be reported as "try again later"."""
        status, detail = _verdict(_HttpError(403, "API key not valid"))
        assert status == "invalid"
        assert "403" in detail

    def test_429_is_quota_and_still_counts_as_usable(self):
        status, _ = _verdict(_HttpError(429, "Resource exhausted"))
        assert status == "quota"
        assert KeyCheck("…abcd", status).usable

    def test_network_failure_is_unreachable_not_invalid(self):
        """An outage says nothing about the key, so it must not be condemned."""
        status, _ = _verdict(ConnectionError("connection refused"))
        assert status == "unreachable"
        assert not KeyCheck("…abcd", status).usable

    def test_unusable_reply_still_means_the_key_authenticated(self):
        status, _ = _verdict(LLMResponseError("no candidates"))
        assert status == "ok"

    @pytest.mark.parametrize(
        "error, expected",
        [
            (LLMQuotaError("quota"), "quota"),
            (LLMTransientError("timeout"), "unreachable"),
            (RuntimeError("something else entirely"), "invalid"),
        ],
    )
    def test_classified_errors_map_through(self, error, expected):
        assert _verdict(error)[0] == expected


class TestReport:
    def test_a_single_bad_key_fails_the_whole_check(self):
        """Exit status drives `make check-keys && make run-all`."""
        report = KeyReport("gemini", "m", "p", checks=[
            KeyCheck("…aaaa", "ok"), KeyCheck("…bbbb", "invalid"),
        ])
        assert not report.ok
        assert "1/2 usable" in report.render()

    def test_throttled_keys_do_not_fail_the_check(self):
        report = KeyReport("gemini", "m", "p", checks=[KeyCheck("…aaaa", "quota")])
        assert report.ok

    def test_no_credentials_is_not_success(self):
        assert not KeyReport("gemini", "m", "p").ok

    def test_render_never_prints_a_whole_key(self):
        key = "AIzaSyTOTALLYSECRET1234"
        report = KeyReport("gemini", "m", "p", checks=[KeyCheck(_mask(key), "ok")])
        assert key not in report.render()
        assert "…1234" in report.render()


class TestRun:
    def test_duplicate_keys_are_flagged(self, monkeypatch):
        """Two identical keys look like two keys but share one quota."""
        async def fake_probe(settings, key, *, live):
            return KeyCheck(_mask(key), "ok")

        monkeypatch.setattr(check_keys, "_probe_gemini_key", fake_probe)
        settings = _settings(gemini_api_keys=(SecretStr("dup"), SecretStr("dup")))

        report = asyncio.run(check_keys.run(settings))

        assert report.ok
        assert any("duplicate" in w for w in report.warnings)

    def test_missing_keys_is_a_config_error_not_a_pass(self):
        from personabot.errors import ConfigError

        with pytest.raises(ConfigError):
            asyncio.run(check_keys.run(_settings(gemini_api_keys=())))
