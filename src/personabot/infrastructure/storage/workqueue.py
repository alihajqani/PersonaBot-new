"""Filesystem work queue with atomic claiming.

The filesystem is this project's database, and a file's *location* is its state::

    answers/<id>.json              ready
    answers/inprogress/<id>.json   claimed by a worker
    answers/done/<id>.json         submitted

The claim mechanism is lifted from the porsline submitter, where it was the one
genuinely correct concurrency primitive in the codebase — and the only thing
preventing duplicate submissions when `make loop` ran in several shells. It was
implemented in exactly one of four providers. Here it is shared, and it guards
in-process concurrent tasks as well as separate processes, because
``os.rename`` is atomic on a single filesystem either way.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Iterator

from personabot.errors import ItemAlreadyClaimed, StorageError
from personabot.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ClaimedItem:
    """A claimed unit of work.

    Used as a context manager: leaving the block without calling
    :meth:`complete` releases the claim so a later run can retry, rather than
    stranding the file in ``inprogress/`` forever.
    """

    name: str
    path: Path
    queue: "WorkQueue"
    _settled: bool = False

    def load(self) -> Any:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(f"{self.path} is not valid JSON: {exc}") from exc

    def complete(self) -> None:
        """Mark the work done — moves the file to ``done/``."""
        if self._settled:
            return
        destination = self.queue.done_dir / self.name
        self.queue.done_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.path), str(destination))
        self._settled = True
        log.debug("Completed %s", self.name)

    def release(self) -> None:
        """Return the work to the queue for a later attempt."""
        if self._settled:
            return
        destination = self.queue.root / self.name
        try:
            shutil.move(str(self.path), str(destination))
        except OSError as exc:
            log.error("Could not release claim on %s: %s", self.name, exc)
        self._settled = True

    def __enter__(self) -> "ClaimedItem":
        return self

    def __exit__(self, exc_type, exc, tb: TracebackType | None) -> None:
        # Anything other than an explicit complete() puts the work back.
        self.release()


class WorkQueue:
    """A directory of pending JSON files, plus ``inprogress/`` and ``done/``."""

    def __init__(self, root: Path, *, inprogress_name: str = "inprogress", done_name: str = "done") -> None:
        self.root = Path(root)
        self.inprogress_dir = self.root / inprogress_name
        self.done_dir = self.root / done_name

    def ensure(self) -> None:
        for directory in (self.root, self.inprogress_dir, self.done_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def pending_names(self) -> list[str]:
        """Names of files waiting to be claimed, oldest first."""
        if not self.root.is_dir():
            return []
        entries = [
            entry for entry in self.root.iterdir()
            if entry.is_file() and entry.suffix == ".json"
        ]
        entries.sort(key=lambda p: p.stat().st_mtime)
        return [entry.name for entry in entries]

    def counts(self) -> dict[str, int]:
        def count(directory: Path) -> int:
            if not directory.is_dir():
                return 0
            return sum(1 for e in directory.iterdir() if e.is_file() and e.suffix == ".json")

        return {
            "pending": len(self.pending_names()),
            "inprogress": count(self.inprogress_dir),
            "done": count(self.done_dir),
        }

    def claim(self, name: str) -> ClaimedItem:
        """Atomically take ownership of one file.

        ``os.rename`` is atomic within a filesystem, so exactly one caller wins
        and every other gets :class:`ItemAlreadyClaimed`.
        """
        source = self.root / name
        destination = self.inprogress_dir / name
        self.inprogress_dir.mkdir(parents=True, exist_ok=True)

        try:
            os.rename(source, destination)
        except FileNotFoundError as exc:
            raise ItemAlreadyClaimed(f"{name} was claimed by another worker") from exc
        except OSError as exc:
            raise ItemAlreadyClaimed(f"Could not claim {name}: {exc}") from exc

        return ClaimedItem(name=name, path=destination, queue=self)

    def iter_claims(self, limit: int | None = None) -> Iterator[ClaimedItem]:
        """Yield successfully claimed items, skipping ones others took first."""
        claimed = 0
        for name in self.pending_names():
            if limit is not None and claimed >= limit:
                return
            try:
                item = self.claim(name)
            except ItemAlreadyClaimed:
                continue
            claimed += 1
            yield item

    def recover_stale_claims(self) -> int:
        """Return everything in ``inprogress/`` to the queue.

        A process killed mid-submission leaves its claim behind. Recovery is an
        explicit operation rather than something that happens on startup,
        because another worker may legitimately hold those claims right now.
        """
        if not self.inprogress_dir.is_dir():
            return 0
        recovered = 0
        for entry in self.inprogress_dir.iterdir():
            if entry.is_file() and entry.suffix == ".json":
                shutil.move(str(entry), str(self.root / entry.name))
                recovered += 1
        return recovered
