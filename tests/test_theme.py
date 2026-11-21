from __future__ import annotations

from pno.tui import theme


def test_load_colors_follow_thresholds():
    assert theme.load_color(None) == theme.LABEL
    assert theme.load_color(0) == theme.GREEN
    assert theme.load_color(39) == theme.GREEN
    assert theme.load_color(40) == theme.YELLOW
    assert theme.load_color(69) == theme.YELLOW
    assert theme.load_color(70) == theme.RED
    assert theme.load_color(100) == theme.RED


def test_load_style_is_bold_and_coloured():
    style = theme.load_style(70)
    assert "bold" in style
    assert theme.RED in style


def test_palette_is_catppuccin_mocha():
    assert theme.CURRENT_BG == "#1e1e2e"
    assert theme.MAUVE == "#cba6f7"
    assert theme.TEAL == "#94e2d5"
