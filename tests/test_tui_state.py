from __future__ import annotations

from pno.tui.state import TuiState, TuiStateStore


def test_state_roundtrip(tmp_path):
    store = TuiStateStore(tmp_path / "tui.json")
    state = TuiState(
        tab="tab-secure",
        servers_query="fran",
        secure_query="CH-US",
        servers_inventory_only=True,
        secure_inventory_only=True,
        selected_country="DE",
        selected_server="DE#1",
        selected_secure_server="CH-US#3",
    )
    assert store.save(state) is True
    loaded = store.load()
    assert loaded == state
    assert store.save(state) is True


def test_missing_file_returns_defaults(tmp_path):
    loaded = TuiStateStore(tmp_path / "missing.json").load()
    assert loaded.tab == "tab-servers"
    assert loaded.servers_query == ""


def test_corrupt_file_returns_defaults(tmp_path):
    path = tmp_path / "tui.json"
    path.write_text("{not json", encoding="utf-8")
    assert TuiStateStore(path).load() == TuiState()


def test_invalid_values_are_normalised(tmp_path):
    path = tmp_path / "tui.json"
    path.write_text(
        '{"tab":"nope","servers_query":5,"selected_country":[],'
        '"servers_inventory_only":"yes","selected_server":"CH#1"}',
        encoding="utf-8",
    )
    state = TuiStateStore(path).load()
    assert state.tab == "tab-servers"
    assert state.servers_query == ""
    assert state.selected_country is None
    assert state.servers_inventory_only is False
    assert state.selected_server == "CH#1"
