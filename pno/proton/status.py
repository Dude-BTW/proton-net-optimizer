"""Read the current Proton VPN connection state.

NetworkManager is the source of truth used by the Proton VPN GUI and CLI, so
polling it is cheap and works no matter who established the connection. If
``nmcli`` is unavailable, fall back to parsing ``protonvpn status``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from pno.errors import VpnError
from pno.proc import run
from pno.proton.models import ServerSnapshot

_CONNECTION_RE = re.compile(r"^ProtonVPN\s+(?P<name>\S+)$", re.IGNORECASE)
_STATUS_SERVER_RE = re.compile(r"^Server:\s*(?P<name>\S+)\s+in\s+(?P<location>.+)$")
_STATUS_LOAD_RE = re.compile(r"^Load:\s*(?P<load>\d+)%")


@dataclass(frozen=True)
class VpnStatus:
    connected: bool
    server_name: str | None = None
    connection_name: str | None = None
    entry_country: str | None = None
    exit_country: str | None = None
    city: str | None = None
    load: int | None = None
    source: str = "none"
    checked_at: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "connected": self.connected,
            "server_name": self.server_name,
            "connection_name": self.connection_name,
            "entry_country": self.entry_country,
            "exit_country": self.exit_country,
            "city": self.city,
            "load": self.load,
            "source": self.source,
            "checked_at": self.checked_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "VpnStatus":
        return cls(
            connected=bool(payload.get("connected")),
            server_name=payload.get("server_name"),
            connection_name=payload.get("connection_name"),
            entry_country=payload.get("entry_country"),
            exit_country=payload.get("exit_country"),
            city=payload.get("city"),
            load=payload.get("load"),
            source=str(payload.get("source") or "state"),
            checked_at=float(payload.get("checked_at") or 0.0),
            error=payload.get("error"),
        )


def _fill_from_snapshot(
    status: VpnStatus, snapshot: ServerSnapshot | None
) -> VpnStatus:
    if not status.server_name or snapshot is None:
        return status
    server = snapshot.lookup(status.server_name)
    if server is None:
        return status
    return VpnStatus(
        connected=status.connected,
        server_name=status.server_name,
        connection_name=status.connection_name,
        entry_country=server.entry_code,
        exit_country=server.exit_code,
        city=server.city or status.city,
        load=server.load,
        source=status.source,
        checked_at=status.checked_at,
        error=status.error,
    )


def _from_nmcli(
    snapshot: ServerSnapshot | None,
    runner: Callable[[list[str]], str],
) -> VpnStatus:
    output = runner(
        ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"]
    )
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        name, kind = line.rsplit(":", 1)
        match = _CONNECTION_RE.match(name.strip())
        if not match:
            continue
        status = VpnStatus(
            connected=True,
            server_name=match.group("name").upper(),
            connection_name=name.strip(),
            source="networkmanager",
            checked_at=time.time(),
        )
        return _fill_from_snapshot(status, snapshot)
    return VpnStatus(connected=False, source="networkmanager", checked_at=time.time())


def _from_protonvpn_cli(
    snapshot: ServerSnapshot | None,
    runner: Callable[[list[str]], str],
) -> VpnStatus:
    output = runner(["protonvpn", "status"])
    if "Status: Connected" in output:
        for raw_line in output.splitlines():
            match = _STATUS_SERVER_RE.match(raw_line.strip())
            if match:
                load_match = _STATUS_LOAD_RE.match(
                    next(
                        (
                            line.strip()
                            for line in output.splitlines()
                            if line.strip().startswith("Load:")
                        ),
                        "",
                    )
                )
                status = VpnStatus(
                    connected=True,
                    server_name=match.group("name").upper(),
                    load=int(load_match.group("load")) if load_match else None,
                    source="protonvpn-cli",
                    checked_at=time.time(),
                )
                return _fill_from_snapshot(status, snapshot)
        return VpnStatus(
            connected=True,
            source="protonvpn-cli",
            checked_at=time.time(),
        )
    if "Status: Disconnected" in output:
        return VpnStatus(
            connected=False, source="protonvpn-cli", checked_at=time.time()
        )
    raise VpnError("unexpected 'protonvpn status' output", detail=output[-500:])


def read_vpn_status(
    snapshot: ServerSnapshot | None = None,
    *,
    timeout: float = 15.0,
    runner: Callable[[list[str]], str] | None = None,
) -> VpnStatus:
    """Return the current connection state, trying NM first."""

    def default_runner(args: list[str]) -> str:
        result = run(args, timeout=timeout, error_factory=VpnError)
        if not result.ok:
            raise VpnError(
                f"{args[0]} exited with {result.returncode}",
                detail=(result.stderr or result.stdout)[-500:],
            )
        return result.stdout

    active_runner = runner or default_runner
    try:
        return _from_nmcli(snapshot, active_runner)
    except VpnError as nm_error:
        try:
            status = _from_protonvpn_cli(snapshot, active_runner)
        except VpnError as cli_error:
            raise VpnError(
                "cannot determine VPN connection state",
                detail=f"nmcli: {nm_error.message}; protonvpn: {cli_error.message}",
            ) from cli_error
        return status
