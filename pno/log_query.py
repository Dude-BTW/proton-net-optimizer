"""Query the machine-readable event log.

``pno logs`` is intentionally similar to ``yt-logs`` from the music project:
it is a small Python API plus a CLI, and it defaults to JSON lines when the
output is not a terminal.
"""

from __future__ import annotations

import datetime
import fnmatch
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

from pno import paths

LEVELS = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}


def resolve_files(inputs: Iterable[str | os.PathLike[str]]) -> list[str]:
    """Expand a log directory, a log file or a glob into a sorted file list."""
    result: list[str] = []
    for raw in inputs:
        text = str(raw)
        path = Path(text)
        if path.is_dir():
            main = path / "events.jsonl"
            if main.is_file():
                result.append(str(main))
            result.extend(sorted(glob.glob(str(path / "archive" / "*.jsonl"))))
        elif path.is_file():
            result.append(str(path))
        else:
            result.extend(sorted(glob.glob(text)))
    seen: set[str] = set()
    unique: list[str] = []
    for item in result:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def default_files() -> list[str]:
    return resolve_files([paths.logs_dir()])


def parse_time(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    if text.startswith("-") and len(text) > 1:
        amount = text[1:-1]
        unit = text[-1]
        try:
            number = float(amount)
        except ValueError:
            return None
        seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
        if seconds is None:
            return None
        return (
            datetime.datetime.now(datetime.timezone.utc).timestamp() - number * seconds
        )
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def iter_events(files: Iterable[str]) -> Iterator[dict[str, Any]]:
    for name in files:
        try:
            handle = open(name, "r", encoding="utf-8")
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


def matches(
    event: dict[str, Any],
    *,
    level: str | None = None,
    event_name: str | None = None,
    component: str | None = None,
    since: float | None = None,
    until: float | None = None,
    grep: str | None = None,
) -> bool:
    if level and LEVELS.get(str(event.get("level")), 0) < LEVELS.get(level, 0):
        return False
    if event_name and not _match_any(str(event.get("event")), event_name.split(",")):
        return False
    if component and not _match_any(str(event.get("component")), component.split(",")):
        return False
    timestamp = parse_time(str(event.get("ts"))) if event.get("ts") else None
    if since is not None and (timestamp is None or timestamp < since):
        return False
    if until is not None and (timestamp is None or timestamp > until):
        return False
    if grep and grep.lower() not in json.dumps(event, ensure_ascii=False).lower():
        return False
    return True


def _match_any(value: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, pattern.strip()) for pattern in patterns)


def summarize(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    by_level: dict[str, int] = {}
    by_component: dict[str, int] = {}
    total = 0
    for event in events:
        total += 1
        level = str(event.get("level") or "unknown")
        component = str(event.get("component") or "unknown")
        by_level[level] = by_level.get(level, 0) + 1
        by_component[component] = by_component.get(component, 0) + 1
    return {"total": total, "by_level": by_level, "by_component": by_component}


def _one_line(value: Any, limit: int = 400) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_text(event: dict[str, Any]) -> str:
    ts = str(event.get("ts") or "")
    level = str(event.get("level") or "").upper()
    component = str(event.get("component") or "")
    name = str(event.get("event") or "")
    data = event.get("data") or {}
    error = event.get("error") or {}
    suffix = ""
    if data:
        compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        suffix = f" {compact}"
    if error:
        suffix += f" error={error.get('code')}:{error.get('message')}"
        detail = _one_line(error.get("detail") or "")
        if detail:
            suffix += f" detail={detail}"
        hint = _one_line(error.get("hint") or "")
        if hint:
            suffix += f" hint={hint}"
    return f"{ts} {level:8} {component}: {name}{suffix}"


def query(
    files: Iterable[str] | None = None,
    *,
    level: str | None = None,
    event_name: str | None = None,
    component: str | None = None,
    since: str | None = None,
    until: str | None = None,
    grep: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    selected = list(files) if files is not None else default_files()
    events = [
        event
        for event in iter_events(selected)
        if matches(
            event,
            level=level,
            event_name=event_name,
            component=component,
            since=parse_time(since),
            until=parse_time(until),
            grep=grep,
        )
    ]
    if limit is not None and limit >= 0:
        events = events[-limit:]
    return events


def cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="pno logs")
    parser.add_argument("paths", nargs="*", help="log files or directories")
    parser.add_argument("--level", choices=sorted(LEVELS))
    parser.add_argument("--event")
    parser.add_argument("--component")
    parser.add_argument("--since", help="ISO timestamp or -30m/-2h/-7d")
    parser.add_argument("--until", help="ISO timestamp or -30m")
    parser.add_argument("--grep")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)

    files = resolve_files(args.paths) if args.paths else default_files()
    events = query(
        files,
        level=args.level,
        event_name=args.event,
        component=args.component,
        since=args.since,
        until=args.until,
        grep=args.grep,
        limit=args.limit,
    )
    if args.summary:
        print(json.dumps(summarize(events), ensure_ascii=False, indent=2))
        return 0
    use_json = args.json or not sys.stdout.isatty()
    for event in events:
        if use_json:
            print(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        else:
            print(format_text(event))
    return 0
