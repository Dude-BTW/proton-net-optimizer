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

The file is rotated by size into ``archive/``. A partially written last line
(when the machine loses power) is ignored by :func:`read_events`.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 20 * 1024 * 1024

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


def _archive_path(path: Path) -> Path:
    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    return archive_dir / f"events-{stamp}-{os.getpid()}.jsonl"


def _rotate_if_needed(path: Path) -> None:
    global _file
    if _file is None:
        return
    try:
        if _file.tell() < MAX_FILE_BYTES:
            return
        _file.close()
    except OSError:
        pass
    try:
        os.replace(path, _archive_path(path))
    except OSError:
        pass
    _open_file(path)


def _open_file(path: Path) -> None:
    global _file
    path.parent.mkdir(parents=True, exist_ok=True)
    _file = open(path, "a", encoding="utf-8", buffering=1)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def configure(
    logs_dir: str | os.PathLike[str],
    *,
    level: str = "info",
    console: bool = False,
    run_id: str | None = None,
    filename: str = "events.jsonl",
) -> None:
    """Open the event stream for this process."""
    global _configured, _file, LOGS_DIR, EVENTS_FILE, LEVEL, CONSOLE_ENABLED, RUN_ID
    with _lock:
        if _file is not None:
            try:
                _file.close()
            except OSError:
                pass
            _file = None
        LOGS_DIR = str(logs_dir)
        EVENTS_FILE = str(Path(logs_dir) / filename)
        LEVEL = level if level in LEVELS else "info"
        CONSOLE_ENABLED = console
        RUN_ID = run_id or new_run_id()
        _open_file(Path(EVENTS_FILE))
        _configured = True


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
        try:
            assert _file is not None
            _rotate_if_needed(Path(EVENTS_FILE))
            assert _file is not None
            _file.write(line + "\n")
        except OSError as exc:
            if not _write_error_reported:
                _write_error_reported = True
                print(f"pno: cannot write event log: {exc}", file=sys.stderr)
        if CONSOLE_ENABLED:
            _console_line(payload)
        return payload


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
