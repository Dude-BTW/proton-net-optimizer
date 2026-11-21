"""Shared test fixtures for Proton and Arch payloads."""

from __future__ import annotations

import time
from typing import Any


def server(
    name: str,
    entry: str,
    exit: str,
    load: int,
    *,
    score: float = 1.0,
    features: int = 0,
    tier: int = 2,
    status: int = 1,
    city: str = "City",
) -> dict[str, Any]:
    return {
        "name": name,
        "entry": entry,
        "exit": exit,
        "city": city,
        "load": load,
        "score": score,
        "tier": tier,
        "features": features,
        "status": status,
    }


def helper_payload(servers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema": 1,
        "ok": True,
        "fetched_at": time.time(),
        "expires_at": time.time() + 60,
        "max_tier": 2,
        "servers": servers or [],
    }


def proton_cache_payload(servers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = []
    for item in servers or []:
        rows.append(
            {
                "Name": item["name"],
                "EntryCountry": item["entry"],
                "ExitCountry": item["exit"],
                "City": item["city"],
                "Load": item["load"],
                "Score": item["score"],
                "Tier": item["tier"],
                "Features": item["features"],
                "Status": item["status"],
            }
        )
    return {
        "LogicalServers": rows,
        "ExpirationTime": time.time() + 600,
        "LoadsExpirationTime": time.time() + 60,
        "MaxTier": 2,
    }


def mirror_status_payload() -> dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "urls": [
            {
                "url": "https://ua.example/archlinux/",
                "protocol": "https",
                "country_code": "UA",
                "completion_pct": 1.0,
                "delay": 60,
                "duration_avg": 0.2,
                "score": 0.5,
                "last_sync": now,
                "active": True,
            },
            {
                "url": "http://ua2.example/archlinux/",
                "protocol": "http",
                "country_code": "UA",
                "completion_pct": 0.99,
                "delay": 300,
                "duration_avg": 0.3,
                "score": 1.0,
                "last_sync": now,
                "active": True,
            },
            {
                "url": "https://de.example/archlinux/",
                "protocol": "https",
                "country_code": "DE",
                "completion_pct": 1.0,
                "delay": 120,
                "duration_avg": 0.4,
                "score": 1.2,
                "last_sync": now,
                "active": True,
            },
            {
                "url": "rsync://rsync.example/archlinux/",
                "protocol": "rsync",
                "country_code": "UA",
                "completion_pct": 1.0,
                "delay": 60,
                "duration_avg": 0.1,
                "score": 0.1,
                "last_sync": now,
                "active": True,
            },
            {
                "url": "https://old.example/archlinux/",
                "protocol": "https",
                "country_code": "UA",
                "completion_pct": 1.0,
                "delay": 60,
                "duration_avg": 0.2,
                "score": 0.4,
                "last_sync": "2020-01-01T00:00:00Z",
                "active": True,
            },
            {
                "url": "https://incomplete.example/archlinux/",
                "protocol": "https",
                "country_code": "UA",
                "completion_pct": 0.2,
                "delay": 60,
                "duration_avg": 0.2,
                "score": 0.3,
                "last_sync": now,
                "active": True,
            },
            {
                "url": "https://inactive.example/archlinux/",
                "protocol": "https",
                "country_code": "UA",
                "completion_pct": 1.0,
                "delay": 60,
                "duration_avg": 0.2,
                "score": 0.2,
                "last_sync": now,
                "active": False,
            },
        ]
    }
