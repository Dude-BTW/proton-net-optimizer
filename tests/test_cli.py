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
    args = parser.parse_args(["uninstall", "--yes", "--purge", "--enable-reflector"])
    assert args.yes is True
    assert args.purge is True
    assert args.enable_reflector is True
