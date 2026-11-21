"""Runtime component checks shown at startup and by ``pno doctor``.

Keep the checks cheap: they must be safe to run every time the TUI, tray or
daemon starts. No network calls and no interactive commands.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from pno import paths
from pno.config import Config
from pno.errors import PnoError
from pno.launcher import resolve_terminal
from pno.mirrors.apply import find_helper
from pno.proton.serverlist import DEFAULT_PROTON_CACHE

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class ComponentCheck:
    key: str
    label: str
    status: str
    detail: str
    hint: str = ""
    required: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "required": self.required,
        }


@dataclass(frozen=True)
class RequirementReport:
    checks: tuple[ComponentCheck, ...]

    @property
    def failures(self) -> tuple[ComponentCheck, ...]:
        return tuple(check for check in self.checks if check.status == FAIL)

    @property
    def warnings(self) -> tuple[ComponentCheck, ...]:
        return tuple(check for check in self.checks if check.status == WARN)

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        problems = [check for check in self.checks if check.status != OK]
        if not problems:
            return "All required components are available."
        lines: list[str] = []
        for check in problems:
            prefix = "FAIL" if check.status == FAIL else "WARN"
            lines.append(f"{prefix}  {check.label}: {check.detail}")
            if check.hint:
                lines.append(f"      {check.hint}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "failures": [check.key for check in self.failures],
            "warnings": [check.key for check in self.warnings],
            "checks": [check.to_dict() for check in self.checks],
        }


def _check(
    key: str,
    label: str,
    status: str,
    detail: str,
    *,
    hint: str = "",
    required: bool = False,
) -> ComponentCheck:
    return ComponentCheck(
        key=key,
        label=label,
        status=status,
        detail=detail,
        hint=hint,
        required=required,
    )


def _python_check() -> ComponentCheck:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 11):
        return _check("python", "Python", OK, version, required=True)
    return _check(
        "python",
        "Python",
        FAIL,
        f"{version} is too old",
        hint="Install Python 3.11 or newer.",
        required=True,
    )


def _arch_check() -> ComponentCheck:
    if Path("/etc/arch-release").is_file():
        return _check("arch", "Arch Linux", OK, "detected", required=True)
    return _check(
        "arch",
        "Arch Linux",
        FAIL,
        "not detected",
        hint="pno targets Arch Linux; pacman/mirror features require it.",
        required=True,
    )


def _tool_check(
    key: str, label: str, executable: str, *, required: bool = True, hint: str = ""
) -> ComponentCheck:
    found = shutil.which(executable)
    if found:
        return _check(key, label, OK, found, required=required)
    return _check(
        key,
        label,
        FAIL if required else WARN,
        f"'{executable}' not found",
        hint=hint or f"Install the package providing {executable}.",
        required=required,
    )


def _proton_data_check() -> ComponentCheck:
    if DEFAULT_PROTON_CACHE.is_file():
        return _check(
            "proton-data",
            "Proton server data",
            OK,
            f"cache {DEFAULT_PROTON_CACHE}",
            required=True,
        )
    module = next(
        iter(
            list(Path("/usr/lib").glob("python3*/site-packages/proton/vpn/core/api.py"))
            + list(
                Path("/usr/lib").glob("python3/dist-packages/proton/vpn/core/api.py")
            )
        ),
        None,
    )
    if module is not None:
        return _check(
            "proton-data",
            "Proton server data",
            OK,
            "system Proton libraries found",
            required=True,
        )
    if paths.servers_cache_file().is_file():
        return _check(
            "proton-data",
            "Proton server data",
            WARN,
            "only the local cache is available",
            hint="Open the Proton VPN app or sign in, then run 'pno doctor'.",
            required=True,
        )
    return _check(
        "proton-data",
        "Proton server data",
        FAIL,
        "no Proton cache or libraries",
        hint="Install proton-vpn-gtk-app and sign in with 'protonvpn signin'.",
        required=True,
    )


def _mirrorlist_check(config: Config) -> ComponentCheck:
    target = Path(config.mirrorlist_path)
    if target.is_file():
        return _check("mirrorlist", "Mirrorlist", OK, str(target))
    return _check(
        "mirrorlist",
        "Mirrorlist",
        WARN,
        f"{target} is missing",
        hint="The first mirror refresh will create it.",
    )


def _mirror_helper_check() -> ComponentCheck:
    helper = find_helper()
    if helper:
        return _check("mirror-helper", "Mirror helper", OK, str(helper))
    return _check(
        "mirror-helper",
        "Mirror helper",
        WARN,
        "not installed",
        hint="Run 'pno install' to enable automatic mirror updates.",
    )


def _sudo_check() -> ComponentCheck:
    found = shutil.which("sudo")
    if found:
        return _check("sudo", "sudo", OK, found)
    return _check(
        "sudo",
        "sudo",
        WARN,
        "not found",
        hint="Automatic mirror writes will be unavailable.",
    )


def _terminal_check(config: Config) -> ComponentCheck:
    try:
        terminal = resolve_terminal(config)
    except PnoError as exc:
        return _check(
            "terminal",
            "Terminal",
            WARN,
            exc.message,
            hint="Set 'terminal' in config.toml to an installed terminal.",
        )
    if config.terminal and config.terminal != terminal:
        return _check(
            "terminal",
            "Terminal",
            WARN,
            f"configured '{config.terminal}' not found; using '{terminal}'",
            hint="Update 'terminal' in config.toml or leave it empty for auto.",
        )
    return _check("terminal", "Terminal", OK, terminal)


def _config_check(config: Config) -> ComponentCheck:
    return _check(
        "config",
        "Configuration",
        OK,
        f"home_country={config.home_country}, language={config.language}",
        hint="Set home_country to your own country if you are not in Ukraine.",
    )


def _tui_check() -> ComponentCheck:
    if importlib.util.find_spec("textual") is not None:
        return _check("textual", "Textual", OK, "available", required=True)
    return _check(
        "textual",
        "Textual",
        FAIL,
        "not installed",
        hint="Install it with: pip install -e '.[tui]'",
        required=True,
    )


def _tray_check() -> ComponentCheck:
    if importlib.util.find_spec("PySide6") is not None:
        return _check("pyside6", "PySide6", OK, "available", required=True)
    return _check(
        "pyside6",
        "PySide6",
        FAIL,
        "not installed",
        hint="Install pyside6 (pacman) for the tray icon.",
        required=True,
    )


def check_requirements(
    config: Config,
    *,
    for_tui: bool = False,
    for_tray: bool = False,
) -> RequirementReport:
    checks: list[ComponentCheck] = [
        _python_check(),
        _arch_check(),
        _tool_check(
            "pacman",
            "pacman",
            "pacman",
            hint="Install pacman (Arch Linux).",
        ),
        _tool_check(
            "nmcli",
            "NetworkManager",
            "nmcli",
            required=False,
            hint="Install NetworkManager; status falls back to 'protonvpn status'.",
        ),
        _tool_check(
            "protonvpn",
            "Proton VPN CLI",
            "protonvpn",
            hint="Install proton-vpn-cli (pacman).",
        ),
        _proton_data_check(),
        _mirrorlist_check(config),
        _sudo_check(),
        _mirror_helper_check(),
        _terminal_check(config),
        _config_check(config),
    ]
    if for_tui:
        checks.append(_tui_check())
    if for_tray:
        checks.append(_tray_check())
    return RequirementReport(checks=tuple(checks))
