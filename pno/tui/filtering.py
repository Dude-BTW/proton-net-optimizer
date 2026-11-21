"""Search and inventory filters for the TUI tables.

The functions are pure and offline-testable; the widgets only pass their
current query, checkbox state and the inventory-derived sets.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from pno.countries import country_name
from pno.proton.models import CountryServers, Server


def normalize_text(value: object) -> str:
    return " ".join(str(value).casefold().split())


def matches(query: str, values: Sequence[object]) -> bool:
    """Case-insensitive substring match; whitespace-separated terms are AND."""
    terms = normalize_text(query).split()
    if not terms:
        return True
    haystack = normalize_text(
        " ".join(str(value) for value in values if value is not None)
    )
    return all(term in haystack for term in terms)


def country_values(country: CountryServers, language: str = "en") -> list[object]:
    values: list[object] = [
        country.code,
        country_name(country.code, "en"),
        country_name(country.code, "uk"),
        str(country.best.load),
        f"{country.best.load}%",
        str(len(country.servers)),
    ]
    for server in country.servers:
        values.extend(
            [
                server.name,
                server.city or "",
                str(server.load),
                f"{server.load}%",
            ]
        )
    return values


def route_values(route: Server, language: str = "en") -> list[object]:
    return [
        route.name,
        route.entry_code,
        route.exit_code,
        country_name(route.entry_code, "en"),
        country_name(route.entry_code, "uk"),
        country_name(route.exit_code, "en"),
        country_name(route.exit_code, "uk"),
        str(route.load),
        f"{route.load}%",
        f"{route.score:.2f}",
        route.city or "",
    ]


def filter_countries(
    countries: Iterable[CountryServers],
    *,
    query: str = "",
    inventory_only: bool = False,
    inventory_codes: Iterable[str] = (),
    language: str = "en",
) -> list[CountryServers]:
    allowed = {code.upper() for code in inventory_codes}
    return [
        country
        for country in countries
        if (not inventory_only or country.code in allowed)
        and matches(query, country_values(country, language))
    ]


def filter_routes(
    routes: Iterable[Server],
    *,
    query: str = "",
    inventory_only: bool = False,
    inventory_names: Iterable[str] = (),
    language: str = "en",
) -> list[Server]:
    allowed = {name.upper() for name in inventory_names}
    return [
        route
        for route in routes
        if (not inventory_only or route.name.upper() in allowed)
        and matches(query, route_values(route, language))
    ]
