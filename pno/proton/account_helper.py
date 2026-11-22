"""Standalone helper executed by the *system* Python interpreter.

It reports the signed-in Proton account, the feature flags that decide which
settings the GUI offers, and the protocols installed on this machine. The
Proton libraries live system-wide with ``proton-vpn-gtk-app``; pno itself does
not depend on them.

Only local state is read: the session cache, the feature-flag cache and the
protocol registry. Nothing is sent to Proton and no credential is printed.
Exactly one JSON document goes to stdout; diagnostics stay on stderr.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any


def _account(api: Any) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    try:
        if not api.is_user_logged_in():
            return None, warnings
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return None, [f"cannot read the Proton session: {type(exc).__name__}"]
    account: dict[str, Any] = {"name": "", "plan": "", "tier": 0}
    try:
        account["name"] = api.account_name
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"cannot read the account name: {type(exc).__name__}")
    try:
        data = api.account_data
        account["plan"] = getattr(data, "plan_title", "") or ""
        account["tier"] = int(getattr(data, "max_tier", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"cannot read the account plan: {type(exc).__name__}")
    return account, warnings


def _protocols(api: Any) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    try:
        from proton.vpn.connection import VPNConnection

        registry = api.create_registry()
        result = []
        for cls in registry.iter(interface=VPNConnection):
            result.append(
                {
                    "id": str(cls.protocol),
                    "label": str(cls.ui_protocol),
                    "family": str(cls.get_protocol_group()),
                    "packet_capture": bool(cls.supports_packet_capture()),
                }
            )
        return result, warnings
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"cannot list Proton protocols: {type(exc).__name__}")
        return [], warnings


def _flags(api: Any) -> tuple[dict[str, bool], list[str]]:
    warnings: list[str] = []
    result: dict[str, bool] = {}
    try:
        flags = api.feature_flags
        result["protun"] = bool(flags.get("ProTunV1"))
        result["split_tunneling"] = bool(flags.get("DisplaySplitTunneling"))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"cannot read Proton feature flags: {type(exc).__name__}")
    return result, warnings


async def _collect() -> dict[str, Any]:
    from proton.vpn.core.api import ClientTypeMetadata, ProtonVPNAPI

    api = ProtonVPNAPI(ClientTypeMetadata(type="cli"))
    account, account_warnings = _account(api)
    protocols, protocol_warnings = _protocols(api)
    flags, flag_warnings = _flags(api)
    return {
        "schema": 1,
        "ok": True,
        "fetched_at": time.time(),
        "account": account,
        "features": flags,
        "protocols": protocols,
        "warnings": account_warnings + protocol_warnings + flag_warnings,
    }


def main() -> int:
    try:
        payload = asyncio.run(_collect())
    except Exception as exc:  # noqa: BLE001 - the parent turns this into a typed error
        payload = {
            "schema": 1,
            "ok": False,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 1
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
