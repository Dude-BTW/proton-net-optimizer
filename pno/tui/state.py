"""Persistent TUI session state.

Only presentation state lives here: the active tab, search queries, inventory
filters and the selected rows. Configuration (language, top-N, automation)
stays in ``config.toml`` and the daemon state stays in ``state.json``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pno import paths
from pno.state import atomic_write_json, read_json

TAB_IDS = (
    "tab-servers",
    "tab-secure",
    "tab-mirrors",
    "tab-logs",
    "tab-settings",
)


@dataclass
class TuiState:
    schema: int = 1
    tab: str = "tab-servers"
    servers_query: str = ""
    secure_query: str = ""
    servers_inventory_only: bool = False
    secure_inventory_only: bool = False
    selected_country: str | None = None
    selected_server: str | None = None
    selected_secure_server: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> "TuiState":
        if not isinstance(payload, dict):
            return cls()

        def text(key: str, default: str = "") -> str:
            value = payload.get(key, default)
            return value if isinstance(value, str) else default

        def optional(key: str) -> str | None:
            value = payload.get(key)
            return value if isinstance(value, str) and value else None

        def boolean(key: str, default: bool = False) -> bool:
            value = payload.get(key, default)
            return value if isinstance(value, bool) else default

        tab = text("tab", "tab-servers")
        return cls(
            schema=1,
            tab=tab if tab in TAB_IDS else "tab-servers",
            servers_query=text("servers_query"),
            secure_query=text("secure_query"),
            servers_inventory_only=boolean("servers_inventory_only"),
            secure_inventory_only=boolean("secure_inventory_only"),
            selected_country=optional("selected_country"),
            selected_server=optional("selected_server"),
            selected_secure_server=optional("selected_secure_server"),
        )


class TuiStateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (paths.state_dir() / "tui.json")

    def load(self) -> TuiState:
        return TuiState.from_dict(read_json(self.path, {}))

    def save(self, state: TuiState) -> bool:
        try:
            atomic_write_json(self.path, state.to_dict())
        except OSError:
            return False
        return True
