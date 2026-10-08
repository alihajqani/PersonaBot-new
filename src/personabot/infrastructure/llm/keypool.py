"""Concurrency-aware, token-budget-aware API key pool.

``APIKeyManager`` handed out keys round-robin behind a ``threading.Lock``. That
is safe for mutation but wrong for the job: with N concurrent tasks, round-robin
happily hands the *same* key to several in-flight requests, so they trip its
rate limit together. It also had no memory — a key that just returned 429 was
offered again on the very next call.

This pool leases keys instead. A key is checked out for the duration of one
request and returned afterwards, so a key is never used by two requests at once,
and concurrency is naturally bounded by the number of keys.

Two things the leasing version still got wrong, both found by instrumenting a
real run against Gemini's free tier:

**Only the first `CONCURRENT_LLM_TASKS` keys were ever used.** Keys were ordered
by ``(available_at, index)`` and every healthy key carried ``available_at = 0``,
so the comparison always fell through to ``index`` and the lowest one won. With
17 keys and a concurrency of 10, keys 11-17 were never handed out at all — 41%
of the configured quota sat idle while the other 10 projects were driven into
their limits. The tiebreaker is now *least recently released*, which is what
round-robin was supposed to mean.

**The limit that actually binds is tokens, not requests.** Gemini's free tier
caps ``generate_content_free_tier_input_token_count`` per project per model per
minute. One 35-item matrix prompt in this project is ~4,250 input tokens against
a 16,000 limit, so a project sustains barely three requests a minute — and
nothing anywhere counted them. A key is now paced by what it actually spent,
tracked in a sliding window, so the pool stops *before* the server does.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from collections import deque
from dataclasses import dataclass, field
from types import TracebackType

from personabot.errors import LLMKeysExhausted
from personabot.logging_setup import get_logger

log = get_logger(__name__)

# The window the provider measures its per-minute quota over.
_WINDOW_SECONDS = 60.0

# Aim below the published ceiling. Request sizes vary and the server's window
# does not line up with ours, so pacing at exactly 100% still trips the limit
# every few minutes — and one 429 cascades, because every waiting task wakes at
# the same moment. The lost throughput is far cheaper than the cascade.
_SAFETY_FRACTION = 0.85

# Waits shorter than this are ordinary pacing, not a problem worth a WARNING.
_QUIET_WAIT_SECONDS = 5.0

# Cooldowns expire together, so tasks wake together and re-create the burst that
# caused them. Spreading the wake-ups breaks the herd.
_JITTER_FRACTION = 0.25
_MAX_JITTER_SECONDS = 5.0


class _TokenLedger:
    """What one key has spent inside the trailing quota window."""

    __slots__ = ("_limit", "_spends")

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit * _SAFETY_FRACTION))
        self._spends: deque[tuple[float, int]] = deque()

    @property
    def limit(self) -> int:
        return self._limit

    def charge(self, tokens: int, *, at: float) -> None:
        if tokens > 0:
            self._spends.append((at, tokens))

    def ready_at(self, need: int, *, now: float) -> float:
        """When ``need`` more tokens would fit under the limit.

        A single request larger than the whole budget can never fit; charging it
        against an empty window is the best available answer, and the server's
        429 — with its own retryDelay — is left to say the rest.
        """
        need = min(max(need, 0), self._limit)

        cutoff = now - _WINDOW_SECONDS
        while self._spends and self._spends[0][0] <= cutoff:
            self._spends.popleft()

        spent = sum(tokens for _, tokens in self._spends)
        if spent + need <= self._limit:
            return now

        # Wait only as long as it takes for enough of the oldest spends to age
        # out of the window — not a flat cooldown.
        remaining = spent
        for at, tokens in self._spends:
            remaining -= tokens
            if remaining + need <= self._limit:
                return at + _WINDOW_SECONDS
        return now + _WINDOW_SECONDS


@dataclass(order=True)
class _Key:
    """One credential, when it next becomes usable, and what it has spent."""

    available_at: float = field(compare=True, default=0.0)
    # Least-recently-released wins among equally-ready keys. Using the key's
    # position in `.env` here is what starved the tail of the pool.
    sequence: int = field(compare=True, default=0)
    index: int = field(compare=False, default=0)
    value: str = field(compare=False, default="")
    ledger: "_TokenLedger | None" = field(compare=False, default=None)

    @property
    def tail(self) -> str:
        """Last four characters, for logs. Never log a whole key."""
        return f"...{self.value[-4:]}" if len(self.value) >= 4 else "..."


class KeyLease:
    """A checked-out key. Use as an async context manager."""

    def __init__(self, pool: "AsyncKeyPool", key: _Key) -> None:
        self._pool = pool
        self._key = key
        self._cooldown: float | None = None
        self._reason = "a quota error"
        self._input_tokens = 0
        self._started = time.monotonic()

    @property
    def value(self) -> str:
        return self._key.value

    @property
    def tail(self) -> str:
        return self._key.tail

    @property
    def started_at(self) -> float:
        return self._started

    def penalize(self, cooldown_seconds: float, *, reason: str = "a quota error") -> None:
        """Park this key on release, for ``reason`` (shown in the log)."""
        self._cooldown = cooldown_seconds
        self._reason = reason

    def record_input_tokens(self, tokens: int) -> None:
        """Charge this request's prompt against the key's minute budget.

        Called on failure as well as success: a request that reached the model
        and came back 500 still spent its input tokens, and not charging it is
        exactly how a transient retry storm turns into a quota storm.
        """
        self._input_tokens = max(self._input_tokens, int(tokens or 0))

    async def __aenter__(self) -> "KeyLease":
        return self

    async def __aexit__(self, exc_type, exc, tb: TracebackType | None) -> None:
        await self._pool.release(
            self._key,
            cooldown=self._cooldown,
            reason=self._reason,
            input_tokens=self._input_tokens,
            spent_at=self._started,
        )


class AsyncKeyPool:
    """Leases keys to concurrent tasks, one holder at a time, paced by budget."""

    def __init__(
        self,
        keys: list[str],
        *,
        default_cooldown: float = 60.0,
        input_tokens_per_minute: int = 0,
    ) -> None:
        if not keys:
            raise LLMKeysExhausted("No API keys configured.")
        self._default_cooldown = default_cooldown
        self._budget = max(0, int(input_tokens_per_minute))
        self._available: asyncio.PriorityQueue[_Key] = asyncio.PriorityQueue()
        self._total = len(keys)
        self._sequence = itertools.count(len(keys))
        # A conservative first guess, replaced by the first real measurement.
        # Zero would leave the opening burst completely unpaced.
        self._expected_tokens = self._budget // 4 if self._budget else 0

        for index, value in enumerate(keys):
            self._available.put_nowait(
                _Key(
                    available_at=0.0,
                    sequence=index,
                    index=index,
                    value=value,
                    ledger=_TokenLedger(self._budget) if self._budget else None,
                )
            )

    @property
    def size(self) -> int:
        return self._total

    @property
    def paced(self) -> bool:
        return self._budget > 0

    @property
    def expected_input_tokens(self) -> int:
        """Best current estimate of one request's prompt size.

        Tracks the *peak* rather than the mean, decaying slowly. Constructs in
        one survey differ by more than 2x, and reserving the average would
        under-book the large ones — which are precisely the requests that
        overrun the limit.
        """
        return self._expected_tokens

    async def acquire(self, *, timeout: float | None = None) -> KeyLease:
        """Check out the key that is available soonest.

        Waits out a cooldown rather than failing, because a rate limit is a
        temporary condition and callers usually prefer to be slowed down over
        losing the work.
        """
        key = await asyncio.wait_for(self._available.get(), timeout=timeout)

        delay = key.available_at - time.monotonic()
        if delay > 0:
            delay += random.uniform(
                0.0, min(_MAX_JITTER_SECONDS, delay * _JITTER_FRACTION)
            )
            if delay <= _QUIET_WAIT_SECONDS:
                log.debug(
                    "Pacing key %s for %.1fs to stay inside its budget.", key.tail, delay
                )
            else:
                log.warning(
                    "Every key is spent for now; waiting %.0fs for key %s "
                    "to regain budget.", delay, key.tail,
                )
            await asyncio.sleep(delay)

        return KeyLease(self, key)

    async def release(
        self,
        key: _Key,
        *,
        cooldown: float | None = None,
        reason: str = "a quota error",
        input_tokens: int = 0,
        spent_at: float | None = None,
    ) -> None:
        now = time.monotonic()

        if input_tokens > 0:
            self._observe(input_tokens)
            if key.ledger is not None:
                # Charged at the moment the request started, which is when the
                # provider counted it — not when we happened to notice.
                key.ledger.charge(
                    input_tokens, at=spent_at if spent_at is not None else now
                )

        if cooldown is not None:
            key.available_at = now + cooldown
            log.warning(
                "Key %s parked for %.0fs after %s.", key.tail, cooldown, reason
            )
        elif key.ledger is not None:
            key.available_at = key.ledger.ready_at(self._expected_tokens, now=now)
        else:
            key.available_at = 0.0

        key.sequence = next(self._sequence)
        await self._available.put(key)

    def _observe(self, input_tokens: int) -> None:
        self._expected_tokens = max(input_tokens, int(self._expected_tokens * 0.9))

    @property
    def default_cooldown(self) -> float:
        return self._default_cooldown
