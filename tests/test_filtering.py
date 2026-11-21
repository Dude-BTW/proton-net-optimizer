from __future__ import annotations

from pno.proton.models import CountryServers, Server
from pno.tui.filtering import (
    filter_countries,
    filter_routes,
    matches,
    normalize_text,
    route_values,
)


def make_server(name, entry, exit_code, load, *, score=1.0, city="City"):
    return Server(
        name=name,
        entry=entry,
        exit=exit_code,
        city=city,
        load=load,
        score=score,
        tier=2,
        features=0,
        status=1,
    )


def make_country(code, servers):
    ordered = tuple(sorted(servers, key=lambda item: item.sort_key))
    return CountryServers(code=code, servers=ordered)


UA = make_country(
    "UA",
    [
        make_server("UA#1", "UA", "UA", 5, city="Kyiv"),
        make_server("UA#2", "UA", "UA", 20),
    ],
)
DE = make_country("DE", [make_server("DE#1", "DE", "DE", 30, city="Frankfurt")])
FR = make_country("FR", [make_server("FR#1", "FR", "FR", 40, city="Paris")])


def test_normalize_and_matches():
    assert normalize_text("  UA   Ukraine ") == "ua ukraine"
    assert matches("", ["anything"])
    assert matches("ua", ["UA", "Ukraine"])
    assert matches("ukraine 5%", ["UA", "Ukraine", "5%"])
    assert not matches("ukraine 7%", ["UA", "Ukraine", "5%"])
    assert matches("FRANKFURT", ["DE", "Frankfurt"])


def test_filter_countries_by_query_columns():
    assert [item.code for item in filter_countries([UA, DE, FR], query="ukraine")] == [
        "UA"
    ]
    assert [item.code for item in filter_countries([UA, DE, FR], query="20%")] == ["UA"]
    assert [
        item.code for item in filter_countries([UA, DE, FR], query="frankfurt")
    ] == ["DE"]
    assert [item.code for item in filter_countries([UA, DE, FR], query="de#1")] == [
        "DE"
    ]
    assert filter_countries([UA, DE, FR], query="zzz") == []


def test_filter_countries_by_inventory_codes():
    visible = filter_countries(
        [UA, DE, FR], inventory_only=True, inventory_codes={"UA", "DE"}
    )
    assert [item.code for item in visible] == ["UA", "DE"]


def test_filter_routes_by_name_country_and_inventory():
    routes = [
        make_server("CH-US#3", "CH", "US", 40, city="New York"),
        make_server("FR-US#9", "FR", "US", 10, city="Paris"),
    ]
    assert [route.name for route in filter_routes(routes, query="CH-US")] == ["CH-US#3"]
    assert [route.name for route in filter_routes(routes, query="switzerland")] == [
        "CH-US#3"
    ]
    assert [route.name for route in filter_routes(routes, query="40%")] == ["CH-US#3"]
    assert [
        route.name
        for route in filter_routes(
            routes, inventory_only=True, inventory_names={"CH-US#3"}
        )
    ] == ["CH-US#3"]
    assert route_values(routes[0], "en")[-1] == "New York"
