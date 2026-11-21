"""Suggestions derived from live server measurements.

No static ranking is encoded here: every list is sorted by the load reported
by Proton at fetch time, with the API score as a deterministic tie-breaker.
"""

from __future__ import annotations

from pno.inventory import Inventory
from pno.proton.models import (
    CountryServers,
    SecureCoreCountry,
    Server,
    ServerSnapshot,
    eligible_servers,
)


def regular_countries(
    snapshot: ServerSnapshot,
    *,
    top_n: int,
    include_tor: bool = False,
    max_tier: int | None = None,
    known_countries: tuple[str, ...] = (),
    include_all: bool = True,
) -> list[CountryServers]:
    """Return per-country least-loaded servers, sorted by their best server."""
    servers = eligible_servers(
        snapshot.servers, max_tier=max_tier, include_tor=include_tor
    )
    grouped: dict[str, list[Server]] = {}
    for server in servers:
        code = server.exit_code
        if not code:
            continue
        grouped.setdefault(code, []).append(server)

    if not include_all:
        allowed = {code.upper() for code in known_countries}
        grouped = {code: items for code, items in grouped.items() if code in allowed}

    result = [
        CountryServers(
            code=code,
            servers=tuple(sorted(items, key=lambda item: item.sort_key)[:top_n]),
        )
        for code, items in grouped.items()
    ]
    result.sort(key=lambda country: country.best.sort_key)
    return result


def secure_core_countries(
    snapshot: ServerSnapshot,
    inventory: Inventory,
    *,
    max_tier: int | None = None,
    include_tor: bool = False,
) -> list[SecureCoreCountry]:
    """Aggregate Secure Core routes by exit country.

    The aggregate load is the minimum load among the routes Proton reports for
    that exit country, i.e. the best route currently available.
    """
    allowed = {name.upper() for name in inventory.secure_core_servers}
    routes = [
        server
        for server in eligible_servers(
            snapshot.servers,
            max_tier=max_tier,
            include_tor=include_tor,
            secure_core=True,
        )
        if server.name.upper() in allowed
    ]
    grouped: dict[str, list[Server]] = {}
    for server in routes:
        grouped.setdefault(server.exit_code, []).append(server)

    result: list[SecureCoreCountry] = []
    for code in inventory.exits():
        items = grouped.get(code)
        if not items:
            continue
        ordered = tuple(sorted(items, key=lambda item: item.sort_key))
        result.append(
            SecureCoreCountry(exit_code=code, best=ordered[0], routes=ordered)
        )
    result.sort(key=lambda country: country.best.sort_key)
    return result


def current_server(snapshot: ServerSnapshot, name: str | None) -> Server | None:
    if not name:
        return None
    return snapshot.lookup(name)
