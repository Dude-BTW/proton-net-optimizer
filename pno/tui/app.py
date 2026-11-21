"""Textual front-end for proton-net-optimizer."""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from pno import config as config_module
from pno import events
from pno import inventory as inventory_module
from pno import log_query, paths
from pno.config import Config
from pno.errors import PnoError
from pno.ipc import request
from pno.mirrors.service import mirrors_overview
from pno.proton.connection import (
    GuiActiveError,
    connect_server,
    disconnect_server,
    wait_for_disconnected,
)
from pno.proton.models import CountryServers, Server, ServerSnapshot
from pno.proton.selection import regular_countries, secure_core_routes
from pno.proton.serverlist import ServerCache
from pno.proton.status import read_vpn_status
from pno.state import StateStore
from pno.tui.filtering import filter_countries, filter_routes
from pno.tui.flags import country_label, server_label
from pno.tui.i18n import get_language, set_language, tr
from pno.tui.modals import GuiConflictScreen


class PnoApp(App[None]):
    """A small cockpit: suggestions first, automation second."""

    TITLE = "Proton Net Optimizer"
    CSS_PATH = str(Path(__file__).with_name("app.tcss"))
    BINDINGS = [
        Binding("f5", "refresh_all", "Refresh", show=True),
        Binding("c", "connect_selected", "Connect", show=True),
        Binding("d", "disconnect", "Disconnect", show=True),
        Binding("p", "toggle_pause", "Pause", show=True),
        Binding("ctrl+q", "quit", "Quit", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config: Config = config_module.ensure()
        set_language(self.config.language)
        self.inventory = inventory_module.load()
        self.state_store = StateStore(paths.state_file())
        self.snapshot: ServerSnapshot | None = None
        self.snapshot_mtime: float = 0.0

        self.all_countries: list[CountryServers] = []
        self.countries: dict[str, CountryServers] = {}
        self.visible_country_codes: list[str] = []
        self.selected_country_code: str | None = None
        self.selected_server: str | None = None
        self.selected_secure_server: str | None = None
        self.secure_route_map: dict[str, Server] = {}

        self.servers_query = ""
        self.secure_query = ""
        self.servers_inventory_only = False
        self.secure_inventory_only = False
        self.current_server: str | None = None

        self.remote_status: dict[str, Any] | None = None
        self.mirror_result: dict[str, Any] | None = None
        self._rebuilding = False
        self._pending_gui_action: str | None = None
        self._pending_gui_server: str | None = None

    # ------------------------------------------------------------- composition
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="status-bar"):
            yield Static("", id="status-text")
            yield Button(tr("button.pause"), id="pause-btn", variant="warning")
            yield Button(tr("servers.refresh"), id="refresh-btn", variant="primary")
            yield Button(tr("button.disconnect"), id="disconnect-btn", variant="error")
        with TabbedContent(id="tabs"):
            with TabPane(tr("tab.servers"), id="tab-servers"):
                with Vertical(id="servers-layout"):
                    with Horizontal(id="servers-filters"):
                        yield Input(
                            placeholder=tr("servers.search"), id="servers-search"
                        )
                        yield Checkbox(
                            tr("servers.inventory_only"),
                            value=self.servers_inventory_only,
                            id="servers-inventory",
                        )
                        yield Static("", id="servers-count")
                    with Horizontal(id="servers-tables"):
                        yield DataTable(id="country-table")
                        with Vertical(id="server-pane"):
                            yield DataTable(id="server-table")
                            with Horizontal(id="server-actions"):
                                yield Button(
                                    tr("servers.connect"),
                                    id="connect-btn",
                                    variant="success",
                                )
                                yield Button(tr("servers.copy"), id="copy-btn")
                                yield Static(tr("servers.hint"), id="servers-hint")
            with TabPane(tr("tab.secure"), id="tab-secure"):
                with Vertical(id="secure-layout"):
                    with Horizontal(id="secure-filters"):
                        yield Input(placeholder=tr("secure.search"), id="secure-search")
                        yield Checkbox(
                            tr("servers.inventory_only"),
                            value=self.secure_inventory_only,
                            id="secure-inventory",
                        )
                        yield Static("", id="secure-count")
                    yield DataTable(id="secure-table")
                    with Horizontal(id="secure-actions"):
                        yield Button(
                            tr("secure.connect"),
                            id="secure-connect-btn",
                            variant="success",
                        )
            with TabPane(tr("tab.mirrors"), id="tab-mirrors"):
                with Vertical(id="mirror-layout"):
                    yield Static("", id="mirror-info")
                    yield DataTable(id="mirror-table")
                    with Horizontal(id="mirror-actions"):
                        yield Checkbox(
                            tr("mirrors.auto"),
                            value=self.config.auto_mirror,
                            id="auto-mirror",
                        )
                        yield Button(tr("mirrors.preview"), id="mirror-preview-btn")
                        yield Button(
                            tr("mirrors.refresh"),
                            id="mirror-refresh-btn",
                            variant="primary",
                        )
            with TabPane(tr("tab.logs"), id="tab-logs"):
                yield RichLog(id="log-view", wrap=True, highlight=False)
            with TabPane(tr("tab.settings"), id="tab-settings"):
                with Vertical(id="settings-layout"):
                    yield Label(tr("settings.top_n"), id="label-top-n")
                    yield Input(
                        value=str(self.config.top_n), id="top-n", type="integer"
                    )
                    yield Label(tr("settings.home_country"), id="label-home-country")
                    yield Input(value=self.config.home_country, id="home-country")
                    yield Label(tr("settings.terminal"), id="label-terminal")
                    yield Input(value=self.config.terminal, id="terminal")
                    yield Label(tr("settings.language"), id="label-language")
                    yield Select(
                        [
                            (tr(key), code)
                            for code, key in (
                                ("en", "language.en"),
                                ("uk", "language.uk"),
                            )
                        ],
                        value=self.config.language,
                        id="language",
                        allow_blank=False,
                    )
                    yield Button(
                        tr("settings.save"), id="settings-save-btn", variant="primary"
                    )
        yield Footer()

    def on_mount(self) -> None:
        self._configure_tables()
        self._load_snapshot_from_disk()
        self._populate_countries()
        self._populate_secure_core()
        self._load_mirror_overview()
        self._refresh_status()
        self._refresh_logs()
        self.set_interval(3.0, self._tick)
        self._refresh_servers_worker(force=False)
        self._remote_status_worker()
        events.emit("info", "tui", "tui.start", {"language": get_language()})

    def _configure_tables(self) -> None:
        for widget_id in (
            "#country-table",
            "#server-table",
            "#secure-table",
            "#mirror-table",
        ):
            table = self.query_one(widget_id, DataTable)
            table.cursor_type = "row"
            table.zebra_stripes = True

    # ------------------------------------------------------------------- data
    def _load_snapshot_from_disk(self) -> None:
        try:
            mtime = paths.servers_cache_file().stat().st_mtime
        except OSError:
            return
        if mtime == self.snapshot_mtime:
            return
        snapshot = ServerCache(helper_timeout=self.config.proton_helper_timeout).read()
        if snapshot is None:
            return
        self.snapshot = snapshot
        self.snapshot_mtime = mtime

    def _all_regular_countries(self) -> list[CountryServers]:
        if self.snapshot is None:
            return []
        return regular_countries(
            self.snapshot,
            top_n=self.config.top_n,
            include_tor=self.config.include_tor,
            max_tier=self.snapshot.max_tier or None,
            known_countries=self.inventory.known_countries,
            include_all=self.inventory.include_all_countries,
        )

    def _populate_countries(self) -> None:
        table = self.query_one("#country-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            tr("servers.country"),
            tr("servers.best"),
            tr("servers.servers"),
        )
        self.all_countries = self._all_regular_countries()
        visible = filter_countries(
            self.all_countries,
            query=self.servers_query,
            inventory_only=self.servers_inventory_only,
            inventory_codes=self.inventory.country_codes(),
            language=get_language(),
        )
        self.countries = {country.code: country for country in visible}
        self.visible_country_codes = [country.code for country in visible]

        self._rebuilding = True
        try:
            for country in visible:
                table.add_row(
                    country_label(country.code, get_language()),
                    f"{country.best.load}%",
                    str(len(country.servers)),
                    key=country.code,
                )
        finally:
            self._rebuilding = False
        self._update_filter_count(
            "servers-count", len(visible), len(self.all_countries)
        )

        if not visible:
            self.selected_server = None
            self._populate_servers(None)
            return

        selected = (
            self.selected_country_code
            if self.selected_country_code in self.countries
            else self.visible_country_codes[0]
        )
        index = self.visible_country_codes.index(selected)
        table.move_cursor(row=index)
        self.selected_country_code = selected
        self._populate_servers(selected)

    def _populate_servers(self, code: str | None) -> None:
        table = self.query_one("#server-table", DataTable)
        if code:
            self.selected_country_code = code
        table.clear(columns=True)
        table.add_columns(
            tr("servers.server"),
            tr("servers.load"),
            tr("servers.score"),
            tr("servers.city"),
            tr("servers.tags"),
        )
        self.selected_server = None
        country = self.countries.get(code or "")
        if country is None:
            return
        self._rebuilding = True
        try:
            for server in country.servers:
                tags = [
                    tag
                    for tag, enabled in (("p2p", server.p2p), ("tor", server.tor))
                    if enabled
                ]
                if (
                    self.current_server
                    and server.name.upper() == self.current_server.upper()
                ):
                    tags.append(tr("servers.current"))
                table.add_row(
                    server_label(server.name, server.exit_code),
                    f"{server.load}%",
                    f"{server.score:.2f}",
                    server.city or "-",
                    " ".join(tags),
                    key=server.name,
                )
        finally:
            self._rebuilding = False
        if country.servers:
            table.move_cursor(row=0)
            self.selected_server = country.servers[0].name

    def _populate_secure_core(self) -> None:
        table = self.query_one("#secure-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            tr("secure.entry"),
            tr("secure.exit"),
            tr("secure.server"),
            tr("servers.load"),
            tr("servers.score"),
            tr("servers.city"),
            tr("secure.inventory"),
        )
        if self.snapshot is None:
            self.secure_route_map = {}
            self._update_filter_count("secure-count", 0, 0)
            return
        routes = secure_core_routes(
            self.snapshot,
            max_tier=self.snapshot.max_tier or None,
            include_tor=self.config.include_tor,
        )
        visible = filter_routes(
            routes,
            query=self.secure_query,
            inventory_only=self.secure_inventory_only,
            inventory_names=self.inventory.secure_core_servers,
            language=get_language(),
        )
        inventory_names = {name.upper() for name in self.inventory.secure_core_servers}
        self.secure_route_map = {route.name: route for route in visible}

        self._rebuilding = True
        try:
            for route in visible:
                table.add_row(
                    country_label(route.entry_code, get_language()),
                    country_label(route.exit_code, get_language()),
                    server_label(route.name, route.exit_code),
                    f"{route.load}%",
                    f"{route.score:.2f}",
                    route.city or "-",
                    "✓" if route.name.upper() in inventory_names else "",
                    key=route.name,
                )
        finally:
            self._rebuilding = False
        self._update_filter_count("secure-count", len(visible), len(routes))

        if not visible:
            self.selected_secure_server = None
            return
        selected = self.selected_secure_server
        names = [route.name for route in visible]
        index = names.index(selected) if selected in names else 0
        table.move_cursor(row=index)
        self.selected_secure_server = visible[index].name

    def _update_filter_count(self, widget_id: str, shown: int, total: int) -> None:
        self.query_one(f"#{widget_id}", Static).update(
            tr("servers.count", shown=shown, total=total)
        )

    def _load_mirror_overview(self) -> None:
        try:
            overview = mirrors_overview(self.config, self.state_store)
        except PnoError:
            overview = {}
        self._render_mirror_info(overview)

    def _render_mirror_info(self, overview: dict[str, Any] | None) -> None:
        widget = self.query_one("#mirror-info", Static)
        if not overview:
            widget.update(tr("mirrors.none"))
            return
        country = (
            overview.get("last_mirror_country")
            or overview.get("last_measure_country")
            or tr("common.unknown")
        )
        source = (
            overview.get("last_mirror_source")
            or overview.get("last_measure_source")
            or tr("common.unknown")
        )
        last = overview.get("last_mirror_refresh") or overview.get("last_measure_at")
        when = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(last))
            if isinstance(last, (int, float))
            else tr("status.never")
        )
        lines = [
            tr("mirrors.target", country=country, source=source),
            tr("mirrors.last", when=when),
        ]
        error = overview.get("mirror_error")
        if isinstance(error, dict):
            lines.append(
                tr("mirrors.error", error=error.get("message") or error.get("code"))
            )
        if self.mirror_result:
            selected = self.mirror_result.get("selected") or []
            if selected:
                lines.append(tr("mirrors.selected", count=len(selected)))
        widget.update("\n".join(lines))

    def _render_mirror_table(self, payload: dict[str, Any] | None) -> None:
        table = self.query_one("#mirror-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            tr("mirrors.host"),
            tr("mirrors.country"),
            tr("mirrors.protocol"),
            tr("mirrors.latency"),
            tr("mirrors.score"),
            tr("mirrors.status"),
        )
        if not payload:
            return
        candidates = list(payload.get("candidates") or [])
        candidates.sort(
            key=lambda item: (
                0 if item.get("ok") else 1,
                item.get("latency_ms") if item.get("latency_ms") is not None else 1e9,
                item.get("host") or "",
            )
        )
        selected_hosts = {
            mirror.get("host") for mirror in (payload.get("selected") or [])
        }
        for item in candidates[:200]:
            host = item.get("host") or ""
            status = (
                "selected"
                if host in selected_hosts
                else ("ok" if item.get("ok") else (item.get("error") or "failed"))
            )
            table.add_row(
                host,
                item.get("country_code") or "-",
                item.get("protocol") or "-",
                f"{item['latency_ms']:.0f} ms" if item.get("latency_ms") else "-",
                f"{item.get('score'):.2f}" if item.get("score") is not None else "-",
                status,
                key=host or None,
            )

    # ----------------------------------------------------------------- status
    def _refresh_status(self) -> None:
        state = self.state_store.read()
        daemon = (self.remote_status or {}).get("daemon") or {}
        online = bool(daemon.get("running"))
        vpn = (self.remote_status or {}).get("vpn") or state.get("vpn") or {}
        connected = bool(vpn.get("connected"))
        previous_server = self.current_server
        self.current_server = vpn.get("server_name") if connected else None
        if connected:
            vpn_text = tr(
                "status.connected",
                server=vpn.get("server_name") or "?",
                exit=vpn.get("exit_country") or "?",
            )
        else:
            vpn_text = tr("status.disconnected", home=self.config.home_country)
        paused = bool((self.remote_status or state).get("paused"))
        automation = tr("status.paused") if paused else tr("status.running")
        if online:
            daemon_text = tr("status.daemon")
        else:
            daemon_text = tr("status.daemon_offline")
        mirror_when = state.get("last_mirror_refresh")
        when = (
            time.strftime("%H:%M", time.localtime(mirror_when))
            if isinstance(mirror_when, (int, float))
            else tr("status.never")
        )
        mirror_text = tr(
            "status.mirror",
            country=state.get("last_mirror_country") or "-",
            when=when,
        )
        parts = [vpn_text, automation, daemon_text, mirror_text]
        if self.snapshot is not None:
            age = int(self.snapshot.age())
            parts.append(
                tr("status.data_stale")
                if self.snapshot.stale
                else tr("status.data_age", age=age)
            )
        self.query_one("#status-text", Static).update("  |  ".join(parts))
        self.query_one("#pause-btn", Button).label = (
            tr("button.resume") if paused else tr("button.pause")
        )
        self.query_one("#disconnect-btn", Button).disabled = not connected
        self.query_one("#disconnect-btn", Button).label = tr("button.disconnect")
        if previous_server != self.current_server and self.selected_country_code:
            self._populate_servers(self.selected_country_code)

    def _refresh_logs(self) -> None:
        view = self.query_one("#log-view", RichLog)
        view.clear()
        events_list = log_query.query(limit=120)
        if not events_list:
            view.write(tr("log.empty"))
            return
        for event in events_list:
            view.write(log_query.format_text(event))

    def _tick(self) -> None:
        self._load_snapshot_from_disk()
        self._refresh_status()
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active == "tab-logs":
            self._refresh_logs()
        if tabs.active == "tab-mirrors" and self.mirror_result is None:
            self._load_mirror_overview()
        self._remote_status_worker()

    # --------------------------------------------------------------- workers
    @work(thread=True, exclusive=True, group="remote")
    def _remote_status_worker(self) -> None:
        try:
            payload = request("status", timeout=2.0)
        except PnoError:
            payload = None
        self.call_from_thread(self._apply_remote_status, payload)

    def _apply_remote_status(self, payload: dict[str, Any] | None) -> None:
        self.remote_status = payload
        self._refresh_status()

    def _apply_local_vpn(self, vpn: dict[str, Any]) -> None:
        self.remote_status = {**(self.remote_status or {}), "vpn": vpn}
        self._refresh_status()

    @work(thread=True, exclusive=True, group="servers")
    def _refresh_servers_worker(self, force: bool) -> None:
        self.call_from_thread(
            self.notify, tr("notify.refresh_started"), severity="information"
        )
        try:
            request("servers.refresh", {"force": force}, timeout=90)
        except PnoError:
            try:
                ServerCache(helper_timeout=self.config.proton_helper_timeout).get(
                    force=force, max_age=self.config.servers_max_age
                )
            except PnoError as exc:
                self.call_from_thread(
                    self.notify, str(exc), severity="error", timeout=8
                )
                return
        self.call_from_thread(self._after_server_refresh)

    def _after_server_refresh(self) -> None:
        self._load_snapshot_from_disk()
        self._populate_countries()
        self._populate_secure_core()
        self._refresh_status()
        if self.snapshot is not None:
            self.notify(
                tr("notify.refreshed", count=len(self.snapshot.servers)),
                severity="information",
            )

    def _status_reader(self):
        return read_vpn_status(self.snapshot)

    @work(thread=True, exclusive=True, group="connect")
    def _connect_worker(self, name: str, close_gui: bool = False) -> None:
        try:
            connect_server(
                name,
                close_gui=close_gui,
                status_reader=self._status_reader,
            )
        except GuiActiveError as exc:
            self.call_from_thread(self._show_gui_conflict, "connect", name, exc)
            return
        except PnoError as exc:
            self.call_from_thread(
                self.notify,
                tr("notify.connect_failed", error=exc.message),
                severity="error",
                timeout=8,
            )
            return
        try:
            fresh = read_vpn_status(self.snapshot).to_dict()
        except PnoError:
            fresh = None
        if fresh is not None:
            self.call_from_thread(self._apply_local_vpn, fresh)
        self.call_from_thread(
            self.notify, tr("notify.connected", server=name), severity="information"
        )
        self.call_from_thread(self._remote_status_worker)

    @work(thread=True, exclusive=True, group="disconnect")
    def _disconnect_worker(self, close_gui: bool = False) -> None:
        try:
            disconnect_server(close_gui=close_gui)
        except GuiActiveError as exc:
            self.call_from_thread(self._show_gui_conflict, "disconnect", None, exc)
            return
        except PnoError as exc:
            self.call_from_thread(
                self.notify,
                tr("notify.disconnect_failed", error=exc.message),
                severity="error",
                timeout=8,
            )
            return
        wait_for_disconnected(self._status_reader, timeout=8.0)
        try:
            fresh = read_vpn_status(self.snapshot).to_dict()
        except PnoError:
            fresh = None
        if fresh is not None:
            self.call_from_thread(self._apply_local_vpn, fresh)
        self.call_from_thread(
            self.notify, tr("notify.disconnected"), severity="information"
        )
        self.call_from_thread(self._remote_status_worker)

    def _show_gui_conflict(
        self, action: str, server: str | None, exc: GuiActiveError
    ) -> None:
        self._pending_gui_action = action
        self._pending_gui_server = server
        pids = ", ".join(str(pid) for pid in exc.pids) or tr("common.unknown")
        if action == "connect":
            title = tr("gui.connect_title")
            message = tr("gui.connect_message", server=server or "?")
            confirm = tr("gui.connect_yes")
        else:
            title = tr("gui.disconnect_title")
            message = tr("gui.disconnect_message")
            confirm = tr("gui.disconnect_yes")
        self.push_screen(
            GuiConflictScreen(
                title=title,
                message=message,
                detail=tr("gui.pids", pids=pids),
                confirm_label=confirm,
                cancel_label=tr("gui.no"),
            ),
            self._on_gui_decision,
        )

    def _on_gui_decision(self, confirmed: bool | None) -> None:
        action = self._pending_gui_action
        server = self._pending_gui_server
        self._pending_gui_action = None
        self._pending_gui_server = None
        if not confirmed:
            return
        if action == "connect" and server:
            self._connect_worker(server, close_gui=True)
        elif action == "disconnect":
            self._disconnect_worker(close_gui=True)

    @work(thread=True, exclusive=True, group="mirrors")
    def _mirror_worker(self, apply: bool) -> None:
        self.call_from_thread(
            self.notify, tr("notify.mirrors_started"), severity="information"
        )
        try:
            payload = request(
                "mirrors.refresh",
                {"apply": apply, "dry_run": not apply},
                timeout=600,
            )
        except PnoError:
            try:
                from pno.mirrors.service import refresh_mirrors

                payload = refresh_mirrors(
                    self.config,
                    self.state_store,
                    apply=apply,
                    dry_run=not apply,
                )
            except PnoError as exc:
                self.call_from_thread(
                    self.notify,
                    tr("notify.mirror_failed", error=exc.message),
                    severity="error",
                )
                return
        self.call_from_thread(self._after_mirrors, payload)

    def _after_mirrors(self, payload: dict[str, Any]) -> None:
        self.mirror_result = payload
        self._render_mirror_table(payload)
        self._load_mirror_overview()
        country = payload.get("target_country") or "-"
        self.notify(tr("notify.mirrors_done", country=country), severity="information")

    # --------------------------------------------------------------- actions
    @on(DataTable.RowHighlighted, "#country-table")
    def _on_country_highlight(self, event: DataTable.RowHighlighted) -> None:
        if self._rebuilding:
            return
        code = event.row_key.value
        if code:
            self.selected_country_code = str(code)
            self._populate_servers(str(code))

    @on(DataTable.RowHighlighted, "#server-table")
    def _on_server_highlight(self, event: DataTable.RowHighlighted) -> None:
        if self._rebuilding:
            return
        if event.row_key.value:
            self.selected_server = str(event.row_key.value)

    @on(DataTable.RowHighlighted, "#secure-table")
    def _on_secure_highlight(self, event: DataTable.RowHighlighted) -> None:
        if self._rebuilding:
            return
        if event.row_key.value:
            self.selected_secure_server = str(event.row_key.value)

    @on(Input.Changed, "#servers-search")
    def _on_servers_search(self, event: Input.Changed) -> None:
        self.servers_query = event.value
        self._populate_countries()

    @on(Checkbox.Changed, "#servers-inventory")
    def _on_servers_inventory(self, event: Checkbox.Changed) -> None:
        self.servers_inventory_only = bool(event.value)
        self._populate_countries()

    @on(Input.Changed, "#secure-search")
    def _on_secure_search(self, event: Input.Changed) -> None:
        self.secure_query = event.value
        self._populate_secure_core()

    @on(Checkbox.Changed, "#secure-inventory")
    def _on_secure_inventory(self, event: Checkbox.Changed) -> None:
        self.secure_inventory_only = bool(event.value)
        self._populate_secure_core()

    @on(Button.Pressed, "#connect-btn")
    def _on_connect(self) -> None:
        self.action_connect_selected()

    @on(Button.Pressed, "#secure-connect-btn")
    def _on_secure_connect(self) -> None:
        if not self.selected_secure_server:
            self.notify(tr("notify.no_selection"), severity="warning")
            return
        self._connect_worker(self.selected_secure_server)

    @on(Button.Pressed, "#copy-btn")
    def _on_copy(self) -> None:
        if not self.selected_server:
            self.notify(tr("notify.no_selection"), severity="warning")
            return
        self.copy_to_clipboard(self.selected_server)
        self.notify(tr("notify.copied", name=self.selected_server))

    @on(Button.Pressed, "#refresh-btn")
    def _on_refresh(self) -> None:
        self.action_refresh_all()

    @on(Button.Pressed, "#pause-btn")
    def _on_pause(self) -> None:
        self.action_toggle_pause()

    @on(Button.Pressed, "#disconnect-btn")
    def _on_disconnect(self) -> None:
        self.action_disconnect()

    @on(Button.Pressed, "#mirror-preview-btn")
    def _on_mirror_preview(self) -> None:
        self._mirror_worker(False)

    @on(Button.Pressed, "#mirror-refresh-btn")
    def _on_mirror_refresh(self) -> None:
        self._mirror_worker(True)

    @on(Checkbox.Changed, "#auto-mirror")
    def _on_auto_changed(self, event: Checkbox.Changed) -> None:
        self.config.auto_mirror = bool(event.value)
        config_module.save(self.config)
        self._reload_daemon_config()
        events.emit(
            "info", "tui", "settings.auto_mirror", {"enabled": self.config.auto_mirror}
        )

    @on(Select.Changed, "#language")
    def _on_language(self, event: Select.Changed) -> None:
        value = str(event.value)
        if value not in {"en", "uk"}:
            return
        self.config.language = value
        set_language(value)
        config_module.save(self.config)
        self._retranslate()

    @on(Button.Pressed, "#settings-save-btn")
    def _on_settings_save(self) -> None:
        try:
            top_n = int(self.query_one("#top-n", Input).value)
        except ValueError:
            self.notify(tr("error.top_n_integer"), severity="error")
            return
        home = self.query_one("#home-country", Input).value.strip().upper()
        terminal = self.query_one("#terminal", Input).value.strip()
        updated = dataclasses.replace(
            self.config,
            top_n=top_n,
            home_country=home,
            terminal=terminal,
        )
        try:
            updated.validate()
        except PnoError as exc:
            self.notify(exc.message, severity="error")
            return
        config_module.save(updated)
        self.config = updated
        self._populate_countries()
        self._reload_daemon_config()
        self.notify(tr("settings.saved"))

    def _reload_daemon_config(self) -> None:
        try:
            request("config.reload", timeout=5)
        except PnoError:
            pass

    def action_refresh_all(self) -> None:
        self._refresh_servers_worker(True)
        self._load_mirror_overview()
        self._refresh_status()

    def action_connect_selected(self) -> None:
        if not self.selected_server:
            self.notify(tr("notify.no_selection"), severity="warning")
            return
        self._connect_worker(self.selected_server)

    def action_disconnect(self) -> None:
        vpn = (
            (self.remote_status or {}).get("vpn")
            or self.state_store.read().get("vpn")
            or {}
        )
        if not vpn.get("connected"):
            self.notify(tr("notify.not_connected"), severity="warning")
            return
        self._disconnect_worker()

    def action_toggle_pause(self) -> None:
        state = self.state_store.read()
        paused = bool(state.get("paused"))
        command = "resume" if paused else "pause"
        try:
            request(command, timeout=5)
        except PnoError:
            self.state_store.update(
                lambda current: current.update(
                    {"paused": command == "pause", "paused_at": None}
                )
            )
            self.notify(tr("notify.daemon_offline"), severity="warning")
        self.notify(tr("notify.paused" if command == "pause" else "notify.resumed"))
        self._refresh_status()

    # ------------------------------------------------------------ retranslate
    def _retranslate(self) -> None:
        self.TITLE = tr("app.title")
        tabs = self.query_one("#tabs", TabbedContent)
        for tab_id, key in (
            ("tab-servers", "tab.servers"),
            ("tab-secure", "tab.secure"),
            ("tab-mirrors", "tab.mirrors"),
            ("tab-logs", "tab.logs"),
            ("tab-settings", "tab.settings"),
        ):
            try:
                tabs.get_tab(tab_id).label = tr(key)
            except Exception:  # noqa: BLE001 - Textual API differences
                pass
        self.query_one("#pause-btn", Button).label = (
            tr("button.resume")
            if self.state_store.read().get("paused")
            else tr("button.pause")
        )
        self.query_one("#refresh-btn", Button).label = tr("servers.refresh")
        self.query_one("#disconnect-btn", Button).label = tr("button.disconnect")
        self.query_one("#connect-btn", Button).label = tr("servers.connect")
        self.query_one("#copy-btn", Button).label = tr("servers.copy")
        self.query_one("#servers-hint", Static).update(tr("servers.hint"))
        self.query_one("#servers-search", Input).placeholder = tr("servers.search")
        self.query_one("#secure-search", Input).placeholder = tr("secure.search")
        self.query_one("#servers-inventory", Checkbox).label = tr(
            "servers.inventory_only"
        )
        self.query_one("#secure-inventory", Checkbox).label = tr(
            "servers.inventory_only"
        )
        self.query_one("#secure-connect-btn", Button).label = tr("secure.connect")
        self.query_one("#mirror-preview-btn", Button).label = tr("mirrors.preview")
        self.query_one("#mirror-refresh-btn", Button).label = tr("mirrors.refresh")
        self.query_one("#auto-mirror", Checkbox).label = tr("mirrors.auto")
        self.query_one("#label-top-n", Label).update(tr("settings.top_n"))
        self.query_one("#label-home-country", Label).update(tr("settings.home_country"))
        self.query_one("#label-terminal", Label).update(tr("settings.terminal"))
        self.query_one("#label-language", Label).update(tr("settings.language"))
        self.query_one("#settings-save-btn", Button).label = tr("settings.save")
        self.query_one("#language", Select).set_options(
            [
                (tr(key), code)
                for code, key in (("en", "language.en"), ("uk", "language.uk"))
            ]
        )
        self._populate_countries()
        self._populate_secure_core()
        if self.mirror_result:
            self._render_mirror_table(self.mirror_result)
        self._load_mirror_overview()
        self._refresh_status()
        self._refresh_logs()


def run() -> None:
    PnoApp().run()


if __name__ == "__main__":
    run()
