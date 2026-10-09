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
Circuits are instead isolated *per SocksPort*: streams arriving on different
SocksPorts never share a circuit, so N ports yield N exit IPs concurrently,
with no global mutation and no renewal at all.

Isolating per *SOCKS credential* (Tor's ``IsolateSOCKSAuth``) would be tidier —
one port, a unique username per context — and Tor supports it, but the browser
does not. Chromium implements no SOCKS5 authentication, and Playwright rejects
the attempt with ``Browser does not support socks5 proxy authentication``, so
every context raised instead of merely sharing an IP. Measured on this host:
credentials → hard failure; separate SocksPorts → three distinct exit IPs;
``HTTPTunnelPort`` with credentials → connects but all three shared one IP,
because Chrome only sends ``Proxy-Authorization`` after a 407 challenge that
Tor never issues.

``NEWNYM`` remains available as an explicit maintenance action.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from personabot.errors import CircuitError, TorError
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
        self._next_port = itertools.count()
        self._lock = threading.Lock()
        # server -> monotonic time it was last found unusable.
        self._burned: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self._settings.use_tor

    def circuit_for(self, label: str) -> ProxyConfig | None:
        """Proxy settings on the next SocksPort in the rotation.

        Isolation is *per SocksPort*, not per SOCKS credential. The obvious
        design — one port, a unique username per context, leaning on Tor's
        ``IsolateSOCKSAuth`` — cannot work here: Chromium implements no SOCKS5
        authentication at all, and Playwright rejects it up front with
        ``Browser does not support socks5 proxy authentication``. Supplying
        credentials therefore did not merely fail to isolate, it made every
        context raise. Tor honours ``IsolateSOCKSAuth`` correctly (curl with
        three credential pairs gets three exit IPs) — the browser is the
        limitation.

        Streams arriving on different SocksPorts never share a circuit, so N
        ports give N concurrent submissions N exit IPs. Beyond N, the rotation
        wraps; a port hands out a fresh circuit once its current one exceeds
        ``MaxCircuitDirtiness`` (torrc sets 60s), so sequential reuse still
        rotates the exit IP.
        """
        if not self.enabled:
            return None

        servers = self._settings.tor_proxy_servers
        with self._lock:
            server = self._next_usable(servers)
        log.debug("%s -> %s", label, server)
        return ProxyConfig(server=server)

    def _next_usable(self, servers: tuple[str, ...]) -> str:
        """The next port in the rotation that is not in cooldown.

        Caller holds ``self._lock``. Advancing the counter on every candidate,
        including the ones skipped, is deliberate: it keeps the rotation moving
        so two callers that both skip the same burned port still land on
        different ports rather than colliding on the first healthy one.
        """
        now = time.monotonic()
        cooldown = self._settings.tor_circuit_cooldown_seconds

        for _ in range(len(servers)):
            server = servers[next(self._next_port) % len(servers)]
            burned_at = self._burned.get(server)
            if burned_at is None or now - burned_at >= cooldown:
                self._burned.pop(server, None)
                return server

        # Every port failed within the cooldown. Rather than refuse to hand out
        # anything — which would fail work that a marginal circuit could still
        # carry — return the one that failed longest ago, since it is the one
        # most likely to have been replaced by now.
        oldest = min(servers, key=lambda s: self._burned.get(s, 0.0))
        log.warning(
            "Every SocksPort failed within the last %.0fs. Falling back to %s, "
            "the least recently failed. If this persists, Tor is not reaching "
            "the network — check `journalctl -u tor@default`.",
            cooldown, oldest,
        )
        self._burned.pop(oldest, None)
        return oldest

    def burn(self, proxy: ProxyConfig | None) -> None:
        """Take a circuit out of the rotation until Tor will have replaced it.

        Called when an attempt fails for a reason attributable to the route.
        The cooldown exists because a SocksPort keeps serving the *same* circuit
        until it exceeds ``MaxCircuitDirtiness``; retrying on that port any
        sooner reuses the exact route that just failed, which is why simply
        rotating to the next port is not enough on its own.
        """
        if proxy is None:
            return
        with self._lock:
            self._burned[proxy.server] = time.monotonic()
        log.warning(
            "Circuit on %s is unusable; skipping it for %.0fs.",
            proxy.server, self._settings.tor_circuit_cooldown_seconds,
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


_IP_ECHO_URL = "https://checkip.amazonaws.com"
_PREFLIGHT_TIMEOUT_MS = 20_000


async def verify_circuit(context, *, label: str = "") -> str:
    """Confirm the circuit reaches the internet, and log the exit IP it uses.

    This is a health check first and a log line second. It used to be only the
    latter — best-effort, swallowing every failure — which meant a dead circuit
    was discovered later, by the form navigation, after it had already spent the
    full navigation budget getting nowhere. Fetching a few bytes of plain text
    costs about a second on a healthy circuit and fails fast on a broken one,
    which is the cheapest available signal.

    The threshold is deliberately far below what real work needs: a route that
    cannot deliver a one-line IP echo inside 20s will not deliver a 649 KB form
    inside 60s. A false positive costs one rotation to the next SocksPort, so
    erring towards rejecting marginal circuits is the right trade.

    Raises:
        CircuitError: the circuit is unusable. The caller should burn it and
            retry on another.
    """
    from playwright.async_api import Error as PlaywrightError

    page = await context.new_page()
    tag = f" [{label}]" if label else ""
    try:
        await page.goto(_IP_ECHO_URL, timeout=_PREFLIGHT_TIMEOUT_MS)
        ip = (await page.inner_text("body")).strip()
        log_highlight(log, "Exit IP%s: %s", tag, ip)
        return ip
    except PlaywrightError as exc:
        raise CircuitError(
            f"Circuit preflight failed{tag}: could not reach {_IP_ECHO_URL} "
            f"within {_PREFLIGHT_TIMEOUT_MS // 1000}s "
            f"({_first_line(exc)}). The route is dead or badly congested."
        ) from exc
    finally:
        await page.close()


def _first_line(exc: BaseException) -> str:
    """Playwright errors carry a multi-line call log; the first line is the
    part worth putting in a message."""
    return (str(exc).strip().splitlines() or [""])[0]


class ExitIpLedger:
    """Exit IPs a survey has already submitted from — one JSON line each.

    Port rotation alone does not keep one survey's IPs unique: every process
    starts its rotation at the first SocksPort, Tor may hand two ports the same
    exit, and a port keeps its circuit until it ages out. Two runs a minute
    apart were seen leaving from the same address. The ledger is the record
    that makes "never twice from one IP" checkable across runs.

    An IP is *reserved* when a session is opened on it (so two concurrent
    sessions cannot share it) and *recorded* — persisted — once a submission
    through it succeeds. A reservation is never released within a run: an
    attempt that failed after the form was opened may still have posted.

    ``min_gap`` relaxes "never twice": with ``min_gap=N`` an IP is free again
    once N other submissions were recorded after its last use (``N=1`` = never
    twice in a row). ``0`` keeps every recorded IP spent for good.
    """

    def __init__(self, path: Path, *, min_gap: int = 0) -> None:
        import json

        self.path = path
        self.min_gap = min_gap
        self._last_use: dict[str, int] = {}     # ip -> position of its last record
        self._count = 0
        self._reserved: set[str] = set()
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    ip = json.loads(line).get("ip")
                except ValueError:
                    continue
                if ip:
                    self._last_use[ip] = self._count
                    self._count += 1

    def __contains__(self, ip: str) -> bool:
        return ip in self._last_use or ip in self._reserved

    def __len__(self) -> int:
        return len(self._last_use)

    def available(self, ip: str) -> bool:
        """Whether a new session may leave from ``ip`` now."""
        if ip in self._reserved:
            return False
        last = self._last_use.get(ip)
        if last is None:
            return True
        return self.min_gap > 0 and self._count - last - 1 >= self.min_gap

    def reserve(self, ip: str) -> bool:
        """Claim ``ip`` for one session. False if it is in flight or too recent."""
        if not self.available(ip):
            return False
        self._reserved.add(ip)
        return True

    def record(self, ip: str, *, label: str) -> None:
        import json
        from datetime import datetime, timezone

        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ip": ip, "label": label, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        self._last_use[ip] = self._count
        self._count += 1
        self._reserved.discard(ip)
