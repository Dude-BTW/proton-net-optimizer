"""Country flag helpers for the TUI.

Flags are Unicode regional-indicator pairs, so they need no extra assets. The
font must contain emoji glyphs; ``Noto Color Emoji`` is present on the target
system. If a code is unknown the caller can fall back to the plain code.
"""

from __future__ import annotations

from pno.countries import country_name, normalize_code


def flag(code: str | None) -> str:
    """Return the emoji flag for an ISO-3166 alpha-2 code (or "")."""
    normalized = normalize_code(code)
    if len(normalized) != 2 or not normalized.isalpha():
        return ""
    if not all("A" <= char <= "Z" for char in normalized):
        return ""
    return "".join(chr(0x1F1E6 + ord(char) - ord("A")) for char in normalized)


def country_label(code: str, language: str = "en") -> str:
    """``🇺🇦 UA  Ukraine`` (falls back to ``UA  Ukraine``)."""
    symbol = flag(code)
    name = country_name(code, language)
    prefix = f"{symbol} " if symbol else ""
    return f"{prefix}{normalize_code(code)}  {name}"


def server_label(server_name: str, exit_code: str) -> str:
    symbol = flag(exit_code)
    prefix = f"{symbol} " if symbol else ""
    return f"{prefix}{server_name}"
