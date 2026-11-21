from __future__ import annotations

from pno.tui.flags import country_label, flag, server_label


def test_flag_maps_iso_codes():
    assert flag("UA") == "🇺🇦"
    assert flag("DE") == "🇩🇪"
    assert flag("US") == "🇺🇸"


def test_flag_normalises_proton_uk():
    assert flag("UK") == "🇬🇧"


def test_flag_falls_back_for_invalid_codes():
    assert flag("") == ""
    assert flag("USA") == ""
    assert flag("1A") == ""
    assert flag(None) == ""


def test_labels_include_flag_and_code():
    assert country_label("UA", "en").startswith("🇺🇦 UA")
    assert "Ukraine" in country_label("UA", "en")
    assert server_label("UA#12", "UA").startswith("🇺🇦 UA#12")
    assert server_label("UA#12", "USA") == "UA#12"
