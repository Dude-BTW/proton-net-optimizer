"""Explicit VPN connect/disconnect actions shared by CLI, TUI and tray.

No automatic switching happens here: every call originates from a user
action. A running Proton VPN GUI can hold the active connection, so when it is
detected the caller must opt in to closing it.

A crash or suspend can also leave Proton's daemon holding a logical connection
that NetworkManager no longer sees. Before connecting, that stale state is
cleared explicitly and the event is logged; the tunnel itself is never started
or stopped without a user action.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Sequence

from pno import events
from pno.errors import PnoError, VpnError
from pno.proc import CommandResult, run
from pno.proton.gui import GuiCloseResult, GuiProcess, close_gui, find_gui_processes
from pno.proton.status import (
    VpnStatus,
    read_proton_vpn_status,
    read_vpn_status,
)

StatusReader = Callable[[], VpnStatus]
Runner = Callable[..., CommandResult]


class GuiActiveError(PnoError):
    """The Proton VPN GUI is running while a VPN connection is active."""

    code = "gui_active"

    def __init__(
        self,
        *,
        action: str,
        server_name: str | None = None,
        pids: Sequence[int] = (),
    ) -> None:
        message = (
            "Proton VPN GUI is running and a VPN connection is active"
            if action == "connect"
            else "cannot disconnect while Proton VPN GUI is holding the connection"
        )
        super().__init__(
            message,
            detail=f"GUI pids: {', '.join(str(pid) for pid in pids) or 'unknown'}",
            hint=(
                "Close the Proton VPN GUI, or allow pno to close it explicitly "
                "(--close-gui / the TUI dialog)."
            ),
        )
        self.action = action
        self.server_name = server_name
        self.pids = tuple(pids)


@dataclass(frozen=True)
class VpnActionResult:
    ok: bool
    action: str
    server: str | None = None
    gui_closed: bool = False
    gui_forced: bool = False
    disconnected_first: bool = False
    stale_cleared: bool = False
    output: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "server": self.server,
            "gui_closed": self.gui_closed,
            "gui_forced": self.gui_forced,
            "disconnected_first": self.disconnected_first,
            "stale_cleared": self.stale_cleared,
            "output": self.output,
        }


def failure_hint(detail: str | None) -> str | None:
    """Explain the phrases Proton's CLI uses when a connection fails."""
    text = (detail or "").lower()
    if not text:
        return None
    if "no working transports" in text or "not reachable" in text:
        return (
            "Proton VPN could not reach its API or the server. Check the "
            "internet connection, disable other VPNs or proxies, or choose "
            "another server."
        )
    if "timeout" in text or "timed out" in text:
        return (
            "The connection attempt timed out. Retry, or choose a server with "
            "a lower load."
        )
    if "invalid access token" in text or "authentication" in text:
        return "Sign in again with 'protonvpn auth', then retry."
    return (
        "Proton VPN reported an error. Check 'protonvpn status' and retry, "
        "or choose another server."
    )


def wait_for_disconnected(
    reader: StatusReader,
    *,
    timeout: float = 10.0,
    interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            if not reader().connected:
                return True
        except PnoError as exc:
            events.emit("debug", "vpn", "vpn.status.poll-failed", {}, error=exc)
        sleep(interval)
    return False


def _run_disconnect(runner: Runner) -> CommandResult:
    return runner(
        ["protonvpn", "disconnect"],
        timeout=60,
        error_factory=VpnError,
        error_detail="protonvpn disconnect",
    )


def _run_connect(runner: Runner, name: str) -> CommandResult:
    return runner(
        ["protonvpn", "connect", name],
        timeout=240,
        error_factory=VpnError,
        error_detail="protonvpn connect",
    )


def _probe(reader: StatusReader) -> VpnStatus | None:
    """Read a status view, treating an unreadable one as unknown."""
    try:
        return reader()
    except PnoError as exc:
        events.emit("debug", "vpn", "vpn.status.poll-failed", {}, error=exc)
        return None


def _close(processes: Sequence[GuiProcess], timeout: float) -> GuiCloseResult:
    result = close_gui(processes, timeout=timeout)
    events.emit(
        "warning" if result.forced else "info",
        "vpn",
        "vpn.gui.closed",
        {
            "pids": [process.pid for process in processes],
            "terminated": list(result.terminated),
            "forced": list(result.forced),
            "still_running": list(result.still_running),
        },
    )
    return result


def _clear_stale_connection(
    *,
    proton_status_reader: StatusReader,
    runner: Runner,
    wait_timeout: float,
    sleep: Callable[[float], None],
) -> bool:
    """Disconnect Proton's stale logical state when NM has no tunnel."""
    proton = _probe(proton_status_reader)
    if proton is None or not proton.connected:
        return False
    events.emit(
        "warning",
        "vpn",
        "vpn.stale-state.detected",
        {"proton_server": proton.server_name, "source": proton.source},
    )
    result = _run_disconnect(runner)
    if result.returncode != 0:
        events.emit(
            "warning",
            "vpn",
            "vpn.stale-state.clear-failed",
            {
                "returncode": result.returncode,
                "detail": (result.stderr or result.stdout).strip()[-200:],
            },
        )
        return False
    if not wait_for_disconnected(
        proton_status_reader, timeout=wait_timeout, sleep=sleep
    ):
        events.emit("warning", "vpn", "vpn.stale-state.clear-timeout", {})
        return False
    events.emit(
        "info",
        "vpn",
        "vpn.stale-state.cleared",
        {"proton_server": proton.server_name},
    )
    return True


def connect_server(
    name: str,
    *,
    close_gui: bool = False,
    processes: Sequence[GuiProcess] | None = None,
    status_reader: StatusReader = read_vpn_status,
    proton_status_reader: StatusReader = read_proton_vpn_status,
    runner: Runner = run,
    close_timeout: float = 10.0,
    wait_timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> VpnActionResult:
    """Connect explicitly, handling a running Proton GUI when allowed."""
    events.emit("info", "vpn", "vpn.connect.requested", {"server": name})
    try:
        return _connect(
            name,
            close_gui=close_gui,
            processes=processes,
            status_reader=status_reader,
            proton_status_reader=proton_status_reader,
            runner=runner,
            close_timeout=close_timeout,
            wait_timeout=wait_timeout,
            sleep=sleep,
        )
    except PnoError as exc:
        events.emit("error", "vpn", "vpn.connect.failed", {"server": name}, error=exc)
        raise


def _connect(
    name: str,
    *,
    close_gui: bool,
    processes: Sequence[GuiProcess] | None,
    status_reader: StatusReader,
    proton_status_reader: StatusReader,
    runner: Runner,
    close_timeout: float,
    wait_timeout: float,
    sleep: Callable[[float], None],
) -> VpnActionResult:
    gui_processes = list(find_gui_processes()) if processes is None else list(processes)
    current = _probe(status_reader)
    gui_closed = False
    gui_forced = False
    disconnected_first = False
    stale_cleared = False

    if gui_processes and current is not None and current.connected:
        if not close_gui:
            events.emit(
                "warning",
                "vpn",
                "vpn.connect.gui-conflict",
                {"server": name, "pids": [process.pid for process in gui_processes]},
            )
            raise GuiActiveError(
                action="connect",
                server_name=name,
                pids=[process.pid for process in gui_processes],
            )
        closing = _close(gui_processes, close_timeout)
        gui_closed = closing.closed
        gui_forced = bool(closing.forced)
        if not closing.closed:
            raise VpnError(
                "cannot close the Proton VPN GUI",
                detail=f"still running: {list(closing.still_running)}",
            )
        disconnected = _run_disconnect(runner)
        if disconnected.returncode == 0:
            disconnected_first = wait_for_disconnected(
                status_reader, timeout=wait_timeout, sleep=sleep
            )
        else:
            events.emit(
                "warning",
                "vpn",
                "vpn.disconnect.before-connect.failed",
                {"returncode": disconnected.returncode},
            )
    elif current is not None and not current.connected:
        stale_cleared = _clear_stale_connection(
            proton_status_reader=proton_status_reader,
            runner=runner,
            wait_timeout=wait_timeout,
            sleep=sleep,
        )

    result = _run_connect(runner, name)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise VpnError(
            "protonvpn connect failed",
            detail=detail,
            hint=failure_hint(detail),
        )
    events.emit(
        "info",
        "vpn",
        "vpn.connect.done",
        {"server": name, "gui_closed": gui_closed, "stale_cleared": stale_cleared},
    )
    return VpnActionResult(
        ok=True,
        action="connect",
        server=name,
        gui_closed=gui_closed,
        gui_forced=gui_forced,
        disconnected_first=disconnected_first,
        stale_cleared=stale_cleared,
        output=result.stdout.strip(),
    )


def disconnect_server(
    *,
    close_gui: bool = False,
    processes: Sequence[GuiProcess] | None = None,
    runner: Runner = run,
    close_timeout: float = 10.0,
) -> VpnActionResult:
    """Disconnect explicitly; offer GUI closing only when asked."""
    events.emit("info", "vpn", "vpn.disconnect.requested")
    try:
        return _disconnect(
            close_gui=close_gui,
            processes=processes,
            runner=runner,
            close_timeout=close_timeout,
        )
    except PnoError as exc:
        events.emit("error", "vpn", "vpn.disconnect.failed", {}, error=exc)
        raise


def _disconnect(
    *,
    close_gui: bool,
    processes: Sequence[GuiProcess] | None,
    runner: Runner,
    close_timeout: float,
) -> VpnActionResult:
    result = _run_disconnect(runner)
    gui_processes = list(find_gui_processes()) if processes is None else list(processes)
    if result.returncode == 0:
        events.emit("info", "vpn", "vpn.disconnect.done", {"gui_closed": False})
        return VpnActionResult(
            ok=True,
            action="disconnect",
            output=result.stdout.strip(),
        )
    if not gui_processes:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise VpnError(
            "protonvpn disconnect failed",
            detail=detail,
            hint=failure_hint(detail),
        )
    if not close_gui:
        raise GuiActiveError(
            action="disconnect",
            pids=[process.pid for process in gui_processes],
        )
    closing = _close(gui_processes, close_timeout)
    if not closing.closed:
        raise VpnError(
            "cannot close the Proton VPN GUI",
            detail=f"still running: {list(closing.still_running)}",
        )
    retry = _run_disconnect(runner)
    if retry.returncode != 0:
        detail = (retry.stderr or retry.stdout).strip()[-500:]
        raise VpnError(
            "protonvpn disconnect failed after closing the GUI",
            detail=detail,
            hint=failure_hint(detail),
        )
    events.emit(
        "info",
        "vpn",
        "vpn.disconnect.done",
        {"gui_closed": True, "forced": bool(closing.forced)},
    )
    return VpnActionResult(
        ok=True,
        action="disconnect",
        gui_closed=True,
        gui_forced=bool(closing.forced),
        output=retry.stdout.strip(),
    )
