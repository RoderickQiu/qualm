"""`qualm uninstall --all`: everything of Qualm's goes, shared downloads stay, nothing without asking."""

import argparse
import types

import pytest

from qualm import autostart, cli, keychain, paths


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A pretend Mac: home, logs, agents, the shim and the HF cache all under tmp_path."""
    home = tmp_path / "Application Support" / "Qualm"
    (home / "data").mkdir(parents=True)
    (home / "rules.toml").write_text("")
    logs = tmp_path / "Logs" / "Qualm"
    logs.mkdir(parents=True)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.qualm.app.plist").write_text("")
    (agents / "com.other.app.plist").write_text("")
    shim = tmp_path / "bin" / "qualm"
    shim.parent.mkdir()
    shim.write_text('#!/bin/sh\nexec "/Applications/Qualm.app/Contents/MacOS/Qualm" -m qualm "$@"\n')
    hub = tmp_path / "hub"
    (hub / "blobs").mkdir(parents=True)  # the shared store, as huggingface_hub lays it out
    (hub / "blobs" / "a").write_bytes(b"x" * 1000)
    snap = hub / "models--jaredpalmer--kev-4b" / "snapshots" / "rev"
    snap.mkdir(parents=True)
    for name in ("one", "two"):  # two links to one file count once
        (snap / name).symlink_to(hub / "blobs" / "a")
    key = {"k": "secret"}
    monkeypatch.setenv("QUALM_HOME", str(home))
    monkeypatch.setattr(paths, "custom_home", lambda: False)  # this pretend home is the main install
    monkeypatch.setattr(paths, "LOGS", logs)
    monkeypatch.setattr(autostart, "AGENTS", agents)
    monkeypatch.setattr(autostart, "SHIM", shim)
    monkeypatch.setattr(autostart, "HF_HUB", hub)
    monkeypatch.setattr(autostart, "_bootout", lambda path: None)  # no launchctl
    monkeypatch.setattr(keychain, "stored", lambda: key["k"])
    monkeypatch.setattr(keychain, "forget", lambda: key.update(k=None) or True)
    return types.SimpleNamespace(home=home, logs=logs, agents=agents, shim=shim, hub=hub, key=key)


def run(monkeypatch, capsys, **flags):
    monkeypatch.setattr("qualm.review.app_running", lambda data_dir, settings=None: None)
    args = argparse.Namespace(**{"all": True, "yes": False, "dry_run": False, **flags})
    cli.cmd_uninstall(args)
    return capsys.readouterr().out


def test_dry_run_lists_and_removes_nothing(mac, monkeypatch, capsys):
    out = run(monkeypatch, capsys, dry_run=True)
    assert "the login item" in out and "keychain" in out and str(mac.home) in out and str(mac.shim) in out
    assert "model/jaredpalmer/kev-4b" in out and "hf cache rm model/jaredpalmer/kev-4b" in out
    assert mac.home.exists() and mac.shim.exists() and mac.key["k"] == "secret"


def test_asks_for_yes(mac, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert "Nothing removed" in run(monkeypatch, capsys)
    assert mac.home.exists()


def test_all_goes_but_shared_downloads_and_other_agents_stay(mac, monkeypatch, capsys):
    out = run(monkeypatch, capsys, yes=True)
    assert not mac.home.exists() and not mac.logs.exists() and not mac.shim.exists()
    assert not (mac.agents / "com.qualm.app.plist").exists() and (mac.agents / "com.other.app.plist").exists()
    assert mac.key["k"] is None
    assert (mac.hub / "models--jaredpalmer--kev-4b").exists() and "Left:" in out


def test_shared_downloads_are_sized_once(mac):
    assert autostart.left_behind() == [("model/jaredpalmer/kev-4b", 1000)]


def test_someone_elses_qualm_command_is_left(mac, monkeypatch, capsys):
    mac.shim.write_text("#!/bin/sh\nexec uv tool run qualm\n")
    run(monkeypatch, capsys, yes=True)
    assert mac.shim.exists()


def test_refuses_while_the_app_runs(mac, monkeypatch, capsys):
    monkeypatch.setattr("qualm.review.app_running", lambda data_dir, settings=None: {"app": "qualm", "watching": True})
    args = argparse.Namespace(all=True, yes=True, dry_run=False)
    with pytest.raises(SystemExit, match="quit it first"):
        cli.cmd_uninstall(args)
    assert mac.home.exists()


def test_plain_uninstall_only_removes_the_login_item(mac, monkeypatch, capsys):
    cli.cmd_uninstall(argparse.Namespace(all=False, yes=False, dry_run=False))
    assert not (mac.agents / "com.qualm.app.plist").exists() and mac.home.exists() and mac.key["k"] == "secret"


def test_a_dry_run_without_all_removes_nothing(mac, monkeypatch, capsys):
    booted = []
    monkeypatch.setattr(autostart, "_bootout", booted.append)
    cli.cmd_uninstall(argparse.Namespace(all=False, yes=False, dry_run=True))
    assert "would remove" in capsys.readouterr().out
    assert (mac.agents / "com.qualm.app.plist").exists() and booted == []


def test_a_qualm_home_elsewhere_removes_only_itself(mac, monkeypatch, capsys):
    """QUALM_HOME points at a second folder: the main install's login item, key, command and logs stay."""
    monkeypatch.setattr(paths, "custom_home", lambda: True)
    asked = []
    monkeypatch.setattr(keychain, "stored", lambda: asked.append("keychain") or "secret")
    out = run(monkeypatch, capsys, dry_run=True)
    assert "QUALM_HOME is set" in out and str(mac.home) in out
    assert "login item:" not in out and str(mac.shim) not in out and str(mac.logs) not in out and asked == []
    run(monkeypatch, capsys, yes=True)
    assert not mac.home.exists() and mac.shim.exists() and mac.logs.exists() and mac.key["k"] == "secret"
    assert (mac.agents / "com.qualm.app.plist").exists()
    cli.cmd_uninstall(argparse.Namespace(all=False, yes=False, dry_run=False))  # the login item isn't this folder's
    assert (mac.agents / "com.qualm.app.plist").exists() and "left alone" in capsys.readouterr().out


def test_migrate_env_key(monkeypatch):
    saved = {}
    monkeypatch.setattr(keychain, "load_env", lambda: None)
    monkeypatch.setattr(keychain, "store", lambda k: saved.update(k=k))
    monkeypatch.setenv("TYPESAFE_API_KEY", "apikey_x")
    monkeypatch.setattr(keychain, "stored", lambda: None)
    assert keychain.migrate_env_key() and saved == {"k": "apikey_x"}
    monkeypatch.setattr(keychain, "stored", lambda: "apikey_x")
    assert not keychain.migrate_env_key()
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setattr(keychain, "stored", lambda: None)
    assert not keychain.migrate_env_key()
