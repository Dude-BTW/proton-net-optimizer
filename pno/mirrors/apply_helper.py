#!/usr/bin/python3
"""Root-side helper that installs a validated mirrorlist.

This file is copied to ``/usr/local/lib/pno/pno-mirror-apply`` by
``pno install`` and executed through a ``sudoers`` NOPASSWD entry. It is
deliberately self-contained (standard library only): a root process must not
import modules the unprivileged user can edit.

Contract:
  * stdin: the complete mirrorlist text
  * stdout: exactly one JSON document
  * exit code 0 on success, 2 on validation failure, 3 on permission failure,
    4 on write/sync failure
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MAX_BYTES = 128 * 1024
MAX_SERVERS = 100
SERVER_RE = re.compile(
    r"^Server = (https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+/\$repo/os/\$arch)$"
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
LOCK_PATH = Path("/var/lib/pacman/db.lck")


def validate(text: str) -> tuple[list[str], str | None]:
    if not text.strip():
        return [], "mirrorlist is empty"
    if len(text.encode("utf-8")) > MAX_BYTES:
        return [], f"mirrorlist exceeds {MAX_BYTES} bytes"
    if CONTROL_RE.search(text):
        return [], "mirrorlist contains control characters"
    servers: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = SERVER_RE.match(line)
        if not match:
            return [], f"invalid line {number}: {line[:120]}"
        servers.append(match.group(1))
    if not servers:
        return [], "mirrorlist contains no Server entries"
    if len(servers) > MAX_SERVERS:
        return [], f"too many servers ({len(servers)})"
    if len(set(servers)) != len(servers):
        return [], "duplicate Server entries"
    return servers, None


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.pno.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def backup(path: Path, backup_dir: Path) -> str | None:
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = backup_dir / f"mirrorlist-{stamp}-{os.getpid()}.bak"
    shutil.copy2(path, target)
    try:
        os.chmod(target, 0o644)
    except OSError:
        pass
    old = sorted(backup_dir.glob("mirrorlist-*.bak"))
    for stale in old[:-5]:
        try:
            stale.unlink()
        except OSError:
            pass
    return str(target)


def pacman_sync() -> dict:
    if LOCK_PATH.exists():
        return {"ok": False, "error": "pacman database is locked"}
    try:
        completed = subprocess.run(
            ["pacman", "-Sy", "--noconfirm"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stderr": (completed.stderr or "")[-500:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--path", default="/etc/pacman.d/mirrorlist")
    parser.add_argument("--backup-dir", default="/var/lib/pno")
    parser.add_argument("--pacman-sync", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid utf-8: {exc}"}))
        return 2

    servers, error = validate(text)
    if error:
        print(json.dumps({"ok": False, "error": error}))
        return 2

    target = Path(args.path)
    result: dict = {
        "ok": True,
        "servers": len(servers),
        "hosts": [re.sub(r"^https?://([^/]+).*$", r"\1", item) for item in servers],
        "path": str(target),
    }
    if args.dry_run:
        result["dry_run"] = True
        print(json.dumps(result))
        return 0

    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "must run as root"}))
        return 3

    try:
        result["backup"] = backup(target, Path(args.backup_dir))
        write_atomic(target, text)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"write failed: {exc}"}))
        return 4

    if args.pacman_sync:
        result["pacman_sync"] = pacman_sync()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
