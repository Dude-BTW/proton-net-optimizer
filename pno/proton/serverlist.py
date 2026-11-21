"""Fetch and cache Proton VPN server loads.

Primary path: run :mod:`pno.proton.fetch_helper` with the system Python, which
uses the Proton libraries already installed by ``proton-vpn-gtk-app`` and the
same session/cache as the GUI.

Fallback path: parse ``~/.cache/Proton/VPN/serverlist.json`` directly, so the
TUI keeps working when the helper or the keyring is unavailable (the snapshot
is flagged ``stale``).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from pno import events, paths
from pno.errors import DataUnavailableError, HelperError, PnoError
from pno.proc import run
from pno.proton.models import Server, ServerSnapshot
from pno.state import atomic_write_json, exclusive_lock, read_json

HELPER_PATH = Path(__file__).with_name("fetch_helper.py")
DEFAULT_PROTON_CACHE = Path.home() / ".cache" / "Proton" / "VPN" / "serverlist.json"
DEFAULT_MAX_AGE = 120.0


def _system_python() -> str:
    override = os.environ.get("PNO_SYSTEM_PYTHON")
    if override:
        return override
    candidate = Path("/usr/bin/python3")
    if candidate.exists():
        return str(candidate)
    return sys.executable


def snapshot_from_payload(payload: dict[str, Any], *, source: str) -> ServerSnapshot:
    servers = tuple(Server.from_mapping(item) for item in payload.get("servers", []))
    if not servers:
        raise HelperError("server list payload contains no servers")
    return ServerSnapshot(
        fetched_at=float(payload.get("fetched_at") or time.time()),
        expires_at=(
            float(payload["expires_at"])
            if payload.get("expires_at") is not None
            else None
        ),
        source=source,
        max_tier=int(payload.get("max_tier") or 0),
        servers=servers,
        stale=bool(payload.get("stale") or False),
    )


def snapshot_from_proton_cache(
    cache_path: Path | None = None,
    *,
    now: float | None = None,
) -> ServerSnapshot | None:
    target = cache_path or DEFAULT_PROTON_CACHE
    payload = read_json(target, None)
    if not isinstance(payload, dict):
        return None
    logicals = payload.get("LogicalServers")
    if not isinstance(logicals, list) or not logicals:
        return None
    timestamp = now if now is not None else time.time()
    expires_at = payload.get("LoadsExpirationTime")
    try:
        expires = float(expires_at) if expires_at is not None else None
    except (TypeError, ValueError):
        expires = None
    try:
        mtime = target.stat().st_mtime
    except OSError:
        mtime = timestamp
    servers: list[Server] = []
    for item in logicals:
        if not isinstance(item, dict):
            continue
        servers.append(Server.from_mapping(item))
    if not servers:
        return None
    return ServerSnapshot(
        fetched_at=min(timestamp, mtime),
        expires_at=expires,
        source="proton-cache",
        max_tier=int(payload.get("MaxTier") or 0),
        servers=tuple(servers),
        stale=bool(expires is None or timestamp > expires),
    )


class ServerCache:
    """Read-through cache with a shared-file refresh lock."""

    def __init__(
        self,
        cache_path: Path | None = None,
        *,
        proton_cache: Path | None = None,
        helper_path: Path | None = None,
        helper_timeout: float = 40.0,
        helper_runner: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.cache_path = cache_path or paths.servers_cache_file()
        self.lock_path = self.cache_path.with_name(f"{self.cache_path.name}.lock")
        self.proton_cache = proton_cache or DEFAULT_PROTON_CACHE
        self.helper_path = helper_path or HELPER_PATH
        self.helper_timeout = helper_timeout
        self._helper_runner = helper_runner

    def read(self) -> ServerSnapshot | None:
        payload = read_json(self.cache_path, None)
        if not isinstance(payload, dict):
            return None
        try:
            snapshot = ServerSnapshot.from_dict(payload)
        except (TypeError, ValueError):
            return None
        return snapshot if snapshot.servers else None

    def store(self, snapshot: ServerSnapshot) -> None:
        atomic_write_json(self.cache_path, snapshot.to_dict())

    def _run_helper(self) -> dict[str, Any]:
        if self._helper_runner is not None:
            return self._helper_runner()
        python = _system_python()
        result = run(
            [python, str(self.helper_path)],
            timeout=self.helper_timeout,
            error_factory=HelperError,
            error_detail="proton fetch helper",
        )
        stderr = result.stderr.strip().splitlines()
        if stderr:
            events.emit(
                "debug",
                "proton",
                "serverlist.helper.stderr",
                {"lines": len(stderr), "last": stderr[-1][:300]},
            )
        if not result.stdout.strip():
            raise HelperError(
                "proton fetch helper returned no output",
                detail=result.stderr[-500:],
            )
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise HelperError("cannot parse helper output", detail=str(exc)) from exc
        if not isinstance(payload, dict):
            raise HelperError("unexpected helper payload", detail=repr(payload)[:500])
        if not payload.get("ok", True):
            error = payload.get("error") or {}
            raise HelperError(
                "proton fetch helper failed",
                detail=f"{error.get('type')}: {error.get('message')}",
            )
        return payload

    def fetch(self) -> ServerSnapshot:
        payload = self._run_helper()
        snapshot = snapshot_from_payload(payload, source="helper")
        return snapshot

    def get(
        self,
        *,
        force: bool = False,
        max_age: float = DEFAULT_MAX_AGE,
        allow_helper: bool = True,
        now: float | None = None,
    ) -> ServerSnapshot:
        timestamp = now if now is not None else time.time()
        with exclusive_lock(self.lock_path):
            stored = self.read()
            if (
                stored is not None
                and not force
                and not stored.stale
                and stored.age(timestamp) <= max_age
            ):
                return stored

            helper_error: PnoError | None = None
            if allow_helper:
                try:
                    snapshot = self.fetch()
                except PnoError as exc:
                    helper_error = exc
                    events.emit(
                        "warning",
                        "proton",
                        "serverlist.helper.failed",
                        {"code": exc.code},
                        error=exc,
                    )
                else:
                    self.store(snapshot)
                    return snapshot

            fallback = snapshot_from_proton_cache(self.proton_cache, now=timestamp)
            if fallback is not None and (
                stored is None or fallback.fetched_at >= stored.fetched_at
            ):
                self.store(fallback)
                return fallback
            if stored is not None:
                return stored.with_stale(True)
            raise DataUnavailableError(
                "no Proton VPN server data available",
                detail=str(helper_error) if helper_error else "proton cache is empty",
                hint=(
                    "Make sure proton-vpn-gtk-app is installed and signed in; "
                    "run 'pno doctor' for details."
                ),
                cause=helper_error,
            )
