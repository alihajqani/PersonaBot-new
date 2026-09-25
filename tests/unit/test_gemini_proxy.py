"""GEMINI_PROXY has to reach the Gemini client, or it is worse than absent.

The setting was declared in `Settings`, documented in `.env`, and read by
nothing: `check-keys` even carried a warning saying so. Where the API is
geo-blocked that turns one configuration line into a dozen
`FAILED_PRECONDITION: User location is not supported` verdicts that look
exactly like bad credentials.
"""

from __future__ import annotations

import pytest

from personabot.errors import ConfigError
from personabot.infrastructure.llm.gemini import build_http_options, require_proxy_support
from personabot.settings import Settings


def _settings(**overrides) -> Settings:
    return Settings(gemini_api_keys=(), _env_file=None, **overrides)


class TestProxyValidation:
    @pytest.mark.parametrize("proxy", ["http://127.0.0.1:8080", "https://proxy.example:3128"])
    def test_a_scheme_httpx_understands_is_accepted(self, proxy):
        require_proxy_support(proxy)   # must not raise

    def test_socks_is_accepted_when_httpx_can_open_it(self):
        pytest.importorskip("socksio")
        require_proxy_support("socks5://127.0.0.1:1080")   # must not raise

    def test_socks_without_socksio_is_refused_by_name(self, monkeypatch):
        """httpx's own error surfaces mid-generation as a transient failure,
        retried against every key; this names the missing package up front."""
        import builtins

        real_import = builtins.__import__

        def no_socksio(name, *args, **kwargs):
            if name == "socksio":
                raise ImportError("no socksio")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_socksio)
        with pytest.raises(ConfigError, match="socksio"):
            require_proxy_support("socks5://127.0.0.1:1080")

    def test_a_bare_host_and_port_is_refused_by_name(self):
        """The most likely typo, and httpx's own error for it is opaque."""
        with pytest.raises(ConfigError, match="no scheme"):
            require_proxy_support("127.0.0.1:8080")

    def test_an_unsupported_scheme_is_refused(self):
        with pytest.raises(ConfigError, match="no scheme"):
            require_proxy_support("ftp://127.0.0.1:21")


class TestHttpOptions:
    def test_no_proxy_leaves_the_client_args_alone(self):
        options = build_http_options(_settings(llm_timeout_seconds=42))

        assert options.timeout == 42_000
        assert not options.client_args
        assert not options.async_client_args

    def test_a_proxy_reaches_both_httpx_clients(self):
        options = build_http_options(_settings(gemini_proxy="http://127.0.0.1:8080"))

        assert options.client_args["proxy"] == "http://127.0.0.1:8080"
        assert options.async_client_args["proxy"] == "http://127.0.0.1:8080"

    def test_the_async_transport_is_pinned_to_httpx(self):
        """google-genai prefers aiohttp for async calls whenever it can.

        aiohttp never sees httpx's `proxy`, so without an explicit transport a
        proxied run would silently go direct the moment aiohttp appeared in the
        environment — and fail with the geo-block the proxy was there to avoid.
        """
        options = build_http_options(_settings(gemini_proxy="http://127.0.0.1:8080"))

        assert options.async_client_args.get("transport") is not None
