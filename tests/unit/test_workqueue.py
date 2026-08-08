"""The filesystem work queue.

Lifted from the porsline submitter, where the atomic ``os.rename`` claim was the
one genuinely correct concurrency primitive in the codebase — implemented in
exactly one of four providers, and the only thing preventing duplicate
submissions when `make loop` ran in several shells.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from personabot.errors import ItemAlreadyClaimed, StorageError
from personabot.infrastructure.storage.json_store import (
    read_json,
    read_json_or_none,
    write_json,
)
from personabot.infrastructure.storage.workqueue import WorkQueue


@pytest.fixture
def queue(tmp_path: Path) -> WorkQueue:
    q = WorkQueue(tmp_path / "answers")
    q.ensure()
    for i in range(5):
        write_json(q.root / f"item{i}.json", {"n": i})
    return q


class TestClaiming:
    def test_claim_moves_the_file_to_inprogress(self, queue):
        item = queue.claim("item0.json")
        assert item.path.parent == queue.inprogress_dir
        assert not (queue.root / "item0.json").exists()

    def test_second_claim_of_the_same_item_fails(self, queue):
        queue.claim("item0.json")
        with pytest.raises(ItemAlreadyClaimed):
            queue.claim("item0.json")

    def test_complete_moves_to_done(self, queue):
        item = queue.claim("item0.json")
        item.complete()
        assert (queue.done_dir / "item0.json").exists()
        assert not item.path.exists()

    def test_release_returns_the_work(self, queue):
        item = queue.claim("item0.json")
        item.release()
        assert (queue.root / "item0.json").exists()

    def test_context_manager_releases_on_failure(self, queue):
        """A crash must not strand work in inprogress/ forever."""
        with pytest.raises(RuntimeError):
            with queue.claim("item0.json"):
                raise RuntimeError("submission blew up")
        assert (queue.root / "item0.json").exists()
        assert not any(queue.inprogress_dir.iterdir())

    def test_context_manager_keeps_completed_work_done(self, queue):
        with queue.claim("item0.json") as item:
            item.complete()
        assert (queue.done_dir / "item0.json").exists()
        assert not (queue.root / "item0.json").exists()

    def test_iter_claims_yields_each_item_once(self, queue):
        names = [item.name for item in queue.iter_claims()]
        assert sorted(names) == [f"item{i}.json" for i in range(5)]

    def test_iter_claims_respects_a_limit(self, queue):
        assert len(list(queue.iter_claims(limit=2))) == 2

    def test_counts_reflect_state(self, queue):
        item = queue.claim("item0.json")
        assert queue.counts() == {"pending": 4, "inprogress": 1, "done": 0}
        item.complete()
        assert queue.counts() == {"pending": 4, "inprogress": 0, "done": 1}


class TestConcurrentClaiming:
    async def test_no_item_is_claimed_twice(self, queue):
        """The property that prevents duplicate live submissions."""
        claimed: list[str] = []

        async def worker():
            for item in queue.iter_claims():
                claimed.append(item.name)
                await asyncio.sleep(0)
                item.complete()

        await asyncio.gather(*(worker() for _ in range(8)))
        assert sorted(claimed) == [f"item{i}.json" for i in range(5)]
        assert len(claimed) == len(set(claimed))


class TestRecovery:
    def test_recovers_claims_left_by_a_killed_process(self, queue):
        queue.claim("item0.json")
        queue.claim("item1.json")
        assert queue.recover_stale_claims() == 2
        assert queue.counts()["pending"] == 5

    def test_recovery_is_safe_when_nothing_is_stale(self, queue):
        assert queue.recover_stale_claims() == 0


class TestJsonStore:
    def test_round_trips_unicode_without_escaping(self, tmp_path):
        path = tmp_path / "a.json"
        write_json(path, {"fa": "همیشه"})
        assert "همیشه" in path.read_text(encoding="utf-8")
        assert read_json(path) == {"fa": "همیشه"}

    def test_write_is_atomic(self, tmp_path):
        """A reader sees the old file or the whole new one, never a truncation."""
        path = tmp_path / "a.json"
        write_json(path, {"v": 1})
        write_json(path, {"v": 2})
        assert read_json(path) == {"v": 2}
        assert not list(tmp_path.glob(".*tmp"))

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(StorageError, match="not found"):
            read_json(tmp_path / "nope.json")

    def test_corrupt_file_raises_with_context(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text('{"truncated": ', encoding="utf-8")
        with pytest.raises(StorageError, match="not valid JSON"):
            read_json(path)

    def test_or_none_variant_survives_corruption(self, tmp_path):
        """`status` must survey a directory of damaged files without crashing."""
        path = tmp_path / "bad.json"
        path.write_text("{", encoding="utf-8")
        assert read_json_or_none(path) is None
        assert read_json_or_none(tmp_path / "absent.json") is None
