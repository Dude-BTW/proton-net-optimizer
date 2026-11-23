from __future__ import annotations

import json
import time

from pno.mirrors.models import Mirror, MirrorStatusSnapshot
from pno.mirrors.status import (
    eligible_mirrors,
    load_status,
    mirror_age_hours,
    mirrors_for_country,
    parse_status_payload,
    restrict_to_sync_generation,
)
from tests.fixtures import mirror_status_payload


def make_mirror(url: str, last_sync: str | None) -> Mirror:
    return Mirror(
        url=url,
        protocol="https",
        country_code="UA",
        completion_pct=1.0,
        delay=60.0,
        duration_avg=0.2,
        score=1.0,
        last_sync=last_sync,
        active=True,
    )


def iso_hours_ago(hours: float, now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - hours * 3600.0))


def snapshot():
    return parse_status_payload(mirror_status_payload())


def test_parse_payload():
    parsed = snapshot()
    assert len(parsed.mirrors) == 7
    assert parsed.mirrors[0].country_code == "UA"


def test_eligible_filters_rsync_old_incomplete_inactive():
    eligible = eligible_mirrors(snapshot(), now=time.time())
    urls = {mirror.url for mirror in eligible}
    assert "rsync://rsync.example/archlinux/" not in urls
    assert "https://old.example/archlinux/" not in urls
    assert "https://incomplete.example/archlinux/" not in urls
    assert "https://inactive.example/archlinux/" not in urls
    assert "https://ua.example/archlinux/" in urls
    assert "https://de.example/archlinux/" in urls


def test_country_filter():
    ua = mirrors_for_country(snapshot().mirrors, "ua")
    assert {mirror.country_code for mirror in ua} == {"UA"}
    assert len(ua) == 6


def test_mirror_age():
    assert mirror_age_hours("2020-01-01T00:00:00Z", now=time.time()) > 1000
    assert mirror_age_hours(None) is None
    assert mirror_age_hours("not-a-date") is None


def test_sync_generation_drops_lagging_mirrors():
    now = time.time()
    fresh = make_mirror("https://fresh.example/archlinux/", iso_hours_ago(1, now))
    lagging = make_mirror("https://lag.example/archlinux/", iso_hours_ago(12, now))
    kept = restrict_to_sync_generation([fresh, lagging], max_skew_hours=6.0, now=now)
    assert [mirror.host for mirror in kept] == ["fresh.example"]


def test_sync_generation_keeps_unknown_sync_times():
    now = time.time()
    fresh = make_mirror("https://fresh.example/archlinux/", iso_hours_ago(1, now))
    unknown = make_mirror("https://unknown.example/archlinux/", None)
    kept = restrict_to_sync_generation([fresh, unknown], max_skew_hours=6.0, now=now)
    assert {mirror.host for mirror in kept} == {"fresh.example", "unknown.example"}


def test_sync_generation_can_be_disabled():
    now = time.time()
    fresh = make_mirror("https://fresh.example/archlinux/", iso_hours_ago(1, now))
    lagging = make_mirror("https://lag.example/archlinux/", iso_hours_ago(12, now))
    kept = restrict_to_sync_generation([fresh, lagging], max_skew_hours=0.0, now=now)
    assert len(kept) == 2


def test_load_status_uses_cache_and_falls_back(tmp_path):
    cache = tmp_path / "status.json"
    calls = {"count": 0}

    def fetcher():
        calls["count"] += 1
        return snapshot()

    first = load_status(cache_path=cache, fetcher=fetcher, max_age=3600)
    second = load_status(cache_path=cache, fetcher=fetcher, max_age=3600)
    assert calls["count"] == 1
    assert len(first.mirrors) == len(second.mirrors)

    def broken():
        from pno.errors import NetworkError

        raise NetworkError("offline")

    stale = load_status(cache_path=cache, fetcher=broken, force=True, max_age=3600)
    assert len(stale.mirrors) == 7


def test_cache_file_is_valid_json(tmp_path):
    cache = tmp_path / "status.json"
    load_status(cache_path=cache, fetcher=snapshot)
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    assert len(payload["mirrors"]) == 7
    assert len(MirrorStatusSnapshot.from_dict(payload).mirrors) == 7
