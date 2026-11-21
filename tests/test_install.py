from __future__ import annotations

from pno.install import (
    _confirm,
    _sudoers_content,
    _template,
    _write_if_changed,
    repo_root,
)


def test_repo_root_contains_pyproject():
    assert (repo_root() / "pyproject.toml").is_file()


def test_unit_template_formats():
    text = _template("pno-daemon.service").format(
        python="/venv/bin/python", repo_root="/repo"
    )
    assert "ExecStart=/venv/bin/python -m pno daemon run" in text
    assert "WorkingDirectory=/repo" in text
    assert "Restart=on-failure" in text


def test_tray_autostart_template_formats():
    text = _template("pno-tray.desktop").format(python="/venv/bin/python")
    assert "Exec=/venv/bin/python -m pno tray" in text
    assert "Type=Application" in text


def test_sudoers_rule_is_narrow():
    text = _sudoers_content("alice", "/etc/pacman.d/mirrorlist")
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert len(lines) == 2
    assert all("NOPASSWD:" in line for line in lines)
    assert all("--path /etc/pacman.d/mirrorlist" in line for line in lines)
    assert all("alice ALL=(root)" in line for line in lines)
    assert any(line.endswith("--pacman-sync") for line in lines)


def test_write_if_changed_is_idempotent(tmp_path):
    path = tmp_path / "unit.service"
    assert _write_if_changed(path, "one\n") is True
    assert _write_if_changed(path, "one\n") is False
    assert path.read_text(encoding="utf-8") == "one\n"


def test_confirm_non_interactive(monkeypatch):
    assert _confirm("q", assume_yes=True) is True
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert _confirm("q", assume_yes=False) is False


def test_desktop_template_formats():
    text = _template("proton-net-optimizer.desktop").format(python="/venv/bin/python")
    assert "Exec=/venv/bin/python -m pno launch" in text
    assert "Icon=proton-net-optimizer" in text
    assert "Categories=Network;" in text


def test_icon_template_is_svg():
    text = _template("proton-net-optimizer.svg")
    assert text.lstrip().startswith("<?xml")
    assert "<svg" in text


def test_install_desktop_files_are_idempotent(tmp_path, monkeypatch):
    import pno.install as install_module

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(install_module, "_refresh_desktop_caches", lambda: [])
    first = install_module._install_desktop("/venv/bin/python")
    second = install_module._install_desktop("/venv/bin/python")
    assert first["desktop_changed"] is True
    assert first["icon_changed"] is True
    assert second["desktop_changed"] is False
    assert second["icon_changed"] is False
    assert install_module.desktop_path().is_file()
    assert install_module.icon_path().is_file()
    assert "pno launch" in install_module.desktop_path().read_text(encoding="utf-8")
