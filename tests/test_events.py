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
