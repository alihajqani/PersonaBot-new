"""LLM error classification and key leasing.

Audit finding A1: the predecessor tested ``"rate" in str(error).lower()``.
"rate" is a substring of "gene-rate", so any error mentioning
``generate_content`` was misclassified as a quota failure, burned every key
rotation, and surfaced as "All N keys exhausted" — hiding the real cause.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from personabot.errors import LLMError, LLMQuotaError, LLMResponseError, LLMTransientError
from personabot.infrastructure.llm.client import backoff_delay, classify_error
from personabot.infrastructure.llm.factory import parse_json_response
from personabot.infrastructure.llm.keypool import AsyncKeyPool, _TokenLedger


class TestErrorClassificationA1:
    def test_generate_content_is_not_a_quota_error(self):
        """The exact regression. 'generate' contains 'rate'."""
        error = classify_error(RuntimeError("Failed to generate_content for model"))
        assert not isinstance(error, LLMQuotaError)

    @pytest.mark.parametrize(
        "message",
        [
            "Error calling generate_content",
            "generate() produced no output",
            "could not generate a response",
            "the accurate model is unavailable for generation",
        ],
    )
    def test_words_containing_rate_are_not_quota_errors(self, message):
        assert not isinstance(classify_error(RuntimeError(message)), LLMQuotaError)

    @pytest.mark.parametrize(
        "message",
        [
            "429 quota exceeded",
            "RESOURCE_EXHAUSTED for this project",
            "rate limit reached",
            "rate-limited, slow down",
            "Too Many Requests",
            "insufficient quota",
        ],
    )
    def test_genuine_quota_messages_are_detected(self, message):
        assert isinstance(classify_error(RuntimeError(message)), LLMQuotaError)

    def test_status_code_beats_message_text(self):
        class ApiError(Exception):
            status_code = 429

        assert isinstance(classify_error(ApiError("generate_content failed")), LLMQuotaError)

    def test_exception_type_is_checked_first(self):
        class ResourceExhausted(Exception):
            pass

        assert isinstance(classify_error(ResourceExhausted("no detail")), LLMQuotaError)

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
    def test_server_errors_are_transient(self, status):
        class ApiError(Exception):
            def __init__(self, code):
                super().__init__(f"HTTP {code}")
                self.status_code = code

        assert isinstance(classify_error(ApiError(status)), LLMTransientError)

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_client_errors_are_not_retried(self, status):
        class ApiError(Exception):
            def __init__(self, code):
                super().__init__(f"HTTP {code}")
                self.status_code = code

        error = classify_error(ApiError(status))
        assert isinstance(error, LLMError)
        assert not isinstance(error, (LLMQuotaError, LLMTransientError))

    def test_timeouts_are_transient(self):
        assert isinstance(classify_error(asyncio.TimeoutError()), LLMTransientError)
        assert isinstance(classify_error(ConnectionError("reset by peer")), LLMTransientError)

    def test_already_classified_errors_pass_through(self):
        original = LLMQuotaError("x")
        assert classify_error(original) is original


class TestBackoff:
    def test_grows_and_stays_capped(self):
        assert all(0 <= backoff_delay(i, cap=30.0) <= 30.0 for i in range(12))

    def test_is_jittered(self):
        """Without jitter, tasks that fail together retry together."""
        samples = {backoff_delay(4) for _ in range(50)}
        assert len(samples) > 1


class TestAsyncKeyPool:
    async def test_leases_are_exclusive(self):
        """Round-robin let N tasks share one key and trip its limit together."""
        pool = AsyncKeyPool(["k1", "k2"])
        first = await pool.acquire()
        second = await pool.acquire()
        assert {first.value, second.value} == {"k1", "k2"}

        third = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0.05)
        assert not third.done(), "a third lease must wait for a key to come back"

        await pool.release(first._key)
        lease = await asyncio.wait_for(third, timeout=1)
        assert lease.value in {"k1", "k2"}

    async def test_context_manager_returns_the_key(self):
        pool = AsyncKeyPool(["only"])
        async with await pool.acquire() as lease:
            assert lease.value == "only"
        async with await pool.acquire() as lease:
            assert lease.value == "only"

    async def test_penalized_key_is_parked(self):
        pool = AsyncKeyPool(["a", "b"], default_cooldown=0.2)
        async with await pool.acquire() as lease:
            penalized = lease.value
            lease.penalize(0.2)

        async with await pool.acquire() as lease:
            assert lease.value != penalized, "a cooling key must not be handed out first"

    async def test_concurrent_tasks_never_share_a_key(self):
        pool = AsyncKeyPool([f"k{i}" for i in range(4)])
        in_flight: set[str] = set()
        conflicts: list[str] = []

        async def worker():
            async with await pool.acquire() as lease:
                if lease.value in in_flight:
                    conflicts.append(lease.value)
                in_flight.add(lease.value)
                await asyncio.sleep(0.01)
                in_flight.discard(lease.value)

        await asyncio.gather(*(worker() for _ in range(40)))
        assert conflicts == []

    async def test_empty_pool_is_rejected(self):
        from personabot.errors import LLMKeysExhausted

        with pytest.raises(LLMKeysExhausted):
            AsyncKeyPool([])

    def test_only_the_key_tail_is_exposed_for_logging(self):
        pool = AsyncKeyPool(["supersecretkey1234"])
        key = pool._available.get_nowait()
        assert key.tail == "...1234"
        assert "supersecret" not in key.tail


class TestJsonExtraction:
    def test_parses_plain_json(self):
        assert parse_json_response('{"a": 1}') == {"a": 1}

    def test_parses_a_fenced_block(self):
        assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}

    def test_parses_a_top_level_array(self):
        """The predecessor sliced between the first '{' and last '}', which
        corrupts a response that is legitimately an array."""
        assert parse_json_response('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]

    def test_recovers_json_surrounded_by_prose(self):
        assert parse_json_response('Sure!\n{"a": 1}\nHope that helps.') == {"a": 1}

    @pytest.mark.parametrize("text", ["", "   ", "not json at all"])
    def test_unparseable_input_raises_with_a_preview(self, text):
        with pytest.raises(LLMResponseError):
            parse_json_response(text)


class TestQuotaDetails:
    """A 429 says which quota tripped and when to come back. Use both."""

    @staticmethod
    def _gemini_429(quota_id: str, retry_delay: str | None = "41s"):
        details = [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": quota_id, "quotaValue": "16000"}],
            }
        ]
        if retry_delay is not None:
            details.append(
                {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                 "retryDelay": retry_delay}
            )

        class ClientError(Exception):
            code = 429

        error = ClientError("429 RESOURCE_EXHAUSTED. quota exceeded")
        error.details = {"error": {"code": 429, "details": details}}
        return error

    def test_reads_retry_delay_and_minute_scope(self):
        error = classify_error(
            self._gemini_429("GenerateContentInputTokensPerModelPerMinute-FreeTier")
        )
        assert isinstance(error, LLMQuotaError)
        assert error.retry_after == pytest.approx(41.0)
        assert error.scope == "minute"

    def test_daily_quota_is_distinguished(self):
        error = classify_error(
            self._gemini_429("GenerateRequestsPerModelPerDay-FreeTier", retry_delay=None)
        )
        assert error.scope == "day"
        assert error.retry_after is None

    def test_falls_back_to_the_prose_delay(self):
        class ClientError(Exception):
            code = 429

        error = classify_error(ClientError("429 quota exceeded. Please retry in 41.15s."))
        assert error.retry_after == pytest.approx(41.15)

    def test_a_quota_error_without_details_still_classifies(self):
        error = classify_error(RuntimeError("resource exhausted"))
        assert isinstance(error, LLMQuotaError)
        assert error.retry_after is None
        assert error.scope == "unknown"


class TestKeyPoolFairness:
    """Every configured key must carry traffic, not just the first N."""

    async def test_every_key_is_used(self):
        """The regression: ordering by (available_at, index) starved the tail.

        With 17 keys and 10 workers, keys 11-17 were never handed out — 41% of
        the configured quota idle while the first 10 projects were driven into
        their limits.
        """
        pool = AsyncKeyPool([f"key-{i:02d}" for i in range(17)])
        used: set[str] = set()

        async def worker():
            for _ in range(12):
                async with await pool.acquire() as lease:
                    used.add(lease.value)
                    await asyncio.sleep(0.001)

        await asyncio.gather(*(worker() for _ in range(10)))
        assert len(used) == 17, f"only {len(used)}/17 keys ever used"


class TestTokenPacing:
    async def test_a_key_is_held_back_once_its_budget_is_spent(self):
        pool = AsyncKeyPool(["only-key"], input_tokens_per_minute=10_000)

        async with await pool.acquire() as lease:
            lease.record_input_tokens(9_000)   # over the 85% safety line

        key = pool._available.get_nowait()
        assert key.available_at > time.monotonic(), "a spent key must be paced"
        assert key.available_at - time.monotonic() <= 61.0

    async def test_a_cheap_request_does_not_pace_the_key(self):
        pool = AsyncKeyPool(["only-key"], input_tokens_per_minute=100_000)

        async with await pool.acquire() as lease:
            lease.record_input_tokens(1_000)

        key = pool._available.get_nowait()
        assert key.available_at <= time.monotonic()

    async def test_pacing_is_off_without_a_configured_budget(self):
        pool = AsyncKeyPool(["a", "b"])
        assert not pool.paced

        async with await pool.acquire() as lease:
            lease.record_input_tokens(999_999)

        key = pool._available.get_nowait()
        assert key.available_at == 0.0

    async def test_the_estimate_tracks_the_largest_request_seen(self):
        pool = AsyncKeyPool(["a"], input_tokens_per_minute=16_000)

        async with await pool.acquire() as lease:
            lease.record_input_tokens(4_249)
        assert pool.expected_input_tokens == 4_249

        async with await pool.acquire() as lease:
            lease.record_input_tokens(1_893)
        assert pool.expected_input_tokens > 1_893, "a small request must not reset it"

    def test_a_ledger_never_reserves_more_than_the_whole_budget(self):
        """A prompt larger than the budget must not wedge a key forever.

        Reserving the observed size verbatim could never be satisfied, so the
        reservation is capped at the budget: the key waits one window and is
        then usable again, instead of being deferred indefinitely.
        """
        ledger = _TokenLedger(1_000)
        ledger.charge(50_000, at=100.0)

        assert ledger.ready_at(50_000, now=100.0) == pytest.approx(160.0)
        assert ledger.ready_at(50_000, now=161.0) == pytest.approx(161.0)

    def test_a_ledger_waits_only_for_the_oldest_spend_to_age_out(self):
        """Partial recovery, not a flat cooldown."""
        ledger = _TokenLedger(10_000)      # 8_500 after the safety margin
        ledger.charge(4_000, at=100.0)
        ledger.charge(4_000, at=130.0)

        # A third 4_000 does not fit until the first one leaves the window.
        assert ledger.ready_at(4_000, now=140.0) == pytest.approx(160.0)


class TestQuotaCooldownChoice:
    """Which 429 costs a key a full minute, and which one costs it seconds."""

    @staticmethod
    def _client():
        from personabot.infrastructure.llm.gemini import GeminiClient

        pool = AsyncKeyPool(["a", "b", "c"], default_cooldown=60.0)
        settings = SimpleNamespace(llm_max_retries=3)
        return GeminiClient(settings, pool)

    def test_the_server_delay_wins(self):
        error = LLMQuotaError("x", retry_after=41.0, scope="minute")
        assert self._client()._cooldown_for(error, 1) == pytest.approx(41.0)

    def test_a_named_per_minute_quota_uses_the_full_cooldown(self):
        error = LLMQuotaError("x", scope="minute")
        assert self._client()._cooldown_for(error, 1) == 60.0

    def test_a_daily_quota_parks_the_key_for_much_longer(self):
        error = LLMQuotaError("x", scope="day")
        assert self._client()._cooldown_for(error, 1) >= 600.0

    def test_detail_free_exhaustion_waits_out_a_full_window(self):
        """A bare RESOURCE_EXHAUSTED names no quota. Wait anyway.

        Measured against the live API: backing off 2-25s instead took 429s from
        ~12/min to ~55/min and produced ten construct failures where the full
        cooldown produced none. A call that cycles the whole pool inside one
        cooldown reports every key exhausted and gives up.
        """
        error = LLMQuotaError("Resource has been exhausted", scope="unknown")
        assert self._client()._cooldown_for(error, 1) == 60.0
