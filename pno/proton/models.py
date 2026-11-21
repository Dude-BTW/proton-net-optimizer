"""Server and VPN data models."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from pno.countries import normalize_code

FEATURE_SECURE_CORE = 1 << 0
FEATURE_TOR = 1 << 1
FEATURE_P2P = 1 << 2
FEATURE_STREAMING = 1 << 3
FEATURE_IPV6 = 1 << 4


@dataclass(frozen=True)
class Server:
    name: str
    entry: str
    exit: str
    city: str
    load: int
    score: float
    tier: int
    features: int
    status: int

    @property
    def enabled(self) -> bool:
        return self.status == 1

    @property
    def secure_core(self) -> bool:
        return bool(self.features & FEATURE_SECURE_CORE)

    @property
    def tor(self) -> bool:
        return bool(self.features & FEATURE_TOR)

    @property
    def p2p(self) -> bool:
        return bool(self.features & FEATURE_P2P)

    @property
    def exit_code(self) -> str:
        return normalize_code(self.exit)

    @property
    def entry_code(self) -> str:
        return normalize_code(self.entry)

    @property
    def sort_key(self) -> tuple[int, float, str]:
        return (self.load, self.score, self.name)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "Server":
        return cls(
            name=str(payload.get("name") or payload.get("Name") or ""),
            entry=str(payload.get("entry") or payload.get("EntryCountry") or ""),
            exit=str(payload.get("exit") or payload.get("ExitCountry") or ""),
            city=str(payload.get("city") or payload.get("City") or ""),
            load=int(payload.get("load") or payload.get("Load") or 0),
            score=float(payload.get("score") or payload.get("Score") or 0.0),
            tier=int(payload.get("tier") or payload.get("Tier") or 0),
            features=int(payload.get("features") or payload.get("Features") or 0),
            status=int(payload.get("status") or payload.get("Status") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entry": self.entry,
            "exit": self.exit,
            "city": self.city,
            "load": self.load,
            "score": self.score,
            "tier": self.tier,
            "features": self.features,
            "status": self.status,
        }


@dataclass(frozen=True)
class ServerSnapshot:
    fetched_at: float
    expires_at: float | None
    source: str
    max_tier: int
    servers: tuple[Server, ...]
    stale: bool = False

    def age(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.fetched_at)

    def lookup(self, name: str) -> Server | None:
        wanted = name.strip().upper()
        for server in self.servers:
            if server.name.upper() == wanted:
                return server
        return None

    def by_name(self) -> dict[str, Server]:
        return {server.name.upper(): server for server in self.servers}

    def with_stale(self, stale: bool) -> "ServerSnapshot":
        if stale == self.stale:
            return self
        return ServerSnapshot(
            fetched_at=self.fetched_at,
            expires_at=self.expires_at,
            source=self.source,
            max_tier=self.max_tier,
            servers=self.servers,
            stale=stale,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "fetched_at": self.fetched_at,
            "expires_at": self.expires_at,
            "source": self.source,
            "max_tier": self.max_tier,
            "stale": self.stale,
            "servers": [server.to_dict() for server in self.servers],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ServerSnapshot":
        servers = tuple(
            Server.from_mapping(item) for item in payload.get("servers", [])
        )
        return cls(
            fetched_at=float(payload.get("fetched_at") or 0.0),
            expires_at=(
                float(payload["expires_at"])
                if payload.get("expires_at") is not None
                else None
            ),
            source=str(payload.get("source") or "unknown"),
            max_tier=int(payload.get("max_tier") or 0),
            servers=servers,
            stale=bool(payload.get("stale") or False),
        )


@dataclass(frozen=True)
class CountryServers:
    code: str
    servers: tuple[Server, ...]

    @property
    def best(self) -> Server:
        return self.servers[0]


@dataclass(frozen=True)
class SecureCoreCountry:
    exit_code: str
    best: Server
    routes: tuple[Server, ...]

    @property
    def entry_codes(self) -> tuple[str, ...]:
        seen: list[str] = []
        for route in self.routes:
            code = route.entry_code
            if code not in seen:
                seen.append(code)
        return tuple(seen)


def eligible_servers(
    servers: Iterable[Server],
    *,
    max_tier: int | None = None,
    include_tor: bool = False,
    secure_core: bool = False,
) -> list[Server]:
    """Filter by account tier and features; never reorder."""
    result: list[Server] = []
    for server in servers:
        if not server.enabled:
            continue
        if max_tier is not None and server.tier and server.tier > max_tier:
            continue
        if server.secure_core != secure_core:
            continue
        if server.tor and not include_tor:
            continue
        result.append(server)
    return result
