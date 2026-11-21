"""Tiny newline-delimited JSON protocol over a Unix socket.

Only one user can reach the socket (the runtime directory is mode 0700), so
there is no network surface and no token to manage.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any, Awaitable, Callable

from pno import paths
from pno.errors import IpcError, PnoError

MAX_LINE = 4 * 1024 * 1024


def request(
    command: str,
    args: dict[str, Any] | None = None,
    *,
    socket_path: Path | None = None,
    timeout: float = 60.0,
) -> Any:
    """Send one command and return its result (raises on errors)."""
    target = socket_path or paths.socket_path()
    payload = (
        json.dumps({"command": command, "args": args or {}}, ensure_ascii=False).encode(
            "utf-8"
        )
        + b"\n"
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(target))
            client.sendall(payload)
            buffer = bytearray()
            while b"\n" not in buffer:
                chunk = client.recv(65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > MAX_LINE:
                    raise IpcError("daemon response is too large")
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
        raise IpcError(
            "cannot talk to the pno daemon",
            detail=str(exc),
            hint="Start it with 'pno daemon run' or 'systemctl --user start pno-daemon'.",
        ) from exc

    line = bytes(buffer).split(b"\n", 1)[0]
    if not line:
        raise IpcError("daemon closed the connection without a response")
    try:
        response = json.loads(line)
    except ValueError as exc:
        raise IpcError(
            "daemon sent invalid JSON", detail=line[:300].decode("utf-8", "replace")
        ) from exc
    if not isinstance(response, dict):
        raise IpcError("daemon sent an unexpected payload")
    if not response.get("ok"):
        error = response.get("error") or {}
        raise IpcError(
            str(error.get("message") or "daemon command failed"),
            detail=str(error.get("detail") or ""),
        )
    return response.get("result")


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    handler: Callable[[str, dict[str, Any]], Awaitable[Any]],
) -> None:
    try:
        while True:
            try:
                line = await reader.readline()
            except (ConnectionError, asyncio.IncompleteReadError):
                break
            if not line:
                break
            try:
                payload = json.loads(line)
                command = str(payload.get("command") or "")
                arguments = payload.get("args") or {}
                if not command or not isinstance(arguments, dict):
                    raise ValueError("missing command or invalid args")
                result = await handler(command, arguments)
                response = {"ok": True, "result": result}
            except PnoError as exc:
                response = {"ok": False, "error": exc.to_dict()}
            except ValueError as exc:
                response = {
                    "ok": False,
                    "error": {"code": "protocol_error", "message": str(exc)},
                }
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                response = {
                    "ok": False,
                    "error": {
                        "code": "internal_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                }
            writer.write(
                json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
            )
            await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def start_server(
    handler: Callable[[str, dict[str, Any]], Awaitable[Any]],
    *,
    socket_path: Path | None = None,
) -> asyncio.Server:
    target = socket_path or paths.socket_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.chmod(0o700)
    except OSError:
        pass
    if target.exists():
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            probe.connect(str(target))
            probe.close()
        except OSError:
            try:
                target.unlink()
            except OSError:
                pass
        else:
            raise IpcError(
                "another pno daemon is already listening", detail=str(target)
            )

    server = await asyncio.start_unix_server(
        lambda reader, writer: _handle_client(reader, writer, handler),
        path=str(target),
    )
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return server


def stop_server(server: asyncio.Server, *, socket_path: Path | None = None) -> None:
    server.close()
    target = socket_path or paths.socket_path()
    try:
        target.unlink()
    except OSError:
        pass
