from __future__ import annotations

from pno.tui.i18n import CATALOGS, set_language, tr


def test_catalogs_have_identical_keys():
    assert set(CATALOGS["en"]) == set(CATALOGS["uk"])


def test_translation_and_fallback():
    set_language("uk")
    assert tr("servers.connect") == "Підключити"
    assert tr("missing.key") == "missing.key"
    assert tr("notify.refreshed", count=3).endswith("(3)")
    set_language("en")
    assert tr("servers.connect") == "Connect"


def test_unknown_language_falls_back_to_english():
    set_language("xx")
    assert tr("servers.connect") == "Connect"
    set_language("en")


def test_every_literal_tr_key_exists():
    import re
    from pathlib import Path

    pattern = re.compile(r'tr\("([^"]+)"')
    root = Path(__file__).resolve().parents[1] / "pno"
    for name in ("tui/app.py", "tray/app.py"):
        source = (root / name).read_text(encoding="utf-8")
        used = set(pattern.findall(source))
        missing = used - set(CATALOGS["en"])
        assert not missing, f"{name} uses unknown keys: {sorted(missing)}"
