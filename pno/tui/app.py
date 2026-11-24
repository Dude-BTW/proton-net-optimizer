"""Textual front-end for proton-net-optimizer."""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
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
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from pno import config as config_module
from pno import events
from pno import favorites as favorites_module
from pno import inventory as inventory_module
from pno import log_query, paths
from pno.config import Config
from pno.errors import PnoError, SettingsError
from pno.ipc import request
from pno.mirrors.pacman_db import format_age_hours
from pno.mirrors.service import mirrors_overview
from pno.proton import settings as proton_settings
from pno.proton.account import ProtonInfo, try_fetch_info
from pno.proton.connection import (
    GuiActiveError,
    connect_server,
    disconnect_server,
    wait_for_disconnected,
)
from pno.proton.gui import find_gui_processes
from pno.proton.killswitch import (
    CleanupResult,
    KillSwitchReport,
    cleanup_stale_killswitch,
    inspect_killswitch,
)
from pno.proton.models import CountryServers, Server, ServerSnapshot
from pno.proton.selection import (
    current_server_info,
    regular_countries,
    secure_core_routes,
)
from pno.proton.serverlist import ServerCache
from pno.proton.status import read_vpn_status
from pno.requirements import RequirementReport, check_requirements
from pno.state import StateStore
from pno.tui import theme
from pno.tui.filtering import filter_countries, filter_routes
from pno.tui.flags import country_label, flag, server_label
from pno.tui.i18n import get_language, set_language, tr
from pno.tui.modals import GuiConflictScreen, RequirementsScreen, RunningAppScreen
from pno.tui.state import TAB_IDS, TuiState, TuiStateStore


def _sync_db_details(report: dict[str, Any]) -> list[str]:
    """Localised fragments describing why the sync databases are stale."""
    reasons = report.get("reasons") or []
    databases = report.get("databases") or []
    details: list[str] = []
    if "too-old" in reasons and databases:
        oldest = min(databases, key=lambda item: item.get("modified_at") or 0.0)
        details.append(
            tr(
                "mirrors.sync_db_oldest",
                name=oldest.get("name") or "?",
                age=format_age_hours(oldest.get("age_hours")),
            )
        )
    if "skewed" in reasons:
        details.append(
            tr("mirrors.sync_db_skew", age=format_age_hours(report.get("skew_hours")))
        )
    return details or [tr("mirrors.sync_db_attention")]


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
        self.tui_state_store = TuiStateStore()
        saved_state = self.tui_state_store.load()
        self.snapshot: ServerSnapshot | None = None
        self.snapshot_mtime: float = 0.0

        self.all_countries: list[CountryServers] = []
        self.countries: dict[str, CountryServers] = {}
        self.visible_country_codes: list[str] = []
        self.selected_country_code: str | None = saved_state.selected_country
        self.selected_server: str | None = saved_state.selected_server
        self.selected_secure_server: str | None = saved_state.selected_secure_server
        self.secure_route_map: dict[str, Server] = {}
        self.favorites_saved = favorites_module.Favorites()
        self.favorites_dirty_side: str | None = None
        self._favorites_syncing = False
        self._favorites_text_draft = ""
        self._favorites_visual_draft: set[str] = set()
        self._favorites_visible_values: set[str] = set()

        self.servers_query = saved_state.servers_query
        self.secure_query = saved_state.secure_query
        self.servers_inventory_only = saved_state.servers_inventory_only
        self.secure_inventory_only = saved_state.secure_inventory_only
        self.current_server: str | None = None
        self._restore_tab = saved_state.tab
        self._state_dirty = False
        self._save_scheduled = False

        self.proton_settings: proton_settings.ProtonSettings | None = None
        self.proton_info = ProtonInfo()
        self.proton_info_error: str | None = None
        self._proton_snapshot: dict[str, Any] = {}
        self._proton_dirty = False
        self._proton_syncing = False
        self._proton_gui_running = False
        self._proton_gui_app = proton_settings.GuiAppSettings()
        self._killswitch_report: KillSwitchReport | None = None
        self._killswitch_checked_at = 0.0
        self._killswitch_warned: frozenset[str] = frozenset()

        self.remote_status: dict[str, Any] | None = None
        self.mirror_result: dict[str, Any] | None = None
        self._rebuilding = False
        self._programmatic = False
        self._pending_gui_action: str | None = None
        self._pending_gui_server: str | None = None

    def get_css_variables(self) -> dict[str, str]:
        variables = super().get_css_variables()
        variables.update(
            {
                "proton-bg-account": theme.CARD_ACCOUNT_BG,
                "proton-bg-features": theme.CARD_FEATURES_BG,
                "proton-bg-connection": theme.CARD_CONNECTION_BG,
                "proton-bg-general": theme.CARD_GENERAL_BG,
                "proton-bg-gui": theme.CARD_GUI_BG,
                "proton-border": theme.CARD_BORDER,
            }
        )
        return variables

    # ------------------------------------------------------------- composition
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="status-bar"):
            yield Static("", id="status-text")
            yield Button(tr("button.pause"), id="pause-btn", variant="warning")
            yield Button(tr("servers.refresh"), id="refresh-btn", variant="primary")
            yield Button(tr("button.disconnect"), id="disconnect-btn", variant="error")
        yield Static("", id="current-info")
        with TabbedContent(id="tabs"):
            with TabPane(tr("tab.servers"), id="tab-servers"):
                with Vertical(id="servers-layout"):
                    with Horizontal(id="servers-filters"):
                        yield Input(
                            value=self.servers_query,
                            placeholder=tr("servers.search"),
                            id="servers-search",
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
            with TabPane(tr("tab.favorites"), id="tab-favorites"):
                with Vertical(id="favorites-layout"):
                    yield Static(tr("favorites.hint"), id="favorites-hint")
                    with Horizontal(id="favorites-panels"):
                        with Vertical(id="favorites-visual-panel"):
                            yield Static(
                                tr("favorites.visual_title"),
                                classes="favorites-title",
                                id="favorites-visual-title",
                            )
                            with Horizontal(id="favorites-filters"):
                                yield Checkbox(
                                    tr("favorites.regular"),
                                    value=True,
                                    id="favorites-regular",
                                )
                                yield Checkbox(
                                    tr("favorites.secure"),
                                    value=True,
                                    id="favorites-secure",
                                )
                            yield SelectionList(id="favorites-list")
                            yield Static("", id="favorites-visual-status")
                        with Vertical(id="favorites-text-panel"):
                            yield Static(
                                tr("favorites.text_title"),
                                classes="favorites-title",
                                id="favorites-text-title",
                            )
                            yield TextArea(id="favorites-text")
                            yield Static("", id="favorites-text-status")
                    with Horizontal(id="favorites-actions"):
                        yield Button(
                            tr("favorites.apply"),
                            id="favorites-apply",
                            variant="primary",
                        )
                        yield Button(tr("favorites.revert"), id="favorites-revert")
                        yield Static("", id="favorites-status")
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
                with Vertical(id="logs-layout"):
                    with Horizontal(id="log-controls"):
                        yield Label(tr("logs.max_size"), id="label-log-size")
                        yield Input(
                            value=str(self.config.log_max_bytes // (1024 * 1024)),
                            id="log-max-size",
                            type="integer",
                        )
                        yield Label(tr("logs.keep_files"), id="label-log-keep")
                        yield Input(
                            value=str(self.config.log_keep_files),
                            id="log-keep-files",
                            type="integer",
                        )
                        yield Label(tr("logs.max_age"), id="label-log-age")
                        yield Input(
                            value=str(self.config.log_max_age_days),
                            id="log-max-age",
                            type="integer",
                        )
                        yield Button(
                            tr("logs.apply"), id="log-apply-btn", variant="primary"
                        )
                        yield Button(tr("logs.prune"), id="log-prune-btn")
                    yield Static("", id="log-stats")
                    yield RichLog(id="log-view", wrap=True, highlight=False)
            with TabPane(tr("tab.proton"), id="tab-proton"):
                with Vertical(id="proton-tab"):
                    yield Static("", id="proton-banner")
                    with VerticalScroll(id="proton-layout"):
                        with Vertical(classes="proton-card", id="proton-card-account"):
                            yield Static(
                                tr("proton.account"),
                                classes="proton-section",
                                id="proton-account-title",
                            )
                            with Horizontal(id="proton-account-row"):
                                yield Static("", id="proton-account")
                                yield Button(
                                    tr("proton.account.manage"),
                                    id="proton-manage-btn",
                                )
                            yield Static(
                                "", id="proton-account-help", classes="proton-help"
                            )

                        with Vertical(classes="proton-card", id="proton-card-features"):
                            yield Static(
                                tr("proton.features"),
                                classes="proton-section",
                                id="proton-features-title",
                            )
                            with Vertical(classes="proton-field"):
                                yield Label(
                                    tr("proton.netshield"),
                                    classes="proton-label",
                                    id="proton-netshield-label",
                                )
                                yield Select(
                                    [
                                        (tr("proton.netshield.off"), "off"),
                                        (
                                            tr("proton.netshield.malware-only"),
                                            "malware-only",
                                        ),
                                        (
                                            tr("proton.netshield.malware-ads-trackers"),
                                            "malware-ads-trackers",
                                        ),
                                    ],
                                    value="off",
                                    id="proton-netshield",
                                    allow_blank=False,
                                )
                                yield Static(
                                    tr("proton.netshield.help"),
                                    id="proton-netshield-help",
                                    classes="proton-help",
                                )
                            with Vertical(classes="proton-field"):
                                yield Label(
                                    tr("proton.killswitch"),
                                    classes="proton-label",
                                    id="proton-killswitch-label",
                                )
                                yield Select(
                                    [
                                        (tr("proton.killswitch.off"), "off"),
                                        (
                                            tr("proton.killswitch.standard"),
                                            "standard",
                                        ),
                                        (
                                            tr("proton.killswitch.advanced"),
                                            "advanced",
                                        ),
                                    ],
                                    value="off",
                                    id="proton-killswitch",
                                    allow_blank=False,
                                )
                                yield Static(
                                    tr("proton.killswitch.help"),
                                    id="proton-killswitch-help",
                                    classes="proton-help",
                                )
                                yield Static(
                                    tr("proton.killswitch.advanced_note"),
                                    id="proton-killswitch-note",
                                    classes="proton-help",
                                )
                                yield Static(
                                    "",
                                    id="proton-killswitch-stale",
                                    classes="proton-help",
                                )
                                yield Button(
                                    tr("proton.killswitch.clean"),
                                    id="proton-killswitch-clean-btn",
                                    variant="warning",
                                    disabled=True,
                                )
                            with Vertical(classes="proton-field"):
                                yield Checkbox(
                                    tr("proton.port_forwarding"),
                                    id="proton-port-forwarding",
                                )
                                yield Static(
                                    tr("proton.port_forwarding.help"),
                                    id="proton-port-forwarding-help",
                                    classes="proton-help",
                                )
                            with Vertical(classes="proton-field"):
                                yield Checkbox(tr("proton.split"), id="proton-split")
                                yield Static(
                                    tr("proton.split.help"),
                                    id="proton-split-help",
                                    classes="proton-help",
                                )
                                yield Label(
                                    tr("proton.split.mode"),
                                    classes="proton-label",
                                    id="proton-split-mode-label",
                                )
                                yield Select(
                                    [
                                        (tr("proton.split.exclude"), "exclude"),
                                        (tr("proton.split.include"), "include"),
                                    ],
                                    value="exclude",
                                    id="proton-split-mode",
                                    allow_blank=False,
                                )
                                yield Label(
                                    tr("proton.split.apps"),
                                    classes="proton-label",
                                    id="proton-split-apps-label",
                                )
                                yield TextArea(
                                    placeholder=tr("proton.split.apps_placeholder"),
                                    id="proton-split-apps",
                                )
                                with Horizontal(id="proton-split-actions"):
                                    yield Button(
                                        tr("proton.split.add_running"),
                                        id="proton-add-app-btn",
                                    )

                        with Vertical(
                            classes="proton-card", id="proton-card-connection"
                        ):
                            yield Static(
                                tr("proton.connection"),
                                classes="proton-section",
                                id="proton-connection-title",
                            )
                            with Vertical(classes="proton-field"):
                                yield Checkbox(tr("proton.protun"), id="proton-protun")
                                yield Static(
                                    tr("proton.protun.help"),
                                    id="proton-protun-help",
                                    classes="proton-help",
                                )
                            with Vertical(classes="proton-field"):
                                yield Label(
                                    tr("proton.protocol"),
                                    classes="proton-label",
                                    id="proton-protocol-label",
                                )
                                yield Select(
                                    [
                                        (option.label, option.id)
                                        for option in proton_settings.FALLBACK_PROTOCOLS
                                        if option.family == "generic"
                                    ],
                                    value="wireguard",
                                    id="proton-protocol",
                                    allow_blank=False,
                                )
                                yield Static(
                                    tr("proton.protocol.help"),
                                    id="proton-protocol-help",
                                    classes="proton-help",
                                )
                            with Vertical(classes="proton-field"):
                                yield Checkbox(
                                    tr("proton.accelerator"), id="proton-accelerator"
                                )
                                yield Static(
                                    tr("proton.accelerator.help"),
                                    id="proton-accelerator-help",
                                    classes="proton-help",
                                )
                            with Vertical(classes="proton-field"):
                                yield Checkbox(
                                    tr("proton.moderate_nat"),
                                    id="proton-moderate-nat",
                                )
                                yield Static(
                                    tr("proton.moderate_nat.help"),
                                    id="proton-moderate-nat-help",
                                    classes="proton-help",
                                )
                            with Vertical(classes="proton-field"):
                                yield Checkbox(tr("proton.ipv6"), id="proton-ipv6")
                                yield Static(
                                    tr("proton.ipv6.help"),
                                    id="proton-ipv6-help",
                                    classes="proton-help",
                                )
                            with Vertical(classes="proton-field"):
                                yield Checkbox(
                                    tr("proton.custom_dns"), id="proton-custom-dns"
                                )
                                yield Static(
                                    tr("proton.custom_dns.help"),
                                    id="proton-custom-dns-help",
                                    classes="proton-help",
                                )
                                yield Label(
                                    tr("proton.custom_dns.ips"),
                                    classes="proton-label",
                                    id="proton-dns-label",
                                )
                                yield Input(
                                    placeholder=tr("proton.dns_placeholder"),
                                    id="proton-dns",
                                )

                        with Vertical(classes="proton-card", id="proton-card-general"):
                            yield Static(
                                tr("proton.general"),
                                classes="proton-section",
                                id="proton-general-title",
                            )
                            with Vertical(classes="proton-field"):
                                yield Checkbox(
                                    tr("proton.telemetry"), id="proton-telemetry"
                                )
                            with Vertical(classes="proton-field"):
                                yield Checkbox(
                                    tr("proton.crash_reports"),
                                    id="proton-crash-reports",
                                )

                        with Vertical(classes="proton-card", id="proton-card-gui"):
                            yield Static(
                                tr("proton.gui_app"),
                                classes="proton-section",
                                id="proton-gui-title",
                            )
                            with Vertical(classes="proton-field"):
                                yield Static(
                                    "", id="proton-gui-app", classes="proton-help"
                                )
                                yield Static(
                                    tr("proton.gui_app.hint"),
                                    id="proton-gui-help",
                                    classes="proton-help",
                                )
                    with Horizontal(id="proton-actions-row"):
                        with Horizontal(id="proton-actions"):
                            yield Button(
                                tr("proton.apply"),
                                id="proton-apply-btn",
                                variant="primary",
                            )
                            yield Button(tr("proton.revert"), id="proton-revert-btn")
                            yield Static("", id="proton-status")
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
        self._refresh_log_stats()
        self._reload_favorites()
        self._load_proton_settings()
        self._proton_info_worker()
        tabs = self.query_one("#tabs", TabbedContent)
        if self._restore_tab in TAB_IDS:
            tabs.active = self._restore_tab
        self.set_interval(3.0, self._tick)
        self._refresh_servers_worker(force=False)
        self._remote_status_worker()
        self._requirements_worker()
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
        self._programmatic = True
        try:
            table.move_cursor(row=index)
        finally:
            self._programmatic = False
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
        desired = self.selected_server
        country = self.countries.get(code or "")
        if country is None:
            self.selected_server = None
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
            names = [server.name for server in country.servers]
            index = names.index(desired) if desired in names else 0
            self._programmatic = True
            try:
                table.move_cursor(row=index)
            finally:
                self._programmatic = False
            self.selected_server = names[index]

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
        self._programmatic = True
        try:
            table.move_cursor(row=index)
        finally:
            self._programmatic = False
        self.selected_secure_server = visible[index].name

    def _update_filter_count(self, widget_id: str, shown: int, total: int) -> None:
        self.query_one(f"#{widget_id}", Static).update(
            tr("servers.count", shown=shown, total=total)
        )

    # ------------------------------------------------------------- favorites
    def _reload_favorites(self) -> None:
        try:
            self.favorites_saved = favorites_module.load()
        except PnoError as exc:
            self.favorites_saved = favorites_module.Favorites()
            self.notify(str(exc), severity="error", timeout=8)
        self.favorites_dirty_side = None
        self.query_one("#favorites-text-status", Static).update("")
        self._sync_favorites_text()
        self._rebuild_favorites_visual()
        self._update_favorites_controls()

    def _sync_favorites_text(self) -> None:
        text = favorites_module.normalized_text(self.favorites_saved)
        self._favorites_text_draft = text
        self._favorites_syncing = True
        try:
            self.query_one("#favorites-text", TextArea).text = text
        finally:
            self._favorites_syncing = False

    def _rebuild_favorites_visual(self, selected: set[str] | None = None) -> None:
        widget = self.query_one("#favorites-list", SelectionList)
        status = self.query_one("#favorites-visual-status", Static)
        if selected is None:
            selected = favorites_module.selection_values(self.favorites_saved)
        self._favorites_visual_draft = set(selected)
        self._favorites_visible_values = set()
        self._favorites_syncing = True
        try:
            widget.clear_options()
            if self.snapshot is None:
                status.update(tr("favorites.no_data"))
                return
            show_regular = self.query_one("#favorites-regular", Checkbox).value
            show_secure = self.query_one("#favorites-secure", Checkbox).value
            options: list[tuple[str, str, bool]] = []
            regular_count = 0
            secure_count = 0
            if show_regular:
                countries = regular_countries(
                    self.snapshot,
                    top_n=1,
                    include_tor=self.config.include_tor,
                    max_tier=self.snapshot.max_tier or None,
                )
                for country in sorted(countries, key=lambda item: item.code):
                    value = f"country:{country.code}"
                    options.append(
                        (
                            country_label(country.code, get_language()),
                            value,
                            value in selected,
                        )
                    )
                    self._favorites_visible_values.add(value)
                    regular_count += 1
            if show_secure:
                routes = secure_core_routes(
                    self.snapshot,
                    max_tier=self.snapshot.max_tier or None,
                    include_tor=self.config.include_tor,
                )
                for route in sorted(
                    routes, key=lambda item: (item.exit_code, item.name)
                ):
                    value = f"sc:{route.name}"
                    label = (
                        f"{flag(route.entry_code)} {route.entry_code} → "
                        f"{flag(route.exit_code)} {route.exit_code}  {route.name}"
                    )
                    options.append((label, value, value in selected))
                    self._favorites_visible_values.add(value)
                    secure_count += 1
            if options:
                widget.add_options(options)
            status.update(
                tr(
                    "favorites.visual_count",
                    regular=regular_count,
                    secure=secure_count,
                )
            )
        finally:
            self.call_after_refresh(self._release_favorites_sync)

    def _release_favorites_sync(self) -> None:
        self._favorites_syncing = False

    def _set_favorites_dirty(self, side: str) -> None:
        self.favorites_dirty_side = side
        self._update_favorites_controls()

    def _update_favorites_controls(self) -> None:
        side = self.favorites_dirty_side
        text = self.query_one("#favorites-text", TextArea)
        visual = self.query_one("#favorites-list", SelectionList)
        visual.disabled = side == "text"
        text.read_only = side == "visual"
        apply_button = self.query_one("#favorites-apply", Button)
        revert_button = self.query_one("#favorites-revert", Button)
        apply_button.disabled = side is None
        revert_button.disabled = side is None
        status = self.query_one("#favorites-status", Static)
        if side == "text":
            status.update(tr("favorites.locked_text"))
        elif side == "visual":
            status.update(tr("favorites.locked_visual"))
        else:
            status.update("")

    def _favorites_text_errors(self, errors) -> None:
        detail = "\n".join(f"line {error.line}: {error.message}" for error in errors)
        self.query_one("#favorites-text-status", Static).update(detail)
        self.notify(tr("favorites.invalid"), severity="error", timeout=8)

    def _apply_favorites(self) -> None:
        side = self.favorites_dirty_side
        if side is None:
            self.notify(tr("favorites.no_changes"), severity="information")
            return
        if self.snapshot is None:
            self.query_one("#favorites-text-status", Static).update(
                tr("favorites.no_data")
            )
            return

        self.query_one("#favorites-text-status", Static).update("")
        if side == "text":
            result = favorites_module.parse_text(
                self.query_one("#favorites-text", TextArea).text,
                self.snapshot,
            )
            if result.errors:
                self._favorites_text_errors(result.errors)
                return
            assert result.favorites is not None
            updated = result.favorites
        else:
            selected = set(self.query_one("#favorites-list", SelectionList).selected)
            updated = favorites_module.from_selection(selected)

        try:
            favorites_module.save(updated)
        except PnoError as exc:
            self.notify(exc.message, severity="error", timeout=8)
            return
        self.favorites_saved = updated.normalized()
        self.favorites_dirty_side = None
        self.query_one("#favorites-text-status", Static).update("")
        self._sync_favorites_text()
        self._rebuild_favorites_visual()
        self._update_favorites_controls()
        self.notify(tr("favorites.saved"), severity="information")

    def _revert_favorites(self) -> None:
        try:
            self.favorites_saved = favorites_module.load()
        except PnoError as exc:
            self.notify(exc.message, severity="error", timeout=8)
            return
        self.favorites_dirty_side = None
        self.query_one("#favorites-text-status", Static).update("")
        self._sync_favorites_text()
        self._rebuild_favorites_visual()
        self._update_favorites_controls()
        self.notify(tr("favorites.reverted"), severity="information")

    # ------------------------------------------------------- proton settings
    def _proton_connected(self) -> bool:
        vpn = (
            (self.remote_status or {}).get("vpn")
            or self.state_store.read().get("vpn")
            or {}
        )
        return bool(vpn.get("connected"))

    def _protun_available(self) -> bool:
        if self.proton_info.protocols:
            return self.proton_info.features.protun
        return True

    def _load_proton_settings(self) -> None:
        try:
            current = proton_settings.load()
            app = proton_settings.read_app_config()
        except SettingsError as exc:
            self.proton_settings = None
            self._proton_snapshot = {}
            self._proton_dirty = False
            self.query_one("#proton-banner", Static).update(
                tr("proton.load_failed", error=exc.message)
            )
            self.query_one("#proton-status", Static).update(tr("proton.unavailable"))
            self.query_one("#proton-apply-btn", Button).disabled = True
            return
        self.proton_settings = current
        self._proton_gui_app = app
        self._proton_snapshot = current.canonical()
        # The Select widgets can only show known values; an unknown value from
        # a newer Proton version stays untouched in the file.
        if self._proton_snapshot["killswitch"] not in proton_settings.KILLSWITCH_VALUES:
            self._proton_snapshot["killswitch"] = "off"
        if self._proton_snapshot["netshield"] not in proton_settings.NETSHIELD_VALUES:
            self._proton_snapshot["netshield"] = "off"
        self._proton_gui_running = bool(find_gui_processes())
        self._proton_syncing = True
        try:
            self._set_proton_widgets(self._proton_snapshot)
            self._populate_protocol_options(
                current=self._proton_snapshot["protocol"], keep=True
            )
        finally:
            self._proton_syncing = False
        self._proton_dirty = False
        self._render_proton_account()
        self._render_proton_gui_app()
        self._render_killswitch_report()
        self.query_one("#proton-status", Static).update("")
        self._refresh_proton_controls()

    def _set_proton_widgets(self, snapshot: dict[str, Any]) -> None:
        netshield = snapshot["netshield"]
        self.query_one("#proton-netshield", Select).value = (
            netshield if netshield in proton_settings.NETSHIELD_VALUES else "off"
        )
        killswitch = snapshot["killswitch"]
        self.query_one("#proton-killswitch", Select).value = (
            killswitch if killswitch in proton_settings.KILLSWITCH_VALUES else "off"
        )
        self.query_one("#proton-port-forwarding", Checkbox).value = bool(
            snapshot["port_forwarding"]
        )
        split = snapshot["split_tunneling"]
        self.query_one("#proton-split", Checkbox).value = bool(split["enabled"])
        self.query_one("#proton-split-mode", Select).value = split["mode"]
        self.query_one("#proton-split-apps", TextArea).text = "\n".join(
            split["app_paths"]
        )
        self.query_one("#proton-accelerator", Checkbox).value = bool(
            snapshot["vpn_accelerator"]
        )
        self.query_one("#proton-moderate-nat", Checkbox).value = bool(
            snapshot["moderate_nat"]
        )
        self.query_one("#proton-ipv6", Checkbox).value = bool(snapshot["ipv6"])
        self.query_one("#proton-custom-dns", Checkbox).value = bool(
            snapshot["custom_dns"]["enabled"]
        )
        self.query_one("#proton-dns", Input).value = ", ".join(
            snapshot["custom_dns"]["ips"]
        )
        self.query_one("#proton-telemetry", Checkbox).value = bool(
            snapshot["telemetry"]
        )
        self.query_one("#proton-crash-reports", Checkbox).value = bool(
            snapshot["anonymous_crash_reports"]
        )

    def _populate_protocol_options(
        self, *, current: str, keep: bool, family: str | None = None
    ) -> None:
        catalog = self.proton_info.catalog
        protun = self._protun_available()
        if family is None:
            family = self.proton_info.protocol_family(current)
        if family == "protun" and not protun:
            family = "generic"
        self.query_one("#proton-protun", Checkbox).value = family == "protun"
        options = [option for option in catalog if option.family == family]
        if not options:
            options = list(catalog)
        select = self.query_one("#proton-protocol", Select)
        select.set_options([(option.label, option.id) for option in options])
        ids = [option.id for option in options]
        if current in ids:
            select.value = current
        elif keep:
            label = self.proton_info.protocol_label(current)
            select.set_options(
                [(f"{label} (unavailable)", current)]
                + [(option.label, option.id) for option in options]
            )
            select.value = current
        elif options:
            select.value = options[0].id

    def _render_proton_account(self) -> None:
        account = self.proton_info.account
        if account is None:
            text = tr("proton.account.none")
        else:
            text = tr(
                "proton.account.line",
                name=account.name or tr("common.unknown"),
                plan=account.plan or tr("proton.account.plan_unknown"),
            )
        self.query_one("#proton-account", Static).update(text)
        help_text = ""
        if self.proton_info_error:
            help_text = tr("proton.account.failed", error=self.proton_info_error)
        elif self.proton_info.warnings:
            help_text = tr("proton.warning", warning=self.proton_info.warnings[0])
        self.query_one("#proton-account-help", Static).update(help_text)

    def _render_proton_gui_app(self) -> None:
        app = self._proton_gui_app
        auto = app.auto_connect or tr("proton.gui_app.off")
        lines = [
            tr("proton.gui_app.auto", value=auto),
            tr(
                "proton.gui_app.start",
                value=tr("common.on") if app.start_minimized else tr("common.off"),
            ),
            tr("proton.gui_app.pinned", count=len(app.pinned)),
        ]
        self.query_one("#proton-gui-app", Static).update("\n".join(lines))

    def _proton_edit_targets(self) -> list[str]:
        return [
            "#proton-netshield",
            "#proton-killswitch",
            "#proton-port-forwarding",
            "#proton-split",
            "#proton-split-mode",
            "#proton-split-apps",
            "#proton-add-app-btn",
            "#proton-protun",
            "#proton-protocol",
            "#proton-accelerator",
            "#proton-moderate-nat",
            "#proton-ipv6",
            "#proton-custom-dns",
            "#proton-dns",
            "#proton-telemetry",
            "#proton-crash-reports",
        ]

    def _refresh_proton_controls(self) -> None:
        try:
            blocked = self._proton_gui_running
            connected = self._proton_connected()
            disconnected_only = {
                "#proton-killswitch",
                "#proton-split",
                "#proton-split-mode",
                "#proton-split-apps",
                "#proton-add-app-btn",
                "#proton-protun",
                "#proton-protocol",
            }
            for widget_id in self._proton_edit_targets():
                widget = self.query_one(widget_id)
                widget.disabled = blocked or (
                    connected and widget_id in disconnected_only
                )
            self.query_one("#proton-apply-btn", Button).disabled = (
                blocked or not self._proton_dirty or self.proton_settings is None
            )
            self.query_one("#proton-revert-btn", Button).disabled = (
                blocked or not self._proton_dirty
            )
            banner = self.query_one("#proton-banner", Static)
            if blocked:
                banner.update(tr("proton.gui_running"))
            elif connected:
                banner.update(tr("proton.connected"))
            else:
                banner.update("")
            status = self.query_one("#proton-status", Static)
            if blocked:
                status.update(tr("proton.gui_running"))
            elif self._proton_dirty:
                status.update(tr("proton.unsaved"))
        except Exception:  # noqa: BLE001 - the widget tree may be gone on exit
            return

    def _mark_proton_dirty(self) -> None:
        if self._proton_syncing:
            return
        if not self._proton_snapshot:
            self._proton_dirty = False
        else:
            try:
                self._proton_dirty = bool(self._collect_proton_changes())
            except SettingsError:
                self._proton_dirty = True
        self._refresh_proton_controls()

    def _collect_proton_changes(self) -> dict[str, Any]:
        snapshot = self._proton_snapshot
        changes: dict[str, Any] = {}

        def select_value(widget_id: str) -> str:
            return str(self.query_one(widget_id, Select).value)

        def check_value(widget_id: str) -> bool:
            return bool(self.query_one(widget_id, Checkbox).value)

        netshield = select_value("#proton-netshield")
        if netshield != snapshot["netshield"]:
            changes["netshield"] = netshield
        killswitch = select_value("#proton-killswitch")
        if killswitch != snapshot["killswitch"]:
            changes["killswitch"] = killswitch
        if check_value("#proton-port-forwarding") != snapshot["port_forwarding"]:
            changes["port_forwarding"] = check_value("#proton-port-forwarding")
        if check_value("#proton-split") != snapshot["split_tunneling"]["enabled"]:
            changes["split_tunneling_enabled"] = check_value("#proton-split")
        if select_value("#proton-split-mode") != snapshot["split_tunneling"]["mode"]:
            changes["split_tunneling_mode"] = select_value("#proton-split-mode")
        apps = proton_settings.parse_app_paths(
            self.query_one("#proton-split-apps", TextArea).text
        )
        if list(apps) != snapshot["split_tunneling"]["app_paths"]:
            changes["split_tunneling_apps"] = list(apps)
        protocol = select_value("#proton-protocol")
        if protocol != snapshot["protocol"]:
            changes["protocol"] = protocol
        if check_value("#proton-accelerator") != snapshot["vpn_accelerator"]:
            changes["vpn_accelerator"] = check_value("#proton-accelerator")
        if check_value("#proton-moderate-nat") != snapshot["moderate_nat"]:
            changes["moderate_nat"] = check_value("#proton-moderate-nat")
        if check_value("#proton-ipv6") != snapshot["ipv6"]:
            changes["ipv6"] = check_value("#proton-ipv6")
        if check_value("#proton-custom-dns") != snapshot["custom_dns"]["enabled"]:
            changes["custom_dns_enabled"] = check_value("#proton-custom-dns")
        dns = proton_settings.parse_dns_servers(
            self.query_one("#proton-dns", Input).value
        )
        if list(dns) != snapshot["custom_dns"]["ips"]:
            changes["custom_dns_ips"] = list(dns)
        if check_value("#proton-telemetry") != snapshot["telemetry"]:
            changes["telemetry"] = check_value("#proton-telemetry")
        if check_value("#proton-crash-reports") != snapshot["anonymous_crash_reports"]:
            changes["anonymous_crash_reports"] = check_value("#proton-crash-reports")
        return changes

    def _apply_proton(self) -> None:
        if self.proton_settings is None:
            self.notify(tr("proton.unavailable"), severity="error")
            return
        if not self._proton_dirty:
            self.notify(tr("proton.no_changes"), severity="information")
            return
        try:
            changes = self._collect_proton_changes()
        except SettingsError as exc:
            self.notify(
                tr("proton.invalid", error=exc.message), severity="error", timeout=8
            )
            return
        if not changes:
            self._proton_dirty = False
            self.query_one("#proton-status", Static).update("")
            self._refresh_proton_controls()
            self.notify(tr("proton.no_changes"), severity="information")
            return
        gui_running = bool(find_gui_processes())
        self._proton_gui_running = gui_running
        if gui_running:
            self.notify(tr("proton.gui_running"), severity="warning", timeout=8)
            self._refresh_proton_controls()
            return
        connected = self._proton_connected()
        killswitch = changes.pop("killswitch", None)
        warnings: list[str] = []
        applied = False
        try:
            if changes:
                result = proton_settings.apply_changes(
                    changes,
                    connected=connected,
                    gui_running=gui_running,
                    tier=self.proton_info.tier,
                    catalog=self.proton_info.catalog,
                )
                applied = result.changed
                warnings.extend(result.warnings)
            if killswitch is not None:
                kill = proton_settings.apply_killswitch(
                    killswitch,
                    connected=connected,
                    gui_running=gui_running,
                )
                warnings.extend(kill.warnings)
        except SettingsError as exc:
            if applied:
                self.notify(
                    tr("proton.partial", error=exc.message),
                    severity="warning",
                    timeout=10,
                )
            else:
                self.notify(
                    tr("proton.invalid", error=exc.message),
                    severity="error",
                    timeout=8,
                )
            self._load_proton_settings()
            return
        events.emit(
            "info",
            "settings",
            "settings.proton.apply",
            {
                "keys": sorted([*changes, *(["killswitch"] if killswitch else [])]),
                "changed": True,
                "gui_running": False,
            },
        )
        self._load_proton_settings()
        self.notify(tr("proton.saved"), severity="information", timeout=6)
        for warning in dict.fromkeys(warnings):
            self.notify(
                tr("proton.warning", warning=warning),
                severity="warning",
                timeout=8,
            )

    def _request_killswitch_check(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._killswitch_checked_at < 15.0:
            return
        self._killswitch_checked_at = now
        self._killswitch_check_worker()

    @work(thread=True, exclusive=True, group="killswitch-check")
    def _killswitch_check_worker(self) -> None:
        try:
            report = inspect_killswitch()
        except PnoError as exc:
            self.call_from_thread(self._killswitch_check_failed, exc.message)
            return
        self.call_from_thread(self._apply_killswitch_report, report)

    def _killswitch_check_failed(self, error: str) -> None:
        try:
            self.query_one("#proton-killswitch-stale", Static).update(
                tr("proton.killswitch.check_failed", error=error)
            )
            self.query_one("#proton-killswitch-clean-btn", Button).disabled = True
        except Exception:  # noqa: BLE001 - widget tree may be gone
            return

    def _apply_killswitch_report(self, report: KillSwitchReport) -> None:
        self._killswitch_report = report
        self._render_killswitch_report()
        names = frozenset(item.name for item in report.transient)
        if report.stale and names != self._killswitch_warned:
            self._killswitch_warned = names
            self.notify(
                tr(
                    "proton.killswitch.stale_notify",
                    connections=", ".join(sorted(names)),
                ),
                severity="warning",
                timeout=10,
            )
        elif not report.transient:
            self._killswitch_warned = frozenset()

    def _render_killswitch_report(self) -> None:
        try:
            label = self.query_one("#proton-killswitch-stale", Static)
            button = self.query_one("#proton-killswitch-clean-btn", Button)
        except Exception:  # noqa: BLE001 - widget tree may be gone
            return
        button.label = tr("proton.killswitch.clean")
        report = self._killswitch_report
        if report is None or report.vpn_connected:
            label.update("")
            button.disabled = True
            return
        if report.stale:
            label.update(
                tr(
                    "proton.killswitch.stale",
                    connections=", ".join(item.name for item in report.transient),
                )
            )
            button.disabled = False
            return
        if report.transient and report.setting in {"standard", "advanced"}:
            label.update(
                tr(
                    "proton.killswitch.protected",
                    connections=", ".join(item.name for item in report.transient),
                    setting=report.setting,
                )
            )
        else:
            label.update("")
        button.disabled = True

    @on(Button.Pressed, "#proton-killswitch-clean-btn")
    def _on_killswitch_clean(self) -> None:
        self._killswitch_clean_worker()

    @work(thread=True, exclusive=True, group="killswitch-clean")
    def _killswitch_clean_worker(self) -> None:
        try:
            result = cleanup_stale_killswitch(force=False)
        except PnoError as exc:
            self.call_from_thread(self._killswitch_clean_failed, exc.message)
            return
        self.call_from_thread(self._killswitch_clean_done, result)

    def _killswitch_clean_done(self, result: CleanupResult) -> None:
        if result.removed:
            self.notify(
                tr(
                    "proton.killswitch.clean_done",
                    connections=", ".join(result.removed),
                ),
                severity="information",
                timeout=6,
            )
        elif result.reason in {"killswitch-enabled", "killswitch-unknown"}:
            self.notify(
                tr("proton.killswitch.clean_disabled"),
                severity="warning",
                timeout=8,
            )
        else:
            self.notify(
                tr("proton.killswitch.clean_none"),
                severity="information",
            )
        self._killswitch_checked_at = 0.0
        self._request_killswitch_check(force=True)

    def _killswitch_clean_failed(self, error: str) -> None:
        self.notify(
            tr("proton.killswitch.clean_failed", error=error),
            severity="error",
            timeout=8,
        )

    def _revert_proton(self) -> None:
        self._load_proton_settings()
        self.notify(tr("proton.reverted"), severity="information")

    @on(Checkbox.Changed)
    def _on_proton_checkbox(self, event: Checkbox.Changed) -> None:
        if self._proton_syncing:
            return
        widget_id = event.checkbox.id or ""
        if not widget_id.startswith("proton-"):
            return
        if widget_id == "proton-protun":
            return
        self._mark_proton_dirty()

    @on(Checkbox.Changed, "#proton-protun")
    def _on_proton_protun(self, event: Checkbox.Changed) -> None:
        if self._proton_syncing:
            return
        current = str(self.query_one("#proton-protocol", Select).value)
        family = "protun" if event.value else "generic"
        self._proton_syncing = True
        try:
            self._populate_protocol_options(current=current, keep=False, family=family)
        finally:
            self._proton_syncing = False
        self._mark_proton_dirty()

    @on(Select.Changed)
    def _on_proton_select(self, event: Select.Changed) -> None:
        if self._proton_syncing:
            return
        widget_id = event.select.id or ""
        if widget_id.startswith("proton-"):
            self._mark_proton_dirty()

    @on(Select.Changed, "#proton-split-mode")
    def _on_proton_split_mode(self, event: Select.Changed) -> None:
        if self._proton_syncing or self.proton_settings is None:
            return
        mode = str(event.value)
        if mode not in {"exclude", "include"}:
            return
        apps = self.proton_settings.split_tunneling_apps_for(mode)
        self._proton_syncing = True
        try:
            self.query_one("#proton-split-apps", TextArea).text = "\n".join(apps)
        finally:
            self._proton_syncing = False
        self._mark_proton_dirty()

    @on(Input.Changed, "#proton-dns")
    def _on_proton_dns(self, event: Input.Changed) -> None:
        if not self._proton_syncing:
            self._mark_proton_dirty()

    @on(TextArea.Changed, "#proton-split-apps")
    def _on_proton_split_apps(self, event: TextArea.Changed) -> None:
        if not self._proton_syncing:
            self._mark_proton_dirty()

    @on(Button.Pressed, "#proton-apply-btn")
    def _on_proton_apply(self) -> None:
        self._apply_proton()

    @on(Button.Pressed, "#proton-revert-btn")
    def _on_proton_revert(self) -> None:
        self._revert_proton()

    @on(Button.Pressed, "#proton-add-app-btn")
    def _on_proton_add_app(self) -> None:
        self.push_screen(RunningAppScreen(), self._on_running_app_selected)

    def _on_running_app_selected(self, path: str | None) -> None:
        if not path:
            return
        area = self.query_one("#proton-split-apps", TextArea)
        lines = [line.strip() for line in area.text.splitlines() if line.strip()]
        if path not in lines:
            lines.append(path)
        self._proton_syncing = True
        try:
            area.text = "\n".join(lines)
        finally:
            self._proton_syncing = False
        self._mark_proton_dirty()

    @on(Button.Pressed, "#proton-manage-btn")
    def _on_proton_manage(self) -> None:
        self._open_account_worker()

    @work(thread=True, exclusive=True, group="proton-manage")
    def _open_account_worker(self) -> None:
        import webbrowser

        try:
            opened = webbrowser.open("https://account.protonvpn.com/account")
        except Exception as exc:  # noqa: BLE001 - the browser is an external tool
            opened = False
            detail = str(exc)
        else:
            detail = "browser refused to open"
        if not opened:
            self.call_from_thread(
                self.notify,
                tr("proton.account.open_failed", error=detail),
                severity="warning",
                timeout=8,
            )

    @work(thread=True, exclusive=True, group="proton")
    def _proton_info_worker(self) -> None:
        info, error = try_fetch_info()
        self.call_from_thread(self._apply_proton_info, info, error)

    def _apply_proton_info(self, info: ProtonInfo, error: str | None) -> None:
        self.proton_info = info
        self.proton_info_error = error
        self._render_proton_account()
        if self.proton_settings is not None:
            self._proton_syncing = True
            try:
                current = str(self.query_one("#proton-protocol", Select).value)
                self._populate_protocol_options(current=current, keep=True)
            finally:
                self._proton_syncing = False
        self._refresh_proton_controls()

    def _activate_proton_tab(self) -> None:
        self._proton_gui_running = bool(find_gui_processes())
        if not self._proton_dirty:
            self._load_proton_settings()
        else:
            self._refresh_proton_controls()
        if not self.proton_info.protocols or self.proton_info.account is None:
            self._proton_info_worker()

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
        sync = overview.get("pacman_db")
        if isinstance(sync, dict):
            hint = tr("mirrors.sync_db_hint")
            if not sync.get("available"):
                lines.append(tr("mirrors.sync_db_missing", hint=hint))
            elif sync.get("stale"):
                lines.append(
                    tr(
                        "mirrors.sync_db_stale",
                        detail="; ".join(_sync_db_details(sync)),
                        hint=hint,
                    )
                )
            else:
                lines.append(
                    tr(
                        "mirrors.sync_db_ok",
                        age=format_age_hours(sync.get("oldest_age_hours")),
                    )
                )
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
        for position, item in enumerate(candidates[:200]):
            host = item.get("host") or ""
            status = (
                "selected"
                if host in selected_hosts
                else ("ok" if item.get("ok") else (item.get("error") or "failed"))
            )
            # One host may be listed twice (HTTP and HTTPS), so the URL is the
            # unique row key; fall back to the position for malformed entries.
            row_key = str(item.get("url") or f"candidate-{position}")
            table.add_row(
                host,
                item.get("country_code") or "-",
                item.get("protocol") or "-",
                f"{item['latency_ms']:.0f} ms" if item.get("latency_ms") else "-",
                f"{item.get('score'):.2f}" if item.get("score") is not None else "-",
                status,
                key=row_key,
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
        paused = bool((self.remote_status or state).get("paused"))

        line = Text()
        if connected:
            load_value = None
            if self.snapshot and self.current_server:
                cached = self.snapshot.lookup(self.current_server)
                load_value = cached.load if cached else None
            if load_value is None:
                load_value = vpn.get("load")
            load_text = f"{load_value}%" if isinstance(load_value, int) else "?"
            line.append(tr("status.vpn_label"), style=theme.LABEL)
            line.append(str(vpn.get("server_name") or "?"), style=theme.server_style())
            line.append(tr("status.arrow"), style=theme.LABEL)
            line.append(tr("status.exit_label"), style=theme.LABEL)
            line.append(str(vpn.get("exit_country") or "?"), style=theme.BLUE)
            line.append(tr("status.load_open"), style=theme.LABEL)
            line.append(
                load_text,
                style=theme.load_style(
                    load_value if isinstance(load_value, int) else None
                ),
            )
            line.append(tr("status.close"), style=theme.LABEL)
        else:
            line.append(tr("status.vpn_label"), style=theme.LABEL)
            line.append(
                tr("status.disconnected_value", home=self.config.home_country),
                style=theme.RED,
            )

        line.append("  │  ", style=theme.SEPARATOR)
        line.append(
            tr("status.paused") if paused else tr("status.running"),
            style=theme.YELLOW if paused else theme.GREEN,
        )
        line.append("  │  ", style=theme.SEPARATOR)
        line.append(
            tr("status.daemon") if online else tr("status.daemon_offline"),
            style=theme.TEAL if online else theme.RED,
        )

        mirror_when = state.get("last_mirror_refresh")
        when = (
            time.strftime("%H:%M", time.localtime(mirror_when))
            if isinstance(mirror_when, (int, float))
            else tr("status.never")
        )
        line.append("  │  ", style=theme.SEPARATOR)
        line.append(tr("status.mirrors_label"), style=theme.LABEL)
        line.append(str(state.get("last_mirror_country") or "-"), style=theme.MAUVE)
        line.append(tr("status.mirror_time", when=when), style=theme.LABEL)

        if self.snapshot is not None:
            age = int(self.snapshot.age())
            line.append("  │  ", style=theme.SEPARATOR)
            line.append(
                (
                    tr("status.data_stale")
                    if self.snapshot.stale
                    else tr("status.data_age", age=age)
                ),
                style=theme.RED if self.snapshot.stale else theme.LABEL,
            )

        self.query_one("#status-text", Static).update(line)
        self.query_one("#pause-btn", Button).label = (
            tr("button.resume") if paused else tr("button.pause")
        )
        self.query_one("#disconnect-btn", Button).disabled = not connected
        self.query_one("#disconnect-btn", Button).label = tr("button.disconnect")
        self._render_current_info()
        self._refresh_proton_controls()
        if previous_server != self.current_server and self.selected_country_code:
            self._populate_servers(self.selected_country_code)

    def _render_current_info(self) -> None:
        widget = self.query_one("#current-info", Static)
        if not self.current_server:
            widget.update(Text(tr("current.none"), style=theme.LABEL))
            return

        line = Text()
        line.append(tr("current.label"), style=f"bold {theme.MAUVE}")
        info = None
        if self.snapshot is not None:
            info = current_server_info(
                self.snapshot,
                self.current_server,
                max_tier=self.snapshot.max_tier or None,
                include_tor=self.config.include_tor,
            )
        if info is None:
            line.append(self.current_server, style=theme.server_style())
            line.append(tr("current.unknown_suffix"), style=theme.LABEL)
            widget.update(line)
            return

        line.append(
            server_label(info.server.name, info.server.exit_code),
            style=theme.server_style(),
        )
        line.append(" · ", style=theme.SEPARATOR)
        line.append(f"{info.server.load}%", style=theme.load_style(info.server.load))
        line.append(" · ", style=theme.SEPARATOR)
        line.append(tr("current.best_in_prefix"), style=theme.LABEL)
        line.append(info.server.exit_code, style=theme.BLUE)
        line.append(" ", style=theme.LABEL)
        if info.country_best is not None:
            delta = info.delta_country or 0
            delta_text = (
                tr("current.best")
                if delta <= 0
                else tr("current.delta", delta=f"+{delta}%")
            )
            line.append("(", style=theme.LABEL)
            line.append(delta_text, style=theme.GREEN if delta <= 0 else theme.RED)
            line.append("): ", style=theme.LABEL)
            line.append(
                server_label(info.country_best.name, info.country_best.exit_code),
                style=theme.GREEN,
            )
            line.append(
                f" {info.country_best.load}%",
                style=theme.load_style(info.country_best.load),
            )
        else:
            line.append(tr("common.unknown"), style=theme.LABEL)
        line.append(" · ", style=theme.SEPARATOR)
        pool_label = tr(
            "current.pool.secure" if info.secure_core else "current.pool.regular"
        )
        line.append(tr("current.best_pool", pool=pool_label), style=theme.TEAL)
        line.append(
            server_label(info.pool_best.name, info.pool_best.exit_code),
            style=theme.GREEN,
        )
        line.append(
            f" {info.pool_best.load}%",
            style=theme.load_style(info.pool_best.load),
        )
        widget.update(line)

    def _refresh_logs(self) -> None:
        view = self.query_one("#log-view", RichLog)
        view.clear()
        events_list = log_query.query(limit=120)
        if not events_list:
            view.write(tr("log.empty"))
            return
        for event in events_list:
            view.write(log_query.format_text(event))

    def _format_bytes(self, value: int) -> str:
        if value >= 1024 * 1024:
            return f"{value / (1024 * 1024):.1f} MB"
        if value >= 1024:
            return f"{value / 1024:.1f} kB"
        return f"{value} B"

    def _refresh_log_stats(self) -> None:
        stats = events.log_stats()
        self.query_one("#log-stats", Static).update(
            tr(
                "logs.stats",
                active=self._format_bytes(stats.active_bytes),
                archives=stats.archive_files,
                total=self._format_bytes(stats.total_bytes),
            )
        )

    def _apply_log_limits(self) -> None:
        try:
            size_mb = int(self.query_one("#log-max-size", Input).value)
            keep = int(self.query_one("#log-keep-files", Input).value)
            age = int(self.query_one("#log-max-age", Input).value)
        except ValueError:
            self.notify(tr("logs.invalid"), severity="error")
            return
        if size_mb < 1 or keep < 0 or age < 0:
            self.notify(tr("logs.invalid"), severity="error")
            return
        updated = dataclasses.replace(
            self.config,
            log_max_bytes=size_mb * 1024 * 1024,
            log_keep_files=keep,
            log_max_age_days=age,
        )
        try:
            updated.validate()
        except PnoError as exc:
            self.notify(exc.message, severity="error")
            return
        config_module.save(updated)
        self.config = updated
        events.configure_from_config(self.config)
        events.prune()
        self._refresh_logs()
        self._refresh_log_stats()
        self._reload_daemon_config()
        self.notify(tr("logs.saved"), severity="information")

    def _prune_logs(self) -> None:
        result = events.prune()
        events.emit(
            "info",
            "logs",
            "logs.prune",
            result.to_dict(),
        )
        self._refresh_logs()
        self._refresh_log_stats()
        self.notify(
            tr("logs.pruned", removed=len(result.removed)),
            severity="information",
        )

    def _tick(self) -> None:
        self._load_snapshot_from_disk()
        self._refresh_status()
        if self._state_dirty:
            self._flush_state()
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active == "tab-logs":
            self._refresh_logs()
            self._refresh_log_stats()
        if tabs.active == "tab-mirrors" and self.mirror_result is None:
            self._load_mirror_overview()
        if tabs.active == "tab-proton":
            self._proton_gui_running = bool(find_gui_processes())
            self._refresh_proton_controls()
            self._request_killswitch_check()
        self._remote_status_worker()

    # --------------------------------------------------------------- workers
    @work(thread=True, exclusive=True, group="requirements")
    def _requirements_worker(self) -> None:
        try:
            report = check_requirements(self.config, for_tui=True)
        except Exception as exc:  # noqa: BLE001 - startup must not crash the TUI
            events.emit("error", "requirements", "requirements.crashed", {}, error=exc)
            return
        self.call_from_thread(self._apply_requirements, report)

    def _apply_requirements(self, report: RequirementReport) -> None:
        events.emit("info", "requirements", "requirements.check", report.to_dict())
        if report.failures:
            self.push_screen(RequirementsScreen(report), self._on_requirements_decision)
        elif report.warnings:
            self.notify(report.summary(), severity="warning", timeout=12)
        else:
            self.notify(tr("requirements.all_ok"), severity="information", timeout=3)

    def _on_requirements_decision(self, continue_startup: bool | None) -> None:
        if not continue_startup:
            self.exit()

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
        if not self.favorites_dirty_side:
            self._reload_favorites()
        else:
            selected = set(self.query_one("#favorites-list", SelectionList).selected)
            self._rebuild_favorites_visual(selected)
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
        server = self.snapshot.lookup(name) if self.snapshot else None
        load_text = f"{server.load}%" if server else "?"
        self.call_from_thread(
            self.notify,
            tr("notify.connected", server=name, load=load_text),
            severity="information",
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

    def _mirror_crashed(self, event: str, exc: BaseException, *, apply: bool) -> None:
        events.emit("error", "mirrors", event, {"apply": apply}, error=exc)
        self.call_from_thread(
            self.notify,
            tr("notify.mirror_failed", error=f"{type(exc).__name__}: {exc}"),
            severity="error",
        )

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
            except Exception as exc:  # noqa: BLE001 - the TUI must stay alive
                self._mirror_crashed("mirrors.worker.crashed", exc, apply=apply)
                return
        except Exception as exc:  # noqa: BLE001 - the TUI must stay alive
            self._mirror_crashed("mirrors.worker.crashed", exc, apply=apply)
            return
        try:
            self.call_from_thread(self._after_mirrors, payload)
        except Exception as exc:  # noqa: BLE001 - a render bug must not kill the TUI
            self._mirror_crashed("mirrors.render.crashed", exc, apply=apply)

    def _after_mirrors(self, payload: dict[str, Any]) -> None:
        self.mirror_result = payload
        self._render_mirror_table(payload)
        self._load_mirror_overview()
        country = payload.get("target_country") or "-"
        self.notify(tr("notify.mirrors_done", country=country), severity="information")

    # --------------------------------------------------------------- actions
    @on(DataTable.RowHighlighted, "#country-table")
    def _on_country_highlight(self, event: DataTable.RowHighlighted) -> None:
        if self._rebuilding or self._programmatic:
            return
        code = event.row_key.value
        if code:
            self.selected_country_code = str(code)
            self._populate_servers(str(code))
            self._mark_state_dirty()

    @on(DataTable.RowHighlighted, "#server-table")
    def _on_server_highlight(self, event: DataTable.RowHighlighted) -> None:
        if self._rebuilding or self._programmatic:
            return
        if event.row_key.value:
            self.selected_server = str(event.row_key.value)
            self._mark_state_dirty()

    @on(DataTable.RowHighlighted, "#secure-table")
    def _on_secure_highlight(self, event: DataTable.RowHighlighted) -> None:
        if self._rebuilding or self._programmatic:
            return
        if event.row_key.value:
            self.selected_secure_server = str(event.row_key.value)
            self._mark_state_dirty()

    @on(Input.Changed, "#servers-search")
    def _on_servers_search(self, event: Input.Changed) -> None:
        self.servers_query = event.value
        self._populate_countries()
        self._mark_state_dirty()

    @on(TextArea.Changed, "#favorites-text")
    def _on_favorites_text(self, event: TextArea.Changed) -> None:
        if self._favorites_syncing:
            return
        if (
            self.query_one("#favorites-text", TextArea).text
            == self._favorites_text_draft
        ):
            return
        if self.favorites_dirty_side is None:
            self._set_favorites_dirty("text")
        elif self.favorites_dirty_side != "text":
            return

    @on(SelectionList.SelectedChanged, "#favorites-list")
    def _on_favorites_selection(self, event: SelectionList.SelectedChanged) -> None:
        if self._favorites_syncing:
            return
        visible_selected = set(event.selection_list.selected)
        selected = (
            self._favorites_visual_draft - self._favorites_visible_values
        ) | visible_selected
        if selected == self._favorites_visual_draft:
            return
        self._favorites_visual_draft = selected
        if self.favorites_dirty_side is None:
            self._set_favorites_dirty("visual")
        elif self.favorites_dirty_side != "visual":
            return

    @on(Checkbox.Changed, "#favorites-regular")
    def _on_favorites_regular(self, event: Checkbox.Changed) -> None:
        self._favorites_filter_changed()

    @on(Checkbox.Changed, "#favorites-secure")
    def _on_favorites_secure(self, event: Checkbox.Changed) -> None:
        self._favorites_filter_changed()

    def _favorites_filter_changed(self) -> None:
        if self._favorites_syncing:
            return
        selected = set(self.query_one("#favorites-list", SelectionList).selected)
        self._rebuild_favorites_visual(selected)

    @on(Checkbox.Changed, "#servers-inventory")
    def _on_servers_inventory(self, event: Checkbox.Changed) -> None:
        self.servers_inventory_only = bool(event.value)
        self._populate_countries()
        self._mark_state_dirty()

    @on(Input.Changed, "#secure-search")
    def _on_secure_search(self, event: Input.Changed) -> None:
        self.secure_query = event.value
        self._populate_secure_core()
        self._mark_state_dirty()

    @on(Checkbox.Changed, "#secure-inventory")
    def _on_secure_inventory(self, event: Checkbox.Changed) -> None:
        self.secure_inventory_only = bool(event.value)
        self._populate_secure_core()
        self._mark_state_dirty()

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

    @on(Button.Pressed, "#favorites-apply")
    def _on_favorites_apply(self) -> None:
        self._apply_favorites()

    @on(Button.Pressed, "#favorites-revert")
    def _on_favorites_revert(self) -> None:
        self._revert_favorites()

    @on(Button.Pressed, "#log-apply-btn")
    def _on_log_apply(self) -> None:
        self._apply_log_limits()

    @on(Button.Pressed, "#log-prune-btn")
    def _on_log_prune(self) -> None:
        self._prune_logs()

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

    @on(TabbedContent.TabActivated)
    def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active in TAB_IDS:
            self._restore_tab = tabs.active
            self._mark_state_dirty()
        if tabs.active == "tab-proton":
            self._activate_proton_tab()

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
        self._mark_state_dirty()
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

    # -------------------------------------------------------------- session
    def _mark_state_dirty(self) -> None:
        self._state_dirty = True
        if not self._save_scheduled:
            self._save_scheduled = True
            self.set_timer(0.4, self._flush_state_from_timer)

    def _flush_state_from_timer(self) -> None:
        self._save_scheduled = False
        self._flush_state()

    def _flush_state(self) -> None:
        try:
            active = self.query_one("#tabs", TabbedContent).active
        except Exception:  # noqa: BLE001 - widget tree may be gone on unmount
            active = self._restore_tab
        if active in TAB_IDS:
            self._restore_tab = active
        state = TuiState(
            tab=self._restore_tab,
            servers_query=self.servers_query,
            secure_query=self.secure_query,
            servers_inventory_only=self.servers_inventory_only,
            secure_inventory_only=self.secure_inventory_only,
            selected_country=self.selected_country_code,
            selected_server=self.selected_server,
            selected_secure_server=self.selected_secure_server,
        )
        if not self.tui_state_store.save(state):
            events.emit(
                "warning",
                "tui",
                "tui.state.save-failed",
                {"path": str(self.tui_state_store.path)},
            )
        self._state_dirty = False

    def on_unmount(self) -> None:
        self._flush_state()

    # ------------------------------------------------------------ retranslate
    def _retranslate_proton(self) -> None:
        for widget_id, key in (
            ("proton-account-title", "proton.account"),
            ("proton-features-title", "proton.features"),
            ("proton-netshield-label", "proton.netshield"),
            ("proton-netshield-help", "proton.netshield.help"),
            ("proton-killswitch-label", "proton.killswitch"),
            ("proton-killswitch-help", "proton.killswitch.help"),
            ("proton-killswitch-note", "proton.killswitch.advanced_note"),
            ("proton-port-forwarding-help", "proton.port_forwarding.help"),
            ("proton-split-help", "proton.split.help"),
            ("proton-split-mode-label", "proton.split.mode"),
            ("proton-split-apps-label", "proton.split.apps"),
            ("proton-connection-title", "proton.connection"),
            ("proton-protun-help", "proton.protun.help"),
            ("proton-protocol-label", "proton.protocol"),
            ("proton-protocol-help", "proton.protocol.help"),
            ("proton-accelerator-help", "proton.accelerator.help"),
            ("proton-moderate-nat-help", "proton.moderate_nat.help"),
            ("proton-ipv6-help", "proton.ipv6.help"),
            ("proton-custom-dns-help", "proton.custom_dns.help"),
            ("proton-dns-label", "proton.custom_dns.ips"),
            ("proton-general-title", "proton.general"),
            ("proton-gui-title", "proton.gui_app"),
            ("proton-gui-help", "proton.gui_app.hint"),
        ):
            try:
                widget = self.query_one(f"#{widget_id}")
            except Exception:  # noqa: BLE001 - widget tree may be gone
                continue
            if isinstance(widget, (Label, Static)):
                widget.update(tr(key))
        for widget_id, key in (
            ("proton-manage-btn", "proton.account.manage"),
            ("proton-apply-btn", "proton.apply"),
            ("proton-revert-btn", "proton.revert"),
            ("proton-add-app-btn", "proton.split.add_running"),
            ("proton-killswitch-clean-btn", "proton.killswitch.clean"),
        ):
            try:
                self.query_one(f"#{widget_id}", Button).label = tr(key)
            except Exception:  # noqa: BLE001
                continue
        for widget_id, key in (
            ("proton-port-forwarding", "proton.port_forwarding"),
            ("proton-split", "proton.split"),
            ("proton-protun", "proton.protun"),
            ("proton-accelerator", "proton.accelerator"),
            ("proton-moderate-nat", "proton.moderate_nat"),
            ("proton-ipv6", "proton.ipv6"),
            ("proton-custom-dns", "proton.custom_dns"),
            ("proton-telemetry", "proton.telemetry"),
            ("proton-crash-reports", "proton.crash_reports"),
        ):
            try:
                self.query_one(f"#{widget_id}", Checkbox).label = tr(key)
            except Exception:  # noqa: BLE001
                continue
        try:
            self.query_one("#proton-dns", Input).placeholder = tr(
                "proton.dns_placeholder"
            )
            self.query_one("#proton-split-apps", TextArea).placeholder = tr(
                "proton.split.apps_placeholder"
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            netshield = self.query_one("#proton-netshield", Select)
            netshield_value = str(netshield.value)
            netshield.set_options(
                [
                    (tr("proton.netshield.off"), "off"),
                    (tr("proton.netshield.malware-only"), "malware-only"),
                    (
                        tr("proton.netshield.malware-ads-trackers"),
                        "malware-ads-trackers",
                    ),
                ]
            )
            if netshield_value in proton_settings.NETSHIELD_VALUES:
                netshield.value = netshield_value
            killswitch = self.query_one("#proton-killswitch", Select)
            killswitch_value = str(killswitch.value)
            killswitch.set_options(
                [
                    (tr("proton.killswitch.off"), "off"),
                    (tr("proton.killswitch.standard"), "standard"),
                    (tr("proton.killswitch.advanced"), "advanced"),
                ]
            )
            if killswitch_value in proton_settings.KILLSWITCH_VALUES:
                killswitch.value = killswitch_value
            mode = self.query_one("#proton-split-mode", Select)
            mode_value = str(mode.value)
            mode.set_options(
                [
                    (tr("proton.split.exclude"), "exclude"),
                    (tr("proton.split.include"), "include"),
                ]
            )
            if mode_value in {"exclude", "include"}:
                mode.value = mode_value
        except Exception:  # noqa: BLE001
            pass
        self._render_proton_account()
        self._render_proton_gui_app()
        self._render_killswitch_report()

    def _retranslate(self) -> None:
        self.TITLE = tr("app.title")
        tabs = self.query_one("#tabs", TabbedContent)
        for tab_id, key in (
            ("tab-servers", "tab.servers"),
            ("tab-secure", "tab.secure"),
            ("tab-favorites", "tab.favorites"),
            ("tab-mirrors", "tab.mirrors"),
            ("tab-logs", "tab.logs"),
            ("tab-proton", "tab.proton"),
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
        self.query_one("#favorites-hint", Static).update(tr("favorites.hint"))
        self.query_one("#favorites-visual-title", Static).update(
            tr("favorites.visual_title")
        )
        self.query_one("#favorites-text-title", Static).update(
            tr("favorites.text_title")
        )
        self.query_one("#favorites-regular", Checkbox).label = tr("favorites.regular")
        self.query_one("#favorites-secure", Checkbox).label = tr("favorites.secure")
        self.query_one("#favorites-apply", Button).label = tr("favorites.apply")
        self.query_one("#favorites-revert", Button).label = tr("favorites.revert")
        self.query_one("#label-log-size", Label).update(tr("logs.max_size"))
        self.query_one("#label-log-keep", Label).update(tr("logs.keep_files"))
        self.query_one("#label-log-age", Label).update(tr("logs.max_age"))
        self.query_one("#log-apply-btn", Button).label = tr("logs.apply")
        self.query_one("#log-prune-btn", Button).label = tr("logs.prune")
        selected = self._favorites_visual_draft if self.favorites_dirty_side else None
        self._rebuild_favorites_visual(selected)
        self._update_favorites_controls()
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
        self._retranslate_proton()
        self._populate_countries()
        self._populate_secure_core()
        if self.mirror_result:
            self._render_mirror_table(self.mirror_result)
        self._load_mirror_overview()
        self._refresh_status()
        self._refresh_logs()
        self._refresh_log_stats()


def run() -> None:
    from pno.tui.single_instance import TuiInstance

    instance = TuiInstance()
    if not instance.acquire():
        focused = instance.focus_existing()
        print(
            "pno: TUI is already running"
            + ("; focused the existing window." if focused else ".")
        )
        return
    try:
        PnoApp().run()
    finally:
        instance.close()


if __name__ == "__main__":
    run()
