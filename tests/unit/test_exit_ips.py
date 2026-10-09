"""One survey never submits twice from the same Tor exit IP.

Port rotation restarts with every process, so two runs a minute apart left
from the same address. The ledger makes the rule hold across runs; a repeated
IP is treated as a route failure — before anything is posted — and retried on
another circuit.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from personabot.errors import CircuitError
from personabot.infrastructure.browser.tor import ExitIpLedger, ProxyConfig
from personabot.providers import base
from personabot.providers.base import FormSubmitter
from personabot.settings import Settings


class TestLedger:
    def test_an_ip_is_reserved_once_and_survives_a_restart(self, tmp_path):
        path = tmp_path / "exit_ips.jsonl"
        ledger = ExitIpLedger(path)
        assert ledger.reserve("1.1.1.1")
        assert not ledger.reserve("1.1.1.1")      # a concurrent session
        ledger.record("1.1.1.1", label="x-000")

        again = ExitIpLedger(path)
        assert "1.1.1.1" in again and len(again) == 1
        assert json.loads(path.read_text())["label"] == "x-000"

    def test_a_gap_frees_an_ip_after_enough_other_submissions(self, tmp_path):
        path = tmp_path / "exit_ips.jsonl"
        path.write_text('{"ip": "1.1.1.1"}\n{"ip": "2.2.2.2"}\n')
        ledger = ExitIpLedger(path, min_gap=2)
        assert not ledger.reserve("1.1.1.1")      # only one submission since
        assert not ledger.reserve("2.2.2.2")      # it was the last one
        ledger.record("3.3.3.3", label="x")
        assert ledger.reserve("1.1.1.1")          # two since: free again
        assert not ledger.reserve("1.1.1.1")      # but not while in flight

    def test_gap_one_only_forbids_consecutive_use(self, tmp_path):
        ledger = ExitIpLedger(tmp_path / "exit_ips.jsonl", min_gap=1)
        ledger.record("1.1.1.1", label="a")
        assert not ledger.reserve("1.1.1.1")
        ledger.record("2.2.2.2", label="b")
        assert ledger.reserve("1.1.1.1")

    def test_a_damaged_line_is_skipped(self, tmp_path):
        path = tmp_path / "exit_ips.jsonl"
        path.write_text('{"ip": "1.1.1.1"}\nnot json\n')
        assert len(ExitIpLedger(path)) == 1


class _Tor:
    enabled = True

    def __init__(self) -> None:
        self.burned: list[str] = []

    def burn(self, proxy) -> None:
        self.burned.append(proxy.server)


class _Page:
    def set_default_timeout(self, _ms) -> None: ...

    async def goto(self, *_a, **_k) -> None: ...


class _Browser:
    def __init__(self) -> None:
        self.tor = _Tor()
        self.port = 9050

    @asynccontextmanager
    async def circuit(self, *, label: str = "ctx"):
        self.port += 1
        context = SimpleNamespace(new_page=self._page)
        yield SimpleNamespace(context=context, proxy=ProxyConfig(server=f"socks5://h:{self.port}"))

    async def _page(self):
        return _Page()


class _Submitter(FormSubmitter):
    name = "fake"

    async def fill_page(self, page, answers):
        return 1

    async def advance(self, page):
        return False

    async def detect_success(self, page):
        return True


@pytest.fixture
def submitter(tmp_path, monkeypatch):
    def make(exits: list[str], **overrides):
        script = iter(exits)

        async def fake_verify(context, *, label=""):
            return next(script)

        monkeypatch.setattr(base, "verify_circuit", fake_verify)
        settings = Settings(_env_file=None, gemini_api_keys=(), use_tor=True, browser_max_attempts=3,
                            **overrides)
        workspace = SimpleNamespace(
            settings=settings, output_dir=tmp_path, receipts_dir=tmp_path / "r",
            max_pages=5, require_form_url=lambda: "https://form.example/x",
        )
        return _Submitter(workspace, _Browser())
    return make


class TestSubmission:
    @pytest.mark.asyncio
    async def test_a_used_ip_is_rejected_before_the_form_and_another_route_tried(
        self, submitter, tmp_path
    ):
        (tmp_path / "exit_ips.jsonl").write_text('{"ip": "1.1.1.1"}\n')
        s = submitter(["1.1.1.1", "2.2.2.2"])

        await s.submit_one({"q": "a"}, "fake-000")

        assert len(s.browser.tor.burned) == 1
        assert "2.2.2.2" in ExitIpLedger(tmp_path / "exit_ips.jsonl")

    @pytest.mark.asyncio
    async def test_two_submissions_never_share_an_ip(self, submitter, tmp_path):
        s = submitter(["3.3.3.3", "3.3.3.3", "4.4.4.4"])
        await s.submit_one({"q": "a"}, "fake-000")
        await s.submit_one({"q": "b"}, "fake-001")

        lines = [json.loads(l)["ip"] for l in (tmp_path / "exit_ips.jsonl").read_text().splitlines()]
        assert lines == ["3.3.3.3", "4.4.4.4"]

    @pytest.mark.asyncio
    async def test_running_out_of_fresh_ips_fails_without_posting(self, submitter, tmp_path):
        (tmp_path / "exit_ips.jsonl").write_text('{"ip": "5.5.5.5"}\n')
        s = submitter(["5.5.5.5"] * 3)
        with pytest.raises(CircuitError, match="already been used"):
            await s.submit_one({"q": "a"}, "fake-000")

    @pytest.mark.asyncio
    async def test_with_a_gap_an_ip_returns_but_never_back_to_back(self, submitter, tmp_path):
        s = submitter(["6.6.6.6", "6.6.6.6", "7.7.7.7", "6.6.6.6"], exit_ip_min_gap=1)
        for n in range(3):
            await s.submit_one({"q": "a"}, f"fake-00{n}")

        lines = [json.loads(l)["ip"] for l in (tmp_path / "exit_ips.jsonl").read_text().splitlines()]
        assert lines == ["6.6.6.6", "7.7.7.7", "6.6.6.6"]
