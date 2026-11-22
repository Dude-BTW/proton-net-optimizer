from __future__ import annotations

from pno.cli import build_parser


def test_parser_has_the_documented_commands():
    parser = build_parser()
    actions = [
        action
        for action in parser._actions
        if action.__class__.__name__ == "_SubParsersAction"
    ]
    assert actions
    commands = set(actions[0].choices)
    assert {
        "doctor",
        "servers",
        "connect",
        "disconnect",
        "mirrors",
        "daemon",
        "tui",
        "tray",
        "logs",
        "install",
        "uninstall",
    } <= commands


def test_servers_flags():
    parser = build_parser()
    args = parser.parse_args(["servers", "--top", "3", "--country", "UA,DE", "--json"])
    assert args.top == 3
    assert args.country == ["UA,DE"]
    assert args.json is True


def test_servers_inventory_and_route_flags():
    parser = build_parser()
    args = parser.parse_args(["servers", "--secure-core", "--routes", "--inventory"])
    assert args.secure_core is True
    assert args.routes is True
    assert args.inventory is True


def test_connect_and_disconnect_gui_flags():
    parser = build_parser()
    connect = parser.parse_args(["connect", "CH#1", "--close-gui"])
    assert connect.close_gui is True
    disconnect = parser.parse_args(["disconnect", "--close-gui", "--json"])
    assert disconnect.close_gui is True
    assert disconnect.json is True


def test_mirrors_subcommands():
    parser = build_parser()
    preview = parser.parse_args(["mirrors", "preview", "--json"])
    assert preview.mirrors_command == "preview"
    assert preview.json is True
    refresh = parser.parse_args(["mirrors", "refresh", "--no-apply"])
    assert refresh.mirrors_command == "refresh"
    assert refresh.no_apply is True


def test_daemon_subcommands():
    parser = build_parser()
    assert parser.parse_args(["daemon", "status"]).daemon_command == "status"
    assert parser.parse_args(["daemon", "pause"]).daemon_command == "pause"


def test_uninstall_flags():
    parser = build_parser()
    args = parser.parse_args(["uninstall", "--yes", "--purge"])
    assert args.yes is True
    assert args.purge is True


def test_launch_command_exists():
    parser = build_parser()
    args = parser.parse_args(["launch"])
    assert args.command == "launch"


def test_favorites_commands_and_aliases():
    parser = build_parser()
    args = parser.parse_args(["favorites", "set", "UA", "CH-US#3", "--json"])
    assert args.favorites_command == "set"
    assert args.entries == ["UA", "CH-US#3"]
    assert args.json is True
    args = parser.parse_args(["servers", "--favorites"])
    assert args.inventory is True


def test_logs_prune_flags():
    parser = build_parser()
    args = parser.parse_args(
        ["logs", "--prune", "--keep-files", "3", "--max-age-days", "7"]
    )
    assert args.prune is True
    assert args.keep_files == 3
    assert args.max_age_days == 7


def test_logs_prune_command(tmp_path, capsys, monkeypatch):
    import json

    from pno import paths

    archive = paths.logs_dir() / "archive"
    archive.mkdir(parents=True)
    active = paths.logs_dir() / "events.jsonl"
    active.write_text("active\n", encoding="utf-8")
    for index in range(3):
        (archive / f"events-{index}.jsonl").write_text("x", encoding="utf-8")
    from pno.cli import main

    code = main(
        ["logs", "--prune", "--keep-files", "1", "--max-age-days", "0", "--json"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["removed"]) == 2
    assert active.is_file()
    assert len(list(archive.glob("*.jsonl"))) == 1
