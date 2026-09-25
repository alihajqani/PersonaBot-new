"""Retrying a unit of work on a different Tor circuit.

Why this exists: measured against the live target form, three of six circuits
were unusable *at the same moment* — one needed 64s to deliver a 649 KB page,
one returned zero bytes, and one landed on an exit IP the form had already
seen. Three consecutive phase-1 runs against that pool: one failure, two
successes. So one attempt failing says nothing about whether the item can
succeed, and the previous behaviour — abort the whole phase — threw away work
that the very next SocksPort would have carried.

The dangerous half of this is the submitter. Retrying an answer set that was
already POSTed would submit it twice, so `on_circuit` retries `CircuitError`
and nothing else, and `CircuitError` is only ever raised before the form is
POSTed. `test_a_submission_error_is_never_retried` is the guard on that.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from personabot.errors import AlreadyRespondedError, CircuitError, SubmissionError
from personabot.infrastructure.browser.factory import Circuit
from personabot.infrastructure.browser.tor import ProxyConfig
from personabot.providers.base import on_circuit, open_form


class FakeTor:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.burned: list[str | None] = []

    def burn(self, proxy: ProxyConfig | None) -> None:
        self.burned.append(proxy.server if proxy else None)


class FakeBrowser:
    """Hands out a new circuit per call, so retries are distinguishable."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.tor = FakeTor(enabled=enabled)
        self.handed_out: list[str] = []
        self.closed = 0

    @asynccontextmanager
    async def circuit(self, *, label: str = "ctx"):
        server = f"socks5://127.0.0.1:{9050 + len(self.handed_out)}"
        self.handed_out.append(server)
        proxy = ProxyConfig(server=server) if self.tor.enabled else None
        try:
            yield Circuit(context=object(), proxy=proxy)
        finally:
            self.closed += 1


async def run(browser, body, *, attempts: int = 3):
    return await on_circuit(browser=browser, label="item", attempts=attempts, body=body)


class TestRetry:
    @pytest.mark.asyncio
    async def test_a_bad_circuit_is_retried_on_the_next_one(self):
        seen: list[str | None] = []

        async def body(circuit: Circuit) -> str:
            seen.append(circuit.proxy.server)
            if len(seen) < 3:
                raise CircuitError("route is dead")
            return "extracted"

        browser = FakeBrowser()
        assert await run(browser, body) == "extracted"
        assert len(set(seen)) == 3, "each attempt must travel a different circuit"

    @pytest.mark.asyncio
    async def test_each_failed_circuit_is_burned(self):
        """Burning is what stops the retry landing back on the same route.

        A SocksPort keeps serving one circuit until `MaxCircuitDirtiness`, so
        rotating without burning wraps straight back onto the failed one.
        """
        browser = FakeBrowser()

        async def body(circuit: Circuit) -> None:
            raise CircuitError("route is dead")

        with pytest.raises(CircuitError):
            await run(browser, body)

        assert browser.tor.burned == browser.handed_out
        assert len(browser.tor.burned) == 3

    @pytest.mark.asyncio
    async def test_the_last_failure_is_reported_not_swallowed(self):
        async def body(circuit: Circuit) -> None:
            raise AlreadyRespondedError("this exit IP has already answered")

        with pytest.raises(CircuitError, match="already answered") as caught:
            await run(FakeBrowser(), body)
        assert "all 3 attempts" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_submission_error_is_never_retried(self):
        """The form may already hold the answer set. Retrying would duplicate it."""
        attempts = 0

        async def body(circuit: Circuit) -> None:
            nonlocal attempts
            attempts += 1
            raise SubmissionError("no success confirmation after submitting")

        browser = FakeBrowser()
        with pytest.raises(SubmissionError):
            await run(browser, body)

        assert attempts == 1
        assert browser.tor.burned == [], "the circuit was fine; the submission was not"

    @pytest.mark.asyncio
    async def test_an_unexpected_error_propagates_untouched(self):
        async def body(circuit: Circuit) -> None:
            raise KeyError("a selector moved")

        with pytest.raises(KeyError):
            await run(FakeBrowser(), body)

    @pytest.mark.asyncio
    async def test_the_budget_is_the_callers_whatever_the_route(self):
        """`on_circuit` spends exactly what it is given; how much to give it is
        `Settings.effective_browser_attempts`' decision (see TestBudget)."""
        attempts = 0

        async def body(circuit: Circuit) -> None:
            nonlocal attempts
            attempts += 1
            raise CircuitError("route is dead")

        browser = FakeBrowser(enabled=False)
        with pytest.raises(CircuitError, match="all 2 attempts failed"):
            await run(browser, body, attempts=2)

        assert attempts == 2

    @pytest.mark.asyncio
    async def test_the_context_is_closed_after_every_attempt(self):
        """Otherwise a retrying run leaks a browser profile per failure."""
        browser = FakeBrowser()

        async def body(circuit: Circuit) -> None:
            raise CircuitError("route is dead")

        with pytest.raises(CircuitError):
            await run(browser, body)

        assert browser.closed == 3


class FakePage:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.goto_kwargs: dict = {}

    async def goto(self, url: str, **kwargs) -> None:
        self.goto_kwargs = kwargs | {"url": url}
        if self.error:
            raise self.error


class TestOpenForm:
    @pytest.mark.asyncio
    async def test_a_navigation_failure_is_a_circuit_failure(self):
        """Connect, proxy, TLS and timeout are all properties of the route."""
        from playwright.async_api import Error as PlaywrightError

        page = FakePage(PlaywrightError("Timeout 60000ms exceeded.\nCall log:\n  - x"))

        with pytest.raises(CircuitError) as caught:
            await open_form(page, "https://example.test/s/x",
                            wait_until="domcontentloaded", timeout_ms=60_000)

        message = str(caught.value)
        assert "Timeout 60000ms exceeded." in message
        assert "Call log" not in message, "only the first line belongs in the message"
        assert "60s" in message and "domcontentloaded" in message

    @pytest.mark.asyncio
    async def test_the_wait_condition_reaches_playwright(self):
        page = FakePage()
        await open_form(page, "https://example.test/s/x",
                        wait_until="domcontentloaded", timeout_ms=30_000)
        assert page.goto_kwargs["wait_until"] == "domcontentloaded"
        assert page.goto_kwargs["timeout"] == 30_000


class TestBudget:
    """How many routes one unit of work may try.

    haana/hadi: without Tor every attempt takes the same route and fails the
    same way, so do not retry. fatemehf: on a slow direct link a hung
    navigation usually succeeds on a second try. Both are right, for their
    own failure — so Tor gets the whole budget and a direct link gets two.
    """

    def _settings(self, **overrides):
        from personabot.settings import Settings

        return Settings(_env_file=None, gemini_api_keys=(), **overrides)

    def test_tor_spends_the_whole_budget(self):
        assert self._settings(use_tor=True, browser_max_attempts=5).effective_browser_attempts == 5

    def test_a_direct_link_gets_one_retry_at_most(self):
        assert self._settings(use_tor=False, browser_max_attempts=5).effective_browser_attempts == 2

    def test_a_budget_of_one_is_respected_everywhere(self):
        assert self._settings(use_tor=False, browser_max_attempts=1).effective_browser_attempts == 1

