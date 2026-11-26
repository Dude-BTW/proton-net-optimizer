"""Suggestions derived from live server measurements.

No static ranking is encoded here: every list is sorted by the load reported
by Proton at fetch time, with the API score as a deterministic tie-breaker.
"""

from __future__ import annotations

from dataclasses import dataclass

from pno.inventory import Inventory
from pno.inventory import load as load_inventory
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


def secure_core_routes(
    snapshot: ServerSnapshot,
    *,
    max_tier: int | None = None,
    include_tor: bool = False,
    inventory: Inventory | None = None,
    inventory_only: bool = False,
) -> list[Server]:
    """Return Secure Core logical servers, least loaded first.

    When ``inventory_only`` is set, only the exact routes listed in the
    inventory are returned.
    """
    routes = eligible_servers(
        snapshot.servers,
        max_tier=max_tier,
        include_tor=include_tor,
        secure_core=True,
    )
    if inventory_only and inventory is not None:
        allowed = {name.upper() for name in inventory.secure_core_servers}
        routes = [server for server in routes if server.name.upper() in allowed]
    return sorted(
        routes, key=lambda server: (server.load, server.exit_code, server.name)
    )


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


@dataclass(frozen=True)
class CurrentServerInfo:
    """Measured context for the server the user is connected to."""

    server: Server
    pool_best: Server | None
    country_best: Server | None
    pool_rank: int | None
    country_rank: int | None

    @property
    def delta_pool(self) -> int | None:
        if self.pool_best is None:
            return None
        return self.server.load - self.pool_best.load

    @property
    def delta_country(self) -> int | None:
        if self.country_best is None:
            return None
        return self.server.load - self.country_best.load

    @property
    def secure_core(self) -> bool:
        return self.server.secure_core


def current_server(snapshot: ServerSnapshot, name: str | None) -> Server | None:
    if not name:
        return None
    return snapshot.lookup(name)


def _favorites_only_pool(
    pool: list[Server], inventory: Inventory, *, secure_core: bool
) -> list[Server]:
    """Restrict an already eligible pool to the user's favorites.

    Regular pools use favorite countries; Secure Core pools use the exact
    routes saved in the favorites file. No ordering is added here.
    """
    if secure_core:
        allowed = {name.upper() for name in inventory.secure_core_servers}
        return [server for server in pool if server.name.upper() in allowed]
    allowed_countries = {code.upper() for code in inventory.known_countries}
    return [
        server
        for server in pool
        if server.exit_code and server.exit_code.upper() in allowed_countries
    ]


def current_server_info(
    snapshot: ServerSnapshot,
    name: str | None,
    *,
    max_tier: int | None = None,
    include_tor: bool = False,
    favorites_only: bool = False,
    inventory: Inventory | None = None,
) -> CurrentServerInfo | None:
    """Compare the connected server with the best alternatives.

    Regular servers are compared with regular servers, Secure Core routes with
    Secure Core routes; the country comparison always uses the same exit
    country. With ``favorites_only`` the pool alternative (``best regular`` /
    ``best Secure Core``) is restricted to the user's favorites while the
    local country comparison stays unchanged.
    """
    server = current_server(snapshot, name)
    if server is None:
        return None
    pool = eligible_servers(
        snapshot.servers,
        max_tier=max_tier,
        include_tor=include_tor,
        secure_core=server.secure_core,
    )
    if not pool:
        return CurrentServerInfo(
            server=server,
            pool_best=server,
            country_best=None,
            pool_rank=1,
            country_rank=None,
        )
    full_pool = pool
    if favorites_only:
        favorites = inventory if inventory is not None else load_inventory()
        pool = _favorites_only_pool(pool, favorites, secure_core=server.secure_core)
    pool_best = min(pool, key=lambda item: item.sort_key) if pool else None
    pool_rank = (
        1 + sum(1 for item in pool if item.sort_key < server.sort_key) if pool else None
    )
    country_pool = [item for item in full_pool if item.exit_code == server.exit_code]
    country_best = (
        min(country_pool, key=lambda item: item.sort_key) if country_pool else None
    )
    country_rank = (
        1 + sum(1 for item in country_pool if item.sort_key < server.sort_key)
        if country_pool
        else None
    )
    return CurrentServerInfo(
        server=server,
        pool_best=pool_best,
        country_best=country_best,
        pool_rank=pool_rank,
        country_rank=country_rank,
    )
