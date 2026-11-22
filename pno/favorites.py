"""User favorites: personal countries and Secure Core routes.

Favorites are user data and live outside the repository in
``~/.config/proton-net-optimizer/inventory.toml``. This module parses the
text format used by the TUI/CLI, validates entries against the current server
snapshot, and saves atomically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pno import paths
from pno.countries import codes_for_name, normalize_code
from pno.errors import ConfigError
from pno.inventory import Inventory
from pno.inventory import load as load_inventory
from pno.proton.models import ServerSnapshot, eligible_servers
from pno.state import atomic_write_text

_SC_NAME = re.compile(r"^[A-Za-z]{2}-[A-Za-z]{2}#\d+$")
_CODE = re.compile(r"^[A-Za-z]{2}$")
_ALLOWED_NAME = re.compile(r"^[^\W\d_][\w .'’ʼ\-]*$", re.UNICODE)
_FORBIDDEN = re.compile(r"[/\\@$%^&*+=|<>\[\]{}~`,;]")


@dataclass(frozen=True)
class Favorites:
    countries: tuple[str, ...] = ()
    secure_core_servers: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.countries and not self.secure_core_servers

    def normalized(self) -> "Favorites":
        return Favorites(
            countries=tuple(
                sorted({normalize_code(code) for code in self.countries if code})
            ),
            secure_core_servers=tuple(
                sorted({name.upper() for name in self.secure_core_servers if name})
            ),
        )

    def to_inventory(self) -> Inventory:
        normalized = self.normalized()
        return Inventory(
            secure_core_servers=normalized.secure_core_servers,
            known_countries=normalized.countries,
            include_all_countries=True,
        )

    @classmethod
    def from_inventory(cls, inventory: Inventory) -> "Favorites":
        return cls(
            countries=inventory.known_countries,
            secure_core_servers=inventory.secure_core_servers,
        ).normalized()


@dataclass(frozen=True)
class FavoritesError:
    line: int
    message: str


@dataclass(frozen=True)
class ParseResult:
    favorites: Favorites | None
    errors: tuple[FavoritesError, ...] = ()
    warnings: tuple[str, ...] = ()


def _country_sets(
    snapshot: ServerSnapshot,
) -> tuple[set[str], dict[str, str]]:
    regular = {
        server.exit_code
        for server in eligible_servers(snapshot.servers)
        if server.exit_code
    }
    secure = {
        server.name.upper(): server.name
        for server in eligible_servers(snapshot.servers, secure_core=True)
    }
    return regular, secure


def _resolve_entry(
    raw: str,
    *,
    regular: set[str],
    secure: dict[str, str],
) -> tuple[str, str] | None:
    if _SC_NAME.match(raw):
        name = raw.upper()
        if name in secure:
            return "secure", name
        return None
    if _CODE.match(raw):
        code = normalize_code(raw)
        if code in regular:
            return "country", code
        return None
    if not _ALLOWED_NAME.match(raw):
        return None
    codes = codes_for_name(raw)
    if len(codes) != 1:
        return None
    code = normalize_code(codes[0])
    if code in regular:
        return "country", code
    return None


def parse_lines(
    lines: Iterable[str],
    snapshot: ServerSnapshot | None,
) -> ParseResult:
    if snapshot is None:
        return ParseResult(
            favorites=None,
            errors=(
                FavoritesError(
                    0,
                    "server data is unavailable; refresh server data first",
                ),
            ),
        )
    regular, secure = _country_sets(snapshot)
    countries: set[str] = set()
    servers: set[str] = set()
    errors: list[FavoritesError] = []
    warnings: list[str] = []

    for number, raw_line in enumerate(lines, start=1):
        line = " ".join(str(raw_line).split())
        if not line:
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in line):
            errors.append(FavoritesError(number, "control characters are not allowed"))
            continue
        if _FORBIDDEN.search(line):
            errors.append(
                FavoritesError(
                    number,
                    "contains a character that is not allowed in a country or route",
                )
            )
            continue
        resolved = _resolve_entry(line, regular=regular, secure=secure)
        if resolved is None:
            errors.append(
                FavoritesError(
                    number,
                    f"unknown country or Secure Core route: {line!r}",
                )
            )
            continue
        kind, value = resolved
        if kind == "country":
            if value in countries:
                warnings.append(f"line {number}: duplicate country {value}")
            countries.add(value)
        else:
            if value in servers:
                warnings.append(f"line {number}: duplicate route {value}")
            servers.add(value)

    if errors:
        return ParseResult(
            favorites=None, errors=tuple(errors), warnings=tuple(warnings)
        )
    return ParseResult(
        favorites=Favorites(
            countries=tuple(sorted(countries)),
            secure_core_servers=tuple(sorted(servers)),
        ),
        warnings=tuple(warnings),
    )


def parse_text(text: str, snapshot: ServerSnapshot | None) -> ParseResult:
    return parse_lines(text.splitlines(), snapshot)


def parse_removal_lines(lines: Iterable[str]) -> ParseResult:
    """Parse entries for removal without requiring them to still exist.

    A removed Proton server must remain removable from a stale favorites file.
    """
    countries: set[str] = set()
    servers: set[str] = set()
    errors: list[FavoritesError] = []
    for number, raw_line in enumerate(lines, start=1):
        line = " ".join(str(raw_line).split())
        if not line:
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in line):
            errors.append(FavoritesError(number, "control characters are not allowed"))
            continue
        if _FORBIDDEN.search(line):
            errors.append(
                FavoritesError(
                    number,
                    "contains a character that is not allowed in a country or route",
                )
            )
            continue
        if _SC_NAME.match(line):
            servers.add(line.upper())
        elif _CODE.match(line):
            countries.add(normalize_code(line))
        elif _ALLOWED_NAME.match(line):
            codes = codes_for_name(line)
            if len(codes) != 1:
                errors.append(FavoritesError(number, f"unknown country name: {line!r}"))
            else:
                countries.add(normalize_code(codes[0]))
        else:
            errors.append(FavoritesError(number, f"invalid entry: {line!r}"))
    if errors:
        return ParseResult(favorites=None, errors=tuple(errors))
    return ParseResult(
        favorites=Favorites(
            countries=tuple(sorted(countries)),
            secure_core_servers=tuple(sorted(servers)),
        )
    )


def parse_removal_text(text: str) -> ParseResult:
    return parse_removal_lines(text.splitlines())


def normalized_text(favorites: Favorites) -> str:
    normalized = favorites.normalized()
    entries = list(normalized.countries) + list(normalized.secure_core_servers)
    return "\n".join(entries) + ("\n" if entries else "")


def load(path: Path | None = None) -> Favorites:
    return Favorites.from_inventory(load_inventory(path))


def _toml_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"


def save(favorites: Favorites, path: Path | None = None) -> Path:
    target = path or paths.user_inventory_file()
    normalized = favorites.normalized()
    text = "\n".join(
        [
            "# Personal favorites. This file is not part of the repository.",
            "# One regular country or Secure Core route per entry is stored below.",
            "",
            "[secure_core]",
            f"servers = {_toml_list(normalized.secure_core_servers)}",
            "exits = []",
            "",
            "[countries]",
            f"known = {_toml_list(normalized.countries)}",
            "include_all = true",
            "",
        ]
    )
    try:
        atomic_write_text(target, text, mode=0o600)
    except OSError as exc:
        raise ConfigError(
            f"cannot save favorites to {target}", detail=str(exc)
        ) from exc
    return target


def selection_values(favorites: Favorites) -> set[str]:
    normalized = favorites.normalized()
    return {f"country:{code}" for code in normalized.countries} | {
        f"sc:{name}" for name in normalized.secure_core_servers
    }


def from_selection(values: Iterable[str]) -> Favorites:
    countries: list[str] = []
    servers: list[str] = []
    for value in values:
        if value.startswith("country:"):
            countries.append(value.split(":", 1)[1])
        elif value.startswith("sc:"):
            servers.append(value.split(":", 1)[1])
    return Favorites(
        countries=tuple(countries), secure_core_servers=tuple(servers)
    ).normalized()


def visual_options(
    snapshot: ServerSnapshot,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return ``(value, label)`` pairs for regular countries and SC routes."""
    regular_codes = sorted(
        {
            server.exit_code
            for server in eligible_servers(snapshot.servers)
            if server.exit_code
        }
    )
    countries = [(f"country:{code}", code) for code in regular_codes]
    routes = [
        (f"sc:{server.name}", server.name)
        for server in sorted(
            eligible_servers(snapshot.servers, secure_core=True),
            key=lambda item: (item.exit_code, item.name),
        )
    ]
    return countries, routes
