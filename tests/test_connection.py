from __future__ import annotations

import time

import pytest

from pno.errors import VpnError
from pno.proc import CommandResult
from pno.proton import connection
from pno.proton.connection import (
    GuiActiveError,
    connect_server,
    disconnect_server,
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


def test_connect_without_gui_runs_cli():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return command(args, stdout="connected")

    outcome = connect_server(
        "CH#1",
        processes=[],
        status_reader=lambda: status(False),
        runner=runner,
    )
    assert outcome.ok is True
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
            runner=lambda args, **kwargs: command(args),
        )


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
            runner=lambda args, **kwargs: command(args, code=1, stderr="busy"),
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
    outcome = disconnect_server(processes=GUI, close_gui=True, runner=runner)
    assert outcome.gui_closed is True
    assert calls == [["protonvpn", "disconnect"], ["protonvpn", "disconnect"]]


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
