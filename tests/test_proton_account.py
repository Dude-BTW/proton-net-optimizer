from __future__ import annotations

import json

import pytest

from pno.errors import HelperError
from pno.proc import CommandResult
from pno.proton.account import (
    AccountInfo,
    ProtonInfo,
    ProtonInfoClient,
    _payload_to_info,
    try_fetch_info,
)
from pno.proton.settings import FALLBACK_PROTOCOLS


def result(stdout: str, returncode: int = 0) -> CommandResult:
    return CommandResult(("helper",), returncode, stdout, "", 1)


PAYLOAD = {
    "schema": 1,
    "ok": True,
    "account": {"name": "user", "plan": "Proton Unlimited", "tier": 2},
    "features": {"protun": True, "split_tunneling": True},
    "protocols": [
        {
            "id": "protun-smart",
            "label": "Smart",
            "family": "protun",
            "packet_capture": True,
        },
        {
            "id": "wireguard",
            "label": "WireGuard",
            "family": "generic",
            "packet_capture": False,
        },
    ],
    "warnings": ["one"],
}


def test_payload_is_normalised():
    info = _payload_to_info(PAYLOAD)
    assert info.account == AccountInfo("user", "Proton Unlimited", 2)
    assert info.account.paid is True
    assert info.features.protun is True
    assert info.features.split_tunneling is True
    assert [option.id for option in info.protocols] == ["protun-smart", "wireguard"]
    assert info.protocol_label("protun-smart") == "Smart"
    assert info.protocol_family("protun-smart") == "protun"
    assert info.protocol_family("openvpn-tcp") == "generic"
    assert info.tier == 2
    assert info.warnings == ("one",)


def test_missing_account_is_not_an_error():
    info = _payload_to_info({**PAYLOAD, "account": None})
    assert info.account is None
    assert info.tier is None


def test_client_parses_helper_output():
    client = ProtonInfoClient(runner=lambda *a, **k: result(json.dumps(PAYLOAD)))
    info = client.fetch()
    assert info.account is not None
    assert info.account.name == "user"


def test_client_rejects_empty_output():
    client = ProtonInfoClient(runner=lambda *a, **k: result(""))
    with pytest.raises(HelperError, match="no output"):
        client.fetch()


def test_client_rejects_invalid_json():
    client = ProtonInfoClient(runner=lambda *a, **k: result("{nope"))
    with pytest.raises(HelperError, match="parse"):
        client.fetch()


def test_client_reports_helper_error():
    payload = {"schema": 1, "ok": False, "error": {"type": "X", "message": "boom"}}
    client = ProtonInfoClient(runner=lambda *a, **k: result(json.dumps(payload)))
    with pytest.raises(HelperError, match="failed"):
        client.fetch()


def test_fallback_catalog_is_used_when_protocols_are_missing():
    info = ProtonInfo()
    assert info.catalog == FALLBACK_PROTOCOLS


def test_try_fetch_info_turns_errors_into_warnings():
    class Broken:
        def fetch(self, timeout=15.0):
            raise HelperError("nope")

    import pno.proton.account as account_module

    original = account_module.fetch_info
    try:
        account_module.fetch_info = Broken().fetch  # type: ignore[assignment]
        info, error = try_fetch_info()
    finally:
        account_module.fetch_info = original
    assert error == "nope"
    assert info.warnings == ("nope",)
