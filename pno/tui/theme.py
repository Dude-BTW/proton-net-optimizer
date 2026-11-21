"""Colour palette and styles for the Textual status area.

Catppuccin Mocha (https://catppuccin.com/palette/) with a traffic-light load
indicator. Keep these values in one place so the status bar, the Current band
and future widgets stay consistent.
"""

from __future__ import annotations

TEXT = "#cdd6f4"
LABEL = "#7f849c"
BLUE = "#89b4fa"
GREEN = "#a6e3a1"
YELLOW = "#f9e2af"
RED = "#f38ba8"
TEAL = "#94e2d5"
MAUVE = "#cba6f7"
SEPARATOR = "#45475a"
CURRENT_BG = "#1e1e2e"

LOAD_LOW = 40
LOAD_HIGH = 70


def load_color(load: int | None) -> str:
    """Green below 40%, yellow 40-69%, red 70% and above."""
    if load is None:
        return LABEL
    if load < LOAD_LOW:
        return GREEN
    if load < LOAD_HIGH:
        return YELLOW
    return RED


def load_style(load: int | None) -> str:
    return f"bold {load_color(load)}"


def server_style() -> str:
    return f"bold {TEXT}"
