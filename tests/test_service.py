from __future__ import annotations

import time

from pno.config import Config
from pno.mirrors.models import ProbeResult
from pno.mirrors.planner import select_results
from pno.mirrors.service import refresh_mirrors, resolve_target
from pno.mirrors.status import parse_status_payload
from pno.proton.status import VpnStatus
from pno.state import StateStore
from tests.fixtures import mirror_status_payload


def fake_probe(mirrors, **kwargs):
    results = []
    for index, mirror in enumerate(mirrors):
        latency = 10.0 + index
        results.append(ProbeResult(mirror, True, latency, 206))
    return results


def test_resolve_target_uses_vpn_exit():
    config = Config()
    vpn = VpnStatus(
        connected=True, server_name="CH-US#3", exit_country="UK", source="test"
    )
    assert resolve_target(config, vpn) == ("GB", "vpn-exit")


def test_resolve_target_uses_home_when_disconnected():
    config = Config(home_country="ua")
    vpn = VpnStatus(connected=False, source="test")
    assert resolve_target(config, vpn) == ("UA", "home")


def test_refresh_uses_exit_country_and_records_state(tmp_path):
    state = StateStore(tmp_path / "state.json")
    vpn = VpnStatus(
        connected=True, server_name="CH-US#3", exit_country="US", source="test"
    )
    result = refresh_mirrors(
        Config(mirror_count=3),
        state,
        vpn_status=vpn,
        apply=False,
        status_fetcher=lambda: parse_status_payload(mirror_status_payload()),
        probe=fake_probe,
        now=time.time(),
    )
    assert result["target_country"] == "US"
    assert result["target_source"] == "vpn-exit"
    assert len(result["selected"]) == 3
    stored = state.read()
    assert stored["last_measure_country"] == "US"
    assert stored["last_measure_source"] == "vpn-exit"
    assert len(stored["last_measure_servers"]) == 3
    assert stored["last_measure_apply"]["reason"] == "not-requested"
    # A measurement must not look like an applied refresh.
    assert stored.get("last_mirror_country") is None


def test_refresh_can_override_the_country(tmp_path):
    state = StateStore(tmp_path / "state.json")
    vpn = VpnStatus(connected=False, source="test")
    result = refresh_mirrors(
        Config(),
        state,
        vpn_status=vpn,
        target_country="de",
        apply=False,
        status_fetcher=lambda: parse_status_payload(mirror_status_payload()),
        probe=fake_probe,
    )
    assert result["target_country"] == "DE"
    assert result["target_source"] == "manual"


def test_https_is_preferred_for_the_same_host():
    mirrors = parse_status_payload(mirror_status_payload()).mirrors
    ua_https = next(mirror for mirror in mirrors if mirror.host == "ua.example")
    from pno.mirrors.models import Mirror

    ua_http = Mirror(
        url="http://ua.example/archlinux/",
        protocol="http",
        country_code="UA",
        completion_pct=1.0,
        delay=0,
        duration_avg=0.1,
        score=0.1,
        last_sync=ua_https.last_sync,
        active=True,
    )
    results = [
        ProbeResult(ua_http, True, 10.0, 206),
        ProbeResult(ua_https, True, 11.0, 206),
    ]
    selected = select_results(results, 1)
    assert selected[0].protocol == "https"
