"""Explicit VPN connect/disconnect actions shared by CLI, TUI and tray.

No automatic switching happens here: every call originates from a user
action. A running Proton VPN GUI can hold the active connection, so when it is
detected the caller must opt in to closing it.

A crash or suspend can also leave Proton's daemon holding a logical connection
that NetworkManager no longer sees. Before connecting, that stale state is
cleared explicitly and the event is logged; the tunnel itself is never started
or stopped without a user action.

Proton's CLI sometimes exits non-zero after the requested state is already
reached (for example when a post-disconnect API call fails). The result is
therefore reconciled with NetworkManager before it is reported as a failure.

Every ``protonvpn`` invocation is logged as ``vpn.command`` with both output
streams, so the machine log alone explains what the user saw.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

from pno import events
from pno.errors import PnoError, VpnBusyError, VpnError
from pno.proc import CommandResult, read_boot_id, run
from pno.proton.gui import GuiCloseResult, GuiProcess, close_gui, find_gui_processes
from pno.proton.status import (
    VpnStatus,
    read_proton_vpn_status,
    read_vpn_status,
)

StatusReader = Callable[[], VpnStatus]
Runner = Callable[..., CommandResult]

# Captured Proton output is kept in full for the machine log up to this size;
# only the UI-facing summary is shortened (see PnoError.describe).
OUTPUT_LIMIT = 4000

_EVENTLET_MARKER = "EventletDeprecationWarning"
_EVENTLET_END = "eventlet.patcher"
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*([=:])\s*\S+"
)


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
    recovered: bool = False
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
            "recovered": self.recovered,
            "output": self.output,
        }


def redact_output(text: str) -> str:
    """Mask credential-looking values before anything reaches the log."""
    return _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)


def clean_output(text: str) -> str:
    """Drop the Eventlet deprecation block sentry prints on every import."""
    lines: list[str] = []
    skipping = False
    for line in text.splitlines():
        if _EVENTLET_MARKER in line:
            skipping = True
            continue
        if skipping:
            if _EVENTLET_END in line:
                skipping = False
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _bounded(text: str, limit: int = OUTPUT_LIMIT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def command_detail(result: CommandResult) -> str:
    """Cleaned stdout and stderr together, ready for an error message."""
    parts = [
        cleaned
        for cleaned in (
            clean_output(result.stdout or ""),
            clean_output(result.stderr or ""),
        )
        if cleaned
    ]
    detail = redact_output("\n".join(parts))
    return detail[-OUTPUT_LIMIT:] if len(detail) > OUTPUT_LIMIT else detail


def failure_hint(detail: str | None) -> str | None:
    """Explain the phrases Proton's CLI uses when an action fails."""
    text = (detail or "").lower()
    if not text:
        return None
    if "single active connection" in text:
        return (
            "Proton VPN still has another connection attempt running. Wait for "
            "it to finish, then retry; do not press Connect repeatedly."
        )
    if (
        "no working transports" in text
        or "not reachable" in text
        or "network connectivity issues" in text
    ):
        return (
            "Proton VPN could not reach its API or the server. Check the "
            "internet connection, disable other VPNs or proxies, or choose "
            "another server."
        )
    if "timed out" in text and "waiting for tasks" in text:
        return (
            "Proton VPN finished the action but its background tasks timed out. "
            "Check the current state with 'pno status'."
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


@contextlib.contextmanager
def vpn_action_lock() -> Iterator[None]:
    """Serialise connect/disconnect actions across pno processes.

    Proton's ``protun`` backend supports a single active connection attempt, so
    two simultaneous CLI calls fail with a confusing error even though one of
    them succeeds. A non-blocking ``flock`` turns that into a clear message.
    If the lock file itself is unavailable, the action proceeds and the problem
    is logged instead of blocking the user.
    """
    from pno import paths

    path = paths.runtime_dir() / "vpn-action.lock"
    descriptor: int | None = None
    locked = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        events.emit("warning", "vpn", "vpn.action.lock-unavailable", {}, error=exc)
    if descriptor is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            os.close(descriptor)
            raise VpnBusyError(
                "another VPN action is already running",
                hint="Wait for the current connect or disconnect attempt to finish.",
            ) from exc
        except OSError as exc:
            events.emit("warning", "vpn", "vpn.action.lock-failed", {}, error=exc)
    try:
        yield
    finally:
        if descriptor is not None:
            if locked:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)


def _mark_manual_disconnect() -> None:
    """Remember that the user disconnected during this boot.

    The daemon reads this marker so a startup auto-connect or a daemon restart
    never undoes an explicit Disconnect in the same boot.
    """
    from pno import paths
    from pno.state import StateStore

    StateStore(paths.state_file()).update(
        lambda current: current.update(
            {
                "manual_disconnect_boot_id": read_boot_id(),
                "manual_disconnect_at": time.time(),
            }
        )
    )


def wait_for_connected(
    name: str,
    reader: StatusReader,
    *,
    timeout: float = 3.0,
    interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    target = name.strip().upper()
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            status = reader()
        except PnoError as exc:
            events.emit("debug", "vpn", "vpn.status.poll-failed", {}, error=exc)
        else:
            if status.connected and (status.server_name or "").upper() == target:
                return True
        sleep(interval)
    return False


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


def _log_command(args: Sequence[str], result: CommandResult) -> None:
    stdout, stdout_cut = _bounded(redact_output(result.stdout or ""))
    stderr, stderr_cut = _bounded(redact_output(result.stderr or ""))
    events.emit(
        "warning" if result.returncode != 0 else "info",
        "vpn",
        "vpn.command",
        {
            "args": list(args),
            "returncode": result.returncode,
            "stdout": stdout,
            "stdout_truncated": stdout_cut,
            "stderr": stderr,
            "stderr_truncated": stderr_cut,
        },
        duration_ms=result.duration_ms,
    )


def _run_proton(args: Sequence[str], runner: Runner, *, timeout: int) -> CommandResult:
    result = runner(
        list(args),
        timeout=timeout,
        error_factory=VpnError,
        error_detail=" ".join(args),
    )
    _log_command(args, result)
    return result


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
    result = _run_proton(["protonvpn", "disconnect"], runner, timeout=60)
    if result.returncode != 0:
        events.emit(
            "warning",
            "vpn",
            "vpn.stale-state.clear-failed",
            {"returncode": result.returncode, "output": command_detail(result)},
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
    recover_timeout: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> VpnActionResult:
    """Connect explicitly, handling a running Proton GUI when allowed."""
    events.emit("info", "vpn", "vpn.connect.requested", {"server": name})
    try:
        with vpn_action_lock():
            return _connect(
                name,
                close_gui=close_gui,
                processes=processes,
                status_reader=status_reader,
                proton_status_reader=proton_status_reader,
                runner=runner,
                close_timeout=close_timeout,
                wait_timeout=wait_timeout,
                recover_timeout=recover_timeout,
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
    recover_timeout: float,
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
        disconnected = _run_proton(["protonvpn", "disconnect"], runner, timeout=60)
        if disconnected.returncode == 0:
            disconnected_first = wait_for_disconnected(
                status_reader, timeout=wait_timeout, sleep=sleep
            )
        else:
            events.emit(
                "warning",
                "vpn",
                "vpn.disconnect.before-connect.failed",
                {
                    "returncode": disconnected.returncode,
                    "output": command_detail(disconnected),
                },
            )
    elif current is not None and not current.connected:
        stale_cleared = _clear_stale_connection(
            proton_status_reader=proton_status_reader,
            runner=runner,
            wait_timeout=wait_timeout,
            sleep=sleep,
        )

    result = _run_proton(["protonvpn", "connect", name], runner, timeout=240)
    recovered = False
    if result.returncode != 0:
        detail = command_detail(result)
        events.emit(
            "warning",
            "vpn",
            "vpn.connect.exit-nonzero",
            {"server": name, "returncode": result.returncode, "output": detail},
        )
        recovered = wait_for_connected(
            name, status_reader, timeout=recover_timeout, sleep=sleep
        )
        if not recovered:
            raise VpnError(
                "protonvpn connect failed",
                detail=detail,
                hint=failure_hint(detail),
            )
    events.emit(
        "info",
        "vpn",
        "vpn.connect.done",
        {
            "server": name,
            "gui_closed": gui_closed,
            "stale_cleared": stale_cleared,
            "recovered": recovered,
        },
    )
    return VpnActionResult(
        ok=True,
        action="connect",
        server=name,
        gui_closed=gui_closed,
        gui_forced=gui_forced,
        disconnected_first=disconnected_first,
        stale_cleared=stale_cleared,
        recovered=recovered,
        output=clean_output(result.stdout or "").strip(),
    )


def disconnect_server(
    *,
    close_gui: bool = False,
    processes: Sequence[GuiProcess] | None = None,
    status_reader: StatusReader = read_vpn_status,
    runner: Runner = run,
    close_timeout: float = 10.0,
    recover_timeout: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> VpnActionResult:
    """Disconnect explicitly; offer GUI closing only when asked."""
    events.emit("info", "vpn", "vpn.disconnect.requested")
    try:
        with vpn_action_lock():
            return _disconnect(
                close_gui=close_gui,
                processes=processes,
                status_reader=status_reader,
                runner=runner,
                close_timeout=close_timeout,
                recover_timeout=recover_timeout,
                sleep=sleep,
            )
    except PnoError as exc:
        events.emit("error", "vpn", "vpn.disconnect.failed", {}, error=exc)
        raise


def _disconnect(
    *,
    close_gui: bool,
    processes: Sequence[GuiProcess] | None,
    status_reader: StatusReader,
    runner: Runner,
    close_timeout: float,
    recover_timeout: float,
    sleep: Callable[[float], None],
) -> VpnActionResult:
    result = _run_proton(["protonvpn", "disconnect"], runner, timeout=60)
    if result.returncode == 0:
        events.emit("info", "vpn", "vpn.disconnect.done", {"gui_closed": False})
        _mark_manual_disconnect()
        return VpnActionResult(
            ok=True,
            action="disconnect",
            output=clean_output(result.stdout or "").strip(),
        )

    detail = command_detail(result)
    events.emit(
        "warning",
        "vpn",
        "vpn.disconnect.exit-nonzero",
        {"returncode": result.returncode, "output": detail},
    )
    if wait_for_disconnected(status_reader, timeout=recover_timeout, sleep=sleep):
        events.emit(
            "info",
            "vpn",
            "vpn.disconnect.done",
            {"gui_closed": False, "recovered": True},
        )
        _mark_manual_disconnect()
        return VpnActionResult(
            ok=True,
            action="disconnect",
            recovered=True,
            output=clean_output(result.stdout or "").strip(),
        )

    gui_processes = list(find_gui_processes()) if processes is None else list(processes)
    if not gui_processes:
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
    retry = _run_proton(["protonvpn", "disconnect"], runner, timeout=60)
    recovered = False
    if retry.returncode != 0:
        retry_detail = command_detail(retry)
        events.emit(
            "warning",
            "vpn",
            "vpn.disconnect.exit-nonzero",
            {"returncode": retry.returncode, "output": retry_detail},
        )
        recovered = wait_for_disconnected(
            status_reader, timeout=recover_timeout, sleep=sleep
        )
        if not recovered:
            raise VpnError(
                "protonvpn disconnect failed after closing the GUI",
                detail=retry_detail,
                hint=failure_hint(retry_detail),
            )
    events.emit(
        "info",
        "vpn",
        "vpn.disconnect.done",
        {"gui_closed": True, "forced": bool(closing.forced), "recovered": recovered},
    )
    _mark_manual_disconnect()
    return VpnActionResult(
        ok=True,
        action="disconnect",
        gui_closed=True,
        gui_forced=bool(closing.forced),
        recovered=recovered,
        output=clean_output(retry.stdout or "").strip(),
    )
