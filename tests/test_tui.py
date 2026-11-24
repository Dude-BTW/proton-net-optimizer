from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Select,
    SelectionList,
    Static,
    TabbedContent,
    TextArea,
)

from pno import favorites as favorites_module
from pno import paths
from pno.proton.killswitch import ConnectionInfo, KillSwitchReport
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

    def _requirements_worker(self) -> None:  # type: ignore[override]
        return None

    def _proton_info_worker(self) -> None:  # type: ignore[override]
        return None

    def _killswitch_check_worker(self) -> None:  # type: ignore[override]
        return None


def seed_favorites() -> None:
    target = paths.user_inventory_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        """[secure_core]
servers = ["CH-US#3"]
exits = []

[countries]
known = ["UA", "DE"]
include_all = true
""",
        encoding="utf-8",
    )


def seed_servers() -> None:
    seed_favorites()
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


def test_mirror_table_keeps_http_and_https_rows_for_one_host(monkeypatch):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            payload = {
                "target_country": "CH",
                "selected": [{"host": "mirror.puzzle.ch"}],
                "candidates": [
                    {
                        "url": "https://mirror.puzzle.ch/archlinux/",
                        "host": "mirror.puzzle.ch",
                        "protocol": "https",
                        "country_code": "CH",
                        "ok": True,
                        "latency_ms": 10.0,
                        "score": 1.0,
                    },
                    {
                        "url": "http://mirror.puzzle.ch/archlinux/",
                        "host": "mirror.puzzle.ch",
                        "protocol": "http",
                        "country_code": "CH",
                        "ok": True,
                        "latency_ms": 12.0,
                        "score": 1.0,
                    },
                ],
            }
            app._after_mirrors(payload)
            await pilot.pause()
            table = app.query_one("#mirror-table", DataTable)
            assert table.row_count == 2
        set_language("en")

    asyncio.run(scenario())


def test_mirror_info_reports_stale_sync_databases(monkeypatch):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(140, 45)) as pilot:
            app._render_mirror_info(
                {
                    "last_mirror_country": "CH",
                    "last_mirror_source": "vpn-exit",
                    "pacman_db": {
                        "available": True,
                        "stale": True,
                        "reasons": ["too-old"],
                        "oldest_age_hours": 125.0,
                        "skew_hours": 0.5,
                        "databases": [
                            {
                                "name": "extra",
                                "modified_at": 1.0,
                                "age_hours": 125.0,
                            }
                        ],
                    },
                }
            )
            await pilot.pause()
            rendered = str(app.query_one("#mirror-info", Static).render())
            assert "extra.db" in rendered
            assert "pacman -Syu" in rendered
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


def test_tui_session_state_is_restored(monkeypatch):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        first = _TestApp()
        async with first.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            first._on_servers_search(SimpleNamespace(value="Frankfurt"))
            first._on_servers_inventory(SimpleNamespace(value=True))
            first._on_secure_inventory(SimpleNamespace(value=True))
            first._on_secure_search(SimpleNamespace(value="CH-US"))
            first.query_one("#tabs", TabbedContent).active = "tab-secure"
            await pilot.pause()
            first._flush_state()

        second = _TestApp()
        async with second.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            assert second.servers_query == "Frankfurt"
            assert second.servers_inventory_only is True
            assert second.secure_query == "CH-US"
            assert second.secure_inventory_only is True
            assert second.selected_country_code == "DE"
            assert second.selected_server == "DE#1"
            assert second.selected_secure_server == "CH-US#3"
            assert second.query_one("#tabs", TabbedContent).active == "tab-secure"
        set_language("en")

    asyncio.run(scenario())


def test_current_server_load_and_comparison_are_visible(monkeypatch):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.remote_status = {
                "vpn": {
                    "connected": True,
                    "server_name": "UA#1",
                    "exit_country": "UA",
                    "load": 10,
                }
            }
            app._refresh_status()
            await pilot.pause()
            status = str(app.query_one("#status-text", Static).content)
            banner = str(app.query_one("#current-info", Static).content)
            assert "load 10%" in status
            assert "UA#1" in banner
            assert "10%" in banner
            assert "UA#2" in banner
            assert "best regular" in banner
            assert "5%" in banner
            content = app.query_one("#current-info", Static).content
            from pno.tui import theme

            styles = {str(span.style) for span in content.spans}
            assert any(theme.GREEN in style for style in styles)
            assert any(theme.RED in style for style in styles)
            assert any(theme.MAUVE in style for style in styles)
            status_content = app.query_one("#status-text", Static).content
            status_styles = {str(span.style) for span in status_content.spans}
            assert any(theme.GREEN in style for style in status_styles)
            assert any(theme.LABEL in style for style in status_styles)
        set_language("en")

    asyncio.run(scenario())


def test_requirements_modal_shows_missing_components(monkeypatch):
    from pno.requirements import FAIL, ComponentCheck, RequirementReport

    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(120, 40)) as pilot:
            report = RequirementReport(
                checks=(
                    ComponentCheck(
                        "proton-data",
                        "Proton server data",
                        FAIL,
                        "no data",
                        hint="install proton-vpn-gtk-app",
                        required=True,
                    ),
                )
            )
            app._apply_requirements(report)
            await pilot.pause()
            assert app.screen.query_one("#requirements-title")
            await pilot.click("#requirements-continue")
            await pilot.pause()

    asyncio.run(scenario())


def test_language_change_is_remembered(monkeypatch, tmp_path):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app._on_language(SimpleNamespace(value="uk"))
            await pilot.pause()
        from pno import config as config_module

        assert config_module.load().language == "uk"
        second = _TestApp()
        assert second.config.language == "uk"
        assert tr("servers.connect") == "Підключити"
        set_language("en")

    asyncio.run(scenario())


def test_favorites_tab_loads_and_locks_are_synced(monkeypatch):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            tabs = app.query_one("#tabs", TabbedContent)
            assert tabs.get_tab("tab-favorites")
            selection = app.query_one("#favorites-list", SelectionList)
            text = app.query_one("#favorites-text", TextArea)
            assert text.text == "DE\nUA\nCH-US#3\n"
            assert set(selection.selected) == {
                "country:DE",
                "country:UA",
                "sc:CH-US#3",
            }

            # Visual filter changes only the visible options.
            app.query_one("#favorites-regular", Checkbox).value = False
            app._favorites_filter_changed()
            await pilot.pause()
            assert selection.option_count == 2
            app.query_one("#favorites-regular", Checkbox).value = True
            app._favorites_filter_changed()
            await pilot.pause()

            # Editing text locks the visual list.
            text.text = "CH-US#3\n"
            await pilot.pause()
            assert app.favorites_dirty_side == "text"
            assert selection.disabled is True
            app._apply_favorites()
            await pilot.pause()
            assert app.favorites_dirty_side is None
            assert selection.disabled is False
            assert favorites_module.load().countries == ()
            assert favorites_module.load().secure_core_servers == ("CH-US#3",)

            # Visual editing locks the text editor.
            selection.select("country:FR")
            await pilot.pause()
            assert app.favorites_dirty_side == "visual"
            assert text.read_only is True
            app._apply_favorites()
            await pilot.pause()
            assert app.favorites_dirty_side is None
            assert "FR" in favorites_module.load().countries
            assert text.text.startswith("FR\n")
        set_language("en")

    asyncio.run(scenario())


def test_favorites_revert_restores_last_saved_state(monkeypatch):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            text = app.query_one("#favorites-text", TextArea)
            original = text.text
            text.text = "invalid entry @@@"
            await pilot.pause()
            assert app.favorites_dirty_side == "text"
            app._revert_favorites()
            await pilot.pause()
            assert app.favorites_dirty_side is None
            assert text.text == original
            assert app.query_one("#favorites-list", SelectionList).disabled is False
        set_language("en")

    asyncio.run(scenario())


def seed_proton_settings() -> None:
    target = paths.proton_settings_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "protocol": "protun-smart",
                "killswitch": 0,
                "ipv6": True,
                "telemetry": False,
                "anonymous_crash_reports": False,
                "features": {
                    "netshield": 2,
                    "vpn_accelerator": True,
                    "moderate_nat": False,
                    "port_forwarding": False,
                    "split_tunneling": {
                        "enabled": False,
                        "mode": "exclude",
                        "config_by_mode": {
                            "exclude": {
                                "mode": "exclude",
                                "app_paths": [],
                                "ip_ranges": [],
                            },
                            "include": {
                                "mode": "include",
                                "app_paths": [],
                                "ip_ranges": [],
                            },
                        },
                    },
                },
                "custom_dns": {"enabled": False, "ip_list": []},
            }
        ),
        encoding="utf-8",
    )


def test_tui_proton_tab_loads_proton_settings(monkeypatch):
    seed_servers()
    seed_proton_settings()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    monkeypatch.setattr(tui_app, "find_gui_processes", lambda *a, **k: [])

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            tabs = app.query_one("#tabs", TabbedContent)
            assert tabs.get_tab("tab-proton")
            tabs.active = "tab-proton"
            await pilot.pause()
            assert app.query_one("#proton-netshield", Select).value == (
                "malware-ads-trackers"
            )
            assert app.query_one("#proton-killswitch", Select).value == "off"
            assert app.query_one("#proton-protocol", Select).value == "protun-smart"
            assert app.query_one("#proton-telemetry", Checkbox).value is False
            assert app.query_one("#proton-account", Static).content == tr(
                "proton.account.none"
            )
            assert app.query_one("#proton-killswitch-clean-btn", Button).disabled
            assert app._proton_dirty is False
        set_language("en")

    asyncio.run(scenario())


def test_tui_proton_protocol_family_toggle(monkeypatch):
    seed_servers()
    seed_proton_settings()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    monkeypatch.setattr(tui_app, "find_gui_processes", lambda *a, **k: [])

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            protun = app.query_one("#proton-protun", Checkbox)
            protocol = app.query_one("#proton-protocol", Select)
            assert protun.value is True
            assert protocol.value == "protun-smart"
            protun.value = False
            await pilot.pause()
            assert protocol.value == "wireguard"
            assert app._proton_dirty is True
            protun.value = True
            await pilot.pause()
            assert protocol.value == "protun-smart"
            assert app._proton_dirty is False
        set_language("en")

    asyncio.run(scenario())


def test_tui_proton_split_mode_loads_its_own_app_list(monkeypatch):
    seed_servers()
    target = paths.proton_settings_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "protocol": "wireguard",
                "features": {
                    "split_tunneling": {
                        "enabled": True,
                        "mode": "exclude",
                        "config_by_mode": {
                            "exclude": {
                                "mode": "exclude",
                                "app_paths": ["/usr/bin/a"],
                                "ip_ranges": [],
                            },
                            "include": {
                                "mode": "include",
                                "app_paths": ["/usr/bin/b"],
                                "ip_ranges": [],
                            },
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    monkeypatch.setattr(tui_app, "find_gui_processes", lambda *a, **k: [])

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            apps = app.query_one("#proton-split-apps", TextArea)
            mode = app.query_one("#proton-split-mode", Select)
            assert apps.text == "/usr/bin/a"
            mode.value = "include"
            await pilot.pause()
            assert apps.text == "/usr/bin/b"
            mode.value = "exclude"
            await pilot.pause()
            assert apps.text == "/usr/bin/a"
            assert app._proton_dirty is False
        set_language("en")

    asyncio.run(scenario())


def test_tui_proton_tab_offers_stale_killswitch_cleanup(monkeypatch):
    seed_servers()
    seed_proton_settings()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    monkeypatch.setattr(tui_app, "find_gui_processes", lambda *a, **k: [])

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            app.query_one("#tabs", TabbedContent).active = "tab-proton"
            await pilot.pause()
            app._apply_killswitch_report(
                KillSwitchReport(
                    vpn_connected=False,
                    setting="off",
                    active=(
                        ConnectionInfo(
                            name="pvpn-killswitch-ipv6",
                            connection_type="dummy",
                            device="ipv6leakintrf0",
                            state="activated",
                        ),
                    ),
                    transient=(
                        ConnectionInfo(
                            name="pvpn-killswitch-ipv6",
                            connection_type="dummy",
                            device="ipv6leakintrf0",
                            state="activated",
                        ),
                    ),
                )
            )
            await pilot.pause()
            label = app.query_one("#proton-killswitch-stale", Static)
            button = app.query_one("#proton-killswitch-clean-btn", Button)
            assert "pvpn-killswitch-ipv6" in str(label.content)
            assert button.disabled is False
        set_language("en")

    asyncio.run(scenario())


def test_tui_proton_cards_share_one_control_width(monkeypatch):
    seed_servers()
    seed_proton_settings()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    monkeypatch.setattr(tui_app, "find_gui_processes", lambda *a, **k: [])

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 60)) as pilot:
            await pilot.pause()
            tabs = app.query_one("#tabs", TabbedContent)
            tabs.active = "tab-proton"
            await pilot.pause()
            widths = {
                "select": app.query_one("#proton-netshield", Select).outer_size.width,
                "input": app.query_one("#proton-dns", Input).outer_size.width,
                "checkbox": app.query_one("#proton-ipv6", Checkbox).outer_size.width,
                "textarea": app.query_one(
                    "#proton-split-apps", TextArea
                ).outer_size.width,
            }
            assert len(set(widths.values())) == 1, widths
            assert widths["select"] > 0
            card = app.query_one("#proton-card-features")
            assert card.size.width <= 80
            assert card.parent is app.query_one("#proton-layout")
            actions = app.query_one("#proton-actions")
            assert actions.parent is app.query_one("#proton-actions-row")
            assert actions.parent.parent is app.query_one("#proton-tab")
            assert actions.outer_size.width == card.outer_size.width
            from textual.color import Color

            from pno.tui import theme

            for card_id, color in (
                ("proton-card-account", theme.CARD_ACCOUNT_BG),
                ("proton-card-features", theme.CARD_FEATURES_BG),
                ("proton-card-connection", theme.CARD_CONNECTION_BG),
                ("proton-card-general", theme.CARD_GENERAL_BG),
                ("proton-card-gui", theme.CARD_GUI_BG),
            ):
                widget = app.query_one(f"#{card_id}")
                assert widget.styles.background == Color.parse(color)
        set_language("en")

    asyncio.run(scenario())


def test_tui_proton_apply_writes_changes(monkeypatch):
    seed_servers()
    seed_proton_settings()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    monkeypatch.setattr(tui_app, "find_gui_processes", lambda *a, **k: [])

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            app.query_one("#proton-netshield", Select).value = "malware-only"
            app.query_one("#proton-ipv6", Checkbox).value = False
            await pilot.pause()
            assert app._proton_dirty is True
            app._apply_proton()
            await pilot.pause()
            raw = json.loads(paths.proton_settings_file().read_text(encoding="utf-8"))
            assert raw["features"]["netshield"] == 1
            assert raw["ipv6"] is False
            assert app._proton_dirty is False
        set_language("en")

    asyncio.run(scenario())


def test_tui_proton_revert_restores_the_file_values(monkeypatch):
    seed_servers()
    seed_proton_settings()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    monkeypatch.setattr(tui_app, "find_gui_processes", lambda *a, **k: [])

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            select = app.query_one("#proton-netshield", Select)
            select.value = "off"
            await pilot.pause()
            assert app._proton_dirty is True
            app._revert_proton()
            await pilot.pause()
            assert select.value == "malware-ads-trackers"
            assert app._proton_dirty is False
        set_language("en")

    asyncio.run(scenario())


def test_tui_proton_disconnected_only_controls_are_locked(monkeypatch):
    seed_servers()
    seed_proton_settings()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    monkeypatch.setattr(tui_app, "find_gui_processes", lambda *a, **k: [])

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            app.remote_status = {"vpn": {"connected": True, "server_name": "UA#1"}}
            app._refresh_status()
            await pilot.pause()
            assert app.query_one("#proton-killswitch", Select).disabled is True
            assert app.query_one("#proton-protocol", Select).disabled is True
            assert app.query_one("#proton-netshield", Select).disabled is False
            assert app.query_one("#proton-apply-btn", Button).disabled is True
        set_language("en")

    asyncio.run(scenario())


def test_tui_proton_tab_is_translated(monkeypatch):
    seed_servers()
    seed_proton_settings()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    monkeypatch.setattr(tui_app, "find_gui_processes", lambda *a, **k: [])

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            app.config.language = "uk"
            app._retranslate()
            await pilot.pause()
            tabs = app.query_one("#tabs", TabbedContent)
            assert str(tabs.get_tab("tab-proton").label) == tr("tab.proton")
            assert str(app.query_one("#proton-apply-btn", Button).label) == tr(
                "proton.apply"
            )
            assert str(app.query_one("#proton-ipv6", Checkbox).label) == tr(
                "proton.ipv6"
            )
        set_language("en")

    asyncio.run(scenario())


def test_tui_running_app_modal_adds_an_executable(monkeypatch):
    seed_servers()
    seed_proton_settings()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    monkeypatch.setattr(tui_app, "find_gui_processes", lambda *a, **k: [])
    from pno.tui import modals

    monkeypatch.setattr(
        modals,
        "list_running_executables",
        lambda *a, **k: ("/usr/bin/alpha", "/usr/bin/beta"),
    )

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            app.push_screen(tui_app.RunningAppScreen(), app._on_running_app_selected)
            await pilot.pause()
            listing = app.screen.query_one("#running-app-list")
            assert listing.option_count == 2
            await pilot.press("enter")
            await pilot.pause()
            text = app.query_one("#proton-split-apps", TextArea).text
            assert "/usr/bin/alpha" in text
            assert app._proton_dirty is True
        set_language("en")

    asyncio.run(scenario())


def test_tui_proton_gui_running_blocks_editing(monkeypatch):
    seed_servers()
    seed_proton_settings()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})
    monkeypatch.setattr(tui_app, "find_gui_processes", lambda *a, **k: [object()])

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            app._load_proton_settings()
            await pilot.pause()
            assert app.query_one("#proton-apply-btn", Button).disabled is True
            banner = str(app.query_one("#proton-banner", Static).content)
            assert banner == tr("proton.gui_running")
            assert app.query_one("#proton-netshield", Select).disabled is True
        set_language("en")

    asyncio.run(scenario())


def test_log_limits_are_editable_in_the_tui(monkeypatch):
    seed_servers()
    monkeypatch.setattr(tui_app, "mirrors_overview", lambda *a, **k: {})

    async def scenario():
        app = _TestApp()
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            assert app.query_one("#log-max-size", Input)
            assert app.query_one("#log-keep-files", Input)
            assert app.query_one("#log-max-age", Input)
            app.query_one("#log-max-size", Input).value = "5"
            app.query_one("#log-keep-files", Input).value = "3"
            app.query_one("#log-max-age", Input).value = "7"
            app._apply_log_limits()
            await pilot.pause()
            from pno import config as config_module

            loaded = config_module.load()
            assert loaded.log_max_bytes == 5 * 1024 * 1024
            assert loaded.log_keep_files == 3
            assert loaded.log_max_age_days == 7
        set_language("en")

    asyncio.run(scenario())
