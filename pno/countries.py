"""Country code normalisation and bilingual names.

Proton and Arch do not always agree on codes (Proton uses ``UK`` where Arch
uses ``GB``, and ``XK`` has no ISO counterpart). The mapping and the display
names live in ``pno/data/countries.toml``; nothing here ranks countries.
"""

from __future__ import annotations

import functools
import tomllib
from importlib import resources
from typing import Any

_DATA: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _DATA
    if _DATA is None:
        target = resources.files("pno").joinpath("data/countries.toml")
        with target.open("rb") as handle:
            _DATA = tomllib.load(handle)
    return _DATA


def aliases() -> dict[str, str]:
    return dict(_load().get("aliases", {}))


def normalize_code(code: str | None) -> str:
    """Return the canonical (ISO-3166 where possible) upper-case code."""
    if not code:
        return ""
    upper = code.strip().upper()
    return aliases().get(upper, upper)


@functools.lru_cache(maxsize=256)
def country_name(code: str | None, language: str = "en") -> str:
    if not code:
        return ""
    canonical = normalize_code(code)
    entry = _load().get("countries", {}).get(canonical, {})
    name = entry.get(language) or entry.get("en") or canonical
    return str(name)


@functools.lru_cache(maxsize=1)
def _name_index() -> dict[str, tuple[str, ...]]:
    index: dict[str, list[str]] = {}
    for code, entry in _load().get("countries", {}).items():
        for language in ("en", "uk"):
            value = entry.get(language)
            if not value:
                continue
            key = " ".join(str(value).casefold().split())
            codes = index.setdefault(key, [])
            if code not in codes:
                codes.append(code)
    return {key: tuple(codes) for key, codes in index.items()}


def codes_for_name(value: str) -> tuple[str, ...]:
    """Resolve a country name in English or Ukrainian to canonical codes."""
    key = " ".join(value.casefold().split())
    return _name_index().get(key, ())


def arch_code(proton_code: str | None) -> str:
    """Code to look up in the Arch mirror status."""
    return normalize_code(proton_code)


def proton_code(arch_code_value: str | None) -> str:
    """Inverse of :func:`arch_code` for the codes we know about."""
    canonical = normalize_code(arch_code_value)
    for alias, target in aliases().items():
        if target == canonical and alias != target:
            return alias
    return canonical
