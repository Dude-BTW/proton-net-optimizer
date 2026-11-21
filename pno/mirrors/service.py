"""High-level mirror refresh orchestration shared by the CLI and daemon."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from pno import events
from pno.config import Config
from pno.countries import arch_code, normalize_code
from pno.errors import PnoError
from pno.mirrors.apply import apply_plan, current_mirrorlist
from pno.mirrors.models import ProbeResult
from pno.mirrors.planner import MirrorPlan, plan_mirrors
from pno.mirrors.status import MirrorStatusSnapshot, load_status
from pno.proton.serverlist import ServerCache
from pno.proton.status import VpnStatus, read_vpn_status
from pno.state import StateStore


def resolve_target(
    config: Config,
    vpn_status: VpnStatus | None,
    *,
    server_cache: ServerCache | None = None,
) -> tuple[str, str]:
    """Return ``(country_code, source)`` for the mirror target."""
    status = vpn_status
    if status is None or (status.connected and not status.exit_country):
        snapshot = (server_cache or ServerCache()).read()
        fresh = read_vpn_status(snapshot)
        if status is None or fresh.connected:
            status = fresh
    if status.connected and status.exit_country:
        return arch_code(status.exit_country), "vpn-exit"
    return normalize_code(config.home_country), "home"


def refresh_mirrors(
    config: Config,
    state: StateStore,
    *,
    vpn_status: VpnStatus | None = None,
    target_country: str | None = None,
    apply: bool = True,
    dry_run: bool = False,
    force_status: bool = False,
    status_fetcher: Callable[[], MirrorStatusSnapshot] | None = None,
    probe: Callable[..., list[ProbeResult]] | None = None,
    apply_runner: Callable[..., Any] | None = None,
    mirrorlist_path: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    timestamp = now if now is not None else time.time()
    if target_country:
        country = normalize_code(target_country)
        source = "manual"
    else:
        country, source = resolve_target(config, vpn_status)

    snapshot = load_status(
        max_age=config.mirror_status_max_age,
        force=force_status,
        fetcher=status_fetcher,
        now=timestamp,
    )
    plan_kwargs: dict[str, Any] = {
        "target_country": country,
        "target_source": source,
        "config": config,
        "now": timestamp,
    }
    if probe is not None:
        plan_kwargs["probe"] = probe
    plan: MirrorPlan = plan_mirrors(snapshot, **plan_kwargs)

    result: dict[str, Any] = {
        "target_country": country,
        "target_source": source,
        "selected": [mirror.to_dict() for mirror in plan.selected],
        "candidates": [item.to_dict() for item in plan.candidates],
        "measured_at": timestamp,
    }

    if dry_run or not apply:
        kwargs: dict[str, Any] = {
            "dry_run": True,
            "mirrorlist_path": mirrorlist_path,
        }
        apply_result = apply_plan(plan, config, **kwargs)
        apply_result.reason = "dry-run" if dry_run else "not-requested"
    else:
        apply_kwargs: dict[str, Any] = {
            "dry_run": False,
            "mirrorlist_path": mirrorlist_path,
        }
        if apply_runner is not None:
            apply_kwargs["runner"] = apply_runner
        apply_result = apply_plan(plan, config, **apply_kwargs)

    result["apply"] = apply_result.to_dict()
    result["status_age"] = snapshot.age(timestamp)

    measured = {
        "last_measure_at": timestamp,
        "last_measure_country": country,
        "last_measure_source": source,
        "last_measure_servers": [mirror.host for mirror in plan.selected],
        "last_measure_apply": apply_result.to_dict(),
        "mirror_error": None,
    }

    def record(current: dict[str, Any]) -> None:
        current.update(measured)
        if apply_result.applied or apply_result.reason == "unchanged":
            current.update(
                {
                    "last_mirror_refresh": timestamp,
                    "last_mirror_country": country,
                    "last_mirror_source": source,
                    "last_mirror_servers": [mirror.host for mirror in plan.selected],
                    "last_mirror_apply": apply_result.to_dict(),
                }
            )

    state.update(record)
    events.emit(
        "info",
        "mirrors",
        "mirrors.refresh.complete",
        {
            "country": country,
            "source": source,
            "applied": apply_result.applied,
            "changed": apply_result.changed,
            "servers": len(plan.selected),
        },
    )
    return result


def mirrors_overview(
    config: Config,
    state: StateStore,
    *,
    server_cache: ServerCache | None = None,
    mirrorlist_path: Path | None = None,
) -> dict[str, Any]:
    """Small status payload for the CLI, TUI and tray."""
    snapshot = state.read()
    target = mirrorlist_path or Path(config.mirrorlist_path)
    content = current_mirrorlist(target)
    from pno.mirrors.planner import server_lines

    lines = server_lines(content)
    vpn: VpnStatus | None = None
    try:
        cache = server_cache or ServerCache()
        vpn = read_vpn_status(cache.read())
    except PnoError:
        vpn = None
    return {
        "paused": bool(snapshot.get("paused")),
        "vpn": vpn.to_dict() if vpn else None,
        "mirrorlist_path": str(target),
        "mirrorlist_servers": len(lines),
        "mirrorlist_preview": lines,
        "last_measure_at": snapshot.get("last_measure_at"),
        "last_measure_country": snapshot.get("last_measure_country"),
        "last_measure_source": snapshot.get("last_measure_source"),
        "last_mirror_refresh": snapshot.get("last_mirror_refresh"),
        "last_mirror_country": snapshot.get("last_mirror_country"),
        "last_mirror_source": snapshot.get("last_mirror_source"),
        "last_mirror_servers": snapshot.get("last_mirror_servers"),
        "last_mirror_apply": snapshot.get("last_mirror_apply"),
        "mirror_error": snapshot.get("mirror_error"),
        "next_mirror_due": snapshot.get("next_mirror_due"),
    }
