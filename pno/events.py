"""Append-only, machine-readable event log.

One JSON object per line, optimised for programs (and AI agents), not for
humans::

    {
      "schema": 1,
      "ts": "2026-11-21T18:00:00.123Z",
      "run_id": "20261121T180000Z-a1b2c3",
      "seq": 42,
      "pid": 1234,
      "level": "info",
      "component": "mirrors",
      "event": "mirrors.apply.done",
      "data": {"servers": 5, "country": "UA"},
      "duration_ms": 1530,
      "error": null
    }

The active file rotates by size into ``archive/``. Writes, rotation and
pruning share an ``fcntl.flock`` so several pno processes can log and clean up
concurrently without deleting live data. A partially written last line (power
loss) is ignored by :func:`read_events`.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = DEFAULT_MAX_FILE_BYTES  # kept for tests/backwards compatibility
DEFAULT_KEEP_FILES = 10
DEFAULT_MAX_AGE_DAYS = 30

LEVELS = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}

_SECRET_RE = re.compile(
    r"(?i)(cookie|passwd|password|secret|token|api[_-]?key|authorization|private[_-]?key)"
)

_lock = threading.RLock()
_file: Any = None
_configured = False
_write_error_reported = False

LOGS_DIR = ""
EVENTS_FILE = ""
LEVEL = "info"
CONSOLE_ENABLED = False
RUN_ID = ""
_seq = 0

_max_file_bytes = MAX_FILE_BYTES
_keep_files = DEFAULT_KEEP_FILES
_max_age_days = DEFAULT_MAX_AGE_DAYS


@dataclass(frozen=True)
class PruneResult:
    removed: tuple[str, ...] = ()
    removed_bytes: int = 0
    remaining_files: int = 0
    remaining_bytes: int = 0
    active_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "removed": list(self.removed),
            "removed_bytes": self.removed_bytes,
            "remaining_files": self.remaining_files,
            "remaining_bytes": self.remaining_bytes,
            "active_bytes": self.active_bytes,
        }


@dataclass(frozen=True)
class LogStats:
    active_bytes: int = 0
    archive_files: int = 0
    archive_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.active_bytes + self.archive_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_bytes": self.active_bytes,
            "archive_files": self.archive_files,
            "archive_bytes": self.archive_bytes,
            "total_bytes": self.total_bytes,
        }


def _utc_now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_run_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{os.urandom(3).hex()}"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_RE.search(str(key)) and not isinstance(item, bool):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    return value


@contextlib.contextmanager
def _cross_process_lock(logs_dir: Path) -> Iterator[None]:
    """Serialise writes/rotation/pruning between pno processes."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    lock_path = logs_dir / "events.lock"
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
    try:
        yield
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _archive_path(path: Path) -> Path:
    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    return archive_dir / f"events-{stamp}-{os.getpid()}-{os.urandom(2).hex()}.jsonl"


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _open_file(path: Path) -> None:
    global _file
    path.parent.mkdir(parents=True, exist_ok=True)
    _file = open(path, "a", encoding="utf-8", buffering=1)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_current_file(path: Path) -> None:
    """Reopen when another process rotated or removed the active file."""
    global _file
    if _file is None:
        _open_file(path)
        return
    try:
        path_stat = path.stat()
        file_stat = os.fstat(_file.fileno())
    except OSError:
        _open_file(path)
        return
    if (path_stat.st_dev, path_stat.st_ino) != (
        file_stat.st_dev,
        file_stat.st_ino,
    ):
        try:
            _file.close()
        except OSError:
            pass
        _open_file(path)


def _archive_files(logs_dir: Path) -> list[Path]:
    archive = logs_dir / "archive"
    if not archive.is_dir():
        return []
    return sorted(
        archive.glob("*.jsonl"), key=lambda item: (item.stat().st_mtime, item.name)
    )


def _prune_locked(
    logs_dir: Path,
    *,
    keep_files: int | None = None,
    max_age_days: int | None = None,
    now: float | None = None,
) -> PruneResult:
    keep = _keep_files if keep_files is None else max(0, keep_files)
    age_days = _max_age_days if max_age_days is None else max(0, max_age_days)
    timestamp = now if now is not None else time.time()
    files = _archive_files(logs_dir)
    removed: list[Path] = []
    removed_bytes = 0

    if age_days > 0:
        cutoff = timestamp - age_days * 86400
        for path in files:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                removed.append(path)
                removed_bytes += _file_size(path)

    remaining = [path for path in files if path not in removed]
    if keep >= 0 and len(remaining) > keep:
        for path in remaining[: len(remaining) - keep]:
            removed.append(path)
            removed_bytes += _file_size(path)
        remaining = remaining[len(remaining) - keep :] if keep else []

    for path in removed:
        try:
            path.unlink()
        except OSError:
            pass

    remaining_bytes = sum(_file_size(path) for path in remaining)
    return PruneResult(
        removed=tuple(str(path) for path in removed),
        removed_bytes=removed_bytes,
        remaining_files=len(remaining),
        remaining_bytes=remaining_bytes,
        active_bytes=_file_size(logs_dir / "events.jsonl"),
    )


def _rotate_if_needed(path: Path) -> None:
    global _file
    if _file is None:
        return
    try:
        if _file.tell() < _max_file_bytes:
            return
        _file.close()
    except OSError:
        pass
    _file = None
    try:
        os.replace(path, _archive_path(path))
    except OSError:
        pass
    _open_file(path)
    _prune_locked(path.parent)


def configure(
    logs_dir: str | os.PathLike[str],
    *,
    level: str = "info",
    console: bool = False,
    run_id: str | None = None,
    filename: str = "events.jsonl",
    max_bytes: int | None = None,
    keep_files: int | None = None,
    max_age_days: int | None = None,
) -> None:
    """Open the event stream for this process and apply retention limits."""
    global _configured, _file, LOGS_DIR, EVENTS_FILE, LEVEL, CONSOLE_ENABLED, RUN_ID
    global _max_file_bytes, _keep_files, _max_age_days
    with _lock:
        target = Path(logs_dir)
        with _cross_process_lock(target):
            if _file is not None:
                try:
                    _file.close()
                except OSError:
                    pass
                _file = None
            LOGS_DIR = str(target)
            EVENTS_FILE = str(target / filename)
            LEVEL = level if level in LEVELS else "info"
            CONSOLE_ENABLED = console
            RUN_ID = run_id or new_run_id()
            _max_file_bytes = (
                MAX_FILE_BYTES if max_bytes is None else max(1, int(max_bytes))
            )
            _keep_files = (
                DEFAULT_KEEP_FILES if keep_files is None else max(0, int(keep_files))
            )
            _max_age_days = (
                DEFAULT_MAX_AGE_DAYS
                if max_age_days is None
                else max(0, int(max_age_days))
            )
            _open_file(Path(EVENTS_FILE))
            _prune_locked(target)
            _configured = True


def configure_from_config(
    config: Any,
    *,
    console: bool = False,
    run_id: str | None = None,
) -> None:
    """Configure logging from a :class:`pno.config.Config` instance."""
    from pno import paths

    configure(
        paths.logs_dir(),
        level=getattr(config, "log_level", "info"),
        console=console,
        run_id=run_id,
        max_bytes=getattr(config, "log_max_bytes", DEFAULT_MAX_FILE_BYTES),
        keep_files=getattr(config, "log_keep_files", DEFAULT_KEEP_FILES),
        max_age_days=getattr(config, "log_max_age_days", DEFAULT_MAX_AGE_DAYS),
    )


def ensure_configured() -> None:
    if _configured:
        return
    from pno import paths

    configure(paths.logs_dir())


def is_enabled(level: str) -> bool:
    ensure_configured()
    return LEVELS.get(level, 20) >= LEVELS.get(LEVEL, 20)


def _console_line(payload: dict[str, Any]) -> None:
    error = payload.get("error") or {}
    suffix = ""
    if error:
        suffix = f" error={error.get('code')}:{error.get('message')}"
    print(
        f"{payload['ts']} {payload['level'].upper():8} "
        f"{payload['component']}: {payload['event']}{suffix}",
        file=sys.stderr,
    )


def _error_payload(
    error: BaseException | dict[str, Any] | None,
) -> dict[str, Any] | None:
    if error is None:
        return None
    if isinstance(error, dict):
        return error
    from pno.errors import PnoError

    if isinstance(error, PnoError):
        payload = error.to_dict()
    else:
        payload = {
            "type": type(error).__name__,
            "code": "unexpected",
            "message": str(error),
        }
    payload["traceback"] = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    return payload


def emit(
    level: str,
    component: str,
    event: str,
    data: dict[str, Any] | None = None,
    *,
    duration_ms: int | None = None,
    error: BaseException | dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append one event. Returns the emitted payload (or ``None`` if filtered)."""
    global _seq, _write_error_reported
    ensure_configured()
    if not is_enabled(level):
        return None
    with _lock:
        _seq += 1
        payload: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "ts": _utc_now(),
            "run_id": RUN_ID,
            "seq": _seq,
            "pid": os.getpid(),
            "level": level,
            "component": component,
            "event": event,
            "data": _redact(data or {}),
            "duration_ms": duration_ms,
            "error": _error_payload(error),
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        path = Path(EVENTS_FILE)
        try:
            with _cross_process_lock(path.parent):
                _ensure_current_file(path)
                _rotate_if_needed(path)
                assert _file is not None
                _file.write(line + "\n")
        except OSError as exc:
            if not _write_error_reported:
                _write_error_reported = True
                print(f"pno: cannot write event log: {exc}", file=sys.stderr)
        if CONSOLE_ENABLED:
            _console_line(payload)
        return payload


def _resolve_logs_dir(logs_dir: str | os.PathLike[str] | None) -> Path:
    if logs_dir is not None:
        return Path(logs_dir)
    if LOGS_DIR:
        return Path(LOGS_DIR)
    from pno import paths

    return paths.logs_dir()


def prune(
    logs_dir: str | os.PathLike[str] | None = None,
    *,
    keep_files: int | None = None,
    max_age_days: int | None = None,
    now: float | None = None,
) -> PruneResult:
    """Apply retention limits to rotated logs; the active file is kept."""
    target = _resolve_logs_dir(logs_dir)
    with _lock:
        with _cross_process_lock(target):
            return _prune_locked(
                target,
                keep_files=keep_files,
                max_age_days=max_age_days,
                now=now,
            )


def log_stats(logs_dir: str | os.PathLike[str] | None = None) -> LogStats:
    target = _resolve_logs_dir(logs_dir)
    with _lock:
        with _cross_process_lock(target):
            files = _archive_files(target)
            return LogStats(
                active_bytes=_file_size(target / "events.jsonl"),
                archive_files=len(files),
                archive_bytes=sum(_file_size(path) for path in files),
            )


def read_events(paths: Iterable[str | os.PathLike[str]]) -> Iterator[dict[str, Any]]:
    """Yield valid events from the given files, skipping torn/invalid lines."""
    from pno.log_query import resolve_files  # local import avoids a cycle

    for path in resolve_files(paths):
        try:
            handle = open(path, "r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict) and "event" in payload:
                    yield payload


def reset_for_tests() -> None:
    """Close and forget the current log file (used by the test suite)."""
    global _file, _configured, _seq, _write_error_reported
    global _max_file_bytes, _keep_files, _max_age_days
    with _lock:
        if _file is not None:
            try:
                _file.close()
            except OSError:
                pass
        _file = None
        _configured = False
        _seq = 0
        _write_error_reported = False
        _max_file_bytes = MAX_FILE_BYTES
        _keep_files = DEFAULT_KEEP_FILES
        _max_age_days = DEFAULT_MAX_AGE_DAYS
