"""Explicit VPN connect/disconnect actions shared by CLI, TUI and tray.

No automatic switching happens here: every call originates from a user
action. A running Proton VPN GUI can hold the active connection, so when it is
detected the caller must opt in to closing it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Sequence

from pno import events
from pno.errors import PnoError, VpnError
from pno.proc import CommandResult, run
from pno.proton.gui import GuiCloseResult, GuiProcess, close_gui, find_gui_processes
from pno.proton.status import VpnStatus, read_vpn_status

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
    output: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "server": self.server,
            "gui_closed": self.gui_closed,
            "gui_forced": self.gui_forced,
            "disconnected_first": self.disconnected_first,
            "output": self.output,
        }


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


def connect_server(
    name: str,
    *,
    close_gui: bool = False,
    processes: Sequence[GuiProcess] | None = None,
    status_reader: StatusReader = read_vpn_status,
    runner: Runner = run,
    close_timeout: float = 10.0,
    wait_timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> VpnActionResult:
    """Connect explicitly, handling a running Proton GUI when allowed."""
    events.emit("info", "vpn", "vpn.connect.requested", {"server": name})
    gui_processes = list(find_gui_processes()) if processes is None else list(processes)
    gui_closed = False
    gui_forced = False
    disconnected_first = False

    if gui_processes and status_reader().connected:
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

    result = _run_connect(runner, name)
    if result.returncode != 0:
        raise VpnError(
            "protonvpn connect failed",
            detail=(result.stderr or result.stdout).strip()[-500:],
        )
    events.emit(
        "info",
        "vpn",
        "vpn.connect.done",
        {"server": name, "gui_closed": gui_closed},
    )
    return VpnActionResult(
        ok=True,
        action="connect",
        server=name,
        gui_closed=gui_closed,
        gui_forced=gui_forced,
        disconnected_first=disconnected_first,
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
        raise VpnError(
            "protonvpn disconnect failed",
            detail=(result.stderr or result.stdout).strip()[-500:],
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
        raise VpnError(
            "protonvpn disconnect failed after closing the GUI",
            detail=(retry.stderr or retry.stdout).strip()[-500:],
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
