"""Launch options assembled by `BrowserFactory`.

The submitter already gets storage isolation for free — `submit_one` wraps every
answer set in its own `browser.context()`, and a Playwright context is a fresh
profile. `BROWSER_INCOGNITO` is about the *window*: when running headful against
Porsa you want to see, and be able to confirm, that Chrome is private.
"""

from __future__ import annotations

from typing import Any

import pytest

from personabot.infrastructure.browser.factory import BrowserFactory
from personabot.settings import Settings


class FakeChromium:
    def __init__(self) -> None:
        self.launch_options: dict[str, Any] = {}
        self.browser = FakeBrowser()

    async def launch(self, **options: Any) -> Any:
        self.launch_options = options
        return self.browser


class FakeBrowser:
    contexts: tuple = ()

    def __init__(self) -> None:
        self.context_options: dict[str, Any] = {}

    async def new_context(self, **options: Any) -> "FakeContext":
        self.context_options = options
        return FakeContext()

    async def close(self) -> None: ...


class FakeContext:
    def __init__(self) -> None:
        self.closed = False
        self.routes: list[tuple[str, object]] = []

    async def route(self, pattern: str, handler) -> None:
        self.routes.append((pattern, handler))

    async def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()

    async def stop(self) -> None: ...


async def started_factory(monkeypatch, **overrides: Any) -> BrowserFactory:
    settings = Settings(_env_file=None, gemini_api_keys=("k",), **overrides)
    factory = BrowserFactory(settings)
    playwright = FakePlaywright()

    class Starter:
        async def start(self) -> FakePlaywright:
            return playwright

    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: Starter(), raising=True
    )
    await factory.start()
    factory._fake = playwright  # type: ignore[attr-defined]
    return factory


async def launch_options_for(monkeypatch, **overrides: Any) -> dict[str, Any]:
    factory = await started_factory(monkeypatch, **overrides)
    return factory._fake.chromium.launch_options  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_incognito_is_on_by_default(monkeypatch):
    options = await launch_options_for(monkeypatch)
    assert options["args"] == ["--incognito"]


@pytest.mark.asyncio
async def test_incognito_can_be_turned_off(monkeypatch):
    options = await launch_options_for(monkeypatch, browser_incognito=False)
    assert "args" not in options


@pytest.mark.asyncio
async def test_incognito_survives_the_channel_fallback(monkeypatch):
    """Falling back to bundled Chromium drops the channel, not the privacy."""
    options = await launch_options_for(monkeypatch, browser_channel="chrome")
    assert options["channel"] == "chrome"
    assert options["args"] == ["--incognito"]


class TestCircuitIsVisibleToTheCaller:
    """A caller that fails has to be able to say *which* route failed.

    A Playwright context does not expose its own proxy settings, so before
    `circuit()` existed there was no way to burn the circuit an attempt had used
    — the retry went straight back onto the port that had just failed.
    """

    @pytest.mark.asyncio
    async def test_the_route_is_yielded_alongside_the_context(self, monkeypatch):
        factory = await started_factory(
            monkeypatch, use_tor=True, tor_socks_port=9050,
            tor_socks_extra_ports="9052",
        )
        async with factory.circuit(label="one") as first:
            assert first.proxy.server == "socks5://127.0.0.1:9050"
        async with factory.circuit(label="two") as second:
            assert second.proxy.server == "socks5://127.0.0.1:9052"

    @pytest.mark.asyncio
    async def test_the_proxy_reaches_the_context_options(self, monkeypatch):
        factory = await started_factory(monkeypatch, use_tor=True, tor_socks_port=9050)
        browser = factory._fake.chromium.browser  # type: ignore[attr-defined]

        async with factory.circuit(label="ctx"):
            assert browser.context_options["proxy"] == {"server": "socks5://127.0.0.1:9050"}

    @pytest.mark.asyncio
    async def test_no_tor_means_no_proxy_and_no_route(self, monkeypatch):
        factory = await started_factory(monkeypatch, use_tor=False)
        browser = factory._fake.chromium.browser  # type: ignore[attr-defined]

        async with factory.circuit(label="ctx") as circuit:
            assert circuit.proxy is None
            assert "proxy" not in browser.context_options

    @pytest.mark.asyncio
    async def test_the_context_is_closed_on_the_way_out(self, monkeypatch):
        """Retries open one context per attempt; leaking them leaks profiles."""
        factory = await started_factory(monkeypatch)

        async with factory.circuit(label="ctx") as circuit:
            context = circuit.context
        assert context.closed

    @pytest.mark.asyncio
    async def test_the_factory_shares_its_tor_manager(self, monkeypatch):
        """Providers burn circuits on this instance; a private copy would be
        written to and never read."""
        factory = await started_factory(monkeypatch, use_tor=True)
        assert factory.tor is factory._tor


class TestHeavyAssets:
    """From the fatemehf fork: on a slow link, images and fonts starved five
    concurrent Google Forms submissions until one timed out."""

    @pytest.mark.asyncio
    async def test_heavy_assets_are_routed_away_by_default(self, monkeypatch):
        factory = await started_factory(monkeypatch)
        async with factory.circuit(label="ctx") as circuit:
            assert [pattern for pattern, _ in circuit.context.routes] == ["**/*"]

    @pytest.mark.asyncio
    async def test_blocking_can_be_turned_off(self, monkeypatch):
        factory = await started_factory(monkeypatch, block_heavy_assets=False)
        async with factory.circuit(label="ctx") as circuit:
            assert circuit.context.routes == []

    @pytest.mark.asyncio
    async def test_only_images_fonts_and_media_are_aborted(self):
        from personabot.infrastructure.browser.factory import _abort_heavy_assets

        class Route:
            def __init__(self, kind: str) -> None:
                self.request = type("Request", (), {"resource_type": kind})()
                self.outcome = None

            async def abort(self) -> None:
                self.outcome = "abort"

            async def continue_(self) -> None:
                self.outcome = "continue"

        outcomes = {}
        for kind in ("image", "font", "media", "script", "document", "xhr", "stylesheet"):
            route = Route(kind)
            await _abort_heavy_assets(route)
            outcomes[kind] = route.outcome
        assert {k for k, v in outcomes.items() if v == "abort"} == {"image", "font", "media"}

