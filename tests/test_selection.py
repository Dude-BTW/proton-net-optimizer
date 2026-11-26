from __future__ import annotations

from pno.inventory import Inventory, load, parse_secure_core_name
from pno.proton.models import FEATURE_SECURE_CORE, FEATURE_TOR
from pno.proton.selection import (
    current_server_info,
    regular_countries,
    secure_core_countries,
    secure_core_routes,
)
from pno.proton.serverlist import snapshot_from_payload
from tests.fixtures import helper_payload, server


def snapshot(servers):
    return snapshot_from_payload(helper_payload(servers), source="helper")


def test_parse_secure_core_name():
    assert parse_secure_core_name("CH-US#3") == ("CH", "US")
    assert parse_secure_core_name("IS") is None
    assert parse_secure_core_name("bad") is None


def test_inventory_country_codes_include_entries_and_exits():
    inventory = Inventory(
        secure_core_servers=("IS-US#1", "CH-JP#2", "CH-US#3"),
        known_countries=("CH", "DE"),
    )
    assert inventory.country_codes() == ("CH", "DE", "IS", "JP", "US")


def test_packaged_inventory_is_empty():
    inventory = load()
    assert inventory.secure_core_servers == ()
    assert inventory.known_countries == ()


def test_regular_countries_top_n_and_order():
    data = snapshot(
        [
            server("UA#1", "UA", "UA", 10, score=1.0),
            server("UA#2", "UA", "UA", 5, score=2.0),
            server("UA#3", "UA", "UA", 5, score=1.0),
            server("DE#1", "DE", "DE", 1, score=1.0),
        ]
    )
    countries = regular_countries(data, top_n=2)
    assert [item.code for item in countries] == ["DE", "UA"]
    assert [item.name for item in countries[1].servers] == ["UA#3", "UA#2"]


def test_regular_countries_filters_tier_tor_and_disabled():
    data = snapshot(
        [
            server("UA#1", "UA", "UA", 5, tier=3),
            server("UA#2", "UA", "UA", 4, features=FEATURE_TOR),
            server("UA#3", "UA", "UA", 3, status=0),
            server("UA#4", "UA", "UA", 2),
        ]
    )
    countries = regular_countries(data, top_n=10, max_tier=2)
    assert [item.name for item in countries[0].servers] == ["UA#4"]


def test_secure_core_aggregates_by_exit_country():
    data = snapshot(
        [
            server("CH-US#3", "CH", "US", 40, features=FEATURE_SECURE_CORE),
            server("IS-US#1", "IS", "US", 20, features=FEATURE_SECURE_CORE),
            server("CH-JP#2", "CH", "JP", 30, features=FEATURE_SECURE_CORE),
            server("CH-DE#2", "CH", "DE", 60, features=FEATURE_SECURE_CORE),
            server("US#1", "US", "US", 1),
        ]
    )
    inventory = Inventory(
        secure_core_servers=("CH-US#3", "IS-US#1", "CH-JP#2", "CH-DE#2"),
        secure_core_exits=("US", "JP", "DE"),
    )
    aggregates = secure_core_countries(data, inventory, max_tier=2)
    assert [item.exit_code for item in aggregates] == ["US", "JP", "DE"]
    assert aggregates[0].best.name == "IS-US#1"
    assert aggregates[0].entry_codes == ("IS", "CH")
    assert len(aggregates[0].routes) == 2


def test_secure_core_routes_lists_all_routes_sorted_by_load():
    data = snapshot(
        [
            server("CH-US#3", "CH", "US", 40, features=FEATURE_SECURE_CORE),
            server("FR-US#9", "FR", "US", 10, features=FEATURE_SECURE_CORE),
            server("US#1", "US", "US", 1),
        ]
    )
    routes = secure_core_routes(data)
    assert [route.name for route in routes] == ["FR-US#9", "CH-US#3"]
    inventory = Inventory(secure_core_servers=("CH-US#3",))
    filtered = secure_core_routes(data, inventory=inventory, inventory_only=True)
    assert [route.name for route in filtered] == ["CH-US#3"]


def test_secure_core_ignores_servers_outside_inventory():
    data = snapshot(
        [
            server("CH-US#3", "CH", "US", 40, features=FEATURE_SECURE_CORE),
            server("SE-US#1", "SE", "US", 5, features=FEATURE_SECURE_CORE),
        ]
    )
    inventory = Inventory(secure_core_servers=("CH-US#3",), secure_core_exits=("US",))
    aggregates = secure_core_countries(data, inventory)
    assert len(aggregates) == 1
    assert aggregates[0].best.name == "CH-US#3"


def test_known_countries_can_limit_the_view():
    data = snapshot(
        [
            server("UA#1", "UA", "UA", 5),
            server("DE#1", "DE", "DE", 1),
        ]
    )
    countries = regular_countries(
        data, top_n=5, known_countries=("UA",), include_all=False
    )
    assert [item.code for item in countries] == ["UA"]


def test_current_server_info_compares_with_regular_servers():
    data = snapshot(
        [
            server("UA#1", "UA", "UA", 10),
            server("UA#2", "UA", "UA", 5),
            server("DE#1", "DE", "DE", 1),
            server("CH-US#3", "CH", "US", 2, features=FEATURE_SECURE_CORE),
        ]
    )
    info = current_server_info(data, "UA#1")
    assert info is not None
    assert info.pool_best.name == "DE#1"
    assert info.country_best is not None
    assert info.country_best.name == "UA#2"
    assert info.delta_country == 5
    assert info.delta_pool == 9
    assert info.pool_rank == 3
    assert info.country_rank == 2
    assert info.secure_core is False


def test_current_server_info_compares_secure_core_routes_only():
    data = snapshot(
        [
            server("CH-US#3", "CH", "US", 40, features=FEATURE_SECURE_CORE),
            server("FR-US#9", "FR", "US", 10, features=FEATURE_SECURE_CORE),
            server("US#1", "US", "US", 1),
        ]
    )
    info = current_server_info(data, "CH-US#3")
    assert info is not None
    assert info.pool_best.name == "FR-US#9"
    assert info.country_best is not None
    assert info.country_best.name == "FR-US#9"
    assert info.delta_country == 30
    assert info.secure_core is True


def test_current_server_info_unknown_name():
    assert (
        current_server_info(snapshot([server("DE#1", "DE", "DE", 1)]), "ZZ#1") is None
    )


def test_current_server_info_favorites_only_limits_regular_pool():
    data = snapshot(
        [
            server("UA#1", "UA", "UA", 10),
            server("UA#2", "UA", "UA", 5),
            server("DE#1", "DE", "DE", 1),
            server("FR#1", "FR", "FR", 2),
        ]
    )
    info = current_server_info(
        data,
        "UA#1",
        favorites_only=True,
        inventory=Inventory(known_countries=("UA",)),
    )
    assert info is not None
    assert info.pool_best is not None
    assert info.pool_best.name == "UA#2"
    assert info.delta_pool == 5
    assert info.pool_rank == 2
    assert info.country_best is not None
    assert info.country_best.name == "UA#2"


def test_current_server_info_favorites_only_limits_secure_core_routes():
    data = snapshot(
        [
            server("CH-US#3", "CH", "US", 40, features=FEATURE_SECURE_CORE),
            server("FR-US#9", "FR", "US", 10, features=FEATURE_SECURE_CORE),
            server("IS-CH#1", "IS", "CH", 5, features=FEATURE_SECURE_CORE),
        ]
    )
    info = current_server_info(
        data,
        "CH-US#3",
        favorites_only=True,
        inventory=Inventory(secure_core_servers=("CH-US#3",)),
    )
    assert info is not None
    assert info.pool_best is not None
    assert info.pool_best.name == "CH-US#3"
    assert info.country_best is not None
    assert info.country_best.name == "FR-US#9"


def test_current_server_info_favorites_only_empty_pool():
    data = snapshot(
        [
            server("UA#1", "UA", "UA", 10),
            server("DE#1", "DE", "DE", 1),
        ]
    )
    info = current_server_info(
        data,
        "UA#1",
        favorites_only=True,
        inventory=Inventory(known_countries=("JP",)),
    )
    assert info is not None
    assert info.pool_best is None
    assert info.pool_rank is None
    assert info.delta_pool is None
    assert info.country_best is not None
    assert info.country_best.name == "UA#1"


def test_current_server_info_secure_pool_ignores_bare_countries():
    data = snapshot(
        [
            server("CH-US#3", "CH", "US", 40, features=FEATURE_SECURE_CORE),
            server("FR-US#9", "FR", "US", 10, features=FEATURE_SECURE_CORE),
        ]
    )
    info = current_server_info(
        data,
        "CH-US#3",
        favorites_only=True,
        inventory=Inventory(known_countries=("US",)),
    )
    assert info is not None
    assert info.pool_best is None
