"""Eligible server inventory (data only, never a ranking).

The inventory is loaded from the packaged ``data/inventory.toml``. A user can
replace it completely with ``~/.config/proton-net-optimizer/inventory.toml``.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from pno import paths
from pno.errors import ConfigError

_SECURE_CORE_NAME = re.compile(r"^(?P<entry>[A-Z]{2})-(?P<exit>[A-Z]{2})#(?P<num>\d+)$")


def parse_secure_core_name(name: str) -> tuple[str, str] | None:
    match = _SECURE_CORE_NAME.match(name.strip().upper())
    if not match:
        return None
    return match.group("entry"), match.group("exit")


@dataclass(frozen=True)
class Inventory:
    secure_core_servers: tuple[str, ...] = ()
    secure_core_exits: tuple[str, ...] = ()
    known_countries: tuple[str, ...] = ()
    include_all_countries: bool = True

    def exits(self) -> tuple[str, ...]:
        """Exit countries, explicit list first, then derived from server names."""
        result: list[str] = []
        for code in self.secure_core_exits:
            if code not in result:
                result.append(code)
        for name in self.secure_core_servers:
            parsed = parse_secure_core_name(name)
            if parsed and parsed[1] not in result:
                result.append(parsed[1])
        return tuple(result)


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read inventory {path}", detail=str(exc)) from exc


def _normalise_list(values: object, field: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ConfigError(f"{field} must be a list of strings", detail=repr(values))
    return tuple(v.strip().upper() for v in values if v.strip())


def load(path: Path | None = None) -> Inventory:
    target = path or paths.user_inventory_file()
    if target.exists():
        raw = _read_toml(target)
    else:
        packaged = resources.files("pno").joinpath("data/inventory.toml")
        with packaged.open("rb") as handle:
            raw = tomllib.load(handle)

    secure_core = raw.get("secure_core", {})
    countries = raw.get("countries", {})
    if not isinstance(secure_core, dict) or not isinstance(countries, dict):
        raise ConfigError("inventory sections must be tables")

    servers = _normalise_list(secure_core.get("servers"), "secure_core.servers")
    for name in servers:
        if not parse_secure_core_name(name):
            raise ConfigError(
                "invalid Secure Core server name",
                detail=f"{name} (expected ENTRY-EXIT#N)",
            )
    known = _normalise_list(countries.get("known"), "countries.known")
    include_all = countries.get("include_all", True)
    if not isinstance(include_all, bool):
        raise ConfigError("countries.include_all must be a boolean")

    return Inventory(
        secure_core_servers=servers,
        secure_core_exits=_normalise_list(
            secure_core.get("exits"), "secure_core.exits"
        ),
        known_countries=known,
        include_all_countries=include_all,
    )
