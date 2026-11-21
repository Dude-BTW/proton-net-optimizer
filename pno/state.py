"""Atomic JSON state store with cross-process locking.

``state.json`` holds the small, frequently rewritten snapshot (VPN status,
pause flag, last mirror refresh). Writers take an exclusive ``flock`` on a
sidecar lock file, write a temporary file, ``fsync`` it and ``os.replace`` it
over the target, so a crash or power loss leaves either the old or the new
document, never a torn one.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterator


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write *data* to *path* atomically and durably."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _fsync_dir(path: Path) -> None:
    """Persist a rename so a power loss cannot resurrect the old name."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode=mode,
    )


def read_json(path: Path, default: Any = None) -> Any:
    """Read a JSON document, returning *default* when missing or invalid."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Exclusive advisory lock used to serialise cache and mirror updates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class StateStore:
    """Read/modify the shared state document under a lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_name(f"{path.name}.lock")
        self._run_id = ""

    def read(self) -> dict[str, Any]:
        payload = read_json(self.path, {})
        return payload if isinstance(payload, dict) else {}

    def write(self, payload: dict[str, Any]) -> None:
        with exclusive_lock(self.lock_path):
            atomic_write_json(self.path, payload)

    def update(self, mutator: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        """Apply *mutator* to the current state and persist the result."""
        with exclusive_lock(self.lock_path):
            current = read_json(self.path, {})
            if not isinstance(current, dict):
                current = {}
            result = mutator(current)
            if isinstance(result, dict):
                current = result
            current["updated_at"] = time.time()
            atomic_write_json(self.path, current)
            return current

    def set(self, **values: Any) -> dict[str, Any]:
        return self.update(lambda state: state.update(values))

    def append_event(self, key: str, payload: dict[str, Any], limit: int = 50) -> None:
        def mutator(state: dict[str, Any]) -> None:
            items = state.setdefault(key, [])
            if not isinstance(items, list):
                items = []
                state[key] = items
            items.append(payload)
            del items[:-limit]

        self.update(mutator)
