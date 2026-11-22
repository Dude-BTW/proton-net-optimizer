from __future__ import annotations

import os

from pno.tui.running_apps import list_running_executables


def _process(proc_root, pid: str, target: str) -> None:
    directory = proc_root / pid
    directory.mkdir()
    os.symlink(target, directory / "exe")


def test_lists_unique_sorted_absolute_executables(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    _process(proc, "10", "/usr/bin/zoom")
    _process(proc, "11", "/usr/bin/zoom")
    _process(proc, "12", "/usr/bin/kdeconnect-app")
    (proc / "self").mkdir()

    assert list_running_executables(proc) == (
        "/usr/bin/kdeconnect-app",
        "/usr/bin/zoom",
    )


def test_skips_deleted_and_unreadable_entries(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    _process(proc, "20", "/usr/bin/old (deleted)")
    (proc / "21").mkdir()
    (proc / "not-a-pid").mkdir()

    assert list_running_executables(proc) == ()


def test_missing_proc_root_is_empty(tmp_path):
    assert list_running_executables(tmp_path / "missing") == ()
