"""Mirror data models."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Mirror:
    url: str
    protocol: str
    country_code: str
    completion_pct: float
    delay: float | None
    duration_avg: float | None
    score: float | None
    last_sync: str | None
    active: bool

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def base_url(self) -> str:
        return self.url if self.url.endswith("/") else self.url + "/"

    @property
    def probe_url(self) -> str:
        return self.base_url + "core/os/x86_64/core.db"

    @property
    def server_line(self) -> str:
        return f"Server = {self.base_url}$repo/os/$arch"

    @property
    def rank_key(self) -> tuple[float, str]:
        return (self.score if self.score is not None else 10_000.0, self.url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "host": self.host,
            "base_url": self.base_url,
            "server_line": self.server_line,
            "protocol": self.protocol,
            "country_code": self.country_code,
            "completion_pct": self.completion_pct,
            "delay": self.delay,
            "duration_avg": self.duration_avg,
            "score": self.score,
            "last_sync": self.last_sync,
            "active": self.active,
        }

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "Mirror":
        return cls(
            url=str(payload.get("url") or ""),
            protocol=str(payload.get("protocol") or ""),
            country_code=str(payload.get("country_code") or "").upper(),
            completion_pct=float(payload.get("completion_pct") or 0.0),
            delay=_optional_float(payload.get("delay")),
            duration_avg=_optional_float(payload.get("duration_avg")),
            score=_optional_float(payload.get("score")),
            last_sync=payload.get("last_sync"),
            active=bool(payload.get("active")),
        )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MirrorStatusSnapshot:
    fetched_at: float
    mirrors: tuple[Mirror, ...]

    def age(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.fetched_at)

    def countries(self) -> dict[str, tuple[Mirror, ...]]:
        grouped: dict[str, list[Mirror]] = {}
        for mirror in self.mirrors:
            if mirror.country_code:
                grouped.setdefault(mirror.country_code, []).append(mirror)
        return {code: tuple(items) for code, items in grouped.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "fetched_at": self.fetched_at,
            "mirrors": [mirror.to_dict() for mirror in self.mirrors],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MirrorStatusSnapshot":
        return cls(
            fetched_at=float(payload.get("fetched_at") or 0.0),
            mirrors=tuple(
                Mirror.from_mapping(item) for item in payload.get("mirrors", [])
            ),
        )


@dataclass(frozen=True)
class ProbeResult:
    mirror: Mirror
    ok: bool
    latency_ms: float | None
    status_code: int | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.mirror.url,
            "host": self.mirror.host,
            "protocol": self.mirror.protocol,
            "country_code": self.mirror.country_code,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 1) if self.latency_ms else None,
            "status_code": self.status_code,
            "error": self.error,
        }
