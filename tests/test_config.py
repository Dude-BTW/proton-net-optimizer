from __future__ import annotations

import pytest

from pno import config as config_module
from pno import paths
from pno.config import Config
from pno.errors import ConfigError


def test_defaults_are_valid():
    config = Config().validate()
    assert config.top_n == 10
    assert config.home_country == "UA"
    assert config.language == "en"


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    config = Config(top_n=7, auto_mirror=False, language="uk")
    config_module.save(config, path)
    loaded = config_module.load(path)
    assert loaded.top_n == 7
    assert loaded.auto_mirror is False
    assert loaded.language == "uk"


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('top_n = 3\nfuture_key = "x"\n', encoding="utf-8")
    assert config_module.load(path).top_n == 3


def test_invalid_values_are_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("top_n = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        config_module.load(path)


def test_mirror_count_cannot_exceed_the_pool():
    with pytest.raises(ConfigError):
        Config(mirror_count=30, mirror_global_candidates=10).validate()


def test_ensure_creates_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PNO_CONFIG_DIR", str(tmp_path / "cfg"))
    config = config_module.ensure()
    assert paths.config_file().is_file()
    assert config.top_n == 10


def test_env_paths_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PNO_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("PNO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PNO_RUNTIME_DIR", str(tmp_path / "run"))
    assert paths.config_file().parent == tmp_path / "cfg"
    assert paths.state_file().parent == tmp_path / "state"
    assert paths.socket_path().parent == tmp_path / "run"


def test_default_language_is_english(tmp_path, monkeypatch):
    monkeypatch.setenv("PNO_CONFIG_DIR", str(tmp_path / "cfg"))
    assert config_module.ensure().language == "en"


def test_language_change_is_persisted(tmp_path):
    path = tmp_path / "config.toml"
    config = Config(language="en")
    config_module.save(config, path)
    assert config_module.load(path).language == "en"
    config.language = "uk"
    config_module.save(config, path)
    assert config_module.load(path).language == "uk"
