from __future__ import annotations

import pytest

from pno import favorites
from pno.proton.serverlist import snapshot_from_payload
from tests.fixtures import helper_payload, server


def snapshot():
    return snapshot_from_payload(
        helper_payload(
            [
                server("UA#1", "UA", "UA", 10),
                server("DE#1", "DE", "DE", 20),
                server("CH-US#3", "CH", "US", 30, features=1),
                server("FR-US#9", "FR", "US", 40, features=1),
            ]
        ),
        source="helper",
    )


def test_parse_codes_names_and_routes():
    result = favorites.parse_text("UA\nUkraine\nCH-US#3\n", snapshot())
    assert not result.errors
    assert result.favorites is not None
    assert result.favorites.countries == ("UA",)
    assert result.favorites.secure_core_servers == ("CH-US#3",)


def test_parse_normalises_uk_and_duplicates():
    data = snapshot_from_payload(
        helper_payload(
            [
                server("UK#1", "UK", "UK", 10),
                server("CH-US#3", "CH", "US", 20, features=1),
            ]
        ),
        source="helper",
    )
    result = favorites.parse_text("UK\nGB\nCH-US#3\nCH-US#3\n", data)
    assert result.favorites is not None
    assert result.favorites.countries == ("GB",)
    assert result.favorites.secure_core_servers == ("CH-US#3",)
    assert len(result.warnings) == 2


@pytest.mark.parametrize(
    "text",
    ["Nope", "UA,DE", "UA/DE", "CH-US#999", "UA\n@bad"],
)
def test_parse_rejects_invalid_entries(text):
    result = favorites.parse_text(text, snapshot())
    assert result.errors
    assert result.favorites is None


def test_parse_requires_snapshot():
    result = favorites.parse_text("UA", None)
    assert result.errors


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "inventory.toml"
    original = favorites.Favorites(
        countries=("DE", "UA"), secure_core_servers=("CH-US#3", "FR-US#9")
    )
    favorites.save(original, path)
    loaded = favorites.load(path)
    assert loaded.countries == ("DE", "UA")
    assert loaded.secure_core_servers == ("CH-US#3", "FR-US#9")
    text = path.read_text(encoding="utf-8")
    assert "components" not in text
    assert "[countries]" in text


def test_selection_values_roundtrip():
    original = favorites.Favorites(
        countries=("UA", "DE"), secure_core_servers=("CH-US#3",)
    )
    values = favorites.selection_values(original)
    assert values == {"country:UA", "country:DE", "sc:CH-US#3"}
    assert favorites.from_selection(values) == original.normalized()


def test_normalized_text_is_stable():
    data = favorites.Favorites(countries=("UA", "DE"), secure_core_servers=("CH-US#3",))
    assert favorites.normalized_text(data) == "DE\nUA\nCH-US#3\n"


def test_visual_options():
    countries, routes = favorites.visual_options(snapshot())
    assert ("country:UA", "UA") in countries
    assert ("sc:CH-US#3", "CH-US#3") in routes


def test_removal_parser_accepts_stale_routes():
    result = favorites.parse_removal_text("CH-US#999\nUA\n")
    assert result.errors == ()
    assert result.favorites is not None
    assert result.favorites.secure_core_servers == ("CH-US#999",)
    assert result.favorites.countries == ("UA",)
