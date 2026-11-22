from __future__ import annotations

import json

from pno import cli, paths
from pno.cli import build_parser
from pno.proton.account import AccountInfo, ProtonFeatures, ProtonInfo
from pno.proton.settings import FALLBACK_PROTOCOLS, ApplyResult


def fake_info(*args, **kwargs):
    return (
        ProtonInfo(
            account=AccountInfo("user", "Proton Unlimited", 2),
            features=ProtonFeatures(protun=True, split_tunneling=True),
            protocols=FALLBACK_PROTOCOLS,
        ),
        [],
    )


def welcome(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_vpn_catalog_info", fake_info)
    monkeypatch.setattr(cli, "_vpn_connection_state", lambda: (False, None))
    monkeypatch.setattr(cli, "_vpn_gui_running", lambda: False)


def test_parser_has_vpn_commands_and_flags():
    parser = build_parser()
    assert parser.parse_args(["vpn", "settings", "--json"]).json is True
    args = parser.parse_args(
        [
            "vpn",
            "set",
            "split-tunneling",
            "on",
            "--mode",
            "include",
            "--app",
            "/usr/bin/example",
            "--dry-run",
            "--json",
        ]
    )
    assert args.vpn_key == "split-tunneling"
    assert args.mode == "include"
    assert args.app == ["/usr/bin/example"]
    assert args.dry_run is True


def test_vpn_settings_json_is_pure_and_uses_defaults(capsys):
    code = cli.main(["vpn", "settings", "--json", "--no-account"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["exists"] is False
    assert payload["settings"]["protocol"] == "wireguard"
    assert payload["settings"]["killswitch"] == "off"
    assert payload["account"] is None
    assert not paths.proton_settings_file().exists()


def test_vpn_set_writes_and_is_idempotent(capsys, monkeypatch):
    welcome(monkeypatch)
    assert cli.main(["vpn", "set", "netshield", "malware-only", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["changed"] is True
    assert first["applied"] == ["netshield"]
    assert json.loads(paths.proton_settings_file().read_text())["features"] == {
        "netshield": 1
    }

    assert cli.main(["vpn", "set", "netshield", "malware-only", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["changed"] is False


def test_vpn_set_custom_dns_disables_netshield(capsys, monkeypatch):
    welcome(monkeypatch)
    paths.proton_settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.proton_settings_file().write_text(
        json.dumps({"features": {"netshield": 2}}), encoding="utf-8"
    )
    assert (
        cli.main(
            [
                "vpn",
                "set",
                "custom-dns",
                "on",
                "--dns",
                "1.1.1.1,9.9.9.9",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert any("NetShield" in warning for warning in payload["warnings"])
    raw = json.loads(paths.proton_settings_file().read_text())
    assert raw["features"]["netshield"] == 0
    assert raw["custom_dns"]["enabled"] is True


def test_vpn_set_custom_dns_without_servers_reports_an_error(capsys, monkeypatch):
    welcome(monkeypatch)
    assert cli.main(["vpn", "set", "custom-dns", "on", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "settings_error"
    assert "no DNS servers" in payload["error"]["message"]


def test_vpn_set_rejects_dns_flag_for_other_keys(capsys, monkeypatch):
    welcome(monkeypatch)
    assert cli.main(["vpn", "set", "ipv6", "off", "--dns", "1.1.1.1"]) == 1
    assert "--dns" in capsys.readouterr().err


def test_vpn_set_split_tunneling_with_apps(capsys, monkeypatch, tmp_path):
    welcome(monkeypatch)
    app = tmp_path / "example"
    app.write_text("", encoding="utf-8")
    assert (
        cli.main(
            [
                "vpn",
                "set",
                "split-tunneling",
                "on",
                "--mode",
                "exclude",
                "--app",
                str(app),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is True
    split = json.loads(paths.proton_settings_file().read_text())["features"][
        "split_tunneling"
    ]
    assert split["config_by_mode"]["exclude"]["app_paths"] == [str(app)]


def test_vpn_set_dry_run_does_not_write(capsys, monkeypatch):
    welcome(monkeypatch)
    assert cli.main(["vpn", "set", "ipv6", "off", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is True
    assert payload["dry_run"] is True
    assert not paths.proton_settings_file().exists()


def test_vpn_set_protocol_validates_the_catalog(capsys, monkeypatch):
    welcome(monkeypatch)
    assert cli.main(["vpn", "set", "protocol", "nope", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "settings_error"


def test_vpn_set_killswitch_uses_handler(capsys, monkeypatch):
    welcome(monkeypatch)
    calls = []

    def fake_killswitch(value, **kwargs):
        calls.append((value, kwargs))
        return ApplyResult(
            changed=True,
            applied=("killswitch",),
            warnings=(),
            path=paths.proton_settings_file(),
        )

    from pno.proton import settings as proton_settings

    monkeypatch.setattr(proton_settings, "apply_killswitch", fake_killswitch)
    assert cli.main(["vpn", "set", "kill-switch", "standard", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is True
    assert calls[0][0] == "standard"
    assert calls[0][1]["connected"] is False


def test_vpn_set_blocks_disconnected_only_changes(capsys, monkeypatch):
    welcome(monkeypatch)
    monkeypatch.setattr(cli, "_vpn_connection_state", lambda: (True, None))
    assert cli.main(["vpn", "set", "protocol", "wireguard", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "settings_error"
    assert "disconnect" in payload["error"]["message"]
