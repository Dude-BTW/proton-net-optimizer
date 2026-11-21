from __future__ import annotations

import pytest

from pno import events


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PNO_HOME", str(tmp_path / "pno-home"))
    events.reset_for_tests()
    yield
    events.reset_for_tests()
