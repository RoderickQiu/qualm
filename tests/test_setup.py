"""Setup: where things live, the server command, the model choice, first-run choices; `doctor` reports in words."""

import tomllib
from pathlib import Path

import pytest

from qualm import autostart, localmodel, paths
from qualm import setup as s
from qualm.doctor import FAIL, OK, WARN, report
from qualm.localmodel import Memory, server_command

SRC = Path(__file__).resolve().parents[1] / "src" / "qualm"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("QUALM_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(autostart, "install", lambda quiet=False: None)
    monkeypatch.setattr(autostart, "installed", lambda: False)
    return tmp_path / "home"


def test_the_server_is_8_bit_by_default_and_bf16_on_request(home):
    argv, env, cwd = server_command()
    assert env["KEV_QUANT_BITS"] == "8" and argv[-4:] == ["--run", "jaredpalmer/kev-4b", "--port", "8009"]
    assert argv[:2] == [str(home / "kev-env" / "bin" / "python"), str(SRC / "kevserve.py")]
    assert env["QUALM_MODEL_CACHE"] == str(home / "models")  # the 8-bit weights, saved once
    assert "KEV_QUANT_BITS" not in server_command(bits=16)[1]


def test_a_kev_checkout_runs_with_its_own_project(home, tmp_path):
    (tmp_path / "kev" / "kev").mkdir(parents=True)
    (tmp_path / "kev" / "kev" / "serve.py").write_text("")
    argv, _, cwd = server_command(kev_dir=tmp_path / "kev")
    assert argv[1:5] == ["run", "--extra", "serve", "python"] and cwd == tmp_path / "kev"
    with pytest.raises(RuntimeError, match="doesn't look like the Kev repo"):
        server_command(kev_dir=tmp_path)


def test_rules_and_data_live_in_home_unless_a_checkout_has_not_moved_yet(home, tmp_path):
    assert paths.rules_file() == home / "rules.toml" and paths.data_dir() == home / "data"
    (tmp_path / "rules.toml").write_text("")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "judgements.jsonl").write_text("{}\n")
    assert paths.legacy() and paths.rules_file() == Path("rules.toml")
    assert s.migrate() == ["rules.toml", "data/"]
    assert not paths.legacy() and paths.rules_file() == home / "rules.toml"
    assert (home / "data" / "judgements.jsonl").read_text() == "{}\n"
    assert (tmp_path / "rules.toml").exists()  # copied, never moved
    assert s.migrate() == []  # home has rules: never overwritten


def test_memory_counts_swap_as_in_use():
    assert Memory(24, 12, 0, 0).fits
    busy = Memory(24, 34, 21, 0)
    assert not busy.fits and busy.spare_gb == 0 and "Not enough" in busy.summary()
    assert "Tight" in Memory(16, 9, 0, 0).summary()  # 7 GB spare: it fits, without headroom


def test_recommend_hosted_without_room(monkeypatch):
    monkeypatch.setattr(localmodel, "apple_silicon", lambda: True)
    monkeypatch.setattr(localmodel, "memory", lambda: Memory(24, 30, 10, 0))
    assert s.recommend()[0] == "jev"
    monkeypatch.setattr(localmodel, "memory", lambda: Memory(32, 12, 0, 0))
    assert s.recommend()[0] == "kev"
    monkeypatch.setattr(localmodel, "apple_silicon", lambda: False)
    assert s.recommend()[0] == "jev"


def test_apply_writes_the_choices(home, monkeypatch):
    monkeypatch.setattr(s.keychain, "store", lambda key: None)
    monkeypatch.setattr(s.keychain, "api_key", lambda: "k")
    done = s.apply(s.Choices("jev", ["shortvideo", "feeds"], login=True, key="k"))
    cfg = tomllib.loads((home / "rules.toml").read_text())
    assert cfg["settings"]["backend"] == "jev"
    on = {r["id"]: r.get("enabled", True) for r in cfg["rules"]}
    assert on == {"shortvideo": True, "feeds": True, "livestream": False, "videos": False, "social": False}
    assert "starts at login" in done
    s.apply(s.Choices("kev", ["social"], login=False))  # again: back to local, other rules
    cfg = tomllib.loads((home / "rules.toml").read_text())
    assert "backend" not in cfg["settings"] and {r["id"] for r in cfg["rules"] if r.get("enabled", True)} == {"social"}


def test_apply_never_writes_a_checkouts_rules(home, tmp_path):
    (tmp_path / "rules.toml").write_text("mine")  # a checkout not copied home yet
    with pytest.raises(ValueError, match="copies it home first"):
        s.apply(s.Choices("kev", ["social"], login=False))
    assert (tmp_path / "rules.toml").read_text() == "mine"


def test_hosted_needs_a_key(home, monkeypatch):
    monkeypatch.setattr(s.keychain, "api_key", lambda: None)
    with pytest.raises(ValueError, match="API key"):
        s.apply(s.Choices("jev", []))


def test_doctor_report():
    rows = [{"check": "A", "status": OK, "found": "fine", "fix": ""},
            {"check": "B", "status": WARN, "found": "meh", "fix": "do this"}]
    assert report(rows).splitlines() == ["✓ A: fine", "! B: meh", "    do this", "",
                                         "Working; the notes above would make it better."]
    assert report(rows + [{"check": "C", "status": FAIL, "found": "no", "fix": "that"}]).endswith("1 to fix before Qualm can work.")
