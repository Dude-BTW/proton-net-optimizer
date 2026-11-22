"""Read the signed-in Proton account and the available protocol catalog.

The data comes from :mod:`pno.proton.account_helper`, executed with the system
Python that already has the Proton libraries. Everything here is offline: the
helper reads Proton's session and feature-flag caches, and pno never stores a
credential.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pno.errors import HelperError, PnoError
from pno.proc import run
from pno.proton.serverlist import system_python
from pno.proton.settings import FALLBACK_PROTOCOLS, ProtocolOption

HELPER_PATH = Path(__file__).with_name("account_helper.py")


@dataclass(frozen=True)
class AccountInfo:
    name: str
    plan: str
    tier: int

    @property
    def paid(self) -> bool:
        return self.tier >= 1

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "plan": self.plan, "tier": self.tier}


@dataclass(frozen=True)
class ProtonFeatures:
    protun: bool = False
    split_tunneling: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "protun": self.protun,
            "split_tunneling": self.split_tunneling,
        }


@dataclass(frozen=True)
class ProtonInfo:
    account: AccountInfo | None = None
    features: ProtonFeatures = field(default_factory=ProtonFeatures)
    protocols: tuple[ProtocolOption, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def tier(self) -> int | None:
        return self.account.tier if self.account else None

    @property
    def catalog(self) -> tuple[ProtocolOption, ...]:
        return self.protocols or FALLBACK_PROTOCOLS

    def protocol_label(self, protocol: str) -> str:
        for option in self.catalog:
            if option.id == protocol:
                return option.label
        return protocol

    def protocol_family(self, protocol: str) -> str:
        for option in self.catalog:
            if option.id == protocol:
                return option.family
        return "protun" if protocol.startswith("protun-") else "generic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account.to_dict() if self.account else None,
            "features": self.features.to_dict(),
            "protocols": [option.to_dict() for option in self.protocols],
            "warnings": list(self.warnings),
        }


class ProtonInfoClient:
    """Run the helper and normalise its payload; never raises for bad output."""

    def __init__(
        self,
        helper_path: Path | None = None,
        *,
        timeout: float = 15.0,
        runner: Callable[..., Any] = run,
    ) -> None:
        self.helper_path = helper_path or HELPER_PATH
        self.timeout = timeout
        self._runner = runner

    def fetch(self) -> ProtonInfo:
        result = self._runner(
            [system_python(), str(self.helper_path)],
            timeout=self.timeout,
            error_factory=HelperError,
            error_detail="proton account helper",
        )
        if not result.stdout.strip():
            raise HelperError(
                "proton account helper returned no output",
                detail=result.stderr[-500:],
            )
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise HelperError(
                "cannot parse the proton account helper output", detail=str(exc)
            ) from exc
        if not isinstance(payload, dict) or not payload.get("ok", False):
            error = payload.get("error") if isinstance(payload, dict) else None
            raise HelperError(
                "proton account helper failed",
                detail=(
                    f"{error.get('type')}: {error.get('message')}"
                    if isinstance(error, dict)
                    else "unexpected payload"
                ),
            )
        return _payload_to_info(payload)


def fetch_info(timeout: float = 15.0) -> ProtonInfo:
    """Convenience wrapper used by the CLI and the TUI."""
    return ProtonInfoClient(timeout=timeout).fetch()


def try_fetch_info(timeout: float = 15.0) -> tuple[ProtonInfo, str | None]:
    """Return the account information plus an optional human-readable error."""
    try:
        return fetch_info(timeout), None
    except PnoError as exc:
        return ProtonInfo(warnings=(exc.message,)), exc.message


def _payload_to_info(payload: dict[str, Any]) -> ProtonInfo:
    account = None
    raw_account = payload.get("account")
    if isinstance(raw_account, dict):
        try:
            tier = int(raw_account.get("tier") or 0)
        except (TypeError, ValueError):
            tier = 0
        account = AccountInfo(
            name=str(raw_account.get("name") or ""),
            plan=str(raw_account.get("plan") or ""),
            tier=tier,
        )
    raw_features = payload.get("features")
    features = ProtonFeatures(
        protun=(
            bool(raw_features.get("protun"))
            if isinstance(raw_features, dict)
            else False
        ),
        split_tunneling=(
            bool(raw_features.get("split_tunneling"))
            if isinstance(raw_features, dict)
            else False
        ),
    )
    protocols: list[ProtocolOption] = []
    for item in payload.get("protocols") or []:
        if not isinstance(item, dict):
            continue
        protocol_id = item.get("id")
        if not isinstance(protocol_id, str) or not protocol_id:
            continue
        protocols.append(
            ProtocolOption(
                id=protocol_id,
                label=str(item.get("label") or protocol_id),
                family=str(item.get("family") or "generic"),
                packet_capture=bool(item.get("packet_capture")),
            )
        )
    warnings = tuple(
        str(item) for item in payload.get("warnings") or [] if isinstance(item, str)
    )
    return ProtonInfo(
        account=account,
        features=features,
        protocols=tuple(protocols),
        warnings=warnings,
    )
