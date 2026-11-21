"""User configuration (TOML).

The configuration is deliberately flat and small. Unknown keys are ignored so
an older build never refuses to start because a newer one added a key.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pno import paths
from pno.errors import ConfigError


@dataclass
class Config:
    # Server suggestions
    top_n: int = 10
    include_tor: bool = False
    home_country: str = "UA"

    # Daemon timing (seconds)
    poll_interval: float = 10.0
    loads_interval: float = 60.0
    servers_max_age: float = 120.0
    mirror_debounce_seconds: float = 15.0

    # Mirror automation
    auto_mirror: bool = True
    auto_mirror_on_vpn_change: bool = True
    mirror_interval_hours: float = 12.0
    mirror_count: int = 5
    mirror_global_candidates: int = 20
    mirror_probe_timeout: float = 5.0
    mirror_probe_attempts: int = 2
    mirror_max_delay_hours: float = 24.0
    mirror_max_age_hours: float = 48.0
    mirror_min_completion: float = 0.95
    mirror_pacman_sync: bool = False
    mirror_status_max_age: float = 3600.0
    mirrorlist_path: str = "/etc/pacman.d/mirrorlist"

    # Interface / integrations
    language: str = "en"
    terminal: str = ""
    log_level: str = "info"
    proton_helper_timeout: float = 40.0

    def validate(self) -> "Config":
        if not 1 <= self.top_n <= 100:
            raise ConfigError("top_n must be between 1 and 100", detail=str(self.top_n))
        if not 1 <= self.mirror_count <= 50:
            raise ConfigError(
                "mirror_count must be between 1 and 50", detail=str(self.mirror_count)
            )
        if self.mirror_count > self.mirror_global_candidates:
            raise ConfigError(
                "mirror_count cannot exceed mirror_global_candidates",
                detail=f"{self.mirror_count} > {self.mirror_global_candidates}",
            )
        for name in (
            "poll_interval",
            "loads_interval",
            "servers_max_age",
            "mirror_debounce_seconds",
            "mirror_interval_hours",
            "mirror_probe_timeout",
            "mirror_status_max_age",
            "proton_helper_timeout",
        ):
            value = getattr(self, name)
            if value <= 0:
                raise ConfigError(f"{name} must be positive", detail=str(value))
        if not 0.0 <= self.mirror_min_completion <= 1.0:
            raise ConfigError(
                "mirror_min_completion must be within 0..1",
                detail=str(self.mirror_min_completion),
            )
        if self.language not in {"en", "uk"}:
            raise ConfigError("language must be 'en' or 'uk'", detail=self.language)
        self.home_country = self.home_country.upper()
        if len(self.home_country) != 2:
            raise ConfigError(
                "home_country must be a two-letter code", detail=self.home_country
            )
        return self

    def to_toml(self) -> str:
        lines = [
            "# proton-net-optimizer configuration",
            "# Edited by the tool only when the file does not exist yet.",
            "",
        ]
        for item in dataclasses.fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, str):
                rendered = '"' + value.replace('"', '\\"') + '"'
            else:
                rendered = str(value)
            lines.append(f"{item.name} = {rendered}")
        lines.append("")
        return "\n".join(lines)


_TYPE_COERCE = {
    "top_n": int,
    "mirror_count": int,
    "mirror_global_candidates": int,
    "mirror_probe_attempts": int,
    "include_tor": bool,
    "auto_mirror": bool,
    "auto_mirror_on_vpn_change": bool,
    "mirror_pacman_sync": bool,
}


def _coerce(name: str, value: Any) -> Any:
    coerce = _TYPE_COERCE.get(name)
    if coerce is bool:
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{name} must be a boolean", detail=repr(value))
    if coerce is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{name} must be an integer", detail=repr(value))
        return value
    return value


def load(path: Path | None = None) -> Config:
    target = path or paths.config_file()
    try:
        with open(target, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return Config().validate()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"cannot read configuration {target}", detail=str(exc)
        ) from exc

    known = {item.name for item in dataclasses.fields(Config)}
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key in known:
            values[key] = _coerce(key, value)
    return Config(**values).validate()


def save(config: Config, path: Path | None = None) -> Path:
    target = path or paths.config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    from pno.state import atomic_write_text

    atomic_write_text(target, config.to_toml(), mode=0o644)
    return target


def ensure(path: Path | None = None) -> Config:
    """Return the configuration, creating the default file when absent."""
    target = path or paths.config_file()
    if not target.exists():
        config = Config()
        save(config, target)
        return config
    return load(target)


@dataclass
class RuntimePaths:
    config: Path = field(default_factory=paths.config_file)
    state: Path = field(default_factory=paths.state_file)
    servers: Path = field(default_factory=paths.servers_cache_file)
    mirror_status: Path = field(default_factory=paths.mirrors_status_cache_file)
    mirror_history: Path = field(default_factory=paths.mirror_history_file)
    logs: Path = field(default_factory=paths.logs_dir)
    socket: Path = field(default_factory=paths.socket_path)
