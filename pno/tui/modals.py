"""Modal dialogs for the TUI."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from pno.requirements import RequirementReport
from pno.tui.i18n import tr


class GuiConflictScreen(ModalScreen[bool]):
    """Ask whether pno may close the Proton VPN GUI and continue."""

    BINDINGS = [
        Binding("escape", "cancel", "No"),
        Binding("n", "cancel", "No"),
        Binding("y", "confirm", "Yes"),
    ]

    def __init__(
        self,
        *,
        title: str,
        message: str,
        detail: str = "",
        confirm_label: str,
        cancel_label: str,
    ) -> None:
        super().__init__()
        self.dialog_title = title
        self.dialog_message = message
        self.dialog_detail = detail
        self.confirm_label = confirm_label
        self.cancel_label = cancel_label

    def compose(self) -> ComposeResult:
        with Vertical(id="gui-dialog"):
            yield Static(self.dialog_title, id="gui-dialog-title")
            yield Static(self.dialog_message, id="gui-dialog-message")
            if self.dialog_detail:
                yield Static(self.dialog_detail, id="gui-dialog-detail")
            with Horizontal(id="gui-dialog-actions"):
                yield Button(self.cancel_label, id="gui-no", variant="primary")
                yield Button(self.confirm_label, id="gui-yes", variant="error")

    def on_mount(self) -> None:
        self.query_one("#gui-no", Button).focus()

    @on(Button.Pressed, "#gui-no")
    def _on_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#gui-yes")
    def _on_yes(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)


class RequirementsScreen(ModalScreen[bool]):
    """Show missing or degraded runtime components at startup."""

    BINDINGS = [
        Binding("escape", "continue_", "Continue"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, report: RequirementReport) -> None:
        super().__init__()
        self.report = report

    def compose(self) -> ComposeResult:
        with Vertical(id="requirements-dialog"):
            yield Static(tr("requirements.title"), id="requirements-title")
            yield Static(self.report.summary(), id="requirements-body")
            with Horizontal(id="requirements-actions"):
                yield Button(
                    tr("requirements.quit"), id="requirements-quit", variant="error"
                )
                yield Button(
                    tr("requirements.continue"),
                    id="requirements-continue",
                    variant="primary",
                )

    def on_mount(self) -> None:
        self.query_one("#requirements-continue", Button).focus()

    @on(Button.Pressed, "#requirements-quit")
    def _on_quit(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#requirements-continue")
    def _on_continue(self) -> None:
        self.dismiss(True)

    def action_continue_(self) -> None:
        self.dismiss(True)

    def action_quit_app(self) -> None:
        self.dismiss(False)
