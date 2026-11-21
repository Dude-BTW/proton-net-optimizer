from __future__ import annotations

from pathlib import Path

import pytest

from pno import launcher
from pno.config import Config
from pno.errors import PnoError


@pytest.fixture(autouse=True)
def fake_terminal(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_kitty_command():
    command = launcher.build_terminal_command(
        Config(terminal="kitty"),
        "/venv/bin/python",
        working_directory=Path("/repo"),
    )
    assert command[:3] == ["kitty", "--class", "proton-net-optimizer"]
    assert "--title" in command
    assert "--directory" in command
    assert command[-5:] == ["-e", "/venv/bin/python", "-m", "pno", "tui"]
    assert command[-1] == "tui"


def test_konsole_command():
    command = launcher.build_terminal_command(
        Config(terminal="konsole"),
        "/venv/bin/python",
        working_directory=Path("/repo"),
    )
    assert command[:2] == ["konsole", "--title"]
    assert "--workdir" in command
    assert command[-2:] == ["pno", "tui"]


def test_generic_terminal_falls_back_to_dash_e():
    command = launcher.build_terminal_command(
        Config(terminal="xterm"),
        "/venv/bin/python",
        working_directory=Path("/repo"),
    )
    assert command[:3] == ["xterm", "-T", launcher.WINDOW_TITLE]
    assert command[-5:] == ["-e", "/venv/bin/python", "-m", "pno", "tui"]
    assert command[-1] == "tui"


def test_missing_terminal_raises(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    with pytest.raises(PnoError):
        launcher.build_terminal_command(Config(terminal="nope"), "/python")


def test_open_tui_uses_the_venv_interpreter(monkeypatch):
    recorded = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            recorded["command"] = command
            recorded["kwargs"] = kwargs

    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)
    monkeypatch.setenv("PNO_VENV_PYTHON", "/venv/bin/python")
    launcher.open_tui(Config(terminal="kitty"))
    assert recorded["command"][-5:] == ["-e", "/venv/bin/python", "-m", "pno", "tui"]
    assert recorded["kwargs"]["start_new_session"] is True
    assert "PNO_KITTY_LISTEN_ON" in recorded["kwargs"]["env"]
    assert "--listen-on" in recorded["command"]


def test_open_tui_focuses_an_existing_window(monkeypatch):
    from pno.tui import single_instance

    monkeypatch.setattr(single_instance, "focus_running_tui", lambda: True)

    def fail(*args, **kwargs):
        raise AssertionError("a second terminal must not be started")

    monkeypatch.setattr(launcher.subprocess, "Popen", fail)
    assert launcher.open_tui(Config(terminal="kitty")) is None


def test_kitty_command_can_include_a_listen_socket(tmp_path):
    command = launcher.build_terminal_command(
        Config(terminal="kitty"),
        "/venv/bin/python",
        working_directory=Path("/repo"),
        kitty_listen_on=tmp_path / "kitty.sock",
    )
    assert "--listen-on" in command
    assert command[command.index("--listen-on") + 1].startswith("unix:")


def test_auto_detects_an_installed_terminal(monkeypatch):
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda name: "/usr/bin/konsole" if name == "konsole" else None,
    )
    assert launcher.resolve_terminal(Config(terminal="")) == "konsole"


def test_configured_terminal_falls_back_to_detected(monkeypatch):
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda name: "/usr/bin/foot" if name == "foot" else None,
    )
    assert launcher.resolve_terminal(Config(terminal="missing")) == "foot"
