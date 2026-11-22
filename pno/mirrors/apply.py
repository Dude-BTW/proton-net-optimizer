"""Apply a mirror plan through the privileged helper."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pno import events, paths
from pno.config import Config
from pno.errors import ApplyError, PrivilegeError
from pno.mirrors.planner import MirrorPlan, same_server_set
from pno.proc import CommandResult, run
from pno.state import exclusive_lock

HELPER_LOCATIONS = (
    Path("/usr/local/lib/pno/pno-mirror-apply"),
    Path("/usr/local/bin/pno-mirror-apply"),
)
BACKUP_DIR = Path("/var/lib/pno")


@dataclass
class ApplyResult:
    applied: bool
    changed: bool
    servers: int = 0
    backup: str | None = None
    pacman_sync: dict[str, Any] | None = None
    reason: str = ""
    preview: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "changed": self.changed,
            "servers": self.servers,
            "backup": self.backup,
            "pacman_sync": self.pacman_sync,
            "reason": self.reason,
            "preview": self.preview,
        }


def current_mirrorlist(path: Path | None = None) -> str:
    target = path or Path(Config().mirrorlist_path)
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ApplyError(f"cannot read {target}", detail=str(exc)) from exc


def find_helper() -> Path | None:
    """Return the root-owned helper, never a user-writable copy from PATH."""
    for candidate in HELPER_LOCATIONS:
        if candidate.is_file():
            return candidate
    return None


def _run_helper(
    helper: Path,
    content: str,
    *,
    mirrorlist_path: Path,
    pacman_sync: bool,
    runner: Callable[..., CommandResult],
) -> dict[str, Any]:
    if runner is run and shutil.which("sudo") is None:
        raise PrivilegeError(
            "sudo is not installed",
            hint="Install sudo, or run the mirror helper manually as root.",
        )
    command = [
        "sudo",
        "-n",
        str(helper),
        "--path",
        str(mirrorlist_path),
        "--backup-dir",
        str(BACKUP_DIR),
    ]
    if pacman_sync:
        command.append("--pacman-sync")
    result = runner(
        command,
        timeout=240,
        input_text=content,
        error_factory=ApplyError,
        error_detail="pno-mirror-apply",
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "password is required" in stderr or "sudo:" in stderr:
            raise PrivilegeError(
                "sudo refused the mirror helper without a password",
                detail=stderr[-500:],
                hint=(
                    "Run 'pno install' to create /etc/sudoers.d/pno-mirror, "
                    "or run the command manually with sudo."
                ),
            )
        payload = _parse_payload(result.stdout)
        detail = payload.get("error") if payload else stderr[-500:]
        raise ApplyError(
            f"mirror helper exited with {result.returncode}",
            detail=str(detail),
        )
    payload = _parse_payload(result.stdout)
    if payload is None:
        raise ApplyError("mirror helper produced no JSON", detail=result.stdout[-500:])
    if not payload.get("ok"):
        raise ApplyError("mirror helper rejected the mirrorlist", detail=str(payload))
    return payload


def _parse_payload(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def record_history(payload: dict[str, Any], *, path: Path | None = None) -> None:
    target = path or paths.mirror_history_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with exclusive_lock(target.with_name(f"{target.name}.lock")):
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()


def apply_plan(
    plan: MirrorPlan,
    config: Config,
    *,
    dry_run: bool = False,
    runner: Callable[..., CommandResult] = run,
    mirrorlist_path: Path | None = None,
    helper: Path | None = None,
    record: bool = True,
) -> ApplyResult:
    target = mirrorlist_path or Path(config.mirrorlist_path)
    current = current_mirrorlist(target)
    if not same_server_set(current, plan.content):
        changed = True
    else:
        changed = False

    if dry_run:
        return ApplyResult(
            applied=False,
            changed=changed,
            servers=len(plan.selected),
            reason="dry-run",
            preview=plan.server_lines,
        )
    if not changed:
        result = ApplyResult(
            applied=False,
            changed=False,
            servers=len(plan.selected),
            reason="unchanged",
        )
        events.emit(
            "info",
            "mirrors",
            "mirrors.apply.unchanged",
            {"country": plan.target_country, "servers": len(plan.selected)},
        )
        return result

    helper_path = helper or find_helper()
    if helper_path is None or not helper_path.is_file():
        raise PrivilegeError(
            "the privileged mirror helper is not installed",
            hint="Run 'pno install' first (it installs /usr/local/lib/pno/pno-mirror-apply).",
        )

    payload = _run_helper(
        helper_path,
        plan.content,
        mirrorlist_path=target,
        pacman_sync=config.mirror_pacman_sync,
        runner=runner,
    )
    result = ApplyResult(
        applied=True,
        changed=True,
        servers=int(payload.get("servers") or len(plan.selected)),
        backup=payload.get("backup"),
        pacman_sync=payload.get("pacman_sync"),
        reason="applied",
    )
    events.emit(
        "info",
        "mirrors",
        "mirrors.apply.done",
        {
            "country": plan.target_country,
            "servers": result.servers,
            "backup": result.backup,
            "pacman_sync": bool(result.pacman_sync and result.pacman_sync.get("ok")),
        },
    )
    if record:
        record_history(
            {
                "ts": time.time(),
                "event": "mirrors.applied",
                "country": plan.target_country,
                "source": plan.target_source,
                "servers": [mirror.host for mirror in plan.selected],
                "malformed": False,
                "backup": result.backup,
            }
        )
    return result
