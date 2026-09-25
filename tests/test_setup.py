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
    """A pretend main install in tmp_path: no login item, keychain or `qualm` command of the real Mac is touched."""
    monkeypatch.setenv("QUALM_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "custom_home", lambda: False)
    monkeypatch.setattr(autostart, "AGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(autostart, "SHIM", tmp_path / "bin" / "qualm")
    monkeypatch.setattr(autostart, "install", lambda quiet=False: None)
    monkeypatch.setattr(autostart, "installed", lambda: False)
    monkeypatch.setattr(localmodel, "apple_silicon", lambda: True)
    return tmp_path / "home"


def test_the_server_is_8_bit_by_default_and_bf16_on_request(home, monkeypatch):
    monkeypatch.setattr(localmodel, "can_build", lambda: False)
    argv, env, cwd = server_command()
    assert env["KEV_QUANT_BITS"] == "8" and argv[-4:] == ["--run", localmodel.MODEL, "--port", "8009"]
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


CHECKOUT = '[project]\nname = "qualm-desktop"\n'


def test_rules_and_data_live_in_home_unless_a_checkout_has_not_moved_yet(home, tmp_path):
    assert paths.rules_file() == home / "rules.toml" and paths.data_dir() == home / "data"
    (tmp_path / "rules.toml").write_text("")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "judgements.jsonl").write_text("{}\n")
    # Any other folder's rules.toml (a linter's, say) isn't ours: never read, edited or copied.
    assert not paths.legacy() and paths.rules_file() == home / "rules.toml" and s.migrate() == []
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-linter"\n')
    assert not paths.legacy() and s.migrate() == []
    (tmp_path / "pyproject.toml").write_text(CHECKOUT)
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
    monkeypatch.setattr(localmodel.platform, "mac_ver", lambda: ("15.1", ("", "", ""), "arm64"))
    monkeypatch.setattr(localmodel, "disk_short", lambda: None)
    monkeypatch.setattr(localmodel, "memory", lambda: Memory(24, 30, 10, 0))
    assert s.recommend()[0] == "jev"
    monkeypatch.setattr(localmodel, "memory", lambda: Memory(32, 12, 0, 0))
    assert s.recommend()[0] == "kev"
    short = "not enough disk space for the model: needs 7 GB free, 2.0 GB left"
    monkeypatch.setattr(localmodel, "disk_short", lambda: short)
    assert s.recommend() == ("jev", f"{Memory(32, 12, 0, 0).summary()} But there's {short}.")
    monkeypatch.setattr(localmodel, "disk_short", lambda: None)
    monkeypatch.setattr(localmodel.platform, "mac_ver", lambda: ("13.6.1", ("", "", ""), "arm64"))
    rec, why = s.recommend()  # MLX has no build for macOS 13: the runtime wouldn't install
    assert rec == "jev" and "macOS 14" in why
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
    (tmp_path / "pyproject.toml").write_text(CHECKOUT)
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


def test_doctor_says_what_to_do_first():
    rows = [{"check": "Setup", "status": FAIL, "found": "not set up yet", "fix": "Run `qualm setup`."},
            {"check": "Qualm app", "status": WARN, "found": "not running", "fix": "Run `qualm app`."}]
    assert report(rows).endswith("Not set up yet. Run `qualm setup`.")
    assert report(rows[1:]).endswith("Nothing is watching yet. Run `qualm app`.")
    assert Memory(8, 5, 1, 0).too_small and "whatever you close" in Memory(8, 5, 1, 0).summary()
    assert not Memory(16, 12, 0, 0).too_small


@pytest.fixture
def mac(home, monkeypatch):
    """`home`, plus a login item, keychain and Accessibility that only record what setup does to them."""
    import types

    from qualm import keychain, review

    rec = types.SimpleNamespace(installed=False, calls=[], key=None)

    def install(quiet=False):
        rec.installed = True
        rec.calls.append("install")

    def uninstall(quiet=False):
        rec.installed = False
        rec.calls.append("uninstall")

    monkeypatch.setattr(autostart, "install", install)
    monkeypatch.setattr(autostart, "uninstall", uninstall)
    monkeypatch.setattr(autostart, "installed", lambda: rec.installed)
    monkeypatch.setattr(autostart, "login_home", lambda: paths.home() if rec.installed else None)
    monkeypatch.setattr(autostart, "up_to_date", lambda: False)
    monkeypatch.setattr(keychain, "stored", lambda: rec.key)
    monkeypatch.setattr(keychain, "api_key", lambda: rec.key)
    monkeypatch.setattr(keychain, "store", lambda k: setattr(rec, "key", k))
    monkeypatch.setattr(s, "check_key", lambda k: None if k.startswith("good") else "401 unauthorized")
    monkeypatch.setattr(s, "accessibility", lambda: True)
    monkeypatch.setattr(review, "app_running", lambda data_dir, settings=None: None)
    monkeypatch.setattr(localmodel, "memory", lambda: Memory(16, 12, 0, 0))  # tight: hosted is recommended
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return rec


def setup_cli(capsys, *argv, stdin=""):
    """`qualm setup ...` in-process, as a script or an agent runs it (no terminal): (exit code, output)."""
    import io
    import sys

    from qualm import cli

    old, sys.stdin = sys.stdin, io.StringIO(stdin)
    try:
        cli.main(["setup", *argv])
        code = 0
    except SystemExit as e:
        code = e.code
    finally:
        sys.stdin = old
    out = capsys.readouterr()
    return code, out.out + out.err


def a_terminal(monkeypatch, *answers, key=""):
    """Setup in a terminal, typing `answers` in turn (and `key` at the key prompt)."""
    import builtins
    import getpass
    import io
    import sys

    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": key)
    tty = io.StringIO()
    tty.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", tty)


def test_setup_is_needed_until_setup_ran_not_until_a_rules_file_exists(home):
    import shutil

    from qualm.config import EXAMPLE

    assert s.needed()
    home.mkdir()
    shutil.copyfile(EXAMPLE, home / "rules.toml")  # what the first `qualm status` or `rules list` does
    assert s.needed()  # the app still shows its setup window
    s.apply(s.Choices("kev", ["social"], login=False))
    assert not s.needed() and (home / s.DONE).exists()
    # A home set up before the mark existed: its logs, or a model chosen, say so.
    (home / s.DONE).unlink()
    (home / "data").mkdir()
    (home / "data" / "judgements.jsonl").write_text("{}\n")
    assert not s.needed()
    (home / "data" / "judgements.jsonl").unlink()
    (home / "rules.toml").write_text((home / "rules.toml").read_text().replace("[settings]", '[settings]\nbackend = "jev"', 1))
    assert not s.needed()


def test_a_focus_session_before_setup_is_not_setup(home, monkeypatch):
    from qualm.policy import start_focus

    start_focus(home / "data", "write", 5)  # `qualm focus` writes decisions.jsonl and session.json
    assert (home / "data" / "decisions.jsonl").exists() and s.needed()


def test_a_home_the_earlier_version_set_up_is_not_a_first_run(mac, capsys):
    import shutil

    from qualm.config import EXAMPLE

    # What setup before the .setup-done mark left, on kev with the app never started: rules.toml saved over
    # the starter copy (so rules.toml.bak beside it, no backups/), no backend line, no logs.
    home = paths.home()
    home.mkdir()
    for name in ("rules.toml", "rules.toml.bak"):
        shutil.copyfile(EXAMPLE, home / name)
    assert not s.needed()
    mac.key = "good-key"  # with a key at hand and tight memory, a first run would pick hosted
    code, out = setup_cli(capsys, "--rules", "shortvideo,feeds")
    assert code == 0 and "using kev, as you chose before" in out and "the model runs on this Mac" in out
    assert "backend" not in tomllib.loads((home / "rules.toml").read_text())["settings"]


def test_a_home_the_earlier_version_set_up_stays_set_up_after_its_first_change(mac, capsys):
    """Its first change with this version makes backups/, which hid the .bak that said it was set up: the
    setup mark is written then, so the setup window, doctor and a scripted re-run don't take it for a first run."""
    import shutil

    from qualm import cli
    from qualm.config import EXAMPLE

    home = paths.home()
    home.mkdir()
    for name in ("rules.toml", "rules.toml.bak"):
        shutil.copyfile(EXAMPLE, home / name)
    cli.main(["rules", "add", "mytest", "--what", "cooking videos"])
    assert (home / "backups").is_dir() and (home / s.DONE).exists() and not s.needed()
    mac.key = "good-key"
    code, out = setup_cli(capsys, "--rules", "shortvideo,feeds")
    assert code == 0 and "using kev, as you chose before" in out
    assert "backend" not in tomllib.loads((home / "rules.toml").read_text())["settings"]


def test_a_new_home_is_still_a_first_run_after_a_command_or_a_change(home):
    """The mark goes only next to an older Qualm's .bak: a fresh home's own .bak (from its first change) isn't one."""
    from qualm import cli

    cli.main(["rules", "list"])
    cli.main(["rules", "set", "social", "threshold=0.4"])
    assert (home / "rules.toml.bak").exists() and not (home / s.DONE).exists() and s.needed()


def test_a_rerun_keeps_the_saved_model(mac, capsys):
    mac.key = "good-key"  # with a key at hand, nothing stopped the old switch to hosted
    assert setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo")[0] == 0
    code, out = setup_cli(capsys, "--rules", "shortvideo,feeds")  # memory is tight: hosted is recommended
    cfg = tomllib.loads((paths.home() / "rules.toml").read_text())
    assert code == 0 and "backend" not in cfg["settings"] and "as you chose before" in out
    assert "jev is the one for this Mac right now" in out  # the advice is still given


def test_a_first_run_without_a_terminal_takes_the_recommendation_and_says_so(mac, capsys):
    mac.key = "good-key"
    code, out = setup_cli(capsys, "--rules", "shortvideo")
    assert code == 0 and "using jev, the one for this Mac" in out
    assert tomllib.loads((paths.home() / "rules.toml").read_text())["settings"]["backend"] == "jev"


def test_without_a_terminal_start_at_login_changes_only_when_asked(mac, capsys):
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo")
    assert code == 0 and mac.calls == [] and " app`" in out.splitlines()[-1]
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo", "--login")
    assert mac.calls == ["install"] and "Don't also run" in out  # the login item starts it: no second copy
    setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo")  # a re-run leaves it on
    assert mac.calls == ["install"] and mac.installed
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo", "--no-login")
    assert mac.calls == ["install", "uninstall"]
    assert out.index("Removing the login item") < out.index("won't start at login")  # said before it's done


def test_setup_reads_rule_lists_as_people_type_them(mac, capsys):
    import json

    from qualm.config import Config

    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo, feeds")
    assert code == 0 and "rules on: shortvideo, feeds" in out
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo,chat,stocks", "--json")
    err = json.loads(out)["error"]
    assert code == 2 and "chat, stocks" in err["message"] and "shortvideo, feeds, livestream, videos, social" in err["message"]
    # A starter removed from the file comes back when asked for, and is only reported on if it is.
    Config(paths.home() / "rules.toml").remove("rules", "videos")
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo,videos")
    assert "added videos back" in out and "rules on: shortvideo, videos" in out


def test_setup_json_for_scripts(mac, capsys):
    import json

    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "social", "--login", "--dry-run", "--json")
    assert code == 0 and json.loads(out) == {"dry_run": True, "backend": "kev", "rules": ["social"], "login": True,
                                             "key": None, "skill": True, "notes": []}
    assert not paths.home().exists() and mac.calls == []  # nothing changed
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "social", "--json")
    d = json.loads(out)
    assert code == 0 and d["backend"] == "kev" and d["rules"] == ["social"] and d["login"] is None
    assert "rules on: social" in d["done"] and d["next"].startswith("Done.")


def test_the_first_setup_gives_claude_code_the_skill_and_a_rerun_leaves_it_be(mac, capsys, claude_config_dir):
    import json

    from qualm import agent

    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "social")
    assert code == 0 and f"✓ Claude Code knows Qualm: its skill is in {claude_config_dir / 'skills' / 'qualm'}" in out
    assert agent.skill_state() == "current"
    agent.remove_skill()  # you took it away: setup run again doesn't bring it back
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "social", "--dry-run", "--json")
    assert json.loads(out)["skill"] is False
    assert setup_cli(capsys, "--backend", "kev", "--rules", "social")[0] == 0 and not agent.skill_file().exists()


def test_no_claude_code_no_skill(home, claude_config_dir):
    from qualm import agent

    claude_config_dir.rmdir()
    c = s.Choices("kev", ["social"], login=False, skill=True)
    s.apply(c)
    assert not claude_config_dir.exists() and not any("Claude Code" in line for line in c.done)
    assert agent.skill_state() == "none"


def test_setup_asks_again_after_a_wrong_answer(mac, capsys, monkeypatch):
    from qualm import cli

    a_terminal(monkeypatch, "foo", "local", "n", "none", "maybe", "n")  # "local" means kev
    cli.main(["setup"])
    out = capsys.readouterr().out
    assert "answer kev (on this Mac) or jev" in out and "not a starter rule: n" in out and "answer y or n" in out
    assert "rules on: none" in out and "backend" not in tomllib.loads((paths.home() / "rules.toml").read_text())["settings"]


def test_no_key_is_never_taken_for_one_in_the_keychain(mac, capsys, monkeypatch):
    from qualm import cli

    code, out = setup_cli(capsys, "--backend", "jev", "--rules", "shortvideo")
    assert code == 2 and "--key -" in out and "TYPESAFE_API_KEY" in out and "--backend kev" in out
    a_terminal(monkeypatch, "jev", "kev", "", "")  # Enter at the key prompt, then the local model after all
    cli.main(["setup"])
    out = capsys.readouterr().out
    assert "no key given" in out and "using the key in your keychain" not in out and "on this Mac (Kev-4B" in out


def test_the_key_can_come_on_stdin(mac, capsys):
    code, out = setup_cli(capsys, "--backend", "jev", "--key", "-", "--rules", "shortvideo", stdin="good-from-stdin\n")
    assert code == 0 and mac.key == "good-from-stdin" and "hosted by TypeSafe" in out


def test_the_local_model_is_refused_without_apple_silicon(mac, capsys, monkeypatch):
    monkeypatch.setattr(localmodel, "apple_silicon", lambda: False)
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo")
    assert code == 2 and "Apple silicon" in out and not (paths.home() / "rules.toml").exists()
    with pytest.raises(ValueError, match="Apple silicon"):
        s.apply(s.Choices("kev", ["social"], login=False))


@pytest.mark.parametrize("app", ["/Volumes/Qualm/Qualm.app", "/private/var/folders/x/T/AppTranslocation/1A2B/d/Qualm.app"])
def test_login_item_and_command_are_refused_from_a_disk_image(mac, monkeypatch, app):
    monkeypatch.setattr(paths, "bundle", lambda: Path(app))
    with pytest.raises(ValueError, match="move Qualm to Applications first"):
        s.apply(s.Choices("kev", ["social"], login=True, shim=False))
    with pytest.raises(ValueError, match="move Qualm to Applications first"):
        s.apply(s.Choices("kev", ["social"], login=False, shim=True))
    assert mac.calls == [] and not autostart.SHIM.exists() and not (paths.home() / "rules.toml").exists()


def test_the_command_says_when_its_folder_isnt_on_path(mac, monkeypatch):
    monkeypatch.setattr(paths, "bundle", lambda: Path("/Applications/Qualm.app"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")  # a stock shell's
    c = s.Choices("kev", ["social"], login=False)
    s.apply(c)
    assert autostart.SHIM.exists() and any("isn't on your PATH" in n and "export PATH=" in n for n in c.notes)
    monkeypatch.setenv("PATH", f"{autostart.SHIM.parent}:/usr/bin:/bin")
    c = s.Choices("kev", ["social"], login=False)
    s.apply(c)
    assert not any("PATH" in n for n in c.notes)


def test_a_qualm_home_elsewhere_leaves_the_main_install_alone(mac, monkeypatch, tmp_path):
    mac.installed = True  # the main install's login item
    monkeypatch.setattr(paths, "custom_home", lambda: True)
    monkeypatch.setattr(autostart, "login_home", lambda: tmp_path / "main")
    monkeypatch.setattr(paths, "bundle", lambda: Path("/Applications/Qualm.app"))
    for login in (None, False):
        c = s.Choices("kev", ["social"], login=login)
        s.apply(c)
        assert mac.calls == [] and mac.installed and not autostart.SHIM.exists()
        assert any("left alone" in n for n in c.notes)
    # --login would take the main install's login item over (and quit the Qualm it started): refused.
    (paths.home() / "rules.toml").unlink()
    with pytest.raises(ValueError, match=f"starts Qualm on {tmp_path / 'main'}.* install`, run with this QUALM_HOME"):
        s.apply(s.Choices("kev", ["social"], login=True))
    assert mac.calls == [] and not (paths.home() / "rules.toml").exists()  # nothing changed


def test_setup_refuses_the_local_model_where_it_cant_run(mac, capsys, monkeypatch):
    monkeypatch.setattr(localmodel.platform, "mac_ver", lambda: ("13.6.1", ("", "", ""), "arm64"))  # MLX has no build
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo", "--dry-run")
    assert code == 2 and "needs macOS 14 or later" in out and "Would set up" not in out
    with pytest.raises(ValueError, match="macOS 14"):
        s.apply(s.Choices("kev", ["shortvideo"], login=False))


def test_the_first_download_note_is_about_the_pinned_copy(mac, capsys, monkeypatch):
    from qualm import review

    old = paths.models_dir() / "0123456789ab-Qwen--Qwen3.5-4B-Base-q8g64"  # an earlier pin's copy: not the one started
    old.mkdir(parents=True)
    (old / "model.safetensors").write_bytes(b"")
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo")
    assert code == 0 and "The first start downloads about 6 GB, once; `" in out  # the runtime (1 GB) too
    monkeypatch.setattr(localmodel, "runtime_ready", lambda: True)
    monkeypatch.setattr(review, "app_running", lambda data_dir, settings=None: {"pid": 1})
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo")  # the app is downloading it already
    assert code == 0 and "downloads about 5 GB, once: the app is at it now" in out and "serve` does it" not in out
    localmodel.saved_copy().mkdir(parents=True)
    (localmodel.saved_copy() / "model.safetensors").write_bytes(b"")
    code, out = setup_cli(capsys, "--backend", "kev", "--rules", "shortvideo")
    assert code == 0 and "downloads about" not in out
