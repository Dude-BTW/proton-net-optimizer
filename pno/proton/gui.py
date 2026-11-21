"""Detect and close the Proton VPN GUI.

The GUI is a Python script (``/usr/bin/protonvpn-app``), so process names are
not reliable. Scanning ``/proc/*/cmdline`` is: it finds the GUI no matter how
it was launched and never matches the ``protonvpn`` CLI or this tool.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import time
from pathlib import Path
from typing import Callable, Iterable


@dataclasses.dataclass(frozen=True)
class GuiProcess:
    pid: int
    cmdline: tuple[str, ...]

    @property
    def command(self) -> str:
        return " ".join(self.cmdline)


@dataclasses.dataclass(frozen=True)
class GuiCloseResult:
    terminated: tuple[int, ...] = ()
    forced: tuple[int, ...] = ()
    still_running: tuple[int, ...] = ()

    @property
    def closed(self) -> bool:
        return not self.still_running


def _is_gui_argument(argument: str) -> bool:
    if not argument:
        return False
    if Path(argument).name in {"protonvpn-app", "protonvpn_app"}:
        return True
    return "proton.vpn.app.gtk" in argument


def find_gui_processes(
    proc_root: str | os.PathLike[str] = "/proc",
    *,
    own_pid: int | None = None,
) -> list[GuiProcess]:
    root = Path(proc_root)
    skip = os.getpid() if own_pid is None else own_pid
    if not root.is_dir():
        return []
    found: list[GuiProcess] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for entry in sorted(entries, key=lambda item: item.name):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == skip:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        arguments = tuple(
            part.decode("utf-8", "replace") for part in raw.split(b"\0") if part
        )
        if arguments and any(_is_gui_argument(arg) for arg in arguments):
            found.append(GuiProcess(pid=pid, cmdline=arguments))
    return found


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal(pid: int, number: int, kill: Callable[[int, int], None]) -> None:
    try:
        kill(pid, number)
    except ProcessLookupError:
        return
    except PermissionError:
        return


def close_gui(
    processes: Iterable[GuiProcess],
    *,
    timeout: float = 10.0,
    term_timeout: float = 7.0,
    kill: Callable[[int, int], None] = os.kill,
    alive: Callable[[int], bool] = _pid_alive,
    sleep: Callable[[float], None] = time.sleep,
) -> GuiCloseResult:
    """Terminate the GUI; SIGKILL only if it ignores SIGTERM."""
    pids = [process.pid for process in processes]
    if not pids:
        return GuiCloseResult()
    for pid in pids:
        _signal(pid, signal.SIGTERM, kill)

    deadline = time.monotonic() + min(term_timeout, timeout)
    while time.monotonic() < deadline:
        if not any(alive(pid) for pid in pids):
            return GuiCloseResult(terminated=tuple(pids))
        sleep(0.2)

    forced: list[int] = []
    for pid in pids:
        if alive(pid):
            _signal(pid, signal.SIGKILL, kill)
            forced.append(pid)

    deadline = time.monotonic() + max(0.5, timeout - term_timeout)
    while time.monotonic() < deadline:
        if not any(alive(pid) for pid in pids):
            break
        sleep(0.2)

    terminated = tuple(pid for pid in pids if pid not in forced and not alive(pid))
    still_running = tuple(pid for pid in pids if alive(pid))
    return GuiCloseResult(
        terminated=terminated, forced=tuple(forced), still_running=still_running
    )
