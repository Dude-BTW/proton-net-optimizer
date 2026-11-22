from __future__ import annotations

import json

from pno import events


def test_emit_writes_jsonl(tmp_path):
    events.configure(tmp_path, level="debug")
    payload = events.emit(
        "info", "test", "thing.happened", {"value": 1, "password": "secret"}
    )
    assert payload is not None
    assert payload["data"]["value"] == 1
    assert payload["data"]["password"] == "<redacted>"
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    parsed = json.loads(lines[-1])
    assert parsed["event"] == "thing.happened"
    assert parsed["schema"] == 1
    assert parsed["seq"] == 1


def test_level_filtering(tmp_path):
    events.configure(tmp_path, level="warning")
    assert events.emit("info", "test", "ignored") is None
    assert events.emit("error", "test", "kept") is not None


def test_read_events_skips_torn_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event":"good","level":"info","ts":"2026-01-01T00:00:00Z"}\n'
        '{"event":"truncated"',
        encoding="utf-8",
    )
    found = list(events.read_events([tmp_path]))
    assert [item["event"] for item in found] == ["good"]


def test_rotation_archives_old_file(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "MAX_FILE_BYTES", 200)
    events.configure(tmp_path, level="debug")
    for index in range(20):
        events.emit("info", "test", f"event.{index}", {"padding": "x" * 50})
    archived = list((tmp_path / "archive").glob("*.jsonl"))
    assert archived, "expected at least one archived file"
    names = sorted(path.name for path in tmp_path.glob("*.jsonl"))
    assert "events.jsonl" in names


def test_error_payload_contains_traceback(tmp_path):
    events.configure(tmp_path)
    try:
        raise ValueError("boom")
    except ValueError as exc:
        events.emit("error", "test", "failed", error=exc)
    payload = json.loads(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert payload["error"]["type"] == "ValueError"
    assert "Traceback" in payload["error"]["traceback"]


def test_prune_keeps_newest_archives_and_active_file(tmp_path):
    logs = tmp_path / "logs"
    archive = logs / "archive"
    archive.mkdir(parents=True)
    active = logs / "events.jsonl"
    active.write_text("active\n", encoding="utf-8")
    now = 1_800_000_000
    for index in range(5):
        path = archive / f"events-{index}.jsonl"
        path.write_text("x" * 10, encoding="utf-8")
        import os

        os.utime(path, (now - index * 3600, now - index * 3600))
    result = events.prune(logs, keep_files=2, max_age_days=0, now=now)
    assert result.active_bytes == len("active\n")
    assert result.remaining_files == 2
    assert len(result.removed) == 3
    assert active.is_file()
    names = sorted(path.name for path in archive.glob("*.jsonl"))
    assert names == ["events-0.jsonl", "events-1.jsonl"]


def test_prune_removes_by_age(tmp_path):
    logs = tmp_path / "logs"
    archive = logs / "archive"
    archive.mkdir(parents=True)
    now = 1_800_000_000
    for index, age in enumerate((0, 40, 80)):
        path = archive / f"events-{index}.jsonl"
        path.write_text("x", encoding="utf-8")
        import os

        os.utime(path, (now - age * 86400, now - age * 86400))
    result = events.prune(logs, keep_files=10, max_age_days=30, now=now)
    assert result.remaining_files == 1
    assert len(result.removed) == 2


def test_configure_rotation_applies_limits(tmp_path):
    events.configure(
        tmp_path, level="debug", max_bytes=200, keep_files=1, max_age_days=30
    )
    for index in range(30):
        events.emit("info", "test", f"event.{index}", {"padding": "x" * 30})
    archives = list((tmp_path / "archive").glob("*.jsonl"))
    assert len(archives) <= 1
    assert (tmp_path / "events.jsonl").is_file()
    stats = events.log_stats(tmp_path)
    assert stats.active_bytes > 0


def test_log_stats(tmp_path):
    events.configure(tmp_path, level="debug", max_bytes=10_000)
    events.emit("info", "test", "one")
    stats = events.log_stats(tmp_path)
    assert stats.active_bytes > 0
    assert stats.archive_files == 0
