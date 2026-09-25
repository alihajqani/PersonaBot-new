"""Browser and context lifecycle.

One ``Browser`` per process; one ``BrowserContext`` per unit of work. A context
is a fully isolated profile — cookies, storage, cache — so concurrent
submissions cannot leak session state into one another. Launching a whole
browser per submission (what the predecessor did) costs seconds of startup each
time and buys nothing extra.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from personabot.errors import BrowserError
from personabot.infrastructure.browser.tor import ProxyConfig, TorManager
from personabot.logging_setup import get_logger
from personabot.settings import Settings

log = get_logger(__name__)

#: Playwright resource types that a form never needs in order to be filled in.
_HEAVY_RESOURCES = frozenset({"image", "font", "media"})


async def _abort_heavy_assets(route: Any) -> None:
    if route.request.resource_type in _HEAVY_RESOURCES:
        await route.abort()
    else:
        await route.continue_()


@dataclass(frozen=True, slots=True)
class Circuit:
    """A browser context together with the route it was handed.

    The pairing is the point. A caller that retries a failed attempt has to be
    able to tell the :class:`TorManager` *which* circuit failed, and the
    Playwright context does not expose its own proxy settings.
    """

    context: Any
    proxy: ProxyConfig | None


class BrowserFactory:
    """Owns the Playwright browser and hands out isolated contexts."""

    def __init__(self, settings: Settings, tor: TorManager | None = None) -> None:
        self._settings = settings
        self._tor = tor or TorManager(settings)
        self._playwright: Any = None
        self._browser: Any = None

    @property
    def tor(self) -> TorManager:
        """The manager that routes this factory's contexts.

        Exposed because it holds mutable rotation state — which SocksPorts are
        in cooldown. A caller that built its own :class:`TorManager` would burn
        circuits on an instance nobody consults, so the failures would be logged
        and then repeated on the very same port.
        """
        return self._tor

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
        if self._settings.browser_incognito:
            # Storage isolation already comes from `new_context()` below — every
            # submission gets its own cookie jar and origin storage regardless.
            # This flag makes the *window* a real incognito window, which is what
            # you see (and can confirm) when running headful.
            launch_options["args"] = ["--incognito"]

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
            "Browser ready (headless=%s, slow_mo=%dms%s%s).",
            self._settings.headless_mode,
            self._settings.slow_mo,
            ", incognito=on" if self._settings.browser_incognito else "",
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
    async def circuit(self, *, label: str = "ctx") -> AsyncIterator[Circuit]:
        """An isolated browser context, on its own Tor circuit when enabled.

        Yields the route alongside the context so a caller that fails can burn
        the circuit it was given and retry on a different one.
        """
        if self._browser is None:
            raise BrowserError("BrowserFactory.start() has not been called")

        options: dict[str, Any] = {
            "locale": self._settings.browser_locale,
            "timezone_id": self._settings.browser_timezone,
            "viewport": {"width": 1366, "height": 900},
        }
        proxy = self._tor.circuit_for(label)
        if proxy is not None:
            options["proxy"] = proxy.as_playwright()

        context = await self._browser.new_context(**options)
        try:
            if self._settings.block_heavy_assets:
                await context.route("**/*", _abort_heavy_assets)
            yield Circuit(context=context, proxy=proxy)
        finally:
            await context.close()

    @asynccontextmanager
    async def context(self, *, label: str = "ctx") -> AsyncIterator[Any]:
        """Just the context, for callers with no interest in the route."""
        async with self.circuit(label=label) as circuit:
            yield circuit.context

    async def __aenter__(self) -> "BrowserFactory":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()
