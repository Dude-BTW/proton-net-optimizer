"""Open the TUI in the configured terminal emulator.

Used by ``pno launch`` (the KDE application-menu entry) and by the tray's
"Open TUI" action. Keeping the command in one place means the desktop entry
does not have to hard-code ``kitty`` or any other terminal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from pno import paths
from pno.config import Config
from pno.errors import PnoError

APP_ID = "proton-net-optimizer"
WINDOW_TITLE = "Proton Net Optimizer"


def repo_root() -> Path:
    candidate = Path(__file__).resolve().parents[1]
    return candidate if (candidate / "pyproject.toml").is_file() else Path.home()


def build_terminal_command(
    config: Config,
    python: str,
    *,
    title: str = WINDOW_TITLE,
    working_directory: Path | None = None,
    kitty_listen_on: Path | None = None,
) -> list[str]:
    """Build an argv that runs ``pno tui`` inside the configured terminal."""
    terminal = config.terminal
    if not shutil.which(terminal):
        raise PnoError(
            "terminal emulator not found",
            detail=terminal,
            hint="Set 'terminal' in config.toml to an installed terminal (kitty, konsole, ...).",
        )
    name = Path(terminal).name
    command = [terminal]
    cwd = working_directory or repo_root()

    if name == "kitty":
        command += [
            "--class",
            APP_ID,
            "--name",
            APP_ID,
            "--title",
            title,
            "--single-instance=no",
            "--directory",
            str(cwd),
        ]
        if kitty_listen_on is not None:
            command += ["--listen-on", f"unix:{kitty_listen_on}"]
    elif name == "konsole":
        command += ["--title", title, "--workdir", str(cwd)]
    elif name == "alacritty":
        command += [
            "--class",
            APP_ID,
            "--title",
            title,
            "--working-directory",
            str(cwd),
        ]
    elif name == "foot":
        command += ["--title", title, "--directory", str(cwd)]
    elif name == "xterm":
        command += ["-T", title]
    elif name == "wezterm":
        command += ["start", "--class", APP_ID, "--cwd", str(cwd), "--"]
    command += ["-e", python, "-m", "pno", "tui"]
    return command


def open_tui(
    config: Config,
    *,
    python: str | None = None,
    title: str = WINDOW_TITLE,
) -> subprocess.Popen[bytes] | None:
    """Focus an existing TUI, or launch one detached.

    Returns ``None`` when an existing window was focused and no new terminal
    was started.
    """
    from pno.tui.single_instance import focus_running_tui

    if focus_running_tui():
        return None

    interpreter = python or os.environ.get("PNO_VENV_PYTHON") or sys.executable
    runtime = paths.runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    try:
        runtime.chmod(0o700)
    except OSError:
        pass
    kitty_socket = runtime / f"kitty-{os.getpid()}.sock"
    command = build_terminal_command(
        config,
        interpreter,
        title=title,
        kitty_listen_on=kitty_socket,
    )
    environment = dict(os.environ)
    environment["PNO_KITTY_LISTEN_ON"] = str(kitty_socket)
    try:
        return subprocess.Popen(
            command,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env=environment,
        )
    except OSError as exc:
        raise PnoError(
            "cannot start the terminal emulator",
            detail=str(exc),
            hint="Check the 'terminal' value in config.toml.",
        ) from exc
