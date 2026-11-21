from __future__ import annotations

import asyncio

import pytest

from pno import ipc
from pno.errors import IpcError


def test_ipc_roundtrip(tmp_path):
    socket_path = tmp_path / "daemon.sock"

    async def handler(command, args):
        if command == "echo":
            return {"echo": args}
        raise IpcError("unknown", detail=command)

    async def scenario():
        server = await ipc.start_server(handler, socket_path=socket_path)
        try:
            result = await asyncio.to_thread(
                ipc.request, "echo", {"value": 1}, socket_path=socket_path
            )
            assert result == {"echo": {"value": 1}}
            with pytest.raises(IpcError):
                await asyncio.to_thread(
                    ipc.request, "missing", {}, socket_path=socket_path
                )
        finally:
            ipc.stop_server(server, socket_path=socket_path)

    asyncio.run(scenario())
    assert not socket_path.exists()


def test_request_without_daemon(tmp_path):
    with pytest.raises(IpcError):
        ipc.request("status", socket_path=tmp_path / "missing.sock", timeout=1)


def test_stale_socket_is_replaced(tmp_path):
    socket_path = tmp_path / "daemon.sock"
    socket_path.write_text("not a socket", encoding="utf-8")

    async def handler(command, args):
        return {"ok": True}

    async def scenario():
        server = await ipc.start_server(handler, socket_path=socket_path)
        ipc.stop_server(server, socket_path=socket_path)

    asyncio.run(scenario())
