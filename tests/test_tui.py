from __future__ import annotations

import asyncio
import json

from textual.widgets import DataTable, TabbedContent

from pno import paths
from pno.proton.serverlist import snapshot_from_payload
from pno.tui import app as tui_app
from pno.tui.i18n import set_language, tr
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
                server("CH-US#3", "CH", "US", 40, features=1, city="New York"),
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
            assert countries.row_count == 2
            assert servers.row_count == 2
            assert secure.row_count == 1
            assert app.query_one("#tabs", TabbedContent).active == "tab-servers"
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
