from __future__ import annotations

import json
import threading

from pno.state import StateStore, atomic_write_json, read_json


def test_atomic_write_and_read(tmp_path):
    path = tmp_path / "nested" / "state.json"
    atomic_write_json(path, {"b": 2, "a": 1})
    assert read_json(path) == {"a": 1, "b": 2}
    assert not list(path.parent.glob(".*.tmp.*"))


def test_read_json_tolerates_broken_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_json(path, {"fallback": True}) == {"fallback": True}


def test_state_store_update_is_persistent(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.update(lambda state: state.update({"paused": True}))
    assert store.read()["paused"] is True
    store.set(paused=False)
    assert store.read()["paused"] is False
    assert "updated_at" in store.read()


def test_state_store_serialises_concurrent_writers(tmp_path):
    store = StateStore(tmp_path / "state.json")
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        try:
            for _ in range(20):
                store.update(lambda state: state.setdefault("items", []).append(index))
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(store.read()["items"]) == 80
    # The file must always contain valid JSON, never a torn document.
    json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))


def test_append_event_keeps_limit(tmp_path):
    store = StateStore(tmp_path / "state.json")
    for index in range(5):
        store.append_event("history", {"index": index}, limit=3)
    assert [item["index"] for item in store.read()["history"]] == [2, 3, 4]
