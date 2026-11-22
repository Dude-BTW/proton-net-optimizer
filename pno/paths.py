"""Filesystem locations used by the tool.

All locations honour the XDG base directory specification and can be
overridden for tests or portable installs:

* ``PNO_HOME`` — single base directory (config/, state/, run/ inside it)
* ``PNO_CONFIG_DIR`` — configuration directory
* ``PNO_STATE_DIR`` — state, cache and log directory
* ``PNO_RUNTIME_DIR`` — daemon socket directory
"""

from __future__ import annotations

import os
from pathlib import Path

from pno import APP_NAME


def _xdg(env_name: str, fallback: str) -> Path:
    value = os.environ.get(env_name)
    if value:
        return Path(value)
    return Path(fallback).expanduser()


def _home_override() -> Path | None:
    value = os.environ.get("PNO_HOME")
    return Path(value).expanduser() if value else None


def config_dir() -> Path:
    override = os.environ.get("PNO_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    home = _home_override()
    if home is not None:
        return home / "config"
    return _xdg("XDG_CONFIG_HOME", "~/.config") / APP_NAME


def state_dir() -> Path:
    override = os.environ.get("PNO_STATE_DIR")
    if override:
        return Path(override).expanduser()
    home = _home_override()
    if home is not None:
        return home / "state"
    return _xdg("XDG_STATE_HOME", "~/.local/state") / APP_NAME


def runtime_dir() -> Path:
    override = os.environ.get("PNO_RUNTIME_DIR")
    if override:
        return Path(override).expanduser()
    home = _home_override()
    if home is not None:
        return home / "run"
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / APP_NAME
    return state_dir() / "run"


def logs_dir() -> Path:
    return state_dir() / "logs"


def proton_config_dir() -> Path:
    """Directory owned by the Proton VPN applications.

    pno only reads and writes the settings files that Proton itself uses. The
    location mirrors ``VPNExecutionEnvironment.path_config`` (XDG config home
    plus ``Proton/VPN``); an explicit override exists for tests and portable
    installs.
    """
    override = os.environ.get("PNO_PROTON_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_CONFIG_HOME", "~/.config") / "Proton" / "VPN"


def proton_settings_file() -> Path:
    return proton_config_dir() / "settings.json"


def proton_app_config_file() -> Path:
    return proton_config_dir() / "app-config.json"


def config_file() -> Path:
    return config_dir() / "config.toml"


def user_inventory_file() -> Path:
    return config_dir() / "inventory.toml"


def state_file() -> Path:
    return state_dir() / "state.json"


def servers_cache_file() -> Path:
    return state_dir() / "servers.json"


def mirrors_status_cache_file() -> Path:
    return state_dir() / "mirrors_status.json"


def mirror_history_file() -> Path:
    return state_dir() / "mirror_history.jsonl"


def socket_path() -> Path:
    return runtime_dir() / "daemon.sock"


def ensure_runtime_dir() -> Path:
    path = runtime_dir()
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def ensure_state_dir() -> Path:
    path = state_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_config_dir() -> Path:
    path = config_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
