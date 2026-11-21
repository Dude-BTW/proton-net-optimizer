from __future__ import annotations

import asyncio
import time

from pno.config import Config
from pno.daemon import Daemon
from pno.proton.serverlist import snapshot_from_payload
from pno.proton.status import VpnStatus
from pno.state import StateStore
from tests.fixtures import helper_payload, server

SERVERS = snapshot_from_payload(
    helper_payload([server("CH-US#3", "CH", "US", 30)]), source="helper"
)


class FakeCache:
    def get(self, **kwargs):
        return SERVERS


def make_daemon(tmp_path, *, vpn=None):
    state = StateStore(tmp_path / "state.json")
    return Daemon(
        Config(),
        state=state,
        server_cache=FakeCache(),
        vpn_reader=lambda snapshot: vpn
        or VpnStatus(connected=False, source="test", checked_at=time.time()),
        socket_path=tmp_path / "run.sock",
    )


def test_status_payload_includes_server_metadata(tmp_path):
    daemon = make_daemon(tmp_path)
    daemon.servers = SERVERS
    payload = daemon.status_payload()
    assert payload["daemon"]["running"] is True
    assert payload["servers"]["count"] == 1
    assert payload["servers"]["source"] == "helper"
    assert payload["paused"] is False


def test_pause_and_resume_are_persisted(tmp_path):
    daemon = make_daemon(tmp_path)
    asyncio.run(daemon._handle("pause", {}))
    assert daemon.state.read()["paused"] is True
    assert daemon.paused is True
    asyncio.run(daemon._handle("resume", {}))
    assert daemon.state.read()["paused"] is False
    # Resume must not trigger an immediate mirror refresh.
    due = daemon.state.read()["next_mirror_due"]
    assert due > time.time()


def test_unknown_command_is_reported(tmp_path):
    from pno.errors import ProtocolError

    daemon = make_daemon(tmp_path)
    try:
        asyncio.run(daemon._handle("nope", {}))
    except ProtocolError as exc:
        assert "nope" in exc.detail
    else:  # pragma: no cover - failure path
        raise AssertionError("expected ProtocolError")


def test_refresh_servers_updates_state(tmp_path):
    daemon = make_daemon(tmp_path)
    result = asyncio.run(daemon.refresh_servers(force=True))
    assert result["count"] == 1
    state = daemon.state.read()
    assert state["servers_count"] == 1
    assert state["servers_source"] == "helper"

    async def scenario():
        server_obj = await daemon._handle("servers.refresh", {"force": True})
        return server_obj

    assert asyncio.run(scenario())["count"] == 1


def test_vpn_change_schedules_only_when_exit_changes(tmp_path):
    daemon = make_daemon(tmp_path)
    previous = VpnStatus(
        connected=True,
        server_name="CH#1",
        exit_country="CH",
        source="test",
        checked_at=time.time(),
    )
    same = VpnStatus(
        connected=True,
        server_name="CH#2",
        exit_country="CH",
        source="test",
        checked_at=time.time(),
    )
    changed = VpnStatus(
        connected=True,
        server_name="US#1",
        exit_country="US",
        source="test",
        checked_at=time.time(),
    )

    async def scenario():
        daemon.vpn_status = previous
        daemon._maybe_trigger_on_vpn_change(previous, same)
        assert daemon._pending_mirror is None
        daemon._maybe_trigger_on_vpn_change(previous, changed)
        assert daemon._pending_mirror is not None
        daemon._pending_mirror.cancel()

    asyncio.run(scenario())
