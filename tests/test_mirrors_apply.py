from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pno.config import Config
from pno.errors import ApplyError, PrivilegeError
from pno.mirrors.apply import apply_plan
from pno.mirrors.planner import MirrorPlan
from pno.mirrors.validate import validate_mirrorlist
from pno.proc import CommandResult

VALID = """\
##
## generated
##
Server = https://mirror.example/archlinux/$repo/os/$arch
Server = http://mirror2.example/archlinux/$repo/os/$arch
"""


def plan(content: str = VALID) -> MirrorPlan:
    return MirrorPlan(
        target_country="UA",
        target_source="home",
        candidates=[],
        selected=[],
        content=content,
        measured_at=0.0,
    )


def result(stdout: str = "", stderr: str = "", code: int = 0) -> CommandResult:
    return CommandResult(
        args=("sudo",),
        returncode=code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1,
    )


def test_validation_accepts_a_generated_file():
    validation = validate_mirrorlist(VALID)
    assert validation.ok
    assert len(validation.servers) == 2


@pytest.mark.parametrize(
    "content",
    [
        "",
        "Server = ftp://mirror.example/archlinux/$repo/os/$arch\n",
        "Server = https://mirror.example/archlinux/$repo/os/x86_64\n",
        "Server = https://mirror.example/archlinux/$repo/os/$arch\n"
        "Server = https://mirror.example/archlinux/$repo/os/$arch\n",
        "Server = rsync://mirror.example/archlinux/$repo/os/$arch\n",
    ],
)
def test_validation_rejects_bad_content(content):
    assert not validate_mirrorlist(content).ok


def test_dry_run_reports_change_without_writing(tmp_path):
    target = tmp_path / "mirrorlist"
    target.write_text("Server = https://old.example/archlinux/$repo/os/$arch\n")
    outcome = apply_plan(
        plan(),
        Config(),
        dry_run=True,
        mirrorlist_path=target,
        helper=tmp_path / "missing-helper",
    )
    assert outcome.applied is False
    assert outcome.changed is True
    assert outcome.reason == "dry-run"
    assert target.read_text().startswith("Server = https://old.example")


def test_unchanged_when_server_lines_match(tmp_path):
    target = tmp_path / "mirrorlist"
    target.write_text(VALID)
    outcome = apply_plan(
        plan(),
        Config(),
        mirrorlist_path=target,
        helper=tmp_path / "missing-helper",
    )
    assert outcome.applied is False
    assert outcome.reason == "unchanged"


def test_apply_calls_helper_and_parses_result(tmp_path):
    target = tmp_path / "mirrorlist"
    target.write_text("Server = https://old.example/archlinux/$repo/os/$arch\n")
    helper = tmp_path / "helper"
    helper.write_text("")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return result(
            json.dumps(
                {
                    "ok": True,
                    "servers": 2,
                    "backup": "/var/lib/pno/mirrorlist-1.bak",
                }
            )
        )

    outcome = apply_plan(
        plan(), Config(), mirrorlist_path=target, helper=helper, runner=runner
    )
    assert outcome.applied is True
    assert outcome.servers == 2
    assert calls[0][0][0] == "sudo"
    assert calls[0][0][1] == "-n"
    assert calls[0][0][2] == str(helper)
    assert "input_text" in calls[0][1]


def test_apply_reports_sudo_password_requirement(tmp_path):
    target = tmp_path / "mirrorlist"
    target.write_text("Server = https://old.example/archlinux/$repo/os/$arch\n")
    helper = tmp_path / "helper"
    helper.write_text("")

    def runner(command, **kwargs):
        return result(stderr="sudo: a password is required", code=1)

    with pytest.raises(PrivilegeError):
        apply_plan(
            plan(),
            Config(),
            mirrorlist_path=target,
            helper=helper,
            runner=runner,
        )


def test_apply_reports_helper_rejection(tmp_path):
    target = tmp_path / "mirrorlist"
    target.write_text("Server = https://old.example/archlinux/$repo/os/$arch\n")
    helper = tmp_path / "helper"
    helper.write_text("")

    def runner(command, **kwargs):
        return result(stdout=json.dumps({"ok": False, "error": "invalid"}), code=2)

    with pytest.raises(ApplyError):
        apply_plan(
            plan(),
            Config(),
            mirrorlist_path=target,
            helper=helper,
            runner=runner,
        )


def test_missing_helper_is_reported(tmp_path):
    target = tmp_path / "mirrorlist"
    target.write_text("Server = https://old.example/archlinux/$repo/os/$arch\n")
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return result()

    with pytest.raises(PrivilegeError):
        apply_plan(
            plan(),
            Config(),
            mirrorlist_path=target,
            helper=tmp_path / "nope",
            runner=runner,
        )
    assert calls == []


def test_missing_sudo_is_reported(tmp_path, monkeypatch):
    target = tmp_path / "mirrorlist"
    target.write_text("Server = https://old.example/archlinux/$repo/os/$arch\n")
    helper = tmp_path / "helper"
    helper.write_text("")
    monkeypatch.setattr("pno.mirrors.apply.shutil.which", lambda name: None)
    with pytest.raises(PrivilegeError) as excinfo:
        apply_plan(plan(), Config(), mirrorlist_path=target, helper=helper)
    assert excinfo.value.hint


def test_apply_helper_dry_run_accepts_valid_input():
    helper = Path(__file__).resolve().parents[1] / "pno" / "mirrors" / "apply_helper.py"
    completed = subprocess.run(
        [sys.executable, str(helper), "--dry-run"],
        input=VALID,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["servers"] == 2


def test_apply_helper_rejects_invalid_input():
    helper = Path(__file__).resolve().parents[1] / "pno" / "mirrors" / "apply_helper.py"
    completed = subprocess.run(
        [sys.executable, str(helper), "--dry-run"],
        input="Server = rsync://mirror.example/archlinux/$repo/os/$arch\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
