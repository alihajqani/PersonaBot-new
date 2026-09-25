"""Circuit isolation: which SOCKS endpoint each browser context is handed.

The mechanism is one circuit per SocksPort, not one per SOCKS credential.
Chromium implements no SOCKS5 authentication, and Playwright refuses the
attempt with `Browser does not support socks5 proxy authentication` — so
attaching credentials made every context raise rather than merely share an
exit IP. `test_credentials_are_never_attached` guards that regression.
"""

from __future__ import annotations

import pytest

from personabot.infrastructure.browser.tor import ProxyConfig, TorManager
from personabot.settings import Settings


def settings_for(**overrides) -> Settings:
    return Settings(_env_file=None, gemini_api_keys=("k",), **overrides)


def test_disabled_tor_hands_out_no_proxy():
    manager = TorManager(settings_for(use_tor=False))
    assert manager.circuit_for("ctx") is None


def test_extra_ports_parse_from_a_comma_separated_string():
    """`.env` cannot reasonably be asked for JSON."""
    settings = settings_for(tor_socks_extra_ports="9052, 9053")
    assert settings.tor_socks_extra_ports == (9052, 9053)


def test_proxy_servers_lead_with_the_primary_port_and_dedupe():
    settings = settings_for(tor_socks_port=9050, tor_socks_extra_ports="9053,9052,9053")
    assert settings.tor_proxy_servers == (
        "socks5://127.0.0.1:9050",
        "socks5://127.0.0.1:9053",
        "socks5://127.0.0.1:9052",
    )


def test_each_context_gets_the_next_port_in_the_rotation():
    manager = TorManager(
        settings_for(use_tor=True, tor_socks_port=9050, tor_socks_extra_ports="9052,9053")
    )
    servers = [manager.circuit_for(f"ctx-{n}").server for n in range(3)]
    assert servers == [
        "socks5://127.0.0.1:9050",
        "socks5://127.0.0.1:9052",
        "socks5://127.0.0.1:9053",
    ]
    assert len(set(servers)) == 3, "concurrent contexts must not share a circuit"


def test_rotation_wraps_once_the_pool_is_exhausted():
    manager = TorManager(
        settings_for(use_tor=True, tor_socks_port=9050, tor_socks_extra_ports="9052")
    )
    assert [manager.circuit_for("c").server for _ in range(4)] == [
        "socks5://127.0.0.1:9050",
        "socks5://127.0.0.1:9052",
        "socks5://127.0.0.1:9050",
        "socks5://127.0.0.1:9052",
    ]


def test_a_single_port_still_works():
    manager = TorManager(settings_for(use_tor=True, tor_socks_port=9050))
    proxy = manager.circuit_for("only")
    assert proxy.server == "socks5://127.0.0.1:9050"


@pytest.mark.parametrize("label", ["ctx", "porsa-000", "extract-porsa"])
def test_credentials_are_never_attached(label):
    """Playwright raises on a SOCKS proxy carrying credentials."""
    manager = TorManager(settings_for(use_tor=True, tor_socks_extra_ports="9052"))
    proxy = manager.circuit_for(label)
    assert proxy.username is None
    assert proxy.password is None
    assert proxy.as_playwright() == {"server": proxy.server}


class TestBurnedCircuits:
    """A port that just failed keeps serving the *same* circuit.

    Tor only replaces a SocksPort's circuit once it exceeds
    `MaxCircuitDirtiness`, so rotating to the next port is not enough on its
    own: wrap the rotation and you are back on the route that just failed.
    Measured against the live target, three of six circuits were unusable
    simultaneously, so this is the common case rather than the edge one.
    """

    def test_a_burned_port_is_skipped(self):
        manager = TorManager(
            settings_for(use_tor=True, tor_socks_port=9050, tor_socks_extra_ports="9052")
        )
        first = manager.circuit_for("a")
        manager.burn(first)

        # The rotation would come back to 9050 on the third call; it must not.
        assert first.server not in [manager.circuit_for("b").server for _ in range(4)]

    def test_the_cooldown_expires(self, monkeypatch):
        clock = {"now": 1_000.0}
        monkeypatch.setattr(
            "personabot.infrastructure.browser.tor.time.monotonic",
            lambda: clock["now"],
        )
        manager = TorManager(
            settings_for(
                use_tor=True, tor_socks_port=9050, tor_socks_extra_ports="9052",
                tor_circuit_cooldown_seconds=60,
            )
        )
        manager.burn(ProxyConfig(server="socks5://127.0.0.1:9050"))
        during = [manager.circuit_for("during").server for _ in range(4)]
        assert set(during) == {"socks5://127.0.0.1:9052"}

        clock["now"] += 61
        after = [manager.circuit_for("after").server for _ in range(4)]
        assert "socks5://127.0.0.1:9050" in after, "cooldown should have expired"

    def test_every_port_burned_still_hands_one_out(self, monkeypatch, caplog):
        """Refusing to route would fail work a marginal circuit could carry."""
        clock = {"now": 1_000.0}
        monkeypatch.setattr(
            "personabot.infrastructure.browser.tor.time.monotonic",
            lambda: clock["now"],
        )
        manager = TorManager(
            settings_for(
                use_tor=True, tor_socks_port=9050, tor_socks_extra_ports="9052",
                tor_circuit_cooldown_seconds=60,
            )
        )
        manager.burn(ProxyConfig(server="socks5://127.0.0.1:9050"))
        clock["now"] += 10
        manager.burn(ProxyConfig(server="socks5://127.0.0.1:9052"))

        # 9050 failed longest ago, so it is likeliest to have been replaced.
        assert manager.circuit_for("desperate").server == "socks5://127.0.0.1:9050"

    def test_burning_nothing_is_a_no_op(self):
        """With Tor off `circuit_for` returns None, and the caller still burns."""
        manager = TorManager(settings_for(use_tor=False))
        manager.burn(None)
        assert manager.circuit_for("ctx") is None
