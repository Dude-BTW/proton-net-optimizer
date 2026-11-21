from __future__ import annotations

import signal

from pno.proton.gui import GuiProcess, close_gui, find_gui_processes


def write_cmdline(root, pid, parts):
    directory = root / str(pid)
    directory.mkdir(parents=True)
    (directory / "cmdline").write_bytes(
        b"\0".join(part.encode() for part in parts) + b"\0"
    )


def test_find_gui_processes_scans_cmdlines(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    write_cmdline(root, 101, ["/usr/bin/python3", "/usr/bin/protonvpn-app"])
    write_cmdline(root, 102, ["/usr/bin/protonvpn", "status"])
    write_cmdline(root, 103, ["/usr/bin/python3", "-m", "proton.vpn.app.gtk"])
    write_cmdline(root, 104, ["/usr/bin/pno", "daemon", "run"])
    (root / "self").mkdir()
    (root / "readme").write_text("not a process", encoding="utf-8")

    found = find_gui_processes(root, own_pid=0)
    assert [process.pid for process in found] == [101, 103]


def test_find_gui_processes_missing_proc_returns_empty(tmp_path):
    assert find_gui_processes(tmp_path / "missing") == []


def test_close_gui_terminates_gracefully():
    signals: list[tuple[int, int]] = []
    alive = {1: True}

    def kill(pid, number):
        signals.append((pid, number))
        if number == signal.SIGTERM:
            alive[pid] = False

    result = close_gui(
        [GuiProcess(1, ("protonvpn-app",))],
        timeout=0.05,
        term_timeout=0.01,
        kill=kill,
        alive=lambda pid: alive.get(pid, False),
        sleep=lambda _: None,
    )
    assert result.closed is True
    assert result.terminated == (1,)
    assert signals == [(1, signal.SIGTERM)]


def test_close_gui_forces_stubborn_process():
    signals: list[tuple[int, int]] = []
    alive = {2: True}

    def kill(pid, number):
        signals.append((pid, number))
        if number == signal.SIGKILL:
            alive[pid] = False

    result = close_gui(
        [GuiProcess(2, ("protonvpn-app",))],
        timeout=0.05,
        term_timeout=0.01,
        kill=kill,
        alive=lambda pid: alive.get(pid, False),
        sleep=lambda _: None,
    )
    assert result.closed is True
    assert result.forced == (2,)
    assert [number for _, number in signals] == [signal.SIGTERM, signal.SIGKILL]


def test_close_gui_with_no_processes_is_a_noop():
    result = close_gui([])
    assert result.closed is True
    assert result.terminated == ()
