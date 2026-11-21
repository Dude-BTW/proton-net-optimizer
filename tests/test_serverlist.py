from __future__ import annotations

import json
import time

import pytest

from pno.errors import DataUnavailableError, HelperError
from pno.proton.serverlist import (
    ServerCache,
    snapshot_from_payload,
    snapshot_from_proton_cache,
)
from tests.fixtures import helper_payload, proton_cache_payload, server


def test_snapshot_from_payload():
    payload = helper_payload(
        [server("UA#1", "UA", "UA", 7), server("UA#2", "UA", "UA", 3)]
    )
    snapshot = snapshot_from_payload(payload, source="helper")
    assert len(snapshot.servers) == 2
    assert snapshot.source == "helper"
    assert snapshot.stale is False


def test_snapshot_from_payload_rejects_empty():
    with pytest.raises(HelperError):
        snapshot_from_payload(helper_payload([]), source="helper")


def test_snapshot_from_proton_cache(tmp_path):
    path = tmp_path / "serverlist.json"
    path.write_text(
        json.dumps(proton_cache_payload([server("UA#1", "UA", "UA", 7)])),
        encoding="utf-8",
    )
    snapshot = snapshot_from_proton_cache(path)
    assert snapshot is not None
    assert snapshot.servers[0].name == "UA#1"
    assert snapshot.max_tier == 2
    assert snapshot.stale is False


def test_snapshot_from_proton_cache_marks_expired(tmp_path):
    payload = proton_cache_payload([server("UA#1", "UA", "UA", 7)])
    payload["LoadsExpirationTime"] = time.time() - 10
    path = tmp_path / "serverlist.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = snapshot_from_proton_cache(path)
    assert snapshot is not None
    assert snapshot.stale is True


def test_server_cache_prefers_helper(tmp_path):
    cache = ServerCache(
        cache_path=tmp_path / "servers.json",
        proton_cache=tmp_path / "missing.json",
        helper_runner=lambda: helper_payload([server("UA#1", "UA", "UA", 5)]),
    )
    snapshot = cache.get()
    assert snapshot.source == "helper"
    assert (tmp_path / "servers.json").is_file()
    # A second call within max_age must not need the helper again.
    cache._helper_runner = lambda: (_ for _ in ()).throw(AssertionError("called"))
    assert cache.get().servers[0].name == "UA#1"


def test_server_cache_falls_back_to_proton_cache(tmp_path):
    proton_path = tmp_path / "serverlist.json"
    proton_path.write_text(
        json.dumps(proton_cache_payload([server("DE#1", "DE", "DE", 9)])),
        encoding="utf-8",
    )

    def boom():
        raise HelperError("keyring locked")

    cache = ServerCache(
        cache_path=tmp_path / "servers.json",
        proton_cache=proton_path,
        helper_runner=boom,
    )
    snapshot = cache.get()
    assert snapshot.source == "proton-cache"
    assert snapshot.servers[0].name == "DE#1"


def test_server_cache_returns_stale_copy_when_everything_fails(tmp_path):
    cache = ServerCache(
        cache_path=tmp_path / "servers.json",
        proton_cache=tmp_path / "missing.json",
        helper_runner=lambda: helper_payload([server("UA#1", "UA", "UA", 5)]),
    )
    cache.get()
    cache._helper_runner = lambda: (_ for _ in ()).throw(HelperError("offline"))
    snapshot = cache.get(force=True)
    assert snapshot.stale is True


def test_server_cache_raises_without_any_data(tmp_path):
    def boom():
        raise HelperError("offline")

    cache = ServerCache(
        cache_path=tmp_path / "servers.json",
        proton_cache=tmp_path / "missing.json",
        helper_runner=boom,
    )
    with pytest.raises(DataUnavailableError):
        cache.get()
