from __future__ import annotations

from pno.countries import arch_code, country_name, normalize_code, proton_code


def test_proton_uk_is_normalised_to_arch_gb():
    assert normalize_code("UK") == "GB"
    assert arch_code("UK") == "GB"
    assert proton_code("GB") == "UK"


def test_names_are_bilingual():
    assert country_name("UA", "en") == "Ukraine"
    assert country_name("UA", "uk") == "Україна"
    assert country_name("GB", "en") == "United Kingdom"


def test_unknown_code_falls_back_to_itself():
    assert country_name("ZZ", "en") == "ZZ"
    assert normalize_code("") == ""
