"""LLM error classification and key leasing.

Audit finding A1: the predecessor tested ``"rate" in str(error).lower()``.
"rate" is a substring of "gene-rate", so any error mentioning
``generate_content`` was misclassified as a quota failure, burned every key
rotation, and surfaced as "All N keys exhausted" — hiding the real cause.
"""

from __future__ import annotations

import asyncio

import pytest

from personabot.errors import LLMError, LLMQuotaError, LLMResponseError, LLMTransientError
from personabot.infrastructure.llm.client import backoff_delay, classify_error
from personabot.infrastructure.llm.factory import parse_json_response
from personabot.infrastructure.llm.keypool import AsyncKeyPool


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
