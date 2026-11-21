"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from pno import __version__
from pno import config as config_module
from pno import events
from pno import inventory as inventory_module
from pno import paths
from pno.config import Config
from pno.errors import PnoError
from pno.ipc import request
from pno.mirrors.service import mirrors_overview, refresh_mirrors
from pno.proton.selection import (
    regular_countries,
    secure_core_countries,
    secure_core_routes,
)
from pno.proton.serverlist import ServerCache
from pno.proton.status import read_vpn_status
from pno.state import StateStore


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable output",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pno",
        description=(
            "Suggest Proton VPN servers by measured load and keep Arch Linux "
            "mirrors matched to the current VPN exit country."
        ),
    )
    parser.add_argument("--version", action="version", version=f"pno {__version__}")
    _add_json_flag(parser)
    parser.add_argument("--verbose", action="store_true", help="log to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check the environment")
    _add_json_flag(doctor)

    servers = sub.add_parser("servers", help="show least-loaded servers")
    _add_json_flag(servers)
    servers.add_argument("--top", type=int, default=None, help="servers per country")
    servers.add_argument(
        "--country",
        action="append",
        default=None,
        help="limit to a country code (repeatable or comma-separated)",
    )
    servers.add_argument(
        "--secure-core", action="store_true", help="show Secure Core servers"
    )
    servers.add_argument(
        "--routes",
        action="store_true",
        help="with --secure-core: list every route instead of country aggregates",
    )
    servers.add_argument(
        "--inventory",
        action="store_true",
        help="limit to countries/routes listed in the inventory",
    )
    servers.add_argument(
        "--refresh", action="store_true", help="force a fresh Proton fetch"
    )

    connect = sub.add_parser("connect", help="connect to a suggested server")
    _add_json_flag(connect)
    connect.add_argument("server", help="logical server name, e.g. CH-US#3")
    connect.add_argument("--force", action="store_true", help="skip name validation")
    connect.add_argument(
        "--close-gui",
        action="store_true",
        help="close a running Proton VPN GUI and disconnect before connecting",
    )

    disconnect = sub.add_parser("disconnect", help="disconnect from Proton VPN")
    _add_json_flag(disconnect)
    disconnect.add_argument(
        "--close-gui",
        action="store_true",
        help="close a running Proton VPN GUI if it holds the connection",
    )

    mirrors = sub.add_parser("mirrors", help="inspect or refresh Arch mirrors")
    _add_json_flag(mirrors)
    mirror_sub = mirrors.add_subparsers(dest="mirrors_command", required=True)
    mirror_status = mirror_sub.add_parser("status", help="show mirror state")
    _add_json_flag(mirror_status)
    mirror_preview = mirror_sub.add_parser(
        "preview", help="measure and show the next mirrorlist without applying it"
    )
    _add_json_flag(mirror_preview)
    _add_mirror_options(mirror_preview)
    mirror_refresh = mirror_sub.add_parser(
        "refresh", help="measure and apply a new mirrorlist"
    )
    _add_json_flag(mirror_refresh)
    _add_mirror_options(mirror_refresh)
    mirror_refresh.add_argument(
        "--no-apply", action="store_true", help="measure only, leave the file untouched"
    )

    daemon = sub.add_parser("daemon", help="control the background daemon")
    _add_json_flag(daemon)
    daemon_sub = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_run = daemon_sub.add_parser("run", help="run in the foreground")
    daemon_run.add_argument("--verbose", action="store_true")
    for name, help_text in (
        ("status", "show daemon state"),
        ("pause", "pause automatic refreshes"),
        ("resume", "resume automatic refreshes"),
        ("reload", "reload the configuration"),
        ("shutdown", "stop the daemon"),
    ):
        item = daemon_sub.add_parser(name, help=help_text)
        _add_json_flag(item)

    tui = sub.add_parser("tui", help="open the Textual interface")
    _add_json_flag(tui)

    launch = sub.add_parser(
        "launch", help="open the TUI in the configured terminal (desktop entry)"
    )
    _add_json_flag(launch)

    tray = sub.add_parser("tray", help="run the tray icon (normally autostarted)")
    _add_json_flag(tray)
    tray.add_argument("--system-python", action="store_true", help=argparse.SUPPRESS)

    logs = sub.add_parser("logs", help="query the machine-readable event log")
    _add_json_flag(logs)
    logs.add_argument("paths", nargs="*")
    logs.add_argument("--level")
    logs.add_argument("--event")
    logs.add_argument("--component")
    logs.add_argument("--since")
    logs.add_argument("--until")
    logs.add_argument("--grep")
    logs.add_argument("--limit", type=int, default=200)
    logs.add_argument("--summary", action="store_true")

    install = sub.add_parser("install", help="install the daemon, tray and sudo helper")
    _add_json_flag(install)
    install.add_argument("--yes", action="store_true", help="assume yes for prompts")
    install.add_argument("--no-sudo", action="store_true", help="skip the sudo helper")
    install.add_argument(
        "--keep-reflector", action="store_true", help="do not disable reflector.timer"
    )
    install.add_argument(
        "--no-start", action="store_true", help="do not start the daemon"
    )

    uninstall = sub.add_parser("uninstall", help="remove service files and the helper")
    _add_json_flag(uninstall)
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument(
        "--purge", action="store_true", help="also delete config/state"
    )
    uninstall.add_argument(
        "--enable-reflector",
        action="store_true",
        help="re-enable reflector.timer after removing pno",
    )

    return parser


def _add_mirror_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--country", help="override the target country code")
    parser.add_argument(
        "--force-status", action="store_true", help="ignore the mirror status cache"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="validate without writing the file"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    try:
        return _dispatch(args)
    except PnoError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False))
        else:
            print(f"pno: {exc.message}", file=sys.stderr)
            if exc.detail:
                print(f"  detail: {exc.detail}", file=sys.stderr)
            if exc.hint:
                print(f"  hint: {exc.hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("pno: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort, still reported
        events.emit(
            "error",
            "cli",
            "cli.crashed",
            {"command": getattr(args, "command", "")},
            error=exc,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "type": type(exc).__name__,
                            "code": "unexpected",
                            "message": str(exc),
                        },
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(
                f"pno: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr
            )
            if getattr(args, "verbose", False):
                import traceback

                traceback.print_exc()
        return 1


def _load_config(args: argparse.Namespace) -> Config:
    return config_module.ensure()


def _configure_logging(config: Config, args: argparse.Namespace) -> None:
    events.configure(
        paths.logs_dir(),
        level=config.log_level,
        console=bool(getattr(args, "verbose", False)),
    )


def _dispatch(args: argparse.Namespace) -> int:
    command = args.command
    if command == "install":
        return _cmd_install(args)
    if command == "uninstall":
        return _cmd_uninstall(args)
    if command == "logs":
        return _cmd_logs(args)

    config = _load_config(args)
    _configure_logging(config, args)
    events.emit(
        "info",
        "cli",
        "cli.command",
        {"command": command, "sub": getattr(args, f"{command}_command", None)},
    )

    if command == "doctor":
        return _cmd_doctor(args, config)
    if command == "servers":
        return _cmd_servers(args, config)
    if command == "connect":
        return _cmd_connect(args, config)
    if command == "disconnect":
        return _cmd_disconnect(args, config)
    if command == "mirrors":
        return _cmd_mirrors(args, config)
    if command == "daemon":
        return _cmd_daemon(args, config)
    if command == "tui":
        return _cmd_tui(args)
    if command == "launch":
        return _cmd_launch(args)
    if command == "tray":
        return _cmd_tray(args)
    raise PnoError("unknown command", detail=command)


def _print(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(payload)


# --------------------------------------------------------------------- doctor
def _cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    from pno.doctor import run_checks, worst_status

    checks = run_checks(config)
    if args.json:
        print(
            json.dumps(
                {"ok": worst_status(checks) != "fail", "checks": checks},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
        for check in checks:
            print(
                f"{marks.get(check['status'], '?'):5} {check['name']:<18} {check['detail']}"
            )
            if check.get("hint"):
                print(f"      hint: {check['hint']}")
    return 0 if worst_status(checks) != "fail" else 1


# -------------------------------------------------------------------- servers
def _country_filter(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    codes: set[str] = set()
    from pno.countries import normalize_code

    for value in values:
        for part in value.split(","):
            part = normalize_code(part)
            if part:
                codes.add(part)
    return codes or None


def _server_payload(server: Any) -> dict[str, Any]:
    return {
        "name": server.name,
        "load": server.load,
        "score": round(server.score, 3),
        "city": server.city,
        "entry": server.entry_code,
        "exit": server.exit_code,
        "p2p": server.p2p,
        "tor": server.tor,
        "secure_core": server.secure_core,
        "tier": server.tier,
    }


def _cmd_servers(args: argparse.Namespace, config: Config) -> int:
    from pno.countries import country_name

    inventory = inventory_module.load()
    snapshot = ServerCache(helper_timeout=config.proton_helper_timeout).get(
        force=args.refresh, max_age=config.servers_max_age
    )
    max_tier = snapshot.max_tier or None
    filters = _country_filter(args.country)
    top_n = args.top if args.top is not None else config.top_n
    if not 1 <= top_n <= 100:
        raise PnoError("--top must be between 1 and 100", detail=str(top_n))
    if args.routes and not args.secure_core:
        raise PnoError("--routes requires --secure-core")

    if args.secure_core and args.routes:
        inventory_names = {name.upper() for name in inventory.secure_core_servers}
        routes = secure_core_routes(
            snapshot,
            max_tier=max_tier,
            include_tor=config.include_tor,
            inventory=inventory,
            inventory_only=args.inventory,
        )
        if filters:
            routes = [
                route
                for route in routes
                if route.exit_code in filters or route.entry_code in filters
            ]
        payload = {
            "kind": "secure-core-routes",
            "generated_at": snapshot.fetched_at,
            "source": snapshot.source,
            "stale": snapshot.stale,
            "max_tier": snapshot.max_tier,
            "routes": [
                {
                    **_server_payload(route),
                    "inventory": route.name.upper() in inventory_names,
                }
                for route in routes
            ],
        }
        if args.json:
            _print(payload, True)
            return 0
        if snapshot.stale:
            print("# warning: server data is stale; run with --refresh")
        for route in routes:
            marker = " [inv]" if route.name.upper() in inventory_names else ""
            print(
                f"{route.entry_code:2} -> {route.exit_code:2}  {route.name:<12} "
                f"{route.load:3}%  score {route.score:5.2f}  {route.city or '-'}{marker}"
            )
        return 0

    if args.secure_core:
        aggregates = secure_core_countries(
            snapshot,
            inventory,
            max_tier=max_tier,
            include_tor=config.include_tor,
        )
        if filters:
            aggregates = [item for item in aggregates if item.exit_code in filters]
        payload = {
            "kind": "secure-core",
            "generated_at": snapshot.fetched_at,
            "source": snapshot.source,
            "stale": snapshot.stale,
            "max_tier": snapshot.max_tier,
            "countries": [
                {
                    "exit": item.exit_code,
                    "name": country_name(item.exit_code, config.language),
                    "best_load": item.best.load,
                    "entries": list(item.entry_codes),
                    "routes": [_server_payload(route) for route in item.routes],
                }
                for item in aggregates
            ],
        }
        if args.json:
            _print(payload, True)
        else:
            if snapshot.stale:
                print("# warning: server data is stale; run with --refresh")
            for item in aggregates:
                entries = ", ".join(item.entry_codes)
                print(
                    f"{item.exit_code:2} {country_name(item.exit_code, config.language):<24} "
                    f"best {item.best.load:3}%  via {entries}  "
                    f"({len(item.routes)} route(s))"
                )
                for route in item.routes:
                    print(
                        f"   {route.name:<12} {route.load:3}%  score {route.score:5.2f}  "
                        f"{route.city or '-'}"
                    )
        return 0

    countries = regular_countries(
        snapshot,
        top_n=top_n,
        include_tor=config.include_tor,
        max_tier=max_tier,
        known_countries=inventory.known_countries,
        include_all=inventory.include_all_countries,
    )
    if args.inventory:
        allowed = set(inventory.country_codes())
        countries = [item for item in countries if item.code in allowed]
    if filters:
        countries = [item for item in countries if item.code in filters]
    payload = {
        "kind": "regular",
        "generated_at": snapshot.fetched_at,
        "source": snapshot.source,
        "stale": snapshot.stale,
        "max_tier": snapshot.max_tier,
        "countries": [
            {
                "code": item.code,
                "name": country_name(item.code, config.language),
                "best_load": item.best.load,
                "servers": [_server_payload(server) for server in item.servers],
            }
            for item in countries
        ],
    }
    if args.json:
        _print(payload, True)
        return 0
    if snapshot.stale:
        print("# warning: server data is stale; run with --refresh")
    for country in countries:
        print(
            f"{country.code:2} {country_name(country.code, config.language):<24} "
            f"best {country.best.load:3}%"
        )
        for server in country.servers:
            tags = " p2p" if server.p2p else ""
            print(
                f"   {server.name:<14} {server.load:3}%  score {server.score:5.2f}  "
                f"{server.city or '-':<18}{tags}"
            )
    return 0


# -------------------------------------------------------------------- connect
def _cmd_connect(args: argparse.Namespace, config: Config) -> int:
    from pno.proton.connection import connect_server

    name = args.server.strip().upper()
    if not args.force:
        snapshot = ServerCache(helper_timeout=config.proton_helper_timeout).get(
            max_age=config.servers_max_age
        )
        if snapshot.lookup(name) is None:
            raise PnoError(
                "unknown server name",
                detail=name,
                hint="Run 'pno servers' or 'pno servers --secure-core' first.",
            )
    outcome = connect_server(name, close_gui=args.close_gui)
    if args.json:
        _print(outcome.to_dict(), True)
    else:
        print(outcome.output or f"connected to {name}")
    return 0


def _cmd_disconnect(args: argparse.Namespace, config: Config) -> int:
    from pno.proton.connection import disconnect_server

    outcome = disconnect_server(close_gui=args.close_gui)
    if args.json:
        _print(outcome.to_dict(), True)
    else:
        print(outcome.output or "disconnected")
    return 0


# -------------------------------------------------------------------- mirrors
def _cmd_mirrors(args: argparse.Namespace, config: Config) -> int:
    state = StateStore(paths.state_file())
    sub = getattr(args, "mirrors_command", None)
    if sub == "status":
        payload = mirrors_overview(config, state)
        if args.json:
            _print(payload, True)
            return 0
        vpn = payload.get("vpn") or {}
        if vpn.get("connected"):
            print(
                f"VPN:      connected to {vpn.get('server_name')} "
                f"(exit {vpn.get('exit_country')})"
            )
        else:
            print("VPN:      disconnected")
        print(
            f"Target:   {payload.get('last_mirror_country') or payload.get('last_measure_country') or '-'} "
            f"via {payload.get('last_mirror_source') or payload.get('last_measure_source') or '-'}"
        )
        import datetime

        refreshed = payload.get("last_mirror_refresh")
        measured = payload.get("last_measure_at")
        if refreshed:
            stamp = datetime.datetime.fromtimestamp(refreshed).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            print(f"Applied:  {stamp}")
        else:
            print("Applied:  never")
        if measured:
            stamp = datetime.datetime.fromtimestamp(measured).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            print(f"Measured: {stamp}")
        print(
            f"Mirrors:  {payload.get('mirrorlist_servers')} in {payload.get('mirrorlist_path')}"
        )
        for line in payload.get("mirrorlist_preview") or []:
            print(f"  {line}")
        if payload.get("mirror_error"):
            print(f"Error:    {payload['mirror_error']}")
        return 0

    snapshot = ServerCache(helper_timeout=config.proton_helper_timeout).read()
    vpn = read_vpn_status(snapshot)
    if not args.json:
        if vpn.connected:
            print(f"# VPN exit: {vpn.exit_country or '?'} ({vpn.server_name or '?'})")
        else:
            print(f"# VPN disconnected; using home country {config.home_country}")
    result = refresh_mirrors(
        config,
        state,
        vpn_status=vpn,
        target_country=args.country,
        apply=sub == "refresh" and not getattr(args, "no_apply", False),
        dry_run=sub == "preview" or args.dry_run,
        force_status=args.force_status,
    )
    if args.json:
        _print(result, True)
        return 0
    selected = result.get("selected") or []
    print(f"Target: {result.get('target_country')} via {result.get('target_source')}")
    for mirror in selected:
        print(
            f"  {mirror.get('host')}  ({mirror.get('protocol')}, {mirror.get('country_code')})"
        )
    apply = result.get("apply") or {}
    if apply.get("preview") and sub == "preview":
        print("\n# proposed mirrorlist\n")
        print("\n".join(apply["preview"]))
    if apply.get("applied"):
        print(
            f"Applied: {apply.get('servers')} servers (backup: {apply.get('backup')})"
        )
    elif apply.get("reason") == "unchanged":
        print("Mirrorlist already matches the plan; nothing changed")
    elif apply.get("reason") == "dry-run":
        print("Dry run: no file was written")
    elif apply.get("reason") == "not-requested":
        print("Measured only; no file was written")
    return 0


# --------------------------------------------------------------------- daemon
def _cmd_daemon(args: argparse.Namespace, config: Config) -> int:
    sub = args.daemon_command
    if sub == "run":
        from pno.daemon import run_daemon

        run_daemon(verbose=bool(getattr(args, "verbose", False)))
        return 0

    if sub == "status":
        try:
            payload = request("status", timeout=10)
        except PnoError:
            state = StateStore(paths.state_file()).read()
            payload = {
                "daemon": {"running": False},
                "paused": bool(state.get("paused")),
                "vpn": state.get("vpn"),
                "last_mirror_refresh": state.get("last_mirror_refresh"),
                "last_mirror_country": state.get("last_mirror_country"),
                "next_mirror_due": state.get("next_mirror_due"),
                "updated_at": state.get("updated_at"),
            }
        if args.json:
            _print(payload, True)
            return 0
        daemon = payload.get("daemon") or {}
        print(f"Daemon:   {'running' if daemon.get('running') else 'not running'}")
        if daemon.get("pid"):
            print(f"PID:      {daemon.get('pid')}")
        print(f"Paused:   {payload.get('paused')}")
        vpn = payload.get("vpn") or {}
        if vpn.get("connected"):
            print(
                f"VPN:      {vpn.get('server_name')} (exit {vpn.get('exit_country')})"
            )
        else:
            print("VPN:      disconnected")
        print(f"Mirrors:  {payload.get('last_mirror_country') or '-'}")
        return 0

    if sub == "shutdown":
        request("shutdown", timeout=10)
        print("Daemon is stopping")
        return 0

    if sub in {"pause", "resume"}:
        try:
            request(sub, timeout=10)
        except PnoError:
            store = StateStore(paths.state_file())
            store.update(
                lambda current: current.update(
                    {"paused": sub == "pause", "paused_at": None}
                )
            )
            print(
                f"Daemon is not running; persisted paused={sub == 'pause'} in the state file"
            )
            return 0
        print(f"Automation {'paused' if sub == 'pause' else 'resumed'}")
        return 0

    if sub == "reload":
        request("config.reload", timeout=10)
        print("Configuration reloaded")
        return 0

    raise PnoError("unknown daemon command", detail=sub)


# ------------------------------------------------------------------------ tui
def _cmd_tui(args: argparse.Namespace) -> int:
    try:
        from pno.tui.app import run
    except ImportError as exc:
        raise PnoError(
            "the Textual interface is not installed",
            detail=str(exc),
            hint="Install it with: .venv/bin/pip install -e '.[tui]'",
        ) from exc

    run()
    return 0


def _cmd_launch(args: argparse.Namespace) -> int:
    from pno.launcher import open_tui

    open_tui(config_module.ensure())
    return 0


def _cmd_tray(args: argparse.Namespace) -> int:
    try:
        from pno.tray.app import main as tray_main
    except ImportError:
        if getattr(args, "system_python", False) or os.environ.get("PNO_TRAY_REEXEC"):
            raise PnoError(
                "PySide6 is not available",
                hint="Install pyside6 (pacman) for the tray icon.",
            )
        system_python = os.environ.get("PNO_SYSTEM_PYTHON") or "/usr/bin/python3"
        if not Path(system_python).exists():
            raise PnoError("no system Python available for the tray")
        repo = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment["PNO_TRAY_REEXEC"] = "1"
        environment["PNO_VENV_PYTHON"] = sys.executable
        environment["PYTHONPATH"] = str(repo) + (
            os.pathsep + environment["PYTHONPATH"]
            if environment.get("PYTHONPATH")
            else ""
        )
        os.execve(
            system_python,
            [system_python, "-m", "pno", "tray", "--system-python"],
            environment,
        )
    tray_main()
    return 0


# ----------------------------------------------------------------------- logs
def _cmd_logs(args: argparse.Namespace) -> int:
    from pno import log_query

    files = (
        log_query.resolve_files(args.paths) if args.paths else log_query.default_files()
    )
    events_list = log_query.query(
        files,
        level=args.level,
        event_name=args.event,
        component=args.component,
        since=args.since,
        until=args.until,
        grep=args.grep,
        limit=args.limit,
    )
    if args.summary:
        _print(log_query.summarize(events_list), args.json)
        return 0
    use_json = args.json or not sys.stdout.isatty()
    for event in events_list:
        if use_json:
            print(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
        else:
            print(log_query.format_text(event))
    return 0


# -------------------------------------------------------------------- install
def _cmd_install(args: argparse.Namespace) -> int:
    from pno.install import install

    config = config_module.ensure()
    events.configure(paths.logs_dir(), level=config.log_level)
    result = install(
        assume_yes=args.yes,
        no_sudo=args.no_sudo,
        keep_reflector=args.keep_reflector,
        no_start=args.no_start,
    )
    if args.json:
        _print(result, True)
        return 0
    print(f"Config:    {result['config']}")
    print(
        f"Unit:      {result['unit']} ({'updated' if result['unit_changed'] else 'unchanged'})"
    )
    print(
        f"Autostart: {result['autostart']} "
        f"({'updated' if result['autostart_changed'] else 'unchanged'})"
    )
    print(
        f"Desktop:   {result.get('desktop')} "
        f"({'updated' if result.get('desktop_changed') else 'unchanged'})"
    )
    print(
        f"Icon:      {result.get('icon')} "
        f"({'updated' if result.get('icon_changed') else 'unchanged'})"
    )
    print(f"Service:   {result.get('service')}")
    print(f"Reflector: {result.get('reflector')}")
    helper = result.get("helper") or {}
    if helper.get("installed"):
        print("Sudo:      helper installed (NOPASSWD)")
    else:
        print(
            f"Sudo:      not installed ({helper.get('reason') or helper.get('error')})"
        )
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from pno.install import uninstall

    result = uninstall(
        assume_yes=args.yes,
        purge=args.purge,
        enable_reflector=args.enable_reflector,
    )
    if args.json:
        _print(result, True)
        return 0
    for path in result.get("removed", []):
        print(f"removed {path}")
    for error in result.get("errors", []):
        print(f"error: {error}", file=sys.stderr)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
