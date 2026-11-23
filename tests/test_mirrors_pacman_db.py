from __future__ import annotations

import os
import time

from pno.mirrors.pacman_db import describe, format_age_hours, read_sync_databases


def touch(path, *, age_hours: float, now: float) -> None:
    path.write_bytes(b"fixture")
    stamp = now - age_hours * 3600.0
    os.utime(path, (stamp, stamp))


def test_missing_directory_is_not_an_error(tmp_path):
    report = read_sync_databases(tmp_path / "absent", now=time.time())
    assert report.available is False
    assert report.databases == ()
    assert report.stale is False
    assert "sync databases found" in describe(report)


def test_fresh_databases_are_ok(tmp_path):
    now = time.time()
    touch(tmp_path / "core.db", age_hours=1.0, now=now)
    touch(tmp_path / "extra.db", age_hours=1.5, now=now)
    report = read_sync_databases(
        tmp_path, now=now, max_age_hours=24.0, max_skew_hours=6.0
    )
    assert report.available is True
    assert [database.name for database in report.databases] == ["core", "extra"]
    assert report.oldest_age_hours is not None
    assert 1.4 < report.oldest_age_hours < 1.6
    assert report.stale is False
    assert report.reasons == ()
    assert describe(report).startswith("sync databases are up to date")


def test_old_databases_are_stale(tmp_path):
    now = time.time()
    touch(tmp_path / "core.db", age_hours=30.0, now=now)
    touch(tmp_path / "extra.db", age_hours=31.0, now=now)
    report = read_sync_databases(tmp_path, now=now, max_age_hours=24.0)
    assert report.stale is True
    assert "too-old" in report.reasons
    assert "extra.db is" in describe(report)
    assert describe(report).endswith("old")


def test_repositories_that_disagree_are_stale(tmp_path):
    now = time.time()
    touch(tmp_path / "core.db", age_hours=1.0, now=now)
    touch(tmp_path / "extra.db", age_hours=10.0, now=now)
    report = read_sync_databases(
        tmp_path, now=now, max_age_hours=24.0, max_skew_hours=6.0
    )
    assert report.stale is True
    assert report.reasons == ("skewed",)
    assert "repositories differ by" in describe(report)


def test_signatures_and_partial_files_are_ignored(tmp_path):
    now = time.time()
    touch(tmp_path / "core.db", age_hours=1.0, now=now)
    touch(tmp_path / "core.db.sig", age_hours=100.0, now=now)
    touch(tmp_path / "extra.db.part", age_hours=100.0, now=now)
    touch(tmp_path / "extra.db.files", age_hours=100.0, now=now)
    report = read_sync_databases(tmp_path, now=now)
    assert [database.name for database in report.databases] == ["core"]
    assert report.stale is False


def test_report_serialises_to_json_ready_dict(tmp_path):
    now = time.time()
    touch(tmp_path / "extra.db", age_hours=30.0, now=now)
    payload = read_sync_databases(tmp_path, now=now, max_age_hours=24.0).to_dict()
    assert payload["available"] is True
    assert payload["stale"] is True
    assert payload["reasons"] == ["too-old"]
    assert payload["databases"][0]["name"] == "extra"
    assert isinstance(payload["oldest_age_hours"], float)


def test_format_age_hours():
    assert format_age_hours(None) == "unknown"
    assert format_age_hours(0.0) == "0m"
    assert format_age_hours(0.75) == "45m"
    assert format_age_hours(5.5) == "5h 30m"
    assert format_age_hours(125.0) == "5d 5h"
