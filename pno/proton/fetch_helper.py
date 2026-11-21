"""Standalone helper executed by the *system* Python interpreter.

The Proton VPN Python libraries are installed system-wide by the Arch package
``proton-vpn-gtk-app``. Running this file with ``/usr/bin/python3`` reuses the
same session/cache as the GUI without adding Proton as a dependency of this
project.

It prints exactly one JSON document on stdout. Diagnostics from Proton go to
stderr and never mix with the payload.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time


async def _collect() -> dict:
    from proton.vpn.core.api import ClientTypeMetadata, ProtonVPNAPI

    api = ProtonVPNAPI(ClientTypeMetadata(type="cli"))
    server_list = await api.refresher.get_up_to_date_server_list()
    payload = server_list.to_dict()
    max_tier = int(payload.get("MaxTier") or 0)
    servers = []
    for logical in server_list.logicals:
        servers.append(
            {
                "name": logical.name,
                "entry": logical.entry_country,
                "exit": logical.exit_country,
                "city": logical.city or "",
                "load": int(logical.load or 0),
                "score": float(logical.score or 0.0),
                "tier": int(logical.tier or 0),
                "features": sum(int(feature) for feature in logical.features),
                "status": 1 if logical.enabled else 0,
            }
        )
    return {
        "schema": 1,
        "ok": True,
        "fetched_at": time.time(),
        "expires_at": payload.get("LoadsExpirationTime"),
        "max_tier": max_tier,
        "servers": servers,
    }


def main() -> int:
    try:
        payload = asyncio.run(_collect())
    except Exception as exc:  # noqa: BLE001 - the parent turns this into a typed error
        payload = {
            "schema": 1,
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
