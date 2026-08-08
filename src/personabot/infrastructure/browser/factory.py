"""Browser and context lifecycle.

One ``Browser`` per process; one ``BrowserContext`` per unit of work. A context
is a fully isolated profile — cookies, storage, cache — so concurrent
submissions cannot leak session state into one another. Launching a whole
browser per submission (what the predecessor did) costs seconds of startup each
time and buys nothing extra.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from personabot.errors import BrowserError
from personabot.infrastructure.browser.tor import TorManager
from personabot.logging_setup import get_logger
from personabot.settings import Settings

log = get_logger(__name__)


class BrowserFactory:
    """Owns the Playwright browser and hands out isolated contexts."""

    def __init__(self, settings: Settings, tor: TorManager | None = None) -> None:
        self._settings = settings
        self._tor = tor or TorManager(settings)
        self._playwright: Any = None
        self._browser: Any = None

    async def start(self) -> None:
        from playwright.async_api import Error as PlaywrightError, async_playwright

        if self._browser is not None:
            return

        self._playwright = await async_playwright().start()

        launch_options: dict[str, Any] = {
            "headless": self._settings.headless_mode,
            "slow_mo": self._settings.slow_mo,
        }
        if self._settings.browser_channel:
            launch_options["channel"] = self._settings.browser_channel

        try:
            self._browser = await self._playwright.chromium.launch(**launch_options)
        except PlaywrightError as exc:
            if self._settings.browser_channel:
                log.warning(
                    "Could not launch channel %r (%s). Falling back to bundled Chromium.",
                    self._settings.browser_channel, exc,
                )
                launch_options.pop("channel", None)
                self._browser = await self._playwright.chromium.launch(**launch_options)
            else:
                raise BrowserError(f"Could not launch Chromium: {exc}") from exc

        log.info(
            "Browser ready (headless=%s, slow_mo=%dms%s).",
            self._settings.headless_mode,
            self._settings.slow_mo,
            ", tor=on" if self._tor.enabled else "",
        )

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @asynccontextmanager
    async def context(self, *, label: str = "ctx") -> AsyncIterator[Any]:
        """An isolated browser context, on its own Tor circuit when enabled."""
        if self._browser is None:
            raise BrowserError("BrowserFactory.start() has not been called")

        options: dict[str, Any] = {
            "locale": "fa-IR",
            "timezone_id": "Asia/Tehran",
            "viewport": {"width": 1366, "height": 900},
        }
        proxy = self._tor.circuit_for(label)
        if proxy is not None:
            options["proxy"] = proxy.as_playwright()

        context = await self._browser.new_context(**options)
        try:
            yield context
        finally:
            await context.close()

    async def __aenter__(self) -> "BrowserFactory":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()
