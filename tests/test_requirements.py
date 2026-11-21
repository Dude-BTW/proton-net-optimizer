from __future__ import annotations

from pno import requirements
from pno.config import Config
from pno.requirements import (
    FAIL,
    OK,
    WARN,
    ComponentCheck,
    RequirementReport,
    check_requirements,
)


def test_report_classifies_problems():
    report = RequirementReport(
        checks=(
            ComponentCheck("a", "A", OK, "fine"),
            ComponentCheck("b", "B", WARN, "degraded", hint="install b"),
            ComponentCheck("c", "C", FAIL, "missing", hint="install c", required=True),
        )
    )
    assert report.ok is False
    assert [item.key for item in report.failures] == ["c"]
    assert [item.key for item in report.warnings] == ["b"]
    summary = report.summary()
    assert "FAIL  C: missing" in summary
    assert "WARN  B: degraded" in summary
    assert "install c" in summary
    assert "A: fine" not in summary


def test_all_ok_summary():
    report = RequirementReport(checks=(ComponentCheck("a", "A", OK, "fine"),))
    assert report.ok is True
    assert "All required components" in report.summary()


def test_proton_data_uses_the_cache(tmp_path, monkeypatch):
    cache = tmp_path / "serverlist.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(requirements, "DEFAULT_PROTON_CACHE", cache)
    check = requirements._proton_data_check()
    assert check.status == OK


def test_proton_data_falls_back_to_local_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(requirements, "DEFAULT_PROTON_CACHE", tmp_path / "missing.json")
    compact = tmp_path / "servers.json"
    compact.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(requirements.paths, "servers_cache_file", lambda: compact)
    monkeypatch.setattr(requirements.Path, "glob", lambda self, pattern: [])
    check = requirements._proton_data_check()
    assert check.status == WARN


def test_proton_data_fails_without_any_source(tmp_path, monkeypatch):
    monkeypatch.setattr(requirements, "DEFAULT_PROTON_CACHE", tmp_path / "missing.json")
    monkeypatch.setattr(
        requirements.paths, "servers_cache_file", lambda: tmp_path / "absent.json"
    )
    monkeypatch.setattr(requirements.Path, "glob", lambda self, pattern: [])
    check = requirements._proton_data_check()
    assert check.status == FAIL
    assert "proton-vpn-gtk-app" in check.hint


def test_terminal_check_reports_auto_detection(monkeypatch):
    monkeypatch.setattr(requirements, "resolve_terminal", lambda config: "konsole")
    check = requirements._terminal_check(Config(terminal="kitty"))
    assert check.status == WARN
    assert "konsole" in check.detail


def test_check_requirements_returns_all_sections():
    report = check_requirements(Config(), for_tui=True)
    keys = {check.key for check in report.checks}
    assert {
        "python",
        "arch",
        "pacman",
        "nmcli",
        "protonvpn",
        "proton-data",
        "mirrorlist",
        "sudo",
        "mirror-helper",
        "terminal",
        "config",
        "textual",
    } <= keys
    assert all(check.status in {OK, WARN, FAIL} for check in report.checks)
