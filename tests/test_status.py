from __future__ import annotations

from pno.proton.serverlist import snapshot_from_payload
from pno.proton.status import VpnStatus, read_proton_vpn_status, read_vpn_status
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
    assert status.load == 40
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
    assert status.load == 40


def test_fallback_disconnected():
    from pno.errors import VpnError

    def runner(args):
        if args[0] == "nmcli":
            raise VpnError("nmcli missing")
        return "Status: Disconnected\n"

    status = read_vpn_status(snap(), runner=runner)
    assert status.connected is False
    assert status.source == "protonvpn-cli"


def test_proton_status_reader_skips_networkmanager():
    calls = []

    def runner(args):
        calls.append(args[0])
        return "Status: Connected\nServer: SE#56 in Sweden\nLoad: 28%\n"

    status = read_proton_vpn_status(runner=runner)
    assert calls == ["protonvpn"]
    assert status.connected is True
    assert status.server_name == "SE#56"
    assert status.source == "protonvpn-cli"


def test_proton_status_reader_reports_disconnected():
    status = read_proton_vpn_status(runner=lambda args: "Status: Disconnected\n")
    assert status.connected is False
    assert status.source == "protonvpn-cli"


def test_status_load_survives_serialisation():
    status = VpnStatus(
        connected=True,
        server_name="CH#1",
        exit_country="CH",
        load=73,
        source="test",
        checked_at=1.0,
    )
    restored = VpnStatus.from_dict(status.to_dict())
    assert restored.load == 73
