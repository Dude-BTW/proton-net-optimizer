from __future__ import annotations

import time

import pytest

from pno.config import Config
from pno.errors import ProbeError
from pno.mirrors.models import ProbeResult
from pno.mirrors.planner import (
    build_candidates,
    plan_mirrors,
    same_server_set,
    select_results,
    server_lines,
)
from pno.mirrors.status import parse_status_payload
from tests.fixtures import mirror_status_payload


def snapshot():
    return parse_status_payload(mirror_status_payload())


def test_candidates_include_country_and_global():
    candidates = build_candidates(
        snapshot(), target_country="UA", config=Config(), now=time.time()
    )
    urls = [mirror.url for mirror in candidates]
    assert "https://ua.example/archlinux/" in urls
    assert "https://de.example/archlinux/" in urls
    assert "rsync://rsync.example/archlinux/" not in urls


def test_select_results_orders_by_latency_and_dedupes_hosts():
    parsed = snapshot().mirrors
    results = [
        ProbeResult(parsed[0], True, 10.0, 206),
        ProbeResult(parsed[1], True, 30.0, 206),
        ProbeResult(parsed[2], True, 20.0, 206),
        ProbeResult(parsed[0], True, 5.0, 206),  # duplicate host
    ]
    selected = select_results(results, 2)
    assert [mirror.host for mirror in selected] == ["ua.example", "de.example"]


def test_plan_uses_measured_latency():
    latencies = {
        "https://ua.example/archlinux/": 90.0,
        "http://ua2.example/archlinux/": 40.0,
        "https://de.example/archlinux/": 10.0,
    }

    def probe(mirrors, **kwargs):
        return [
            ProbeResult(
                mirror,
                ok=mirror.url in latencies,
                latency_ms=latencies.get(mirror.url),
                status_code=206 if mirror.url in latencies else None,
                error=None if mirror.url in latencies else "timeout",
            )
            for mirror in mirrors
        ]

    plan = plan_mirrors(
        snapshot(),
        target_country="UA",
        target_source="home",
        config=Config(mirror_count=3),
        probe=probe,
    )
    assert [mirror.host for mirror in plan.selected] == [
        "de.example",
        "ua2.example",
        "ua.example",
    ]
    assert len(plan.server_lines) == 3
    assert all(line.endswith("$repo/os/$arch") for line in plan.server_lines)


def test_plan_raises_when_every_probe_fails():
    def probe(mirrors, **kwargs):
        return [ProbeResult(mirror, False, None, None, "timeout") for mirror in mirrors]

    with pytest.raises(ProbeError):
        plan_mirrors(
            snapshot(),
            target_country="UA",
            target_source="home",
            config=Config(),
            probe=probe,
        )


def test_same_server_set_ignores_comments_and_order():
    first = "# a\nServer = https://a/$repo/os/$arch\n"
    second = "# b\nServer = https://b/$repo/os/$arch\n"
    assert same_server_set(first, first)
    assert not same_server_set(first, second)
    assert server_lines(first) == ["Server = https://a/$repo/os/$arch"]


def test_render_contains_measurements():
    parsed = snapshot().mirrors
    plan = plan_mirrors(
        snapshot(),
        target_country="UA",
        target_source="home",
        config=Config(mirror_count=1),
        probe=lambda mirrors, **kwargs: [
            ProbeResult(
                mirror,
                ok=mirror.url == parsed[0].url,
                latency_ms=12.3 if mirror.url == parsed[0].url else None,
                status_code=206 if mirror.url == parsed[0].url else None,
            )
            for mirror in mirrors
        ],
    )
    assert "12 ms" in plan.content
    assert "Target country: UA" in plan.content
