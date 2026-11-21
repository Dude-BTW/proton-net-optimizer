from __future__ import annotations

from pno.proton.serverlist import snapshot_from_payload
from pno.proton.status import read_vpn_status
from tests.fixtures import helper_payload, server


def snap():
    return snapshot_from_payload(
        helper_payload([server("CH-US#3", "CH", "US", 40)]), source="helper"
    )


def test_nmcli_connected():
    def runner(args):
        assert args[0] == "nmcli"
        return "ProtonVPN CH-US#3:vpn\n"

    status = read_vpn_status(snap(), runner=runner)
    assert status.connected is True
    assert status.server_name == "CH-US#3"
    assert status.exit_country == "US"
    assert status.entry_country == "CH"
    assert status.source == "networkmanager"


def test_nmcli_disconnected():
    status = read_vpn_status(snap(), runner=lambda args: "wlan0:wifi\n")
    assert status.connected is False
    assert status.exit_country is None


def test_fallback_to_protonvpn_status():
    from pno.errors import VpnError

    def runner(args):
        if args[0] == "nmcli":
            raise VpnError("nmcli missing")
        return "Status: Connected\nServer: CH-US#3 in United States\nLoad: 40%\n"

    status = read_vpn_status(snap(), runner=runner)
    assert status.connected is True
    assert status.server_name == "CH-US#3"
    assert status.source == "protonvpn-cli"
    assert status.exit_country == "US"


def test_fallback_disconnected():
    from pno.errors import VpnError

    def runner(args):
        if args[0] == "nmcli":
            raise VpnError("nmcli missing")
        return "Status: Disconnected\n"

    status = read_vpn_status(snap(), runner=runner)
    assert status.connected is False
    assert status.source == "protonvpn-cli"
