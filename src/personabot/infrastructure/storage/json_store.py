"""Atomic JSON reads and writes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from personabot.errors import StorageError
from personabot.logging_setup import get_logger

log = get_logger(__name__)


def read_json(path: str | Path) -> Any:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StorageError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise StorageError(f"{path} is not valid JSON: {exc}") from exc


def read_json_or_none(path: str | Path) -> Any | None:
    """Read, returning ``None`` for a missing or corrupt file.

    Used by ``status``, which must survey a directory containing damaged files
    without crashing on the first one.
    """
    try:
        return read_json(path)
    except StorageError:
        return None


def write_json(path: str | Path, data: Any, *, indent: int = 2) -> None:
    """Write via a temporary file and rename.

    A direct write leaves a truncated file behind if the process dies partway,
    and phase 4 would later read that as a corrupt answer set. Renaming into
    place is atomic, so a reader sees either the old file or the complete new one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    handle = None
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        )
        json.dump(data, handle, ensure_ascii=False, indent=indent)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except OSError as exc:
        if handle is not None:
            handle.close()
            Path(handle.name).unlink(missing_ok=True)
        raise StorageError(f"Could not write {path}: {exc}") from exc
