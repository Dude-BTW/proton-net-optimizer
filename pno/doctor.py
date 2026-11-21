"""Environment diagnostics used by ``pno doctor`` and the tray."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from pno import paths
from pno.config import Config
from pno.errors import PnoError
from pno.ipc import request
from pno.mirrors.apply import find_helper
from pno.mirrors.planner import server_lines
from pno.proton.serverlist import DEFAULT_PROTON_CACHE

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_FAIL = "fail"


def _check(name: str, status: str, detail: str = "", hint: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "status": status, "detail": detail}
    if hint:
        payload["hint"] = hint
    return payload


def _system_python_has_proton() -> bool:
    python = os.environ.get("PNO_SYSTEM_PYTHON") or "/usr/bin/python3"
    try:
        result = subprocess.run(
            [python, "-c", "import proton.vpn.core.api"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _daemon_state() -> dict[str, Any]:
    try:
        payload = request("ping", timeout=3.0)
        return payload if isinstance(payload, dict) else {"ok": True}
    except PnoError:
        return {}


def run_checks(config: Config) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    checks.append(
        _check(
            "python",
            LEVEL_OK,
            f"{sys.version.split()[0]} ({'.venv' if sys.prefix != sys.base_prefix else 'system'})",
        )
    )

    if _system_python_has_proton():
        checks.append(
            _check(
                "proton helper",
                LEVEL_OK,
                "system python can import proton.vpn.core.api",
            )
        )
    else:
        checks.append(
            _check(
                "proton helper",
                LEVEL_FAIL,
                "system python cannot import proton.vpn.core.api",
                "Install proton-vpn-gtk-app (pacman) and sign in with 'protonvpn signin'.",
            )
        )

    cache = DEFAULT_PROTON_CACHE
    if cache.is_file():
        age = max(0.0, time.time() - cache.stat().st_mtime)
        checks.append(
            _check("proton cache", LEVEL_OK, f"{cache} (age {age / 60:.1f} min)")
        )
    else:
        checks.append(
            _check(
                "proton cache",
                LEVEL_WARN,
                f"{cache} is missing",
                "Open the Proton VPN app or run 'protonvpn status' once.",
            )
        )

    for tool in ("nmcli", "protonvpn", "sudo"):
        if shutil.which(tool):
            checks.append(_check(tool, LEVEL_OK, shutil.which(tool) or ""))
        else:
            checks.append(
                _check(
                    tool,
                    LEVEL_FAIL,
                    "not found",
                    f"Install the package providing {tool}.",
                )
            )

    helper = find_helper()
    if helper:
        checks.append(_check("mirror helper", LEVEL_OK, str(helper)))
        try:
            result = subprocess.run(
                ["sudo", "-n", "-l", str(helper)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                checks.append(
                    _check("sudo rule", LEVEL_OK, "NOPASSWD helper available")
                )
            else:
                checks.append(
                    _check(
                        "sudo rule",
                        LEVEL_WARN,
                        (result.stderr or result.stdout).strip()[:300],
                        "Run 'pno install' to create /etc/sudoers.d/pno-mirror.",
                    )
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append(_check("sudo rule", LEVEL_WARN, str(exc)))
    else:
        checks.append(
            _check(
                "mirror helper",
                LEVEL_WARN,
                "not installed",
                "Run 'pno install' to enable automatic mirror updates.",
            )
        )

    mirrorlist = Path(config.mirrorlist_path)
    if mirrorlist.is_file():
        try:
            lines = server_lines(mirrorlist.read_text(encoding="utf-8"))
        except OSError as exc:
            lines = []
            detail = str(exc)
        else:
            detail = f"{len(lines)} Server entries"
        if any(line.startswith("Server = rsync://") for line in lines):
            status = LEVEL_WARN
            hint = "pacman does not support rsync; pno will replace these entries."
        elif not lines:
            status = LEVEL_WARN
            hint = "The file has no usable Server entries."
        else:
            status = LEVEL_OK
            hint = ""
        checks.append(_check("mirrorlist", status, detail, hint))
    else:
        checks.append(_check("mirrorlist", LEVEL_WARN, f"{mirrorlist} does not exist"))

    try:
        ping = _daemon_state()
        if ping:
            checks.append(
                _check(
                    "daemon",
                    LEVEL_OK,
                    f"running (pid {ping.get('pid')}, paused={ping.get('paused')})",
                )
            )
        else:
            checks.append(
                _check(
                    "daemon",
                    LEVEL_WARN,
                    "not running",
                    "Run 'pno daemon run' or 'systemctl --user start pno-daemon'.",
                )
            )
    except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
        checks.append(_check("daemon", LEVEL_WARN, str(exc)))

    if shutil.which(config.terminal):
        checks.append(_check("terminal", LEVEL_OK, config.terminal))
    else:
        checks.append(
            _check(
                "terminal",
                LEVEL_WARN,
                f"'{config.terminal}' not found",
                "Set 'terminal' in the configuration to your terminal emulator.",
            )
        )

    from pno.proton.gui import find_gui_processes

    gui_processes = find_gui_processes()
    if gui_processes:
        checks.append(
            _check(
                "proton gui",
                LEVEL_WARN,
                "running (pids: " + ", ".join(str(p.pid) for p in gui_processes) + ")",
                "pno can close it with consent before connecting from the TUI.",
            )
        )
    else:
        checks.append(_check("proton gui", LEVEL_OK, "not running"))

    from pno.install import desktop_path, icon_path

    if desktop_path().is_file() and icon_path().is_file():
        checks.append(_check("desktop entry", LEVEL_OK, str(desktop_path())))
    else:
        checks.append(
            _check(
                "desktop entry",
                LEVEL_WARN,
                "not installed",
                "Run 'pno install' to add the KDE application-menu entry and icon.",
            )
        )

    if importlib.util.find_spec("textual") is not None:
        checks.append(_check("textual", LEVEL_OK, "TUI available"))
    else:
        checks.append(
            _check("textual", LEVEL_FAIL, "not installed", "pip install -e '.[tui]'")
        )

    tray_ok = importlib.util.find_spec("PySide6") is not None
    if not tray_ok:
        try:
            probe_result = subprocess.run(
                ["/usr/bin/python3", "-c", "import PySide6"],
                capture_output=True,
                timeout=20,
                check=False,
            )
            tray_ok = probe_result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            tray_ok = False
    checks.append(
        _check(
            "tray",
            LEVEL_OK if tray_ok else LEVEL_WARN,
            "PySide6 available" if tray_ok else "PySide6 not available",
            "" if tray_ok else "Install pyside6 (pacman) for the tray icon.",
        )
    )

    checks.append(
        _check(
            "paths",
            LEVEL_OK,
            f"config={paths.config_dir()} state={paths.state_dir()}",
        )
    )
    return checks


def worst_status(checks: list[dict[str, Any]]) -> str:
    order = {LEVEL_OK: 0, LEVEL_WARN: 1, LEVEL_FAIL: 2}
    worst = LEVEL_OK
    for check in checks:
        if order.get(check["status"], 0) > order[worst]:
            worst = str(check["status"])
    return worst
