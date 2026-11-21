from __future__ import annotations

import json
import time

from pno.mirrors.models import MirrorStatusSnapshot
from pno.mirrors.status import (
    eligible_mirrors,
    load_status,
    mirror_age_hours,
    mirrors_for_country,
    parse_status_payload,
)
from tests.fixtures import mirror_status_payload


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
