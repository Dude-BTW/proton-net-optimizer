from __future__ import annotations

import time

import pytest

from pno import events, paths
from pno.autoconnect import (
    SKIP_ALREADY_CONNECTED,
    SKIP_DISABLED,
    SKIP_GUI_RUNNING,
    SKIP_NO_SNAPSHOT,
    SKIP_NO_TARGET,
    SKIP_UNAVAILABLE,
    AutoconnectResult,
    normalize_target,
    run_once,
    select_server,
    target_kind,
    validate_target,
)
from pno.config import Config
from pno.errors import VpnError
from pno.proton.connection import VpnActionResult
from pno.proton.gui import GuiProcess
from pno.proton.models import FEATURE_SECURE_CORE, FEATURE_TOR
from pno.proton.serverlist import snapshot_from_payload
from pno.proton.status import VpnStatus
from tests.fixtures import helper_payload, server

GUI = [GuiProcess(pid=4242, cmdline=("/usr/bin/protonvpn-app",))]


def snapshot(servers):
    return snapshot_from_payload(helper_payload(servers), source="helper")


def status(connected: bool) -> VpnStatus:
    return VpnStatus(connected=connected, source="test", checked_at=time.time())


def read_log():
    return list(events.read_events([paths.logs_dir()]))


DATA = snapshot(
    [
        server("UA#1", "UA", "UA", 10),
        server("UA#2", "UA", "UA", 5),
        server("DE#1", "DE", "DE", 1),
        server("CH-US#3", "CH", "US", 2, features=FEATURE_SECURE_CORE),
        server("FR-US#9", "FR", "US", 30, features=FEATURE_SECURE_CORE),
    ]
)


def test_target_normalisation_and_kind():
    assert normalize_target(" ua ") == "UA"
    assert target_kind("UA") == "country"
    assert target_kind("ch-us#3") == "secure_core"
    assert target_kind("UA#1") is None
    assert target_kind("") is None


def test_validate_target_accepts_known_targets_only():
    assert validate_target(DATA, "UA") is None
    assert validate_target(DATA, "CH-US#3") is None
    assert validate_target(DATA, "") is None
    assert "unknown" in (validate_target(DATA, "ZZ") or "")
    assert validate_target(DATA, "UA#1") is not None


def test_select_server_picks_the_least_loaded_regular_server():
    assert select_server(DATA, "UA") is not None
    assert select_server(DATA, "UA").name == "UA#2"
    assert select_server(DATA, "CH-US#3").name == "CH-US#3"
    assert select_server(DATA, "FR-US#9").name == "FR-US#9"
    assert select_server(DATA, "ZZ") is None


def test_select_server_skips_tor_servers_unless_allowed():
    data = snapshot(
        [
            server("UA#1", "UA", "UA", 1, features=FEATURE_TOR),
            server("UA#2", "UA", "UA", 9),
        ]
    )
    assert select_server(data, "UA").name == "UA#2"
    assert select_server(data, "UA", include_tor=True).name == "UA#1"


@pytest.mark.parametrize(
    "config, expected",
    [
        (Config(), SKIP_DISABLED),
        (Config(autoconnect_enabled=True), SKIP_NO_TARGET),
    ],
)
def test_startup_skips_when_not_armed(config, expected):
    result = run_once(config, snapshot=DATA, status_reader=lambda: status(False))
    assert result.status == "skipped"
    assert result.reason == expected
    assert [event["event"] for event in read_log()].count("autoconnect.skipped") >= 1


def test_manual_attempt_ignores_the_enabled_flag():
    calls = []

    def connect(name, **kwargs):
        calls.append(name)
        return VpnActionResult(ok=True, action="connect", server=name)

    result = run_once(
        Config(autoconnect_target="UA"),
        reason="manual",
        snapshot=DATA,
        status_reader=lambda: status(False),
        processes=[],
        connect=connect,
    )
    assert result.ok is True
    assert result.server == "UA#2"
    assert calls == ["UA#2"]


def test_startup_skips_when_already_connected_or_gui_running():
    config = Config(autoconnect_enabled=True, autoconnect_target="UA")
    connected = run_once(
        config, snapshot=DATA, status_reader=lambda: status(True), processes=[]
    )
    assert connected.reason == SKIP_ALREADY_CONNECTED
    gui = run_once(
        config, snapshot=DATA, status_reader=lambda: status(False), processes=GUI
    )
    assert gui.reason == SKIP_GUI_RUNNING


def test_startup_skips_without_snapshot_or_available_target():
    config = Config(autoconnect_enabled=True, autoconnect_target="UA")
    missing = run_once(config, snapshot=None, status_reader=lambda: status(False))
    assert missing.reason == SKIP_NO_SNAPSHOT
    unavailable = run_once(
        Config(autoconnect_enabled=True, autoconnect_target="ZZ"),
        snapshot=DATA,
        status_reader=lambda: status(False),
    )
    assert unavailable.reason == SKIP_UNAVAILABLE


def test_connect_failure_is_reported_and_logged():
    def connect(name, **kwargs):
        raise VpnError("protonvpn connect failed", detail="timeout")

    result = run_once(
        Config(autoconnect_enabled=True, autoconnect_target="UA"),
        snapshot=DATA,
        status_reader=lambda: status(False),
        processes=[],
        connect=connect,
    )
    assert result.status == "failed"
    assert result.error == "protonvpn connect failed"
    failed = [event for event in read_log() if event["event"] == "autoconnect.failed"]
    assert failed and failed[-1]["error"]["detail"] == "timeout"


def test_status_failure_does_not_connect():
    def reader():
        raise VpnError("cannot read status")

    result = run_once(
        Config(autoconnect_enabled=True, autoconnect_target="UA"),
        snapshot=DATA,
        status_reader=reader,
        processes=[],
        connect=lambda *a, **k: AutoconnectResult("connected", "connected"),
    )
    assert result.status == "failed"
    assert result.reason == "status-unavailable"


def test_snapshot_loader_supplies_missing_data():
    calls = []

    def connect(name, **kwargs):
        calls.append(name)
        return VpnActionResult(ok=True, action="connect", server=name)

    result = run_once(
        Config(autoconnect_enabled=True, autoconnect_target="UA"),
        snapshot_loader=lambda: DATA,
        status_reader=lambda: status(False),
        processes=[],
        connect=connect,
    )
    assert result.ok is True
    assert calls == ["UA#2"]


def test_snapshot_loader_failure_does_not_connect():
    def loader():
        raise VpnError("cannot fetch server data")

    result = run_once(
        Config(autoconnect_enabled=True, autoconnect_target="UA"),
        snapshot_loader=loader,
        status_reader=lambda: status(False),
        processes=[],
        connect=lambda *a, **k: None,
    )
    assert result.status == "failed"
    assert result.reason == SKIP_NO_SNAPSHOT


def test_stale_data_is_logged_but_still_used():
    result = run_once(
        Config(autoconnect_enabled=True, autoconnect_target="UA"),
        snapshot=DATA.with_stale(True),
        status_reader=lambda: status(False),
        processes=[],
        connect=lambda name, **kwargs: VpnActionResult(
            ok=True, action="connect", server=name
        ),
    )
    assert result.ok is True
    names = [event["event"] for event in read_log()]
    assert "autoconnect.stale-data" in names
