"""Single-instance TUI and window focusing.

The TUI lives in a terminal window, so a second ``pno tui`` should raise the
existing window instead of opening a new one. The running instance holds an
``flock`` and serves a tiny Unix-socket control channel:

* ``{"command": "focus"}`` — raise/focus the terminal window;
* ``{"command": "ping"}`` — liveness check.

Focusing uses kitty's remote-control socket when available (the launcher
passes its path in ``PNO_KITTY_LISTEN_ON``), then ``kdotool``, ``wmctrl`` or
``xdotool``. If none works the TUI stays single-instance anyway and a desktop
notification explains that it is already running.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, TextIO

from pno import events, paths

APP_CLASS = "proton-net-optimizer"
WINDOW_TITLE = "Proton Net Optimizer"
MAX_MESSAGE = 4096


def _send(
    payload: dict,
    socket_path: Path,
    *,
    timeout: float = 2.0,
) -> dict | None:
    data = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(data)
            raw = b""
            while b"\n" not in raw and len(raw) < MAX_MESSAGE:
                chunk = client.recv(4096)
                if not chunk:
                    break
                raw += chunk
    except (OSError, TimeoutError):
        return None
    try:
        response = json.loads(raw.split(b"\n", 1)[0])
    except ValueError:
        return None
    return response if isinstance(response, dict) else None


def _run(command: list[str], timeout: float = 5.0) -> bool:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _notify_already_running() -> None:
    if not shutil.which("notify-send"):
        return
    _run(
        [
            "notify-send",
            "-a",
            "pno",
            WINDOW_TITLE,
            "TUI is already running in another window.",
        ],
        timeout=5,
    )


class TuiInstance:
    """Own the TUI lock and focus channel for the lifetime of the process."""

    def __init__(
        self,
        *,
        lock_path: Path | None = None,
        socket_path: Path | None = None,
        kitty_listen_on: str | os.PathLike[str] | None = None,
        focuser: Callable[[], str] | None = None,
    ) -> None:
        runtime = paths.runtime_dir()
        self.lock_path = lock_path or (runtime / "tui.lock")
        self.socket_path = socket_path or (runtime / "tui.sock")
        self.kitty_listen_on = kitty_listen_on or os.environ.get("PNO_KITTY_LISTEN_ON")
        self.focuser = focuser
        self.pid = os.getpid()
        self._lock: TextIO | None = None
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------- lifecycle
    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.lock_path.parent.chmod(0o700)
        except OSError:
            pass
        handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._lock = handle
        try:
            self._start_server()
        except OSError as exc:
            events.emit(
                "warning",
                "tui",
                "tui.instance.server-failed",
                {"error": str(exc)},
            )
        events.emit(
            "info",
            "tui",
            "tui.instance.primary",
            {"pid": self.pid, "socket": str(self.socket_path)},
        )
        return True

    def close(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self.socket_path.unlink()
        except OSError:
            pass
        if self._lock is not None:
            try:
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._lock.close()
            self._lock = None
        events.emit("info", "tui", "tui.instance.closed", {"pid": self.pid})

    def focus_existing(self, *, timeout: float = 2.0) -> bool:
        response = _send({"command": "focus"}, self.socket_path, timeout=timeout)
        return bool(response and response.get("ok"))

    # --------------------------------------------------------------- server
    def _start_server(self) -> None:
        try:
            self.socket_path.unlink()
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        try:
            self.socket_path.chmod(0o600)
        except OSError:
            pass
        server.listen(4)
        server.settimeout(0.5)
        self._server = server
        self._thread = threading.Thread(
            target=self._serve, name="pno-tui-focus", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle, args=(connection,), daemon=True
            ).start()

    def _handle(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(2.0)
            try:
                raw = connection.recv(MAX_MESSAGE)
                request = json.loads(raw.decode("utf-8", "replace").strip() or "{}")
            except (OSError, ValueError):
                return
            command = str(request.get("command") or "")
            response: dict[str, Any]
            if command == "ping":
                response = {"ok": True, "pid": self.pid}
            elif command == "focus":
                method = self.focus_window()
                response = {"ok": bool(method), "method": method}
            else:
                response = {"ok": False, "error": "unknown command"}
            try:
                connection.sendall(json.dumps(response).encode() + b"\n")
            except OSError:
                pass

    # ---------------------------------------------------------------- focus
    def focus_window(self) -> str:
        if self.focuser is not None:
            return self.focuser()
        if self._focus_with_kitty():
            return "kitty"
        if self._focus_with_tool(
            "kdotool", ["kdotool", "search", "--class", APP_CLASS, "windowactivate"]
        ):
            return "kdotool"
        if self._focus_with_tool("wmctrl", ["wmctrl", "-x", "-a", APP_CLASS]):
            return "wmctrl"
        if self._focus_with_tool(
            "xdotool", ["xdotool", "search", "--class", APP_CLASS, "windowactivate"]
        ):
            return "xdotool"
        _notify_already_running()
        events.emit(
            "warning",
            "tui",
            "tui.focus.failed",
            {"pid": self.pid, "terminal": "unknown"},
        )
        return ""

    def _focus_with_kitty(self) -> bool:
        if not self.kitty_listen_on:
            return False
        listen = Path(str(self.kitty_listen_on))
        if not listen.exists() or not shutil.which("kitty"):
            return False
        return _run(
            [
                "kitty",
                "@",
                "--to",
                f"unix:{listen}",
                "focus-window",
                "--match",
                f"pid:{self.pid}",
            ]
        )

    @staticmethod
    def _focus_with_tool(name: str, command: list[str]) -> bool:
        if not shutil.which(name):
            return False
        return _run(command)


def tui_is_running(lock_path: Path | None = None) -> bool:
    path = lock_path or (paths.runtime_dir() / "tui.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(path, "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
    return False


def focus_running_tui(
    *,
    lock_path: Path | None = None,
    socket_path: Path | None = None,
) -> bool:
    """Focus an existing TUI. Returns True when one is running (even if focus failed)."""
    if not tui_is_running(lock_path):
        return False
    target = socket_path or (paths.runtime_dir() / "tui.sock")
    response = _send({"command": "focus"}, target, timeout=2.5)
    if response is None:
        _notify_already_running()
    return True
