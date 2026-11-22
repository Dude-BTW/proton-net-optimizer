"""List executables of running processes for the split-tunneling editor."""

from __future__ import annotations

import os
from pathlib import Path


def list_running_executables(
    proc_root: str | os.PathLike[str] = "/proc",
    *,
    max_entries: int = 1000,
) -> tuple[str, ...]:
    """Return unique absolute executable paths, sorted for display.

    ``/proc/<pid>/exe`` is readable only for the current user's processes, so
    the list is naturally scoped to what the user can tunnel.
    """
    root = Path(proc_root)
    if not root.is_dir():
        return ()
    found: set[str] = set()
    try:
        entries = list(root.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            target = os.readlink(entry / "exe")
        except OSError:
            continue
        if not target.startswith("/") or target.endswith(" (deleted)"):
            continue
        found.add(target)
        if len(found) >= max_entries:
            break
    return tuple(sorted(found, key=str.casefold))
