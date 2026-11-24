from __future__ import annotations

import pytest

from pno.errors import KillSwitchError
from pno.proc import CommandResult
from pno.proton.killswitch import (
    CleanupResult,
    cleanup_stale_killswitch,
    inspect_killswitch,
)
from pno.proton.status import VpnStatus


def command_result(
    args: list[str],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> CommandResult:
    return CommandResult(
        args=tuple(args),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=0,
    )


class FakeNmcli:
    def __init__(self, connections: list[tuple[str, str, str, str]] | None = None):
        self.connections = list(connections or [])
        self.calls: list[list[str]] = []
        self.delete_failures: set[str] = set()
        self.ignore_deletes = False

    def __call__(self, args, *, timeout: float = 10.0) -> CommandResult:
        self.calls.append(list(args))
        if args[:3] == ["nmcli", "-t", "-f"]:
            rows = [
                ":".join((name, kind, device, state))
                for name, kind, device, state in self.connections
            ]
            return command_result(args, stdout="\n".join(rows) + ("\n" if rows else ""))
        if args[:3] == ["nmcli", "connection", "delete"]:
            name = args[3]
            if name in self.delete_failures:
                return command_result(args, stderr="not permitted", returncode=1)
            if not self.ignore_deletes:
                self.connections = [
                    item for item in self.connections if item[0] != name
                ]
            return command_result(args, stdout=f"Connection '{name}' deleted\n")
        raise AssertionError(f"unexpected command: {args}")

    def delete_calls(self) -> list[str]:
        return [
            call[3]
            for call in self.calls
            if call[:3] == ["nmcli", "connection", "delete"]
        ]


def report_status(connected: bool = False) -> VpnStatus:
    return VpnStatus(connected=connected, source="test", checked_at=1.0)


def test_inspect_classifies_transient_and_permanent_profiles():
    runner = FakeNmcli(
        [
            ("pvpn-killswitch-ipv6", "dummy", "ipv6leakintrf0", "activated"),
            ("pvpn-killswitch-perm", "dummy", "pvpnksintrf1", "activated"),
            ("251", "802-11-wireless", "wlan0", "activated"),
            ("ProtonVPN CH-US#3", "vpn", "--", "activated"),
        ]
    )

    report = inspect_killswitch(
        report_status(), setting="off", runner=runner  # type: ignore[arg-type]
    )

    assert [item.name for item in report.transient] == ["pvpn-killswitch-ipv6"]
    assert [item.name for item in report.permanent] == ["pvpn-killswitch-perm"]
    assert report.stale is True
    assert report.reason == "clean"
    assert report.active[0].connection_type == "dummy"
    assert report.active[0].device == "ipv6leakintrf0"


def test_inspect_ignores_unrelated_connections():
    runner = FakeNmcli([("251", "802-11-wireless", "wlan0", "activated")])
    report = inspect_killswitch(
        report_status(), setting="off", runner=runner  # type: ignore[arg-type]
    )
    assert report.transient == ()
    assert report.permanent == ()
    assert report.stale is False
    assert report.reason == "nothing-to-do"


def test_cleanup_skips_when_vpn_is_connected():
    runner = FakeNmcli(
        [("pvpn-killswitch-ipv6", "dummy", "ipv6leakintrf0", "activated")]
    )
    result = cleanup_stale_killswitch(
        setting="off",
        runner=runner,  # type: ignore[arg-type]
        status_reader=lambda: report_status(connected=True),
    )
    assert result.changed is False
    assert result.reason == "vpn-connected"
    assert runner.delete_calls() == []


def test_cleanup_skips_when_killswitch_setting_is_standard():
    runner = FakeNmcli(
        [("pvpn-killswitch-ipv6", "dummy", "ipv6leakintrf0", "activated")]
    )
    result = cleanup_stale_killswitch(
        setting="standard",
        runner=runner,  # type: ignore[arg-type]
        status_reader=report_status,
    )
    assert result.changed is False
    assert result.reason == "killswitch-enabled"
    assert runner.delete_calls() == []


def test_cleanup_skips_when_killswitch_setting_is_unknown():
    runner = FakeNmcli(
        [("pvpn-killswitch-ipv6", "dummy", "ipv6leakintrf0", "activated")]
    )
    result = cleanup_stale_killswitch(
        setting="unknown",
        runner=runner,  # type: ignore[arg-type]
        status_reader=report_status,
    )
    assert result.reason == "killswitch-unknown"
    assert runner.delete_calls() == []


def test_cleanup_removes_only_transient_profiles():
    runner = FakeNmcli(
        [
            ("pvpn-killswitch-ipv6", "dummy", "ipv6leakintrf0", "activated"),
            ("pvpn-killswitch-perm", "dummy", "pvpnksintrf1", "activated"),
        ]
    )
    result = cleanup_stale_killswitch(
        setting="off",
        runner=runner,  # type: ignore[arg-type]
        status_reader=report_status,
    )
    assert result.changed is True
    assert result.removed == ("pvpn-killswitch-ipv6",)
    assert result.reason == "cleaned"
    assert runner.delete_calls() == ["pvpn-killswitch-ipv6"]
    assert [item.name for item in result.report.permanent] == ["pvpn-killswitch-perm"]


def test_cleanup_force_removes_transient_profiles_with_killswitch_on():
    runner = FakeNmcli(
        [
            ("pvpn-killswitch", "dummy", "pvpnksintrf0", "activated"),
            ("pvpn-killswitch-perm", "dummy", "pvpnksintrf1", "activated"),
        ]
    )
    result = cleanup_stale_killswitch(
        setting="advanced",
        force=True,
        runner=runner,  # type: ignore[arg-type]
        status_reader=report_status,
    )
    assert result.changed is True
    assert result.removed == ("pvpn-killswitch",)
    assert runner.delete_calls() == ["pvpn-killswitch"]


def test_cleanup_is_idempotent():
    runner = FakeNmcli(
        [("pvpn-killswitch-ipv6", "dummy", "ipv6leakintrf0", "activated")]
    )
    first = cleanup_stale_killswitch(
        setting="off",
        runner=runner,  # type: ignore[arg-type]
        status_reader=report_status,
    )
    second = cleanup_stale_killswitch(
        setting="off",
        runner=runner,  # type: ignore[arg-type]
        status_reader=report_status,
    )
    assert first.changed is True
    assert second.changed is False
    assert second.reason == "nothing-to-do"
    assert runner.delete_calls() == ["pvpn-killswitch-ipv6"]


def test_cleanup_dry_run_does_not_delete():
    runner = FakeNmcli(
        [("pvpn-killswitch-ipv6", "dummy", "ipv6leakintrf0", "activated")]
    )
    result = cleanup_stale_killswitch(
        setting="off",
        dry_run=True,
        runner=runner,  # type: ignore[arg-type]
        status_reader=report_status,
    )
    assert result.changed is True
    assert result.dry_run is True
    assert result.removed == ()
    assert result.reason == "dry-run"
    assert runner.delete_calls() == []


def test_cleanup_reports_a_delete_failure():
    runner = FakeNmcli(
        [("pvpn-killswitch-ipv6", "dummy", "ipv6leakintrf0", "activated")]
    )
    runner.delete_failures.add("pvpn-killswitch-ipv6")
    with pytest.raises(KillSwitchError) as error:
        cleanup_stale_killswitch(
            setting="off",
            runner=runner,  # type: ignore[arg-type]
            status_reader=report_status,
        )
    assert "not permitted" in (error.value.detail or "")


def test_cleanup_verifies_that_the_profile_is_gone():
    runner = FakeNmcli(
        [("pvpn-killswitch-ipv6", "dummy", "ipv6leakintrf0", "activated")]
    )
    runner.ignore_deletes = True
    with pytest.raises(KillSwitchError) as error:
        cleanup_stale_killswitch(
            setting="off",
            runner=runner,  # type: ignore[arg-type]
            status_reader=report_status,
        )
    assert "still active" in error.value.message


def test_result_payloads_are_machine_readable():
    runner = FakeNmcli(
        [("pvpn-killswitch-ipv6", "dummy", "ipv6leakintrf0", "activated")]
    )
    result: CleanupResult = cleanup_stale_killswitch(
        setting="off",
        runner=runner,  # type: ignore[arg-type]
        status_reader=report_status,
    )
    payload = result.to_dict()
    assert payload["ok"] is True
    assert payload["removed"] == ["pvpn-killswitch-ipv6"]
    assert payload["killswitch"]["stale"] is False
