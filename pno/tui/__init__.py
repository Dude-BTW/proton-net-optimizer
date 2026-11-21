"""Textual front-end package.

The app module is imported lazily by the CLI so that modules which only need
``pno.tui.i18n`` do not require Textual (the tray runs under the system Python,
which does not have it).
"""
