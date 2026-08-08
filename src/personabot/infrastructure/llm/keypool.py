"""Concurrency-aware API key pool.

``APIKeyManager`` handed out keys round-robin behind a ``threading.Lock``. That
is safe for mutation but wrong for the job: with N concurrent tasks, round-robin
happily hands the *same* key to several in-flight requests, so they trip its
rate limit together. It also had no memory — a key that just returned 429 was
offered again on the very next call.

This pool leases keys instead. A key is checked out for the duration of one
request and returned afterwards, so a key is never used by two requests at once,
and concurrency is naturally bounded by the number of keys. A key that reports
quota exhaustion is parked with a cooldown deadline before rejoining the queue.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from types import TracebackType

from personabot.errors import LLMKeysExhausted
from personabot.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(order=True)
class _Key:
    """One credential and when it next becomes usable."""

    available_at: float = field(compare=True, default=0.0)
    index: int = field(compare=True, default=0)
    value: str = field(compare=False, default="")

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

    @property
    def value(self) -> str:
        return self._key.value

    @property
    def tail(self) -> str:
        return self._key.tail

    def penalize(self, cooldown_seconds: float) -> None:
        """Park this key on release — it reported quota exhaustion."""
        self._cooldown = cooldown_seconds

    async def __aenter__(self) -> "KeyLease":
        return self

    async def __aexit__(self, exc_type, exc, tb: TracebackType | None) -> None:
        await self._pool.release(self._key, cooldown=self._cooldown)


class AsyncKeyPool:
    """Leases keys to concurrent tasks, one holder at a time."""

    def __init__(self, keys: list[str], *, default_cooldown: float = 60.0) -> None:
        if not keys:
            raise LLMKeysExhausted("No API keys configured.")
        self._default_cooldown = default_cooldown
        self._available: asyncio.PriorityQueue[_Key] = asyncio.PriorityQueue()
        self._total = len(keys)
        for index, value in enumerate(keys):
            self._available.put_nowait(_Key(available_at=0.0, index=index, value=value))

    @property
    def size(self) -> int:
        return self._total

    async def acquire(self, *, timeout: float | None = None) -> KeyLease:
        """Check out the key that is available soonest.

        Waits out a cooldown rather than failing, because a rate limit is a
        temporary condition and callers usually prefer to be slowed down over
        losing the work.
        """
        key = await asyncio.wait_for(self._available.get(), timeout=timeout)

        delay = key.available_at - time.monotonic()
        if delay > 0:
            log.warning(
                "All keys are rate-limited; waiting %.0fs for key %s to cool down.",
                delay, key.tail,
            )
            await asyncio.sleep(delay)

        return KeyLease(self, key)

    async def release(self, key: _Key, *, cooldown: float | None = None) -> None:
        if cooldown is not None:
            key.available_at = time.monotonic() + cooldown
            log.warning(
                "Key %s parked for %.0fs after a quota error.", key.tail, cooldown
            )
        else:
            key.available_at = 0.0
        await self._available.put(key)

    @property
    def default_cooldown(self) -> float:
        return self._default_cooldown
