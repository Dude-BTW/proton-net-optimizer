from __future__ import annotations

import time

import pytest

from pno import events, paths
from pno.errors import VpnBusyError, VpnError
from pno.proc import CommandResult
from pno.proton import connection
from pno.proton.connection import (
    GuiActiveError,
    clean_output,
    command_detail,
    connect_server,
    disconnect_server,
    redact_output,
    vpn_action_lock,
    wait_for_connected,
    wait_for_disconnected,
)
from pno.proton.gui import GuiCloseResult, GuiProcess
from pno.proton.status import VpnStatus

GUI = [GuiProcess(pid=4242, cmdline=("/usr/bin/protonvpn-app",))]


def command(args, code=0, stdout="", stderr=""):
    return CommandResult(
        args=tuple(args),
        returncode=code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1,
    )


def status(connected: bool) -> VpnStatus:
    return VpnStatus(connected=connected, source="test", checked_at=time.time())


def read_log():
    return list(events.read_events([paths.logs_dir()]))


def test_connect_without_gui_runs_cli():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return command(args, stdout="connected")

    outcome = connect_server(
        "CH#1",
        processes=[],
        status_reader=lambda: status(False),
        proton_status_reader=lambda: status(False),
        runner=runner,
    )
    assert outcome.ok is True
    assert outcome.stale_cleared is False
    assert calls == [["protonvpn", "connect", "CH#1"]]


def test_connect_refuses_when_gui_holds_a_connection():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return command(args)

    with pytest.raises(GuiActiveError) as caught:
        connect_server(
            "CH#1",
            processes=GUI,
            status_reader=lambda: status(True),
            proton_status_reader=lambda: status(True),
            runner=runner,
        )
    assert caught.value.pids == (4242,)
    assert caught.value.server_name == "CH#1"
    assert calls == []


def test_connect_closes_gui_and_reconnects(monkeypatch):
    calls = []
    state = {"connected": True}

    def runner(args, **kwargs):
        calls.append(args)
        if args[1] == "disconnect":
            state["connected"] = False
        return command(args, stdout="ok")

    monkeypatch.setattr(
        connection,
        "close_gui",
        lambda processes, timeout: GuiCloseResult(terminated=(4242,)),
    )
    outcome = connect_server(
        "CH#1",
        close_gui=True,
        processes=GUI,
        status_reader=lambda: status(state["connected"]),
        proton_status_reader=lambda: status(state["connected"]),
        runner=runner,
        sleep=lambda _: None,
    )
    assert outcome.gui_closed is True
    assert outcome.disconnected_first is True
    assert calls == [
        ["protonvpn", "disconnect"],
        ["protonvpn", "connect", "CH#1"],
    ]


def test_connect_fails_when_gui_cannot_be_closed(monkeypatch):
    monkeypatch.setattr(
        connection,
        "close_gui",
        lambda processes, timeout: GuiCloseResult(still_running=(4242,)),
    )
    with pytest.raises(VpnError):
        connect_server(
            "CH#1",
            close_gui=True,
            processes=GUI,
            status_reader=lambda: status(True),
            proton_status_reader=lambda: status(True),
            runner=lambda args, **kwargs: command(args),
        )


def test_connect_clears_stale_proton_state():
    calls = []
    proton = {"connected": True}

    def runner(args, **kwargs):
        calls.append(args)
        if args[1] == "disconnect":
            proton["connected"] = False
            return command(args, stdout="disconnected")
        return command(args, stdout="connected")

    def proton_status():
        return VpnStatus(
            connected=proton["connected"],
            server_name="CH#290" if proton["connected"] else None,
            source="protonvpn-cli",
            checked_at=time.time(),
        )

    outcome = connect_server(
        "SE#56",
        processes=[],
        status_reader=lambda: status(False),
        proton_status_reader=proton_status,
        runner=runner,
        sleep=lambda _: None,
    )
    assert outcome.stale_cleared is True
    assert calls == [
        ["protonvpn", "disconnect"],
        ["protonvpn", "connect", "SE#56"],
    ]
    names = [event["event"] for event in read_log()]
    assert "vpn.stale-state.detected" in names
    assert "vpn.stale-state.cleared" in names


def test_connect_proceeds_when_stale_clear_fails():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if args[1] == "disconnect":
            return command(args, code=1, stderr="busy")
        return command(args, stdout="connected")

    outcome = connect_server(
        "SE#56",
        processes=[],
        status_reader=lambda: status(False),
        proton_status_reader=lambda: status(True),
        runner=runner,
        sleep=lambda _: None,
    )
    assert outcome.stale_cleared is False
    assert calls == [
        ["protonvpn", "disconnect"],
        ["protonvpn", "connect", "SE#56"],
    ]
    names = [event["event"] for event in read_log()]
    assert "vpn.stale-state.clear-failed" in names


def test_connect_skips_stale_probe_when_networkmanager_is_connected():
    calls = []

    def proton_status():
        raise AssertionError("Proton status must not be probed")

    def runner(args, **kwargs):
        calls.append(args)
        return command(args, stdout="connected")

    outcome = connect_server(
        "CH#2",
        processes=[],
        status_reader=lambda: status(True),
        proton_status_reader=proton_status,
        runner=runner,
    )
    assert outcome.stale_cleared is False
    assert calls == [["protonvpn", "connect", "CH#2"]]


def test_connect_failure_is_logged_with_detail_and_hint():
    stderr = (
        "/usr/lib/python3.14/site-packages/sentry_sdk/utils.py:1395: "
        "EventletDeprecationWarning: \n"
        "Eventlet is deprecated. It is currently being maintained in bugfix mode.\n"
        "\n"
        "  from eventlet.patcher import is_monkey_patched  # type: ignore"
    )

    def runner(args, **kwargs):
        return command(
            args,
            code=1,
            stdout="Error: VPN server NOT reachable.",
            stderr=stderr,
        )

    with pytest.raises(VpnError) as caught:
        connect_server(
            "SE#56",
            processes=[],
            status_reader=lambda: status(False),
            proton_status_reader=lambda: status(False),
            runner=runner,
            recover_timeout=0.0,
        )
    assert "NOT reachable" in (caught.value.detail or "")
    assert "Eventlet" not in (caught.value.detail or "")
    assert caught.value.hint is not None
    log = read_log()
    failed = [event for event in log if event["event"] == "vpn.connect.failed"]
    assert len(failed) == 1
    assert failed[0]["level"] == "error"
    assert "NOT reachable" in failed[0]["error"]["detail"]
    assert failed[0]["error"]["hint"]
    commands = [event for event in log if event["event"] == "vpn.command"]
    assert len(commands) == 1
    data = commands[0]["data"]
    assert data["returncode"] == 1
    assert data["args"] == ["protonvpn", "connect", "SE#56"]
    assert "NOT reachable" in data["stdout"]
    assert "EventletDeprecationWarning" in data["stderr"]


def test_connect_recovers_when_cli_exits_nonzero_but_tunnel_is_up():
    state = {"connected": False}

    def runner(args, **kwargs):
        if args[1] == "connect":
            state["connected"] = True
        return command(
            args,
            code=1,
            stdout="Error: Network connectivity issues detected.",
        )

    def reader():
        return VpnStatus(
            connected=state["connected"],
            server_name="SE#56" if state["connected"] else None,
            source="test",
            checked_at=time.time(),
        )

    outcome = connect_server(
        "SE#56",
        processes=[],
        status_reader=reader,
        proton_status_reader=lambda: status(False),
        runner=runner,
        recover_timeout=1.0,
        sleep=lambda _: None,
    )
    assert outcome.ok is True
    assert outcome.recovered is True
    log = read_log()
    names = [event["event"] for event in log]
    assert "vpn.connect.exit-nonzero" in names
    done = [event for event in log if event["event"] == "vpn.connect.done"]
    assert done[-1]["data"]["recovered"] is True


def test_connect_still_fails_when_the_tunnel_stays_down():
    def runner(args, **kwargs):
        return command(args, code=1, stdout="Error: Connection failed.")

    with pytest.raises(VpnError):
        connect_server(
            "SE#56",
            processes=[],
            status_reader=lambda: status(False),
            proton_status_reader=lambda: status(False),
            runner=runner,
            recover_timeout=0.0,
        )


def test_connect_refuses_while_another_action_holds_the_lock():
    with vpn_action_lock():
        with pytest.raises(VpnBusyError) as caught:
            connect_server(
                "SE#56",
                processes=[],
                status_reader=lambda: status(False),
                proton_status_reader=lambda: status(False),
                runner=lambda args, **kwargs: command(args),
            )
    assert caught.value.code == "vpn_busy"
    failed = [event for event in read_log() if event["event"] == "vpn.connect.failed"]
    assert failed[-1]["error"]["code"] == "vpn_busy"


def test_disconnect_failure_is_logged():
    with pytest.raises(VpnError):
        disconnect_server(
            processes=[],
            status_reader=lambda: status(True),
            runner=lambda args, **kwargs: command(args, code=1, stderr="no tunnel"),
            recover_timeout=0.0,
        )
    failed = [
        event for event in read_log() if event["event"] == "vpn.disconnect.failed"
    ]
    assert len(failed) == 1
    assert failed[0]["level"] == "error"
    assert failed[0]["error"]["detail"] == "no tunnel"


def test_disconnect_recovers_when_cli_exits_nonzero_but_tunnel_is_down():
    state = {"connected": True}

    def runner(args, **kwargs):
        if args[1] == "disconnect":
            state["connected"] = False
        return command(
            args,
            code=1,
            stdout="Error: Network connectivity issues detected.",
        )

    def reader():
        return VpnStatus(
            connected=state["connected"], source="test", checked_at=time.time()
        )

    outcome = disconnect_server(
        processes=[],
        status_reader=reader,
        runner=runner,
        recover_timeout=1.0,
        sleep=lambda _: None,
    )
    assert outcome.ok is True
    assert outcome.recovered is True
    log = read_log()
    assert [event["event"] for event in log].count("vpn.disconnect.exit-nonzero") == 1
    done = [event for event in log if event["event"] == "vpn.disconnect.done"]
    assert done[-1]["data"]["recovered"] is True


def test_disconnect_success():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return command(args, stdout="disconnected")

    outcome = disconnect_server(processes=[], runner=runner)
    assert outcome.ok is True
    assert calls == [["protonvpn", "disconnect"]]


def test_disconnect_offers_gui_when_cli_fails():
    with pytest.raises(GuiActiveError):
        disconnect_server(
            processes=GUI,
            status_reader=lambda: status(True),
            runner=lambda args, **kwargs: command(args, code=1, stderr="busy"),
            recover_timeout=0.0,
        )


def test_disconnect_closes_gui_and_retries(monkeypatch):
    calls = []
    responses = [1, 0]

    def runner(args, **kwargs):
        calls.append(args)
        return command(args, code=responses.pop(0))

    monkeypatch.setattr(
        connection,
        "close_gui",
        lambda processes, timeout: GuiCloseResult(terminated=(4242,)),
    )
    outcome = disconnect_server(
        processes=GUI,
        close_gui=True,
        status_reader=lambda: status(True),
        runner=runner,
        recover_timeout=0.0,
        sleep=lambda _: None,
    )
    assert outcome.gui_closed is True
    assert calls == [["protonvpn", "disconnect"], ["protonvpn", "disconnect"]]


def test_clean_output_drops_the_eventlet_block():
    text = (
        "/usr/lib/python3.14/site-packages/sentry_sdk/utils.py:1395: "
        "EventletDeprecationWarning: \n"
        "Eventlet is deprecated.\n"
        "\n"
        "  from eventlet.patcher import is_monkey_patched  # type: ignore\n"
        "Error: disconnected"
    )
    assert clean_output(text) == "Error: disconnected"


def test_redact_output_masks_credentials():
    assert redact_output("Error: password=hunter2 token: abc") == (
        "Error: password=<redacted> token=<redacted>"
    )


def test_command_detail_prefers_clean_text_from_both_streams():
    result = CommandResult(
        args=("protonvpn", "disconnect"),
        returncode=1,
        stdout="Disconnected.\nError: Network connectivity issues detected.",
        stderr="EventletDeprecationWarning: \n  from eventlet.patcher import x\n",
        duration_ms=5,
    )
    assert command_detail(result) == (
        "Disconnected.\nError: Network connectivity issues detected."
    )


def test_wait_for_connected_matches_the_requested_server():
    assert (
        wait_for_connected(
            "SE#56",
            lambda: VpnStatus(
                connected=True,
                server_name="SE#56",
                source="test",
                checked_at=time.time(),
            ),
            timeout=0.01,
            interval=0.0,
            sleep=lambda _: None,
        )
        is True
    )
    assert (
        wait_for_connected(
            "SE#57",
            lambda: VpnStatus(
                connected=True,
                server_name="SE#56",
                source="test",
                checked_at=time.time(),
            ),
            timeout=0.01,
            interval=0.0,
            sleep=lambda _: None,
        )
        is False
    )


def test_wait_for_disconnected_times_out():
    assert (
        wait_for_disconnected(
            lambda: status(True),
            timeout=0.01,
            interval=0.0,
            sleep=lambda _: None,
        )
        is False
    )
    assert (
        wait_for_disconnected(
            lambda: status(False),
            timeout=0.01,
            interval=0.0,
            sleep=lambda _: None,
        )
        is True
    )
