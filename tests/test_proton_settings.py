from __future__ import annotations

import json
import os

import pytest

from pno import paths
from pno.errors import SettingsError
from pno.proc import CommandResult
from pno.proton import settings as proton_settings


def write_settings(payload: dict) -> None:
    target = paths.proton_settings_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def read_settings() -> dict:
    return json.loads(paths.proton_settings_file().read_text(encoding="utf-8"))


def test_missing_file_returns_proton_defaults_without_writing():
    loaded = proton_settings.load()
    assert loaded.exists is False
    canonical = loaded.canonical()
    assert canonical["protocol"] == "wireguard"
    assert canonical["killswitch"] == "off"
    assert canonical["ipv6"] is True
    assert canonical["telemetry"] is True
    assert canonical["anonymous_crash_reports"] is True
    assert canonical["split_tunneling"]["enabled"] is False
    assert canonical["custom_dns"] == {"enabled": False, "ips": []}
    assert not paths.proton_settings_file().exists()


def test_reads_existing_file_and_keeps_unknown_values_for_display():
    write_settings(
        {
            "protocol": "protun-smart",
            "killswitch": 1,
            "features": {
                "netshield": 2,
                "split_tunneling": {
                    "enabled": True,
                    "mode": "include",
                    "config_by_mode": {
                        "exclude": {
                            "mode": "exclude",
                            "app_paths": ["/a"],
                            "ip_ranges": [],
                        },
                        "include": {
                            "mode": "include",
                            "app_paths": ["/b"],
                            "ip_ranges": ["10.0.0.0/8"],
                        },
                    },
                },
            },
            "custom_dns": {
                "enabled": True,
                "ip_list": [{"ip": "1.1.1.1", "enabled": True}],
            },
        }
    )
    canonical = proton_settings.load().canonical()
    assert canonical["protocol"] == "protun-smart"
    assert canonical["killswitch"] == "standard"
    assert canonical["netshield"] == "malware-ads-trackers"
    assert canonical["split_tunneling"] == {
        "enabled": True,
        "mode": "include",
        "app_paths": ["/b"],
        "ip_ranges": ["10.0.0.0/8"],
    }
    assert canonical["custom_dns"]["ips"] == ["1.1.1.1"]


def test_apply_writes_only_changed_keys_and_is_idempotent():
    result = proton_settings.apply_changes({"ipv6": "off"})
    assert result.changed is True
    assert read_settings() == {"ipv6": False}
    mtime = os.stat(paths.proton_settings_file()).st_mtime_ns

    again = proton_settings.apply_changes({"ipv6": "off"})
    assert again.changed is False
    assert os.stat(paths.proton_settings_file()).st_mtime_ns == mtime


def test_apply_preserves_unknown_keys():
    write_settings({"custom": {"keep": True}, "features": {"netshield": 0}})
    proton_settings.apply_changes({"netshield": "malware-only"})
    raw = read_settings()
    assert raw["custom"] == {"keep": True}
    assert raw["features"]["netshield"] == 1


def test_malformed_file_is_reported_and_never_overwritten():
    target = paths.proton_settings_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(SettingsError):
        proton_settings.load()
    with pytest.raises(SettingsError):
        proton_settings.apply_changes({"ipv6": False})
    assert target.read_text(encoding="utf-8") == "{not json"


def test_custom_dns_disables_netshield_and_keeps_both_lists():
    write_settings({"features": {"netshield": 2}})
    result = proton_settings.apply_changes(
        {"custom_dns_enabled": True, "custom_dns_ips": "1.1.1.1, 9.9.9.9"},
        tier=2,
    )
    assert result.changed is True
    assert any("NetShield" in warning for warning in result.warnings)
    raw = read_settings()
    assert raw["features"]["netshield"] == 0
    assert [entry["ip"] for entry in raw["custom_dns"]["ip_list"]] == [
        "1.1.1.1",
        "9.9.9.9",
    ]


def test_netshield_disables_custom_dns():
    write_settings(
        {
            "custom_dns": {
                "enabled": True,
                "ip_list": [{"ip": "1.1.1.1", "enabled": True}],
            }
        }
    )
    result = proton_settings.apply_changes({"netshield": "malware-only"}, tier=2)
    assert any("custom dns" in warning.lower() for warning in result.warnings)
    assert read_settings()["custom_dns"]["enabled"] is False


def test_enabling_netshield_and_custom_dns_together_is_rejected():
    with pytest.raises(SettingsError, match="cannot be enabled together"):
        proton_settings.apply_changes(
            {"netshield": "malware-only", "custom_dns_enabled": True},
            tier=2,
        )


def test_custom_dns_requires_at_least_one_server():
    with pytest.raises(SettingsError, match="no DNS servers"):
        proton_settings.apply_changes({"custom_dns_enabled": True}, tier=2)


def test_invalid_dns_is_rejected():
    with pytest.raises(SettingsError, match="invalid DNS"):
        proton_settings.apply_changes({"custom_dns_ips": "not-an-ip"}, tier=2)


def test_disconnected_only_settings_are_blocked_while_connected():
    for changes in (
        {"protocol": "wireguard"},
        {"split_tunneling_enabled": True},
        {"split_tunneling_mode": "include"},
        {"split_tunneling_apps": ["/usr/bin/example"]},
    ):
        with pytest.raises(SettingsError, match="disconnect"):
            proton_settings.apply_changes(changes, connected=True, tier=2)


def test_other_settings_are_allowed_while_connected():
    result = proton_settings.apply_changes(
        {"netshield": "malware-ads-trackers"}, connected=True, tier=2
    )
    assert result.changed is True
    assert read_settings()["features"]["netshield"] == 2


def test_gui_running_blocks_every_write():
    with pytest.raises(SettingsError, match="GUI"):
        proton_settings.apply_changes({"ipv6": False}, gui_running=True)


def test_paid_settings_are_blocked_on_free_plan():
    with pytest.raises(SettingsError, match="paid"):
        proton_settings.apply_changes({"port_forwarding": True}, tier=0)
    with pytest.raises(SettingsError, match="paid"):
        proton_settings.apply_changes({"split_tunneling_enabled": True}, tier=0)


def test_unknown_plan_warns_but_saves():
    result = proton_settings.apply_changes({"port_forwarding": True}, tier=None)
    assert result.changed is True
    assert any("plan is unknown" in warning for warning in result.warnings)


def test_unknown_field_is_rejected():
    with pytest.raises(SettingsError, match="unsupported"):
        proton_settings.apply_changes({"made_up": True})


def test_split_tunneling_keeps_both_mode_lists(tmp_path):
    app = tmp_path / "example"
    app.write_text("", encoding="utf-8")
    proton_settings.apply_changes(
        {
            "split_tunneling_enabled": True,
            "split_tunneling_mode": "include",
            "split_tunneling_apps": [str(app)],
        },
        tier=2,
    )
    raw = read_settings()
    split = raw["features"]["split_tunneling"]
    assert split["mode"] == "include"
    assert split["config_by_mode"]["include"]["app_paths"] == [str(app)]
    assert split["config_by_mode"]["exclude"]["app_paths"] == []

    proton_settings.apply_changes(
        {"split_tunneling_mode": "exclude", "split_tunneling_apps": []}, tier=2
    )
    split = read_settings()["features"]["split_tunneling"]
    assert split["config_by_mode"]["include"]["app_paths"] == [str(app)]
    assert split["config_by_mode"]["exclude"]["app_paths"] == []


def test_split_tunneling_applies_apps_to_the_requested_mode_regardless_of_order():
    proton_settings.apply_changes(
        {
            "split_tunneling_apps": ["/usr/bin/example"],
            "split_tunneling_mode": "include",
            "split_tunneling_enabled": True,
        },
        tier=2,
    )
    split = read_settings()["features"]["split_tunneling"]
    assert split["config_by_mode"]["include"]["app_paths"] == ["/usr/bin/example"]
    assert split["config_by_mode"]["exclude"]["app_paths"] == []


def test_split_tunneling_warns_about_missing_apps_and_killswitch(tmp_path):
    write_settings({"killswitch": 1})
    result = proton_settings.apply_changes(
        {
            "split_tunneling_enabled": True,
            "split_tunneling_apps": [str(tmp_path / "missing")],
        },
        tier=2,
    )
    assert any("does not exist" in warning for warning in result.warnings)
    assert any("kill switch" in warning for warning in result.warnings)


def test_split_tunneling_rejects_relative_paths():
    with pytest.raises(SettingsError, match="absolute"):
        proton_settings.apply_changes(
            {"split_tunneling_apps": ["relative/app"]}, tier=2
        )


def test_split_tunneling_reads_pre_6_layout(tmp_path):
    write_settings(
        {
            "features": {
                "split_tunneling": {
                    "enabled": True,
                    "mode": "exclude",
                    "exclude": {"app_paths": ["/legacy"]},
                }
            }
        }
    )
    assert proton_settings.load().canonical()["split_tunneling"]["app_paths"] == [
        "/legacy"
    ]


def test_protocol_aliases_are_normalised():
    result = proton_settings.apply_changes({"protocol": "smart"})
    assert result.changed is True
    assert read_settings()["protocol"] == "protun-smart"
    with pytest.raises(SettingsError, match="unknown protocol"):
        proton_settings.apply_changes({"protocol": "nope"})


def test_app_config_defaults_when_missing():
    app = proton_settings.read_app_config()
    assert app.available is False
    assert app.auto_connect is None
    assert app.start_minimized is False
    assert app.pinned == ()


def test_app_config_reads_gui_preferences(tmp_path):
    target = paths.proton_app_config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "connect_at_app_startup": "CH",
                "start_app_minimized": True,
                "tray_pinned_servers": ["CH", "IS-US#1"],
            }
        ),
        encoding="utf-8",
    )
    app = proton_settings.read_app_config()
    assert app.available is True
    assert app.auto_connect == "CH"
    assert app.start_minimized is True
    assert app.pinned == ("CH", "IS-US#1")


def test_killswitch_uses_the_official_cli_for_immediate_apply():
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(list(args))
        write_settings({"killswitch": 1})
        return CommandResult(tuple(args), 0, "", "", 0)

    result = proton_settings.apply_killswitch("standard", runner=runner)
    assert result.changed is True
    assert calls == [["protonvpn", "config", "set", "kill-switch", "standard"]]
    assert read_settings()["killswitch"] == 1


def test_killswitch_unchanged_does_not_run_the_cli():
    write_settings({"killswitch": 1})

    def runner(args, **kwargs):
        raise AssertionError("the CLI must not run")

    result = proton_settings.apply_killswitch("standard", runner=runner)
    assert result.changed is False


def test_killswitch_advanced_is_gui_only():
    with pytest.raises(SettingsError, match="GUI"):
        proton_settings.apply_killswitch("advanced")


def test_killswitch_respects_connection_and_gui_locks():
    with pytest.raises(SettingsError, match="disconnect"):
        proton_settings.apply_killswitch("standard", connected=True)
    with pytest.raises(SettingsError, match="GUI"):
        proton_settings.apply_killswitch("standard", gui_running=True)


def test_killswitch_dry_run_does_not_run_the_cli(tmp_path):
    def runner(args, **kwargs):
        raise AssertionError("the CLI must not run")

    result = proton_settings.apply_killswitch("standard", dry_run=True, runner=runner)
    assert result.changed is True
    assert not paths.proton_settings_file().exists()
