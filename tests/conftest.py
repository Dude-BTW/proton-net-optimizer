from __future__ import annotations

import pytest

from pno import events


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PNO_HOME", str(tmp_path / "pno-home"))
    monkeypatch.setenv("PNO_CONFIG_DIR", str(tmp_path / "pno-home" / "config"))
    monkeypatch.setenv("PNO_STATE_DIR", str(tmp_path / "pno-home" / "state"))
    monkeypatch.setenv("PNO_RUNTIME_DIR", str(tmp_path / "pno-home" / "run"))
    monkeypatch.setenv(
        "PNO_PROTON_CONFIG_DIR", str(tmp_path / "pno-home" / "proton-vpn")
    )
    events.reset_for_tests()
    yield
    events.reset_for_tests()
