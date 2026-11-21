from __future__ import annotations

import os
import threading

from pno.tui import single_instance
from pno.tui.single_instance import TuiInstance, focus_running_tui, tui_is_running


def test_second_instance_is_rejected_and_focused(tmp_path):
    lock = tmp_path / "tui.lock"
    sock = tmp_path / "tui.sock"
    focused = threading.Event()

    def focuser() -> str:
        focused.set()
        return "test"

    first = TuiInstance(lock_path=lock, socket_path=sock, focuser=focuser)
    assert first.acquire() is True
    try:
        second = TuiInstance(lock_path=lock, socket_path=sock)
        assert second.acquire() is False
        assert focus_running_tui(lock_path=lock, socket_path=sock) is True
        assert focused.wait(2.0)
        assert tui_is_running(lock)
    finally:
        first.close()
    assert not tui_is_running(lock)
    assert not sock.exists()


def test_focus_when_nothing_is_running(tmp_path):
    assert (
        focus_running_tui(
            lock_path=tmp_path / "missing.lock",
            socket_path=tmp_path / "missing.sock",
        )
        is False
    )


def test_focus_with_dead_socket_does_not_claim_success(tmp_path, monkeypatch):
    lock = tmp_path / "tui.lock"
    sock = tmp_path / "tui.sock"
    first = TuiInstance(lock_path=lock, socket_path=sock, focuser=lambda: "test")
    assert first.acquire()
    first.close()
    # Recreate the lock as held without a server: simulate a wedged process.
    handle = open(lock, "a+", encoding="utf-8")
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    notifications = []
    monkeypatch.setattr(
        single_instance, "_notify_already_running", lambda: notifications.append(1)
    )
    try:
        assert focus_running_tui(lock_path=lock, socket_path=sock) is True
        assert notifications == [1]
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def test_kitty_focus_command(tmp_path, monkeypatch):
    listen = tmp_path / "kitty.sock"
    listen.write_text("", encoding="utf-8")
    calls = []

    monkeypatch.setattr(single_instance.shutil, "which", lambda name: "/usr/bin/kitty")
    monkeypatch.setattr(
        single_instance,
        "_run",
        lambda command, timeout=5: calls.append(command) or True,
    )
    instance = TuiInstance(kitty_listen_on=listen)
    assert instance._focus_with_kitty() is True
    assert calls[0][:3] == ["kitty", "@", "--to"]
    assert calls[0][-2:] == ["--match", f"pid:{os.getpid()}"]


def test_focuser_shortcut():
    instance = TuiInstance(focuser=lambda: "custom")
    assert instance.focus_window() == "custom"
