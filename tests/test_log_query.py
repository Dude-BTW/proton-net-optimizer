from __future__ import annotations

import json

from pno import events, log_query


def write_events(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_resolve_files_expands_directory_and_archive(tmp_path):
    write_events(
        tmp_path / "events.jsonl", [{"event": "a", "ts": "2026-01-01T00:00:00Z"}]
    )
    write_events(
        tmp_path / "archive" / "events-1.jsonl",
        [{"event": "b", "ts": "2026-01-01T00:00:00Z"}],
    )
    files = log_query.resolve_files([tmp_path])
    assert len(files) == 2


def test_query_filters(tmp_path):
    write_events(
        tmp_path / "events.jsonl",
        [
            {
                "event": "mirrors.apply.done",
                "level": "info",
                "component": "mirrors",
                "ts": "2026-01-01T00:00:00Z",
                "data": {},
            },
            {
                "event": "vpn.state.changed",
                "level": "warning",
                "component": "vpn",
                "ts": "2026-01-01T00:00:00Z",
                "data": {},
            },
            {
                "event": "mirrors.plan.done",
                "level": "error",
                "component": "mirrors",
                "ts": "2026-01-01T00:00:00Z",
                "data": {},
            },
        ],
    )
    files = log_query.resolve_files([tmp_path])
    assert len(log_query.query(files, level="warning")) == 2
    assert len(log_query.query(files, component="mirrors")) == 2
    assert len(log_query.query(files, event_name="mirrors.*")) == 2
    assert len(log_query.query(files, grep="vpn")) == 1
    assert len(log_query.query(files, limit=1)) == 1


def test_summarize():
    summary = log_query.summarize(
        [
            {"event": "a", "level": "info", "component": "x"},
            {"event": "b", "level": "error", "component": "x"},
        ]
    )
    assert summary["total"] == 2
    assert summary["by_level"] == {"info": 1, "error": 1}


def test_torn_line_is_ignored(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"event":"ok","ts":"2026-01-01T00:00:00Z"}\n{"event":', encoding="utf-8"
    )
    assert [event["event"] for event in log_query.iter_events([path])] == ["ok"]


def test_events_module_and_log_query_agree(tmp_path):
    events.configure(tmp_path)
    events.emit("info", "test", "one")
    events.emit("warning", "test", "two")
    found = list(events.read_events([tmp_path]))
    assert [item["event"] for item in found] == ["one", "two"]
    assert (
        log_query.query(log_query.resolve_files([tmp_path]), level="warning")[0][
            "event"
        ]
        == "two"
    )


def test_parse_time_relative():
    now = datetime_now()
    parsed = log_query.parse_time("-1h")
    assert parsed is not None
    assert abs((now - parsed) - 3600) < 10


def datetime_now():
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).timestamp()
