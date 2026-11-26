"""Background daemon: polls VPN state, refreshes loads and mirrors.

The daemon observes NetworkManager, keeps the server cache warm, and applies
mirror plans when the exit country changes or the schedule is due. The only
connection action it performs is the opt-in startup auto-connect: one attempt
per boot to the user's configured target, never a switch of an active tunnel.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import signal
import time
from pathlib import Path
from typing import Any, Callable

from pno import config as config_module
from pno import events, ipc, paths
from pno.autoconnect import AutoconnectResult
from pno.autoconnect import run_once as autoconnect_once
from pno.config import Config
from pno.errors import PnoError
from pno.mirrors.service import refresh_mirrors
from pno.proc import read_boot_id
from pno.proton.killswitch import CleanupResult, cleanup_stale_killswitch
from pno.proton.models import ServerSnapshot
from pno.proton.serverlist import ServerCache
from pno.proton.status import VpnStatus, read_vpn_status
from pno.requirements import check_requirements
from pno.state import StateStore

MIRROR_CHECK_INTERVAL = 60.0
RESUME_GRACE_SECONDS = 60.0
FAILURE_RETRY_SECONDS = 900.0
AUTOCONNECT_RETRY_DELAYS = (5.0, 10.0, 20.0, 30.0, 30.0, 25.0)


class Daemon:
    def __init__(
        self,
        config: Config,
        *,
        state: StateStore | None = None,
        server_cache: ServerCache | None = None,
        vpn_reader: Callable[[ServerSnapshot | None], VpnStatus] = read_vpn_status,
        mirror_refresher: Callable[..., dict[str, Any]] = refresh_mirrors,
        killswitch_cleaner: Callable[..., CleanupResult] = cleanup_stale_killswitch,
        autoconnect_runner: Callable[..., AutoconnectResult] = autoconnect_once,
        boot_id: str | None = None,
        autoconnect_delays: tuple[float, ...] = AUTOCONNECT_RETRY_DELAYS,
        socket_path: Path | None = None,
    ) -> None:
        self.config = config
        self.state = state or StateStore(paths.state_file())
        self.server_cache = server_cache or ServerCache(
            helper_timeout=config.proton_helper_timeout
        )
        self.vpn_reader = vpn_reader
        self.mirror_refresher = mirror_refresher
        self.killswitch_cleaner = killswitch_cleaner
        self.autoconnect_runner = autoconnect_runner
        self.boot_id = read_boot_id() if boot_id is None else boot_id
        self.autoconnect_delays = autoconnect_delays
        self.socket_path = socket_path or paths.socket_path()

        self.servers: ServerSnapshot | None = None
        self.vpn_status: VpnStatus | None = None
        self.started_at = time.time()
        self.stopping = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._server: asyncio.Server | None = None
        self._servers_lock = asyncio.Lock()
        self._mirror_lock = asyncio.Lock()
        self._pending_mirror: asyncio.Task[Any] | None = None
        self._disconnected_since: float | None = None
        self._next_killswitch_attempt = 0.0

    # ------------------------------------------------------------------ state
    @property
    def paused(self) -> bool:
        return bool(self.state.read().get("paused"))

    def _initial_state(self) -> None:
        now = time.time()

        def mutator(current: dict[str, Any]) -> None:
            current.setdefault("paused", False)
            current["daemon_pid"] = os.getpid()
            current["daemon_started_at"] = now
            current["daemon_running"] = True
            current["schema"] = 1
            due = current.get("next_mirror_due")
            if due is None:
                current["next_mirror_due"] = (
                    now + self.config.mirror_interval_hours * 3600
                )

        self.state.update(mutator)

    def status_payload(self) -> dict[str, Any]:
        state = self.state.read()
        servers = self.servers
        server_payload: dict[str, Any] | None = None
        if servers is not None:
            server_payload = {
                "count": len(servers.servers),
                "fetched_at": servers.fetched_at,
                "age": round(servers.age(), 1),
                "source": servers.source,
                "stale": servers.stale,
                "max_tier": servers.max_tier,
            }
        return {
            "daemon": {
                "running": True,
                "pid": os.getpid(),
                "started_at": self.started_at,
                "uptime": round(time.time() - self.started_at, 1),
            },
            "paused": bool(state.get("paused")),
            "paused_at": state.get("paused_at"),
            "autoconnect": {
                "enabled": self.config.autoconnect_enabled,
                "target": self.config.autoconnect_target or None,
                "attempted_this_boot": bool(
                    self.boot_id
                    and state.get("autoconnect_boot_id") == self.boot_id
                    and state.get("autoconnect_attempted_at")
                ),
                "attempted_at": state.get("autoconnect_attempted_at"),
                "result": state.get("autoconnect_result"),
                "reason": state.get("autoconnect_reason"),
                "server": state.get("autoconnect_server"),
                "error": state.get("autoconnect_error"),
            },
            "vpn": self.vpn_status.to_dict() if self.vpn_status else state.get("vpn"),
            "servers": server_payload or state.get("servers"),
            "last_mirror_refresh": state.get("last_mirror_refresh"),
            "last_mirror_country": state.get("last_mirror_country"),
            "last_mirror_source": state.get("last_mirror_source"),
            "last_mirror_servers": state.get("last_mirror_servers"),
            "last_mirror_apply": state.get("last_mirror_apply"),
            "next_mirror_due": state.get("next_mirror_due"),
            "mirror_error": state.get("mirror_error"),
            "servers_error": state.get("servers_error"),
            "updated_at": state.get("updated_at"),
        }

    # --------------------------------------------------------------- refresh
    async def refresh_servers(self, *, force: bool = False) -> dict[str, Any]:
        async with self._servers_lock:
            try:
                snapshot = await asyncio.to_thread(
                    self.server_cache.get,
                    force=force,
                    max_age=self.config.servers_max_age,
                )
            except PnoError as exc:
                events.emit(
                    "error", "proton", "serverlist.refresh.failed", {}, error=exc
                )
                error_payload = exc.to_dict()
                error_at = time.time()
                self.state.update(
                    lambda current: current.update(
                        {"servers_error": error_payload, "servers_error_at": error_at}
                    )
                )
                raise
            self.servers = snapshot
            self.state.update(
                lambda current: current.update(
                    {
                        "servers_updated_at": snapshot.fetched_at,
                        "servers_source": snapshot.source,
                        "servers_stale": snapshot.stale,
                        "servers_count": len(snapshot.servers),
                        "servers_error": None,
                    }
                )
            )
            events.emit(
                "info",
                "proton",
                "serverlist.refresh.done",
                {
                    "servers": len(snapshot.servers),
                    "source": snapshot.source,
                    "stale": snapshot.stale,
                    "age": round(snapshot.age(), 1),
                },
            )
            return {
                "count": len(snapshot.servers),
                "source": snapshot.source,
                "stale": snapshot.stale,
                "age": round(snapshot.age(), 1),
                "max_tier": snapshot.max_tier,
            }

    async def refresh_mirrors_now(
        self,
        *,
        apply: bool = True,
        dry_run: bool = False,
        country: str | None = None,
        force_status: bool = False,
        trigger: str = "manual",
    ) -> dict[str, Any]:
        async with self._mirror_lock:
            try:
                result = await asyncio.to_thread(
                    self.mirror_refresher,
                    self.config,
                    self.state,
                    vpn_status=self.vpn_status,
                    target_country=country,
                    apply=apply,
                    dry_run=dry_run,
                    force_status=force_status,
                )
            except PnoError as exc:
                events.emit(
                    "error",
                    "mirrors",
                    "mirrors.refresh.failed",
                    {"trigger": trigger},
                    error=exc,
                )
                error_payload = exc.to_dict()
                error_at = time.time()
                self.state.update(
                    lambda current: current.update(
                        {"mirror_error": error_payload, "mirror_error_at": error_at}
                    )
                )
                self._schedule_next_mirror(base=time.time() + FAILURE_RETRY_SECONDS)
                raise
            reason = (result.get("apply") or {}).get("reason")
            if reason in {"applied", "unchanged"}:
                self._schedule_next_mirror()
            events.emit(
                "info",
                "mirrors",
                "mirrors.refresh.triggered",
                {"trigger": trigger, "applied": result.get("apply", {}).get("applied")},
            )
            return result

    def _schedule_next_mirror(self, *, base: float | None = None) -> None:
        interval = self.config.mirror_interval_hours * 3600
        jitter = random.uniform(0.9, 1.1)
        due = (base if base is not None else time.time()) + interval * jitter
        self.state.update(lambda current: current.update({"next_mirror_due": due}))
        events.emit(
            "info",
            "mirrors",
            "mirrors.schedule.next",
            {"due_in": round(due - time.time())},
        )

    def _schedule_mirror_refresh(self, reason: str) -> None:
        if self._pending_mirror is not None and not self._pending_mirror.done():
            self._pending_mirror.cancel()
        self._pending_mirror = asyncio.create_task(
            self._debounced_mirror_refresh(reason)
        )

    async def _debounced_mirror_refresh(self, reason: str) -> None:
        try:
            await asyncio.sleep(self.config.mirror_debounce_seconds)
        except asyncio.CancelledError:
            return
        if self.stopping.is_set() or self.paused:
            return
        if not self.config.auto_mirror:
            return
        try:
            await self.refresh_mirrors_now(trigger=reason)
        except PnoError:
            pass
        except Exception as exc:  # noqa: BLE001 - the daemon must stay alive
            events.emit("error", "mirrors", "mirrors.debounce.crashed", {}, error=exc)

    # ------------------------------------------------------------ autoconnect
    def _autoconnect_attempted_for_boot(self) -> bool:
        if not self.boot_id:
            return False
        state = self.state.read()
        return state.get("autoconnect_boot_id") == self.boot_id and bool(
            state.get("autoconnect_attempted_at")
        )

    def _manual_disconnect_in_this_boot(self) -> bool:
        if not self.boot_id:
            return False
        state = self.state.read()
        return state.get("manual_disconnect_boot_id") == self.boot_id and bool(
            state.get("manual_disconnect_at")
        )

    def _record_autoconnect(self, result: AutoconnectResult) -> None:
        self.state.update(
            lambda current: current.update(
                {
                    "autoconnect_boot_id": self.boot_id,
                    "autoconnect_attempted_at": time.time(),
                    "autoconnect_result": result.status,
                    "autoconnect_reason": result.reason,
                    "autoconnect_server": result.server,
                    "autoconnect_error": result.error,
                }
            )
        )

    async def _autoconnect_loop(self) -> None:
        """Try the configured target once per boot, with bounded retries."""
        target = self.config.autoconnect_target or None
        if self.paused:
            events.emit(
                "info",
                "vpn",
                "autoconnect.skipped",
                {"trigger": "startup", "code": "paused", "target": target},
            )
            return
        if not self.config.autoconnect_enabled:
            events.emit(
                "info",
                "vpn",
                "autoconnect.skipped",
                {"trigger": "startup", "code": "disabled", "target": target},
            )
            return
        if self._autoconnect_attempted_for_boot():
            events.emit(
                "info",
                "vpn",
                "autoconnect.skipped",
                {"trigger": "startup", "code": "already-attempted", "target": target},
            )
            return
        if self._manual_disconnect_in_this_boot():
            events.emit(
                "info",
                "vpn",
                "autoconnect.skipped",
                {"trigger": "startup", "code": "manual-disconnect", "target": target},
            )
            return

        last: AutoconnectResult | None = None
        for delay in self.autoconnect_delays:
            await self._sleep(delay)
            if self.stopping.is_set():
                return
            if self.paused:
                events.emit(
                    "info",
                    "vpn",
                    "autoconnect.skipped",
                    {"trigger": "startup", "code": "paused", "target": target},
                )
                return
            if self._manual_disconnect_in_this_boot():
                events.emit(
                    "info",
                    "vpn",
                    "autoconnect.skipped",
                    {
                        "trigger": "startup",
                        "code": "manual-disconnect",
                        "target": target,
                    },
                )
                return
            if self.servers is None:
                try:
                    await self.refresh_servers(force=False)
                except PnoError:
                    pass
                if self.servers is None:
                    continue
            last = await asyncio.to_thread(
                self.autoconnect_runner,
                self.config,
                reason="startup",
                snapshot=self.servers,
            )
            if last.status in {"connected", "failed"}:
                self._record_autoconnect(last)
            if last.status in {"connected", "skipped"}:
                return
        events.emit(
            "warning",
            "vpn",
            "autoconnect.gave-up",
            {
                "target": target,
                "reason": last.reason if last else "no-data",
                "error": last.error if last else None,
            },
        )

    # ----------------------------------------------------------------- loops
    async def _vpn_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                status = await asyncio.to_thread(self.vpn_reader, self.servers)
            except PnoError as exc:
                events.emit("warning", "vpn", "vpn.status.failed", {}, error=exc)
                status = None
            except Exception as exc:  # noqa: BLE001
                events.emit("error", "vpn", "vpn.status.crashed", {}, error=exc)
                status = None

            if status is not None:
                previous = self.vpn_status
                self.vpn_status = status
                self.state.update(
                    lambda current: current.update(
                        {"vpn": status.to_dict(), "vpn_checked_at": status.checked_at}
                    )
                )
                changed = previous is None or (
                    previous.connected != status.connected
                    or previous.server_name != status.server_name
                    or previous.exit_country != status.exit_country
                )
                if changed:
                    events.emit(
                        "info",
                        "vpn",
                        "vpn.state.changed",
                        {
                            "connected": status.connected,
                            "server": status.server_name,
                            "exit": status.exit_country,
                            "source": status.source,
                        },
                    )
                    self._maybe_trigger_on_vpn_change(previous, status)
                await self._maybe_clean_stale_killswitch(status)
            await self._sleep(self.config.poll_interval)

    async def _maybe_clean_stale_killswitch(
        self,
        status: VpnStatus,
        *,
        now: float | None = None,
    ) -> None:
        """Remove leftover transient kill-switch profiles after a VPN drop."""
        moment = time.monotonic() if now is None else now
        if status.connected:
            self._disconnected_since = None
            self._next_killswitch_attempt = 0.0
            return
        if self.paused or not self.config.auto_clean_stale_killswitch:
            return
        if self._disconnected_since is None:
            self._disconnected_since = moment
        if moment - self._disconnected_since < self.config.killswitch_cleanup_delay:
            return
        if moment < self._next_killswitch_attempt:
            return
        try:
            result = await asyncio.to_thread(self.killswitch_cleaner, force=False)
        except PnoError as exc:
            self._next_killswitch_attempt = moment + 300.0
            events.emit(
                "warning", "vpn", "vpn.killswitch.cleanup.failed", {}, error=exc
            )
            error_payload = exc.to_dict()
            self.state.update(
                lambda current: current.update(
                    {
                        "killswitch_last_error": error_payload,
                        "killswitch_last_error_at": time.time(),
                    }
                )
            )
            return
        except Exception as exc:  # noqa: BLE001 - the daemon must stay alive
            self._next_killswitch_attempt = moment + 300.0
            events.emit("error", "vpn", "vpn.killswitch.cleanup.crashed", {}, error=exc)
            return

        # Do not inspect NetworkManager on every poll when there is nothing to
        # remove; the delay after a disconnect is handled above.
        self._next_killswitch_attempt = moment + 30.0
        if not result.changed:
            return
        events.emit(
            "info",
            "vpn",
            "vpn.killswitch.auto-cleaned",
            {
                "removed": list(result.removed),
                "setting": result.report.setting,
            },
        )
        self.state.update(
            lambda current: current.update(
                {
                    "killswitch_last_cleanup_at": time.time(),
                    "killswitch_removed": list(result.removed),
                    "killswitch_last_error": None,
                }
            )
        )

    def _maybe_trigger_on_vpn_change(
        self, previous: VpnStatus | None, current: VpnStatus
    ) -> None:
        if not self.config.auto_mirror or not self.config.auto_mirror_on_vpn_change:
            return
        if self.paused or not current.connected or not current.exit_country:
            return
        previous_exit = previous.exit_country if previous else None
        if (
            previous is not None
            and previous.connected
            and previous_exit == current.exit_country
        ):
            return
        last_country = self.state.read().get("last_mirror_country")
        if previous is None and last_country == current.exit_country:
            return
        events.emit(
            "info",
            "mirrors",
            "mirrors.trigger.vpn-change",
            {"server": current.server_name, "exit": current.exit_country},
        )
        self._schedule_mirror_refresh("vpn-change")

    async def _loads_loop(self) -> None:
        while not self.stopping.is_set():
            await self._sleep(self.config.loads_interval)
            if self.stopping.is_set():
                return
            if self.paused:
                continue
            try:
                await self.refresh_servers(force=False)
            except PnoError:
                pass
            except Exception as exc:  # noqa: BLE001
                events.emit("error", "proton", "serverlist.loop.crashed", {}, error=exc)

    async def _mirror_loop(self) -> None:
        while not self.stopping.is_set():
            await self._sleep(MIRROR_CHECK_INTERVAL)
            if self.stopping.is_set():
                return
            if self.paused or not self.config.auto_mirror:
                continue
            due = self.state.read().get("next_mirror_due")
            if not isinstance(due, (int, float)) or time.time() < due:
                continue
            try:
                await self.refresh_mirrors_now(trigger="schedule")
            except PnoError:
                pass
            except Exception as exc:  # noqa: BLE001
                events.emit("error", "mirrors", "mirrors.loop.crashed", {}, error=exc)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stopping.wait(), timeout=max(0.1, seconds))

    # ------------------------------------------------------------------- IPC
    async def _handle(self, command: str, args: dict[str, Any]) -> Any:
        if command == "ping":
            return {
                "pong": True,
                "pid": os.getpid(),
                "started_at": self.started_at,
                "paused": self.paused,
            }
        if command == "status":
            return self.status_payload()
        if command == "servers.refresh":
            return await self.refresh_servers(force=bool(args.get("force")))
        if command == "servers.snapshot":
            if self.servers is None:
                await self.refresh_servers(force=False)
            return self.servers.to_dict() if self.servers else None
        if command == "mirrors.refresh":
            return await self.refresh_mirrors_now(
                apply=bool(args.get("apply", True)),
                dry_run=bool(args.get("dry_run", False)),
                country=args.get("country"),
                force_status=bool(args.get("force_status", False)),
                trigger=str(args.get("trigger") or "ipc"),
            )
        if command == "pause":
            self._pause()
            return {"paused": True}
        if command == "resume":
            self._resume()
            return {"paused": False}
        if command == "config.reload":
            self.config = config_module.load()
            events.configure_from_config(self.config)
            events.emit(
                "info",
                "daemon",
                "config.reloaded",
                {"auto_mirror": self.config.auto_mirror},
            )
            return {"reloaded": True}
        if command == "shutdown":
            events.emit("info", "daemon", "daemon.shutdown.requested")
            self.stopping.set()
            return {"stopping": True}
        from pno.errors import ProtocolError

        raise ProtocolError("unknown client command", detail=command)

    def _pause(self) -> None:
        now = time.time()
        self.state.update(
            lambda current: current.update({"paused": True, "paused_at": now})
        )
        if self._pending_mirror is not None:
            self._pending_mirror.cancel()
        events.emit("info", "daemon", "automation.paused")

    def _resume(self) -> None:
        now = time.time()
        self.state.update(
            lambda current: current.update(
                {
                    "paused": False,
                    "paused_at": None,
                    "next_mirror_due": min(
                        current.get("next_mirror_due") or (now + RESUME_GRACE_SECONDS),
                        now + RESUME_GRACE_SECONDS,
                    ),
                }
            )
        )
        events.emit("info", "daemon", "automation.resumed")

    # ----------------------------------------------------------------- start
    async def run(self) -> None:
        self._initial_state()
        try:
            report = check_requirements(self.config)
        except Exception as exc:  # noqa: BLE001 - the daemon must still run
            events.emit("error", "requirements", "requirements.crashed", {}, error=exc)
        else:
            events.emit(
                "info",
                "requirements",
                "requirements.check",
                report.to_dict(),
            )
            for check in report.failures:
                events.emit(
                    "warning",
                    "requirements",
                    "requirements.failed",
                    check.to_dict(),
                )
        events.emit(
            "info",
            "daemon",
            "daemon.start",
            {"pid": os.getpid(), "paused": self.paused},
        )
        try:
            await self.refresh_servers(force=False)
        except PnoError:
            pass
        try:
            vpn_status = await asyncio.to_thread(self.vpn_reader, self.servers)
            self.vpn_status = vpn_status
            self.state.update(
                lambda current: current.update({"vpn": vpn_status.to_dict()})
            )
        except PnoError as exc:
            events.emit("warning", "vpn", "vpn.status.failed", {}, error=exc)

        self._server = await ipc.start_server(
            self._handle, socket_path=self.socket_path
        )
        events.emit(
            "info", "daemon", "daemon.ipc.ready", {"socket": str(self.socket_path)}
        )

        self._tasks = [
            asyncio.create_task(self._vpn_loop(), name="pno-vpn-loop"),
            asyncio.create_task(self._loads_loop(), name="pno-loads-loop"),
            asyncio.create_task(self._mirror_loop(), name="pno-mirror-loop"),
            asyncio.create_task(self._autoconnect_loop(), name="pno-autoconnect"),
        ]
        try:
            await self.stopping.wait()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        events.emit("info", "daemon", "daemon.stopping")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._pending_mirror is not None:
            self._pending_mirror.cancel()
        if self._server is not None:
            ipc.stop_server(self._server, socket_path=self.socket_path)
        self.state.update(
            lambda current: current.update(
                {
                    "daemon_running": False,
                    "daemon_stopped_at": time.time(),
                    "daemon_pid": None,
                }
            )
        )
        events.emit("info", "daemon", "daemon.stopped")


async def _run(config: Config, *, verbose: bool = False) -> None:
    events.configure_from_config(config, console=verbose)
    daemon = Daemon(config)
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        daemon.stopping.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_stop)
    await daemon.run()


def run_daemon(*, verbose: bool = False) -> None:
    config = config_module.ensure()
    asyncio.run(_run(config, verbose=verbose))
