"""Minimal i18n for the Textual interface.

English is the default; Ukrainian is selectable. Machine contracts (log
events, JSON keys, CLI flags) stay English and never go through ``tr``.
"""

from __future__ import annotations

from pno.tui.i18n import en, uk

CATALOGS = {"en": en.MESSAGES, "uk": uk.MESSAGES}
DEFAULT_LANGUAGE = "en"
LANGUAGES = (("en", "language.en"), ("uk", "language.uk"))

_language = DEFAULT_LANGUAGE


def set_language(language: str) -> None:
    global _language
    _language = language if language in CATALOGS else DEFAULT_LANGUAGE


def get_language() -> str:
    return _language


def tr(key: str, **kwargs: object) -> str:
    text = CATALOGS[_language].get(key) or CATALOGS[DEFAULT_LANGUAGE].get(key) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text
