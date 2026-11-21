from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from textual.widgets import Button, Checkbox, DataTable, Input, TabbedContent

from pno import paths
from pno.proton.serverlist import snapshot_from_payload
from pno.tui import app as tui_app
from pno.tui.i18n import set_language, tr
from pno.tui.modals import GuiConflictScreen
from tests.fixtures import helper_payload, server


class _TestApp(tui_app.PnoApp):
    """Avoid the network and the daemon while exercising the UI."""

    def _refresh_servers_worker(self, force: bool) -> None:  # type: ignore[override]
        return None

    def _remote_status_worker(self) -> None:  # type: ignore[override]
        return None


def seed_servers() -> None:
    snapshot = snapshot_from_payload(
        helper_payload(
            [
                server("UA#1", "UA", "UA", 10, city="Kyiv"),
                server("UA#2", "UA", "UA", 5, city="Kyiv"),
                server("DE#1", "DE", "DE", 30, city="Frankfurt"),
                server("FR#1", "FR", "FR", 20, city="Paris"),
                server("CH-US#3", "CH", "US", 40, features=1, city="New York"),
                server("FR-US#9", "FR", "US", 10, features=1, city="Paris"),
            ]
        ),
        source="helper",
    )
    paths.servers_cache_file().parent.mkdir(parents=True, exist_ok=True)
    paths.servers_cache_file().write_text(
        json.dumps(snapshot.to_dict()), encoding="utf-8"
    )


def test_tui_mounts_and_populates_tables(monkeypatch):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            countries = app.query_one("#country-table", DataTable)
            servers = app.query_one("#server-table", DataTable)
            secure = app.query_one("#secure-table", DataTable)
            assert countries.row_count == 3
            assert servers.row_count == 2
            assert secure.row_count == 2
            assert app.query_one("#tabs", TabbedContent).active == "tab-servers"
            assert app.query_one("#servers-search", Input)
            assert app.query_one("#secure-search", Input)
            assert app.query_one("#servers-inventory", Checkbox)
            assert app.query_one("#secure-inventory", Checkbox)
            assert app.query_one("#disconnect-btn", Button).disabled is True
            app._retranslate()
            await pilot.pause()
            assert app.query_one("#tabs", TabbedContent).get_tab("tab-servers")
        set_language("en")

    asyncio.run(scenario())


def test_tui_language_switch_updates_labels(monkeypatch):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    set_language("uk")

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.config.language = "uk"
            app._retranslate()
            await pilot.pause()
            tab = app.query_one("#tabs", TabbedContent).get_tab("tab-servers")
            assert str(tab.label) == tr("tab.servers")
        set_language("en")

    asyncio.run(scenario())


def test_tui_search_and_inventory_filters(monkeypatch):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()

            app._on_servers_search(SimpleNamespace(value="Frankfurt"))
            await pilot.pause()
            assert app.query_one("#country-table", DataTable).row_count == 1
            assert app.visible_country_codes == ["DE"]

            app._on_servers_search(SimpleNamespace(value=""))
            app._on_servers_inventory(SimpleNamespace(value=True))
            await pilot.pause()
            assert "FR" not in app.visible_country_codes
            assert "UA" in app.visible_country_codes
            assert "DE" in app.visible_country_codes

            app._on_secure_search(SimpleNamespace(value="FR-US"))
            await pilot.pause()
            secure = app.query_one("#secure-table", DataTable)
            assert secure.row_count == 1
            assert app.selected_secure_server == "FR-US#9"

            app._on_secure_search(SimpleNamespace(value=""))
            app._on_secure_inventory(SimpleNamespace(value=True))
            await pilot.pause()
            assert secure.row_count == 1
            assert app.selected_secure_server == "CH-US#3"
        set_language("en")

    asyncio.run(scenario())


def test_gui_conflict_modal_can_be_cancelled(monkeypatch):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(120, 40)) as pilot:
            result = {}
            app.push_screen(
                GuiConflictScreen(
                    title="title",
                    message="message",
                    detail="detail",
                    confirm_label="yes",
                    cancel_label="no",
                ),
                lambda value: result.setdefault("value", value),
            )
            await pilot.pause()
            assert app.screen.query_one("#gui-dialog-title")
            await pilot.press("n")
            await pilot.pause()
            assert result["value"] is False
        set_language("en")

    asyncio.run(scenario())
