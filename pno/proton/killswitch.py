"""Inspect and remove leftover Proton kill-switch connections.

Proton VPN creates temporary NetworkManager dummy connections while a tunnel
is active: ``pvpn-killswitch``, ``pvpn-killswitch-ipv6`` and
``pvpn-routed-killswitch``. The IPv6 one is created as leak protection even
when the user-facing kill switch is off. A normal disconnect removes them,
but a suspend/resume cycle or a crash can leave them active with no VPN, which
blocks traffic.

Only the transient profiles are removed here. Connections ending in ``-perm``
belong to Advanced kill switch and are never touched. No Proton setting is
written, and the VPN is never connected or disconnected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Sequence

from pno import events, paths
from pno.errors import KillSwitchError, SettingsError
from pno.proc import CommandResult, run
from pno.proton.settings import load as load_settings
from pno.proton.status import VpnStatus, read_vpn_status
from pno.state import exclusive_lock

Runner = Callable[..., CommandResult]
StatusReader = Callable[[], VpnStatus]

# Temporary profiles created for the duration of a connection. The optional
# ``routed-`` prefix belongs to Proton's OpenVPN backend.
_TRANSIENT_RE = re.compile(r"^pvpn-(?:routed-)?killswitch(?:-ipv6)?$")
# Permanent profiles are the user's Advanced kill switch and must survive.
_PERMANENT_RE = re.compile(r"^pvpn-(?:routed-)?killswitch(?:-ipv6)?-perm$")

ACTIVE_CONNECTIONS_COMMAND = (
    "nmcli",
    "-t",
    "-f",
    "NAME,TYPE,DEVICE,STATE",
    "connection",
    "show",
    "--active",
)


@dataclass(frozen=True)
class ConnectionInfo:
    name: str
    connection_type: str = ""
    device: str = ""
    state: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "type": self.connection_type,
            "device": self.device,
            "state": self.state,
        }


@dataclass(frozen=True)
class KillSwitchReport:
    vpn_connected: bool
    setting: str
    active: tuple[ConnectionInfo, ...] = ()
    transient: tuple[ConnectionInfo, ...] = ()
    permanent: tuple[ConnectionInfo, ...] = ()
    settings_error: str | None = None

    @property
    def stale(self) -> bool:
        """True when transient profiles are leftover from a dead connection."""
        return not self.vpn_connected and self.setting == "off" and bool(self.transient)

    @property
    def reason(self) -> str:
        if self.vpn_connected:
            return "vpn-connected"
        if self.setting == "unknown":
            return "killswitch-unknown"
        if self.setting in {"standard", "advanced"}:
            return "killswitch-enabled"
        if not self.transient:
            return "nothing-to-do"
        return "clean"

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "vpn_connected": self.vpn_connected,
            "setting": self.setting,
            "active": [item.to_dict() for item in self.active],
            "transient": [item.name for item in self.transient],
            "permanent": [item.name for item in self.permanent],
            "stale": self.stale,
            "reason": self.reason,
        }
        if self.settings_error:
            payload["settings_error"] = self.settings_error
        return payload


@dataclass(frozen=True)
class CleanupResult:
    changed: bool
    dry_run: bool
    removed: tuple[str, ...]
    reason: str
    report: KillSwitchReport

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "changed": self.changed,
            "dry_run": self.dry_run,
            "removed": list(self.removed),
            "reason": self.reason,
            "killswitch": self.report.to_dict(),
        }


def _default_runner(args: Sequence[str], *, timeout: float = 10.0) -> CommandResult:
    return run(
        args,
        timeout=timeout,
        error_factory=KillSwitchError,
        error_detail="nmcli kill-switch inspection",
    )


def _split_terse(line: str) -> list[str]:
    """Split an ``nmcli -t`` row on unescaped colons."""
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    fields.append("".join(current))
    return fields


def _active_connections(runner: Runner) -> tuple[ConnectionInfo, ...]:
    result = runner(list(ACTIVE_CONNECTIONS_COMMAND), timeout=10.0)
    if not result.ok:
        raise KillSwitchError(
            "cannot list active NetworkManager connections",
            detail=(result.stderr or result.stdout).strip()[-500:]
            or f"exit code {result.returncode}",
            hint="Check that NetworkManager is running and 'nmcli' works.",
        )
    connections: list[ConnectionInfo] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = _split_terse(line)
        while len(fields) < 4:
            fields.append("")
        name, kind, device, state = fields[:4]
        if not name:
            continue
        connections.append(
            ConnectionInfo(
                name=name,
                connection_type=kind,
                device=device,
                state=state,
            )
        )
    return tuple(connections)


def _read_setting(setting: str | None) -> tuple[str, str | None]:
    if setting is not None:
        return setting, None
    try:
        return load_settings().killswitch, None
    except SettingsError as exc:
        return "unknown", exc.message


def inspect_killswitch(
    status: VpnStatus | None = None,
    *,
    setting: str | None = None,
    runner: Runner | None = None,
) -> KillSwitchReport:
    """Return the active kill-switch profiles and whether they are stale."""
    current = status if status is not None else read_vpn_status()
    active_runner = runner or _default_runner
    current_setting, settings_error = _read_setting(setting)
    all_active = _active_connections(active_runner)
    transient = tuple(item for item in all_active if _TRANSIENT_RE.match(item.name))
    permanent = tuple(item for item in all_active if _PERMANENT_RE.match(item.name))
    active = transient + permanent
    return KillSwitchReport(
        vpn_connected=current.connected,
        setting=current_setting,
        active=active,
        transient=transient,
        permanent=permanent,
        settings_error=settings_error,
    )


def _skip_reason(report: KillSwitchReport, *, force: bool) -> str:
    if report.vpn_connected:
        return "vpn-connected"
    if not report.transient:
        return "nothing-to-do"
    if report.setting == "unknown" and not force:
        return "killswitch-unknown"
    if report.setting in {"standard", "advanced"} and not force:
        return "killswitch-enabled"
    return ""


def cleanup_stale_killswitch(
    *,
    force: bool = False,
    dry_run: bool = False,
    setting: str | None = None,
    runner: Runner | None = None,
    status_reader: StatusReader | None = None,
) -> CleanupResult:
    """Remove temporary kill-switch profiles when no VPN is connected.

    Without ``force`` the cleanup runs only while the user-facing kill switch
    is off. Permanent ``-perm`` profiles are never removed.
    """
    active_runner = runner or _default_runner
    read_status = status_reader or read_vpn_status
    lock_path = paths.state_dir() / "killswitch-cleanup.lock"

    try:
        with exclusive_lock(lock_path):
            return _cleanup_locked(
                force=force,
                dry_run=dry_run,
                setting=setting,
                runner=active_runner,
                read_status=read_status,
            )
    except OSError as exc:
        raise KillSwitchError(
            "cannot lock the kill-switch cleanup",
            detail=str(exc),
            hint="Check the permissions of the pno state directory.",
        ) from exc


def _cleanup_locked(
    *,
    force: bool,
    dry_run: bool,
    setting: str | None,
    runner: Runner,
    read_status: StatusReader,
) -> CleanupResult:
    # Re-read the live state under the lock so a connection started while the
    # caller was waiting is not mistaken for a leftover profile.
    status = read_status()
    report = inspect_killswitch(
        status=status,
        setting=setting,
        runner=runner,
    )
    reason = _skip_reason(report, force=force)
    if reason:
        return CleanupResult(
            changed=False,
            dry_run=dry_run,
            removed=(),
            reason=reason,
            report=report,
        )

    names = tuple(item.name for item in report.transient)
    if dry_run:
        return CleanupResult(
            changed=True,
            dry_run=True,
            removed=(),
            reason="dry-run",
            report=report,
        )

    removed: list[str] = []
    for name in names:
        result = runner(
            ["nmcli", "connection", "delete", name],
            timeout=15.0,
        )
        if not result.ok:
            raise KillSwitchError(
                f"cannot remove stale kill-switch connection: {name}",
                detail=(result.stderr or result.stdout).strip()[-500:]
                or f"exit code {result.returncode}",
                hint=(
                    "The connection may belong to another user. "
                    "Run 'pno vpn killswitch status' to inspect it."
                ),
            )
        removed.append(name)

    remaining_report = inspect_killswitch(
        status=read_status(),
        setting=report.setting,
        runner=runner,
    )
    remaining = sorted(
        set(removed) & {item.name for item in remaining_report.transient}
    )
    if remaining:
        raise KillSwitchError(
            "the stale kill-switch connections are still active",
            detail=", ".join(remaining),
            hint="Try disconnecting them once in the NetworkManager applet.",
        )

    events.emit(
        "info",
        "vpn",
        "vpn.killswitch.cleaned",
        {
            "removed": removed,
            "force": force,
            "setting": report.setting,
        },
    )
    return CleanupResult(
        changed=bool(removed),
        dry_run=False,
        removed=tuple(removed),
        reason="cleaned" if removed else "nothing-to-do",
        report=remaining_report,
    )
