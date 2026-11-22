"""Modal dialogs for the TUI."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from pno.requirements import RequirementReport
from pno.tui.i18n import tr
from pno.tui.running_apps import list_running_executables


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


class RunningAppScreen(ModalScreen[str]):
    """Pick one running executable for the split-tunneling list."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.apps = list_running_executables()

    def compose(self) -> ComposeResult:
        with Vertical(id="running-app-dialog"):
            yield Static(tr("proton.add_app.title"), id="running-app-title")
            yield Input(
                placeholder=tr("proton.add_app.search"),
                id="running-app-search",
            )
            yield OptionList(id="running-app-list")
            yield Static("", id="running-app-empty")
            with Horizontal(id="running-app-actions"):
                yield Button(tr("proton.add_app.cancel"), id="running-app-cancel")
                yield Button(
                    tr("proton.add_app.add"),
                    id="running-app-add",
                    variant="primary",
                )

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#running-app-search", Input).focus()

    def _fill(self, query: str) -> None:
        widget = self.query_one("#running-app-list", OptionList)
        empty = self.query_one("#running-app-empty", Static)
        needle = query.strip().casefold()
        matches = [app for app in self.apps if needle in app.casefold()]
        widget.clear_options()
        if matches:
            widget.add_options([Option(app, id=app) for app in matches])
            widget.highlighted = 0
            empty.update("")
        else:
            widget.highlighted = None
            empty.update(tr("proton.add_app.empty"))

    @on(Input.Changed, "#running-app-search")
    def _on_search(self, event: Input.Changed) -> None:
        self._fill(event.value)

    @on(Input.Submitted, "#running-app-search")
    def _on_search_submitted(self) -> None:
        self._on_add()

    @on(OptionList.OptionSelected, "#running-app-list")
    def _on_option(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss(str(event.option.id))

    @on(Button.Pressed, "#running-app-add")
    def _on_add(self) -> None:
        option = self.query_one("#running-app-list", OptionList).highlighted_option
        if option is not None and option.id:
            self.dismiss(str(option.id))

    @on(Button.Pressed, "#running-app-cancel")
    def _on_cancel(self) -> None:
        self.dismiss("")

    def action_cancel(self) -> None:
        self.dismiss("")


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
