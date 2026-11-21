"""Download and cache the official Arch Linux mirror status."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from pno import events, paths
from pno.errors import DataUnavailableError, NetworkError
from pno.mirrors.models import Mirror, MirrorStatusSnapshot
from pno.state import atomic_write_json, exclusive_lock, read_json

STATUS_URL = "https://archlinux.org/mirrors/status/json/"
USER_AGENT = "proton-net-optimizer/0.1 (+https://archlinux.org/mirrors/status/)"
DEFAULT_MAX_AGE = 3600.0


def parse_status_payload(payload: dict[str, Any]) -> MirrorStatusSnapshot:
    urls = payload.get("urls")
    if not isinstance(urls, list):
        raise DataUnavailableError("mirror status payload has no 'urls' list")
    mirrors: list[Mirror] = []
    for item in urls:
        if not isinstance(item, dict):
            continue
        mirror = Mirror(
            url=str(item.get("url") or ""),
            protocol=str(item.get("protocol") or ""),
            country_code=str(item.get("country_code") or "").upper(),
            completion_pct=float(item.get("completion_pct") or 0.0),
            delay=_float_or_none(item.get("delay")),
            duration_avg=_float_or_none(item.get("duration_avg")),
            score=_float_or_none(item.get("score")),
            last_sync=item.get("last_sync"),
            active=bool(item.get("active")),
        )
        if mirror.url and mirror.protocol:
            mirrors.append(mirror)
    if not mirrors:
        raise DataUnavailableError("mirror status payload contains no usable mirrors")
    return MirrorStatusSnapshot(fetched_at=time.time(), mirrors=tuple(mirrors))


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def fetch_status(
    url: str = STATUS_URL,
    *,
    timeout: float = 30.0,
    opener: Callable[..., Any] | None = None,
) -> MirrorStatusSnapshot:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise NetworkError(
            f"mirror status request failed: HTTP {exc.code}", detail=url
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NetworkError(
            "cannot reach archlinux.org mirror status", detail=str(exc)
        ) from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise DataUnavailableError(
            "mirror status is not valid JSON", detail=str(exc)
        ) from exc
    snapshot = parse_status_payload(payload)
    events.emit(
        "info",
        "mirrors",
        "mirrors.status.fetched",
        {"mirrors": len(snapshot.mirrors)},
    )
    return snapshot


def load_status(
    *,
    cache_path: Path | None = None,
    max_age: float = DEFAULT_MAX_AGE,
    force: bool = False,
    fetcher: Callable[[], MirrorStatusSnapshot] | None = None,
    now: float | None = None,
) -> MirrorStatusSnapshot:
    target = cache_path or paths.mirrors_status_cache_file()
    lock_path = target.with_name(f"{target.name}.lock")
    timestamp = now if now is not None else time.time()
    with exclusive_lock(lock_path):
        payload = read_json(target, None)
        cached: MirrorStatusSnapshot | None = None
        if isinstance(payload, dict):
            try:
                cached = MirrorStatusSnapshot.from_dict(payload)
            except (TypeError, ValueError):
                cached = None
        if (
            cached is not None
            and cached.mirrors
            and not force
            and cached.age(timestamp) <= max_age
        ):
            return cached
        try:
            snapshot = (fetcher or fetch_status)()
        except (NetworkError, DataUnavailableError) as exc:
            if cached is not None and cached.mirrors:
                events.emit(
                    "warning",
                    "mirrors",
                    "mirrors.status.stale",
                    {"age": round(cached.age(timestamp))},
                    error=exc,
                )
                return cached
            raise
        if snapshot.fetched_at <= 0:
            snapshot = MirrorStatusSnapshot(timestamp, snapshot.mirrors)
        atomic_write_json(target, snapshot.to_dict())
        return snapshot


def mirror_age_hours(last_sync: str | None, now: float | None = None) -> float | None:
    if not last_sync:
        return None
    try:
        parsed = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now if now is not None else time.time()
    return max(0.0, (current - parsed.timestamp()) / 3600.0)


def eligible_mirrors(
    snapshot: MirrorStatusSnapshot,
    *,
    min_completion: float = 0.95,
    max_delay_hours: float = 24.0,
    max_age_hours: float = 48.0,
    now: float | None = None,
) -> list[Mirror]:
    """Filter mirrors without ranking them."""
    result: list[Mirror] = []
    for mirror in snapshot.mirrors:
        if not mirror.active:
            continue
        if mirror.protocol not in {"http", "https"}:
            continue
        if mirror.completion_pct < min_completion:
            continue
        if mirror.delay is not None and mirror.delay > max_delay_hours * 3600:
            continue
        age = mirror_age_hours(mirror.last_sync, now)
        if age is not None and age > max_age_hours:
            continue
        if mirror.score is None:
            continue
        result.append(mirror)
    return result


def mirrors_for_country(mirrors: Iterable[Mirror], country_code: str) -> list[Mirror]:
    wanted = country_code.upper()
    return [mirror for mirror in mirrors if mirror.country_code == wanted]
