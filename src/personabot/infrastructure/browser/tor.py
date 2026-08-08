"""Tor integration, scoped strictly to the browser.

Two changes from the predecessor.

**Proxy settings never touch ``os.environ``.** The old ``config.py`` did
``os.environ.setdefault("HTTPS_PROXY", ...)`` at import time, which leaked the
proxy into every outbound request in the process — the Gemini SDK, the IP
check, everything — not just the browser it was meant for. Proxy configuration
is passed to Playwright launch/context arguments only.

**Concurrent submissions get separate circuits without ``NEWNYM``.** ``NEWNYM``
is process-global: renewing mid-flight changes the exit IP for every in-flight
context at once, so it cannot give N concurrent submissions N distinct IPs.
Tor's ``IsolateSOCKSAuth`` (on by default) already isolates circuits *per SOCKS
credential pair*, so handing each browser context its own username yields its
own circuit and its own exit IP — concurrently, with no global mutation and no
renewal at all.

``NEWNYM`` remains available as an explicit maintenance action.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from personabot.errors import TorError
from personabot.logging_setup import get_logger, log_highlight
from personabot.settings import Settings

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """Playwright proxy settings for one browser context."""

    server: str
    username: str | None = None
    password: str | None = None

    def as_playwright(self) -> dict[str, str]:
        proxy: dict[str, str] = {"server": self.server}
        if self.username:
            proxy["username"] = self.username
        if self.password:
            proxy["password"] = self.password
        return proxy


class TorManager:
    """Issues isolated circuits and, on request, renews them."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.use_tor

    def circuit_for(self, label: str) -> ProxyConfig | None:
        """Proxy settings whose credentials pin a distinct circuit.

        Any two contexts with different credentials travel different circuits and
        therefore exit from different IPs. The password is random per call so a
        label reused across runs does not reuse a circuit.
        """
        if not self.enabled:
            return None
        return ProxyConfig(
            server=self._settings.tor_proxy_server,
            username=f"personabot-{label}",
            password=secrets.token_hex(8),
        )

    def renew_identity(self) -> None:
        """Send ``NEWNYM``. Raises rather than continuing on the old IP.

        The predecessor's four providers disagreed: avalform aborted,
        google_forms carried on with the stale IP, and porsline and digisurvey
        swallowed the exception with a bare ``except``. Silently reusing an IP
        defeats the only reason Tor was enabled.
        """
        if not self.enabled:
            return

        try:
            from stem import Signal
            from stem.control import Controller
        except ImportError as exc:
            raise TorError("USE_TOR is set but `stem` is not installed") from exc

        try:
            with Controller.from_port(port=self._settings.tor_control_port) as controller:
                controller.authenticate(
                    password=self._settings.tor_control_password.get_secret_value()
                )
                if not controller.is_newnym_available():
                    wait = controller.get_newnym_wait()
                    log.info("Tor rate-limits identity changes; waiting %.0fs.", wait)
                    controller.wait_for_newnym()
                controller.signal(Signal.NEWNYM)
                log_highlight(log, "Requested a new Tor identity.")
        except TorError:
            raise
        except Exception as exc:
            raise TorError(
                f"Tor control port {self._settings.tor_control_port} is unreachable "
                f"({type(exc).__name__}: {exc}). Check that Tor is running, that "
                f"ControlPort is enabled, and that TOR_CONTROL_PASSWORD matches."
            ) from exc


async def report_exit_ip(context, *, label: str = "") -> str | None:
    """Log the exit IP for one context. Best-effort; never fails a submission."""
    from playwright.async_api import Error as PlaywrightError

    page = await context.new_page()
    try:
        await page.goto("https://checkip.amazonaws.com", timeout=30_000)
        ip = (await page.inner_text("body")).strip()
        log_highlight(log, "Exit IP%s: %s", f" [{label}]" if label else "", ip)
        return ip
    except PlaywrightError as exc:
        log.warning("Could not determine the exit IP%s: %s", f" [{label}]" if label else "", exc)
        return None
    finally:
        await page.close()
