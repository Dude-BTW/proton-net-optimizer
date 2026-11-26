"""User-configured startup auto-connect.

This is the only place where pno connects without an immediate click: the
daemon calls :func:`run_once` once per boot when the user enabled it. The same
helper backs the explicit ``pno autoconnect once`` command and the TUI/tray
"Connect now" buttons.

Safety rules (see AGENTS.md):

* only while disconnected and only to the one configured target;
* a regular country resolves to its least-loaded eligible regular server, a
  Secure Core target must match the exact route;
* the Proton GUI is never closed to make room and a running GUI skips the
  attempt;
* an unreadable VPN status or missing server data is a failure/skip, never a
  blind connection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Sequence

from pno import events, favorites
from pno.config import Config
from pno.errors import PnoError
from pno.proton.connection import VpnActionResult, connect_server
from pno.proton.gui import GuiProcess, find_gui_processes
from pno.proton.models import Server, ServerSnapshot, eligible_servers
from pno.proton.status import VpnStatus, read_vpn_status

StatusReader = Callable[[], VpnStatus]
Connector = Callable[..., VpnActionResult]

_COUNTRY = re.compile(r"^[A-Z]{2}$")
_SECURE_CORE = re.compile(r"^[A-Z]{2}-[A-Z]{2}#\d+$")

SKIP_DISABLED = "disabled"
SKIP_NO_TARGET = "no-target"
SKIP_INVALID = "invalid-target"
SKIP_ALREADY_CONNECTED = "already-connected"
SKIP_GUI_RUNNING = "gui-running"
SKIP_NO_SNAPSHOT = "no-snapshot"
SKIP_UNAVAILABLE = "target-unavailable"
FAILED_STATUS = "status-unavailable"


@dataclass(frozen=True)
class AutoconnectResult:
    status: str
    reason: str
    target: str | None = None
    server: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "connected"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "target": self.target,
            "server": self.server,
            "error": self.error,
        }


def normalize_target(value: str) -> str:
    return value.strip().upper()


def target_kind(value: str) -> str | None:
    """Return ``country``, ``secure_core`` or ``None`` for an invalid value."""
    text = normalize_target(value)
    if _COUNTRY.match(text):
        return "country"
    if _SECURE_CORE.match(text):
        return "secure_core"
    return None


def validate_target(snapshot: ServerSnapshot, value: str) -> str | None:
    """Return a human-readable reason or ``None`` when the target is usable."""
    target = normalize_target(value)
    if not target:
        return None
    kind = target_kind(target)
    if kind is None:
        return (
            "expected a two-letter country code (UA) or a Secure Core route "
            "(CH-US#3)"
        )
    parsed = favorites.parse_text(target, snapshot)
    if parsed.errors:
        return parsed.errors[0].message
    if parsed.favorites is None:
        return f"unknown country or Secure Core route: {target}"
    if len(parsed.favorites.countries) + len(parsed.favorites.secure_core_servers) != 1:
        return "exactly one country or Secure Core route is required"
    return None


def select_server(
    snapshot: ServerSnapshot,
    target: str,
    *,
    include_tor: bool = False,
    max_tier: int | None = None,
) -> Server | None:
    """Resolve a target against the live snapshot; no static ranking."""
    kind = target_kind(target)
    wanted = normalize_target(target)
    if kind == "country":
        pool = [
            server
            for server in eligible_servers(
                snapshot.servers,
                max_tier=max_tier,
                include_tor=include_tor,
            )
            if (server.exit_code or "").upper() == wanted
        ]
    elif kind == "secure_core":
        pool = [
            server
            for server in eligible_servers(
                snapshot.servers,
                max_tier=max_tier,
                include_tor=include_tor,
                secure_core=True,
            )
            if server.name.upper() == wanted
        ]
    else:
        return None
    if not pool:
        return None
    return min(pool, key=lambda server: server.sort_key)


def _skipped(code: str, *, trigger: str, target: str | None) -> AutoconnectResult:
    events.emit(
        "info",
        "vpn",
        "autoconnect.skipped",
        {"trigger": trigger, "code": code, "target": target},
    )
    return AutoconnectResult(status="skipped", reason=code, target=target)


def run_once(
    config: Config,
    *,
    reason: str = "startup",
    snapshot: ServerSnapshot | None = None,
    snapshot_loader: Callable[[], ServerSnapshot] | None = None,
    status_reader: StatusReader = read_vpn_status,
    processes: Sequence[GuiProcess] | None = None,
    connect: Connector = connect_server,
) -> AutoconnectResult:
    """Make at most one auto-connect attempt.

    ``reason`` is ``startup`` (respects ``autoconnect_enabled``) or ``manual``
    (an explicit user action that only needs a target). Callers without an
    in-memory snapshot may pass ``snapshot_loader``; they must not connect
    blind when no data can be loaded.
    """
    target = normalize_target(config.autoconnect_target) or None
    if reason != "manual" and not config.autoconnect_enabled:
        return _skipped(SKIP_DISABLED, trigger=reason, target=target)
    if target is None:
        return _skipped(SKIP_NO_TARGET, trigger=reason, target=None)
    if target_kind(target) is None:
        return _skipped(SKIP_INVALID, trigger=reason, target=target)

    try:
        status = status_reader()
    except PnoError as exc:
        events.emit(
            "warning",
            "vpn",
            "autoconnect.failed",
            {"trigger": reason, "target": target, "code": FAILED_STATUS},
            error=exc,
        )
        return AutoconnectResult(
            status="failed",
            reason=FAILED_STATUS,
            target=target,
            error=exc.message,
        )
    if status.connected:
        return _skipped(SKIP_ALREADY_CONNECTED, trigger=reason, target=target)

    gui_processes = list(find_gui_processes()) if processes is None else list(processes)
    if gui_processes:
        return _skipped(SKIP_GUI_RUNNING, trigger=reason, target=target)

    if snapshot is None and snapshot_loader is not None:
        try:
            snapshot = snapshot_loader()
        except PnoError as exc:
            events.emit(
                "warning",
                "vpn",
                "autoconnect.failed",
                {"trigger": reason, "target": target, "code": SKIP_NO_SNAPSHOT},
                error=exc,
            )
            return AutoconnectResult(
                status="failed",
                reason=SKIP_NO_SNAPSHOT,
                target=target,
                error=exc.message,
            )
    if snapshot is None:
        return _skipped(SKIP_NO_SNAPSHOT, trigger=reason, target=target)
    if snapshot.stale:
        events.emit(
            "warning",
            "vpn",
            "autoconnect.stale-data",
            {
                "trigger": reason,
                "target": target,
                "age": round(snapshot.age(), 1),
            },
        )
    server = select_server(
        snapshot,
        target,
        include_tor=config.include_tor,
        max_tier=snapshot.max_tier or None,
    )
    if server is None:
        return _skipped(SKIP_UNAVAILABLE, trigger=reason, target=target)

    events.emit(
        "info",
        "vpn",
        "autoconnect.attempt",
        {
            "trigger": reason,
            "target": target,
            "kind": target_kind(target),
            "server": server.name,
            "load": server.load,
        },
    )
    try:
        connect(server.name, processes=gui_processes, status_reader=status_reader)
    except PnoError as exc:
        events.emit(
            "error",
            "vpn",
            "autoconnect.failed",
            {"trigger": reason, "target": target, "server": server.name},
            error=exc,
        )
        return AutoconnectResult(
            status="failed",
            reason="connect-failed",
            target=target,
            server=server.name,
            error=exc.message,
        )
    events.emit(
        "info",
        "vpn",
        "autoconnect.done",
        {"trigger": reason, "target": target, "server": server.name},
    )
    return AutoconnectResult(
        status="connected",
        reason="connected",
        target=target,
        server=server.name,
    )
