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


def test_sync_database_thresholds_must_be_positive():
    assert Config().validate().pacman_db_max_age_hours == 24.0
    assert Config().validate().pacman_db_max_skew_hours == 6.0
    assert Config().validate().mirror_max_sync_skew_hours == 6.0
    with pytest.raises(ConfigError):
        Config(pacman_db_max_age_hours=0).validate()
    with pytest.raises(ConfigError):
        Config(pacman_db_max_skew_hours=-1).validate()
    with pytest.raises(ConfigError):
        Config(mirror_max_sync_skew_hours=0).validate()


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


def test_killswitch_cleanup_defaults_and_validation():
    config = Config().validate()
    assert config.auto_clean_stale_killswitch is True
    assert config.killswitch_cleanup_delay == 10.0
    with pytest.raises(ConfigError):
        Config(killswitch_cleanup_delay=0).validate()


def test_killswitch_cleanup_can_be_disabled(tmp_path):
    path = tmp_path / "config.toml"
    config_module.save(
        Config(auto_clean_stale_killswitch=False, killswitch_cleanup_delay=30),
        path,
    )
    loaded = config_module.load(path)
    assert loaded.auto_clean_stale_killswitch is False
    assert loaded.killswitch_cleanup_delay == 30.0


def test_log_defaults():
    config = Config().validate()
    assert config.log_max_bytes == 20 * 1024 * 1024
    assert config.log_keep_files == 10
    assert config.log_max_age_days == 30


def test_log_limits_validation():
    with pytest.raises(ConfigError):
        Config(log_max_bytes=0).validate()
    with pytest.raises(ConfigError):
        Config(log_keep_files=-1).validate()
    with pytest.raises(ConfigError):
        Config(log_max_age_days=-1).validate()
