"""Tray icon front-end (PySide6).

The tray is optional. When PySide6 is not installed in the project virtualenv,
``pno tray`` re-executes this module with the system Python, which has the
``pyside6`` package from pacman.
"""

from __future__ import annotations

import fcntl
import sys
import threading
from typing import Any

try:
    from PySide6.QtCore import Qt, QTimer, Signal
    from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
except ImportError as exc:  # pragma: no cover - depends on the host
    raise ImportError("PySide6 is required for the tray icon") from exc

from pno import config as config_module
from pno import events, paths
from pno.config import Config
from pno.errors import PnoError
from pno.ipc import request
from pno.proton.connection import GuiActiveError, disconnect_server
from pno.state import StateStore
from pno.tui.i18n import set_language, tr

COLORS = {
    "running": QColor("#2ecc71"),
    "paused": QColor("#f1c40f"),
    "offline": QColor("#95a5a6"),
    "error": QColor("#e74c3c"),
}


def _icon(state: str) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(COLORS.get(state, COLORS["offline"]))
    painter.setPen(QPen(QColor("#1b1b1b"), 3))
    painter.drawEllipse(6, 6, 52, 52)
    painter.setPen(QPen(QColor("#ffffff"), 5))
    painter.drawLine(20, 40, 20, 22)
    painter.drawLine(20, 22, 44, 22)
    painter.drawLine(20, 32, 36, 32)
    painter.end()
    return QIcon(pixmap)


class Tray(QSystemTrayIcon):
    status_ready = Signal(object)

    def __init__(self, app: QApplication, config: Config) -> None:
        super().__init__()
        self.app = app
        self.config = config
        self.state_store = StateStore(paths.state_file())
        self.status: dict[str, Any] = {}
        self._probe_running = False

        self.menu = QMenu()
        self.status_action = QAction(tr("app.title"), self.menu)
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)
        self.menu.addSeparator()

        self.open_action = QAction(tr("tray.open_tui"), self.menu)
        self.open_action.triggered.connect(self._open_tui)
        self.menu.addAction(self.open_action)

        self.refresh_servers_action = QAction(tr("servers.refresh"), self.menu)
        self.refresh_servers_action.triggered.connect(self._refresh_servers)
        self.menu.addAction(self.refresh_servers_action)

        self.refresh_mirrors_action = QAction(tr("mirrors.refresh"), self.menu)
        self.refresh_mirrors_action.triggered.connect(self._refresh_mirrors)
        self.menu.addAction(self.refresh_mirrors_action)

        self.disconnect_action = QAction(tr("button.disconnect"), self.menu)
        self.disconnect_action.triggered.connect(self._disconnect)
        self.disconnect_action.setEnabled(False)
        self.menu.addAction(self.disconnect_action)

        self.pause_action = QAction(tr("button.pause"), self.menu)
        self.pause_action.setCheckable(True)
        self.pause_action.triggered.connect(self._toggle_pause)
        self.menu.addAction(self.pause_action)

        self.menu.addSeparator()
        self.quit_action = QAction(tr("tray.quit"), self.menu)
        self.quit_action.triggered.connect(self._quit)
        self.menu.addAction(self.quit_action)

        self.setContextMenu(self.menu)
        self.activated.connect(self._on_activated)
        self.status_ready.connect(self._apply_status)

        self.setIcon(_icon("offline"))
        self.setToolTip(tr("app.title"))
        self.show()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._probe)
        self.timer.start(4000)
        self._probe()

    # ------------------------------------------------------------------ probe
    def _probe(self) -> None:
        if self._probe_running:
            return
        self._probe_running = True

        def worker() -> None:
            try:
                payload = request("status", timeout=2.0)
            except PnoError:
                payload = None
            self.status_ready.emit(payload)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_status(self, payload: object) -> None:
        self._probe_running = False
        self.status = payload if isinstance(payload, dict) else {}
        state = self.state_store.read()
        paused = bool((self.status or state).get("paused"))
        online = bool((self.status.get("daemon") or {}).get("running"))
        error = state.get("mirror_error") or state.get("servers_error")
        if error:
            visual = "error"
        elif not online:
            visual = "offline"
        elif paused:
            visual = "paused"
        else:
            visual = "running"

        vpn = self.status.get("vpn") or state.get("vpn") or {}
        if vpn.get("connected"):
            load = vpn.get("load")
            vpn_text = tr(
                "status.connected",
                server=vpn.get("server_name") or "?",
                exit=vpn.get("exit_country") or "?",
                load=f"{load}%" if isinstance(load, int) else "?",
            )
        else:
            vpn_text = tr("status.disconnected", home=self.config.home_country)
        automation = tr("status.paused") if paused else tr("status.running")
        daemon_text = "daemon" if online else tr("status.daemon_offline")
        self.status_action.setText(f"{vpn_text}\n{automation} | {daemon_text}")
        self.setToolTip(f"{tr('app.title')}\n{vpn_text}\n{automation}")
        self.setIcon(_icon(visual))
        self.pause_action.setChecked(paused)
        self.pause_action.setText(tr("button.resume") if paused else tr("button.pause"))
        self.disconnect_action.setEnabled(bool(vpn.get("connected")))

    # --------------------------------------------------------------- commands
    def _notify(self, message: str, *, error: bool = False) -> None:
        icon = (
            QSystemTrayIcon.MessageIcon.Critical
            if error
            else QSystemTrayIcon.MessageIcon.Information
        )
        self.showMessage(tr("app.title"), message, icon, 5000)

    def _run(
        self, label: str, command: str, args: dict[str, Any] | None = None
    ) -> None:
        def worker() -> None:
            try:
                request(command, args or {}, timeout=600)
            except PnoError as exc:
                self.status_ready.emit(self.status)
                self.showMessage(
                    tr("app.title"),
                    f"{label}: {exc.message}",
                    QSystemTrayIcon.MessageIcon.Critical,
                    6000,
                )
            else:
                self.showMessage(
                    tr("app.title"),
                    f"{label}: ok",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
            self._probe()

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_servers(self) -> None:
        self._run(tr("servers.refresh"), "servers.refresh", {"force": True})

    def _refresh_mirrors(self) -> None:
        self._run(tr("mirrors.refresh"), "mirrors.refresh", {"apply": True})

    def _toggle_pause(self, checked: bool) -> None:
        label = tr("button.pause") if checked else tr("button.resume")
        self._run(label, "pause" if checked else "resume")

    def _disconnect(self) -> None:
        def worker() -> None:
            try:
                disconnect_server()
            except GuiActiveError:
                self.showMessage(
                    tr("app.title"),
                    tr("tray.gui_active"),
                    QSystemTrayIcon.MessageIcon.Warning,
                    6000,
                )
            except PnoError as exc:
                self.showMessage(
                    tr("app.title"),
                    tr("notify.disconnect_failed", error=exc.message),
                    QSystemTrayIcon.MessageIcon.Critical,
                    6000,
                )
            else:
                self.showMessage(
                    tr("app.title"),
                    tr("notify.disconnected"),
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
            self._probe()

        threading.Thread(target=worker, daemon=True).start()

    def _open_tui(self) -> None:
        from pno.launcher import open_tui

        try:
            open_tui(self.config)
        except PnoError as exc:
            self.showMessage(
                tr("app.title"),
                tr("tray.open_error", terminal=self.config.terminal, error=exc.message),
                QSystemTrayIcon.MessageIcon.Critical,
                6000,
            )

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._open_tui()

    def _quit(self) -> None:
        self.hide()
        self.app.quit()


def _acquire_single_instance_lock():
    """Return an open lock handle, or ``None`` when a tray already runs."""
    paths.ensure_runtime_dir()
    handle = open(paths.runtime_dir() / "tray.lock", "w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> int:
    config = config_module.ensure()
    set_language(config.language)
    events.configure(paths.logs_dir(), level=config.log_level)
    lock = _acquire_single_instance_lock()
    if lock is None:
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName("pno-tray")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("pno: system tray is not available", file=sys.stderr)
        return 1
    tray = Tray(app, config)
    events.emit("info", "tray", "tray.start")
    code = app.exec()
    tray.hide()
    events.emit("info", "tray", "tray.stop")
    lock.close()
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
