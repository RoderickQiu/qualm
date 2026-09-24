"""rules.toml as a file: changes at the same moment, saved versions and
undo, broken files, value types, sites, hours, and logs with a torn line.
No model, no screen."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tomllib
from datetime import datetime

import pytest

from qualm import config as config_module
from qualm.config import EXAMPLE, Config
from qualm.rules import Rule, _parse_when, in_window, load_config, parse_config, site_pattern


@pytest.fixture
def cfg(tmp_path):
    shutil.copyfile(EXAMPLE, tmp_path / "rules.toml")
    return Config(tmp_path / "rules.toml")


def cli(tmp_path, *args, background=False):
    cmd = [sys.executable, "-m", "qualm.cli", *args, "--rules", str(tmp_path / "rules.toml"), "--data", str(tmp_path / "d")]
    env = dict(os.environ, KEV_URL="http://127.0.0.1:9", QUALM_HOME=str(tmp_path / "home"))
    if background:
        return subprocess.Popen(cmd, env=env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return subprocess.run(cmd, env=env, cwd=tmp_path, capture_output=True, text=True)


def saved(tmp_path):
    """Every file Qualm keeps for rules.toml, as bytes."""
    return {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*")
            if p.is_file() and "rules.toml" in p.name and not p.name.endswith(".lock")}


# -- changes at the same moment ----------------------------------------------


def test_commands_run_at_once_all_land(tmp_path):
    """An agent runs independent commands in parallel: every one that says ok
    is saved, on a new home too, and nothing else is lost."""
    procs = [cli(tmp_path, "rules", "add", f"new{i}", "--what", f"thing {i}", "--off", "--json", background=True)
             for i in range(5)]
    procs += [cli(tmp_path, "rules", "set", "social", f"sites+=site{i}.com", "--json", background=True) for i in range(3)]
    assert [p.wait() for p in procs] == [0] * 8
    data = tomllib.loads((tmp_path / "rules.toml").read_text())
    ids = {r["id"] for r in data["rules"]}
    assert ids >= {f"new{i}" for i in range(5)} | {"shortvideo", "feeds", "livestream", "videos", "social"}
    assert data["settings"]["no_monitor"] and len(data["allow"]) == 3
    social = next(r for r in data["rules"] if r["id"] == "social")
    assert {f"site{i}.com" for i in range(3)} <= set(social["sites"])


def test_threads_in_one_process_wait_for_each_other(cfg):
    """The menu and the dashboard both edit from the app's process."""
    ids = ["feeds", "social", "videos", "livestream"]

    def edit(rule_id):
        for k in range(8):
            Config(cfg.path).edit("rules", rule_id, {"note": f"{rule_id} {k}"})

    threads = [threading.Thread(target=edit, args=(i,)) for i in ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert {r.id: r.note for r in cfg.load()[1] if r.id in ids} == {i: f"{i} 7" for i in ids}


def test_a_change_waits_for_another_and_says_so_if_it_never_ends(cfg, monkeypatch):
    holder = subprocess.Popen([sys.executable, "-c", "import fcntl, os, sys, time\n"
                               f"fd = os.open({str(cfg.path) + '.lock'!r}, os.O_RDWR | os.O_CREAT)\n"
                               "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(3)"],
                              stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    monkeypatch.setattr(config_module, "LOCK_WAIT_S", 0.3)
    before = cfg.text()
    with pytest.raises(ValueError, match="try again"):
        cfg.edit("rules", "social", {"threshold": 0.4})
    assert cfg.text() == before
    holder.kill()
    holder.wait()
    cfg.edit("rules", "social", {"threshold": 0.4})
    assert "threshold = 0.4" in cfg.text()


def test_a_refused_or_failed_save_changes_nothing(cfg, monkeypatch):
    cfg.edit("rules", "social", {"threshold": 0.4})
    before = saved(cfg.path.parent)
    with pytest.raises(ValueError, match="aren't text"):  # what a Latin-1 terminal's "café" becomes
        cfg.edit("rules", "social", {"note": "caf\udce9"})
    with pytest.raises(ValueError):
        cfg.edit("rules", "social", {"threshold": 3.0})
    monkeypatch.setattr(config_module.os, "replace", lambda *a: (_ for _ in ()).throw(OSError(28, "No space left")))
    with pytest.raises(ValueError, match="nothing was changed"):
        cfg.edit("rules", "social", {"threshold": 0.45})
    assert saved(cfg.path.parent) == before
    assert not [p for p in cfg.path.parent.rglob("*.tmp")]


def test_a_change_that_changes_nothing_writes_nothing(cfg):
    cfg.edit("rules", "social", {"threshold": 0.4})
    before, mtime = saved(cfg.path.parent), cfg.path.stat().st_mtime_ns
    cfg.edit("rules", "social", {"threshold": 0.4})
    cfg.edit("rules", "social", {}, ["enabled"])  # on, and it was on
    cfg.edit_settings({"max_wait_s": 60})
    assert cfg.apply(cfg.export()) == []
    assert saved(cfg.path.parent) == before and cfg.path.stat().st_mtime_ns == mtime


# -- saved versions and undo --------------------------------------------------


def test_undo_steps_back_one_saved_change_at_a_time(cfg, monkeypatch):
    monkeypatch.setattr(config_module, "KEEP", 3)
    for t in (0.31, 0.32, 0.33, 0.34):
        cfg.edit("rules", "social", {"threshold": t})
    assert len(list((cfg.path.parent / "backups").glob("rules.toml.[0-9]*"))) == 3  # only the last KEEP
    threshold = lambda: next(r for r in cfg.load()[1] if r.id == "social").threshold
    done = cfg.undo()
    assert threshold() == 0.33 and done["left"] == 2 and "-threshold = 0.34" in done["diff"]
    assert datetime.fromisoformat(done["restored"])
    # rules.toml.bak is still the version before the latest change.
    assert "threshold = 0.32" in cfg.path.with_name("rules.toml.bak").read_text()
    cfg.undo()
    cfg.undo()
    assert threshold() == 0.31
    with pytest.raises(KeyError, match="nothing to undo"):
        cfg.undo()
    assert not cfg.path.with_name("rules.toml.bak").exists()


def test_undo_after_a_hand_edit_goes_back_to_the_last_save_and_no_further(cfg):
    """A broken file's message says undo goes back to the version Qualm last
    saved: it does, and the change saved before the hand edit stays."""
    threshold = lambda: next(r for r in cfg.load()[1] if r.id == "social").threshold
    cfg.edit("rules", "social", {"threshold": 0.4})
    cfg.path.write_text(cfg.path.read_text().replace("max_wait_s = 60", "max_wait_s = 60\nmax_wiat_s = 90", 1))
    with pytest.raises(ValueError, match="go back to the version Qualm last saved"):
        load_config(cfg.path)
    done = cfg.undo()
    assert done["undid"] == "hand_edit" and done["left"] == 1 and "-max_wiat_s = 90" in done["diff"]
    assert threshold() == 0.4
    # A hand edit that loads is undone the same way; then undo steps back through the saved changes.
    cfg.path.write_text(cfg.path.read_text().replace("threshold = 0.4", "threshold = 0.5", 1))
    assert cfg.undo()["undid"] == "hand_edit" and threshold() == 0.4
    done = cfg.undo()
    assert done["undid"] == "saved_change" and done["left"] == 0 and threshold() == 0.3
    with pytest.raises(KeyError, match="nothing to undo"):
        cfg.undo()


def test_undo_after_breaking_a_new_home_s_starter_rules(tmp_path):
    assert cli(tmp_path, "rules", "list").returncode == 0  # created from the starters
    starters = (tmp_path / "rules.toml").read_text()
    (tmp_path / "rules.toml").write_text(starters.replace('kind = "deny"', 'kind = "block"', 1))
    out = cli(tmp_path, "config", "undo")
    assert out.returncode == 0, out.stderr
    assert "by hand" in out.stdout and "last saved" in out.stdout and "0 older version(s)" in out.stdout
    assert (tmp_path / "rules.toml").read_text() == starters
    help = " ".join(cli(tmp_path, "config", "undo", "--help").stdout.split())
    assert "changed by hand since Qualm last saved it" in help and "otherwise step back one saved change" in help
    # Deleted to start again from the starters: undo still brings back what was saved there.
    assert cli(tmp_path, "rules", "set", "social", "threshold=0.4").returncode == 0
    (tmp_path / "rules.toml").unlink()
    assert cli(tmp_path, "rules", "list").returncode == 0 and (tmp_path / "rules.toml").read_text() == starters
    assert cli(tmp_path, "config", "undo").returncode == 0
    assert next(r for r in load_config(tmp_path / "rules.toml")[1] if r.id == "social").threshold == 0.4


def test_undo_passes_over_a_kept_version_that_no_longer_loads(cfg):
    """An older Qualm saved `exceptions = [""]`; the checks refuse it now. Undo
    says so, without sending you back to undo, and takes the one before."""
    starters = cfg.path.read_text()
    broken = starters.replace('id = "social"\n', 'id = "social"\nexceptions = [""]\n', 1)
    cfg._bak().write_text(broken)
    with pytest.raises(KeyError) as e:
        cfg.undo()
    assert "nothing to undo that loads" in str(e.value) and "exceptions has an empty item" in str(e.value)
    assert "qualm config undo" not in str(e.value) and cfg.path.read_text() == starters
    cfg._bak().unlink()
    for t in (0.31, 0.32, 0.33):
        cfg.edit("rules", "social", {"threshold": t})
    newest = cfg._versions()[-1]  # threshold 0.32
    newest.write_text(newest.read_text().replace('id = "social"\n', 'id = "social"\nexceptions = [""]\n', 1))
    done = cfg.undo()
    assert "threshold = 0.31" in cfg.path.read_text() and done["left"] == 1
    assert len(done["skipped"]) == 1 and str(newest) in done["skipped"][0]


def test_a_bak_from_before_backups_is_kept_as_the_oldest_version(cfg):
    """An upgrade from a Qualm that kept only rules.toml.bak: the first new
    change doesn't overwrite it, and undo gets back to it."""
    original = cfg.path.read_bytes()
    older = original.replace(b"threshold = 0.3\n", b"threshold = 0.25\n", 1)
    cfg._bak().write_bytes(older)
    for t in (0.35, 0.36, 0.37):
        cfg.edit("rules", "social", {"threshold": t})
    versions = cfg._versions()
    assert versions[0].read_bytes() == older and versions[1].read_bytes() == original
    assert [cfg.undo()["left"] for _ in range(4)] == [3, 2, 1, 0]
    assert cfg.path.read_bytes() == older and not cfg._bak().exists()
    with pytest.raises(KeyError, match="nothing to undo"):
        cfg.undo()


def test_removing_the_last_entry_leaves_a_file_that_loads(tmp_path):
    (tmp_path / "rules.toml").write_text('[[rules]]\nid = "x"\nkind = "deny"\ndescription = "gambling sites"\n')
    out = cli(tmp_path, "rules", "remove", "x", "--json")
    assert out.returncode == 0, out.stdout + out.stderr
    assert load_config(tmp_path / "rules.toml")[1] == []
    assert cli(tmp_path, "rules", "add", "y", "--what", "news", "--json").returncode == 0


def test_undo_says_what_it_covers_and_what_it_put_back(tmp_path):
    assert cli(tmp_path, "rules", "set", "feeds", "threshold=0.6").returncode == 0
    assert cli(tmp_path, "never", "add", "--app", "com.apple.dt.Xcode", "--name", "Xcode").returncode == 0
    out = json.loads(cli(tmp_path, "config", "undo", "--json").stdout)
    # A never-here place isn't in rules.toml: the diff shows which change was undone.
    assert out["undone"] and "-threshold = 0.6" in out["diff"] and out["left"] == 0
    help = " ".join(cli(tmp_path, "config", "undo", "--help").stdout.split())
    assert "Only rules.toml" in help and "never-here" in help


def older_qualms_home(tmp_path) -> tuple[str, str]:
    """What a Qualm from before backups/ leaves: rules.toml as it last saved it (here with the social rule at 0.4)
    and rules.toml.bak, the version before that save. No backups/, no record of the last save."""
    before = EXAMPLE.read_text()
    last = before.replace("threshold = 0.3\n", "threshold = 0.4\n", 1)
    (tmp_path / "rules.toml").write_text(last)
    (tmp_path / "rules.toml.bak").write_text(before)
    return before, last


def test_a_home_from_an_older_qualm_keeps_its_last_save_through_an_undo(tmp_path):
    """The first command records rules.toml as Qualm's last save: undo after a broken hand edit drops the edit,
    not the change that older Qualm saved last, and keeps the file it replaced."""
    before, last = older_qualms_home(tmp_path)
    assert cli(tmp_path, "config", "check").returncode == 0  # any command, the app's start included
    assert (tmp_path / "backups" / "rules.toml.saved").read_text() == last
    edited = last.replace("max_wait_s = 60", "max_wait_s = 60\nmax_wiat_s = 90\n# my long notes", 1)
    (tmp_path / "rules.toml").write_text(edited)
    out = cli(tmp_path, "config", "check")
    assert out.returncode == 2 and "go back to the version Qualm last saved" in out.stderr
    dry = json.loads(cli(tmp_path, "config", "undo", "--dry-run", "--json").stdout)
    assert dry["undid"] == "hand_edit" and dry["left"] == 1 and (tmp_path / "rules.toml").read_text() == edited
    out = json.loads(cli(tmp_path, "config", "undo", "--json").stdout)
    assert out["undid"] == "hand_edit" and out["left"] == 1 and "note" not in out
    assert (tmp_path / "rules.toml").read_text() == last  # the last saved change is still there
    replaced = tmp_path / "backups" / "rules.toml.replaced"
    assert out["replaced_file"] == str(replaced) and replaced.read_text() == edited  # the hand edit isn't lost either
    assert json.loads(cli(tmp_path, "config", "undo", "--json").stdout)["undid"] == "saved_change"
    assert (tmp_path / "rules.toml").read_text() == before


def test_a_first_undo_that_can_t_know_the_last_save_says_so_and_keeps_it(tmp_path):
    """Broken before Qualm ever saw it load: nothing tells the hand edit from the last save, so the error and
    the undo say it goes back to the version before that save, and the file it replaces is kept."""
    before, last = older_qualms_home(tmp_path)
    edited = last.replace("max_wait_s = 60", "max_wait_s = 60\nmax_wiat_s = 90", 1)
    (tmp_path / "rules.toml").write_text(edited)
    out = cli(tmp_path, "rules", "list")
    assert out.returncode == 2 and "go back to the version before the last change Qualm saved" in out.stderr
    assert "last saved." not in out.stderr and not (tmp_path / "backups").exists()
    out = cli(tmp_path, "config", "undo")
    assert out.returncode == 0 and (tmp_path / "rules.toml").read_text() == before
    assert "Note: Qualm kept no copy of the version it last saved" in out.stdout and "rules.toml.replaced" in out.stdout
    assert (tmp_path / "backups" / "rules.toml.replaced").read_text() == edited  # the last save, with the typo


def test_undo_sets_aside_a_version_it_passes_over_and_its_dry_run_says_so(cfg):
    """A kept version that no longer loads can be corrected later: undo keeps it aside, and the dry run
    shows what undo will do and what it will pass over."""
    for t in (0.31, 0.32, 0.33):
        cfg.edit("rules", "social", {"threshold": t})
    newest = cfg._versions()[-1]  # threshold 0.32
    broken = newest.read_text().replace('id = "social"\n', 'id = "social"\nexceptions = [""]\n', 1)
    newest.write_text(broken)
    out = cli(cfg.path.parent, "config", "undo", "--dry-run", "--json")
    dry = json.loads(out.stdout)
    aside = newest.with_name(newest.name + ".aside")
    assert dry["dry_run"] and dry["undid"] == "saved_change" and dry["left"] == 1 and "-threshold = 0.33" in dry["diff"]
    assert str(newest) in dry["skipped"][0] and dry["kept_aside"] == [str(aside)] and not aside.exists()
    text = cli(cfg.path.parent, "config", "undo", "--dry-run").stdout
    assert f"would pass over (it can't be put back; it would be kept aside as {aside})" in text
    assert "would go back to the newest kept version that loads" in text and "(dry run: nothing saved)" in text
    done = cfg.undo()
    assert "threshold = 0.31" in cfg.path.read_text() and done["kept_aside"] == [str(aside)]
    assert aside.read_text() == broken and aside not in cfg._versions() and done["left"] == 1
    # Put back by hand once corrected, it loads.
    shutil.copyfile(aside, cfg.path)
    cfg.path.write_text(cfg.path.read_text().replace('exceptions = [""]\n', "", 1))
    assert next(r for r in load_config(cfg.path)[1] if r.id == "social").threshold == 0.32


def test_a_dry_run_on_a_new_home_starts_from_the_starter_rules(tmp_path):
    out = cli(tmp_path, "rules", "add", "fb", "--what", "the Facebook news feed", "--dry-run", "--json")
    assert out.returncode == 0, out.stdout + out.stderr
    diff = json.loads(out.stdout)["diff"]
    assert "the starter rules" in diff and '+id = "fb"' in diff
    assert not [line for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")]
    assert not (tmp_path / "rules.toml").exists()


# -- values of the right type, files that don't load ---------------------------


@pytest.mark.parametrize("bad, field", [
    ('[settings]\nno_monitor = "com.tinyspeck.slackmacgap"\n', "no_monitor must be a list of text"),
    ('[settings]\nblock_clicks = "false"\n', "block_clicks must be true or false"),
    ('[settings]\nmax_wait_s = 1.5\n', "max_wait_s must be a whole number"),
    ('[settings]\nallow_urls = ["[x"]\n', "allow_urls: '[x' isn't a regular expression"),
    ('[settings]\nallow_urls = ["*.example.com"]\n', "isn't a regular expression"),
    ('[[rules]]\nid = "a"\nkind = "deny"\ndescription = "x"\nenabled = "false"\n', "enabled must be true or false"),
    ('[[rules]]\nid = "a"\nkind = "deny"\ndescription = "x"\nthreshold = "0.15"\n', "threshold must be a number"),
    ('[[rules]]\nid = "a"\nkind = "deny"\ndescription = "x"\nexceptions = "a news article"\n', "exceptions must be a list"),
    ('[[rules]]\nid = "a"\nkind = "deny"\ndescription = "x"\nsites = "douyin.com"\n', 'like sites = ["douyin.com"]'),
    ('[[rules]]\nid = "a"\nkind = "deny"\ndescription = "x"\nwhen = "weekends"\n', 'like when = ["weekends"]'),
    ('[[rules]]\nid = "a"\nkind = "deny"\ndescription = "x"\nexceptions = [""]\n', "exceptions has an empty item"),
    ('[[rules]]\nid = "a"\nkind = "deny"\ndescription = "x"\npatterns = ["(unclosed"]\n', "isn't a regular expression"),
    ('[[rules]]\nid = "a"\nkind = "deny"\ndescription = "   "\n', "needs a description"),
    ('[[rules]]\nid = "a"\ndescription = "x"\n', "needs a kind"),
    ('rules = ["x"]\n', "starts with a [[rules]] line"),
    ("settings = 5\n", "settings must be a table"),
    ("[lint]\nmax_line = 100\n", "unknown 'lint'"),
])
def test_values_of_the_wrong_type_are_refused_by_name(bad, field):
    with pytest.raises(ValueError, match=re.escape(field)):
        parse_config(bad)


def test_config_apply_refuses_wrong_types(cfg):
    before = cfg.text()
    for desired in ({"settings": {"no_monitor": "com.tinyspeck.slackmacgap"}}, {"settings": {"block_clicks": "false"}},
                    {"rules": [{"id": "newr", "kind": "deny", "description": "gambling", "enabled": "false"}]},
                    {"settings": {"no_monitor": {"a": 1}}}, {"rules": {"id": "x"}}, ["not", "an", "object"]):
        with pytest.raises(ValueError):
            cfg.apply(desired)
    assert cfg.text() == before


def test_a_broken_file_names_itself_the_line_and_the_fix(tmp_path):
    path = tmp_path / "rules.toml"
    starters = EXAMPLE.read_text()
    path.write_text(starters.replace('kind = "deny"\ntarget = "page"', 'target = "page"', 1))
    with pytest.raises(ValueError) as e:
        load_config(path)
    line = starters.splitlines().index('id = "feeds"')  # the feeds block's header is the line above its id
    assert str(path) in str(e.value) and f"(line {line})" in str(e.value) and "Correct it in the file." in str(e.value)
    assert "config undo" not in str(e.value)  # nothing kept to go back to: no dead end
    path.with_name("rules.toml.bak").write_text(starters)  # an older Qualm's: the version before its last save
    with pytest.raises(ValueError) as e:
        load_config(path)
    assert "qualm config undo` to go back to the version before the last change Qualm saved." in str(e.value)
    path.write_text(starters.replace('id = "feeds"', 'id = "feeds\x7f"', 1))  # a DEL no editor shows
    with pytest.raises(ValueError, match=r"a control character \('\\x7f', invisible in most editors\) at line \d+, column \d+"):
        load_config(path)
    for text in ('kind = deny\n', "", "# only a comment\n"):
        path.write_text(text)
        with pytest.raises(ValueError, match=re.escape(str(path))):
            load_config(path)
    path.write_bytes(starters.encode("latin-1", "replace").replace(b"?", b"\xe9"))
    with pytest.raises(ValueError, match="isn't UTF-8"):
        load_config(path)
    # An editor's byte-order mark and Windows line ends are fine, and edits keep working.
    path.write_bytes(b"\xef\xbb\xbf" + starters.replace("\n", "\r\n").encode())
    assert len(load_config(path)[1]) == 5
    Config(path).edit("rules", "social", {"threshold": 0.4})
    assert next(r for r in load_config(path)[1] if r.id == "social").threshold == 0.4


def test_commands_on_a_broken_file_say_so_without_a_traceback(tmp_path):
    shutil.copyfile(EXAMPLE, tmp_path / "rules.toml")
    text = (tmp_path / "rules.toml").read_text()
    (tmp_path / "rules.toml").write_text(re.sub(r"threshold = ([0-9.]+)", r'threshold = "\1"', text, count=1))
    for args in (("rules", "list"), ("config", "check"), ("rules", "on", "social")):
        out = cli(tmp_path, *args, "--json")
        assert out.returncode == 2 and "Traceback" not in out.stderr
        assert "threshold must be a number" in json.loads(out.stdout)["error"]["message"]


def test_apply_keeps_what_the_json_leaves_out(cfg):
    no_monitor = cfg.load()[0].no_monitor
    changes = cfg.apply({"settings": {"max_wait_s": 30},
                         "rules": [{"id": "shortvideo", "kind": "deny", "description": "short videos"}]})
    settings, rules = cfg.load()
    shortvideo = next(r for r in rules if r.id == "shortvideo")
    assert settings.max_wait_s == 30 and settings.no_monitor == no_monitor and shortvideo.sites
    assert {c["op"] for c in changes} == {"settings", "change"}
    cfg.apply({"settings": {"extensions": None}})  # null: back to the default
    assert "extensions =" not in cfg.text()
    cfg.apply({"settings": {"max_wait_s": 30}}, prune=True)  # prune: only what it names is left
    assert cfg.load()[0].no_monitor == () and cfg.load()[0].max_wait_s == 30


def test_only_a_qualm_checkout_rules_toml_is_used_from_the_current_folder(tmp_path):
    (tmp_path / "rules.toml").write_text("# my linter config\n[lint]\nmax_line = 100\n")
    home = tmp_path / "home"
    env = dict(os.environ, KEV_URL="http://127.0.0.1:9", QUALM_HOME=str(home))
    out = subprocess.run([sys.executable, "-m", "qualm.cli", "rules", "add", "gambling", "--what", "gambling sites"],
                         env=env, cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0
    assert (tmp_path / "rules.toml").read_text() == "# my linter config\n[lint]\nmax_line = 100\n"
    assert "gambling" in (home / "rules.toml").read_text()


# -- what an edit keeps --------------------------------------------------------


def test_edits_keep_a_changed_line_s_comment(tmp_path):
    from qualm.review import set_threshold

    path = tmp_path / "rules.toml"
    original = EXAMPLE.read_text() + ('\n[[rules]]\nid = "news"\nkind = "deny"\ndescription = "news # my words"\n'
                                      'threshold = 0.30  # tuned on 09-20, keep\nenabled = true    # on for now\n'
                                      'sites = ["cnn.com"]')  # and no newline at the end
    path.write_text(original)
    c = Config(path)
    c.edit("rules", "news", {"enabled": False})
    assert "enabled = false    # on for now\n" in path.read_text()
    c.edit("rules", "news", {}, ["enabled"])
    assert path.read_text() == original
    set_threshold(path, "news", 0.25)
    assert "threshold = 0.25  # tuned on 09-20, keep\n" in path.read_text() and 'description = "news # my words"' in path.read_text()


def test_changing_a_starter_list_keeps_the_comments_about_it(cfg):
    # The starters had comments inside their arrays (what the Facebook Watch regex skips),
    # and `rules set feeds patterns+=...` rewrote the array without them.
    comments = [line for line in cfg.path.read_text().splitlines() if line.lstrip().startswith("#")]
    for table, id in (("rules", "feeds"), ("rules", "livestream"), ("rules", "shortvideo"), ("allow", "music")):
        raw = cfg.raw(table, id)
        cfg.edit(table, id, {k: raw[k][:-1] for k in ("sites", "patterns", "apps") if raw.get(k)})
    cfg.edit_settings({"no_monitor": ["com.apple.keychainaccess"], "allow_sites": ["github.com"]})
    assert [line for line in cfg.path.read_text().splitlines() if line.lstrip().startswith("#")] == comments


# -- sites and hours ------------------------------------------------------------


def test_sites_ignore_letter_case_and_take_any_script():
    for site, url in (("reddit.com/r/AskReddit", "https://www.reddit.com/r/AskReddit/"),
                      ("youtube.com/@MrBeast", "https://www.youtube.com/@MrBeast/videos"),
                      ("YouTube.com/Shorts", "https://m.youtube.com/shorts/abc")):
        assert Rule("t", "deny", "x", sites=(site,)).matches_url(url)
    assert not Rule("t", "deny", "x", sites=("reddit.com/r/AskReddit",)).matches_url("https://www.reddit.com/r/AskRedditor")
    chinese = Rule("t", "deny", "x", sites=("例子.中国",))
    assert chinese.matches_url("https://xn--fsqu00a.xn--fiqs8s/") and chinese.matches_url("https://例子.中国/a")
    assert re.search(site_pattern("bücher.de"), "https://www.xn--bcher-kva.de/x")
    assert re.search(site_pattern("xn--fsqu00a.xn--fiqs8s"), "https://xn--fsqu00a.xn--fiqs8s/")
    assert re.search(site_pattern("例子。中国"), "https://xn--fsqu00a.xn--fiqs8s/")
    # Browsers keep ß and ς (IDNA 2008); Python's codec (IDNA 2003) maps them away. Both forms count.
    for site, urls in (("straße.de", ("https://xn--strae-oqa.de/", "https://strasse.de/", "https://straße.de/")),
                       ("ελληνικός.gr", ("https://xn--qxaegecap6byf.gr/", "https://xn--qxaegecap4c9d.gr/"))):
        assert all(Rule("t", "deny", "x", sites=(site,)).matches_url(url) for url in urls)
    for bad in ("not a site", "a..com", "192.168.1.1"):
        with pytest.raises(ValueError, match="write a domain"):
            site_pattern(bad)


def test_when_takes_several_hours_after_the_days():
    fri = lambda h, m=0: datetime(2026, 9, 25, h, m)
    for spec in ("mon-fri 09:00-12:00, 13:00-17:00", "mon-fri 09:00-12:00 13:00-17:00"):
        assert _parse_when(spec) == (frozenset(range(5)), ((540, 720), (780, 1020)))
        assert in_window((spec,), fri(10)) and in_window((spec,), fri(14)) and not in_window((spec,), fri(12, 30))
    assert _parse_when("sat, sun")[0] == {5, 6} and _parse_when("09:00-18:00 mon-fri")[1] == ((540, 1080),)
    for bad in ("mon 09:00-12:00, tue 13:00-18:00", "09:00-09:00", "00:00-24:30"):
        with pytest.raises(ValueError):
            _parse_when(bad)


# -- logs with a torn line --------------------------------------------------------


def test_a_torn_line_in_a_log_is_skipped(tmp_path, capsys):
    from qualm.agent import wrong_popups
    from qualm.policy import Policy
    from qualm.review import exceptions, insights, load_judgements, load_reviews
    from qualm.trial import load_trials

    good = {"id": "abc12345", "at": "2026-09-23T10:00:00", "screen": {"app": "Safari"}, "decisions": []}
    torn = {"judgements.jsonl": json.dumps(good) + "\n" + '{"id": "abc12346", "at": "2026-09-23T10:00:05", "scr',
            "exceptions.jsonl": '{"rule": "social", "ur\n' + json.dumps({"never": {"app": "com.x"}}) + "\n",
            "decisions.jsonl": '{"at": "2026-09-23T10:00:00", "type": "interv\n',
            "reviews.jsonl": '{"id": "abc1\n', "trials.jsonl": '{"rule": "x", "q"\n', "reflections.jsonl": '{"week": \n'}
    for name, text in torn.items():
        (tmp_path / name).write_text(text)
    assert [j["id"] for j in load_judgements(tmp_path)] == ["abc12345"]
    assert exceptions(tmp_path) == [{"never": {"app": "com.x"}}]
    assert load_reviews(tmp_path) == {} and load_trials(tmp_path, "x", "q", "kev") == {} and wrong_popups(tmp_path) == []
    settings, rules = load_config(EXAMPLE)
    insights(tmp_path, rules)
    assert Policy(settings, rules, tmp_path).precheck("com.x", "") is not None  # the good line still counts
    assert "judgements.jsonl" in capsys.readouterr().err


def test_a_record_added_after_a_torn_line_starts_a_line_of_its_own(tmp_path):
    from qualm import jsonl
    from qualm.policy import Policy

    data = tmp_path / "d"
    data.mkdir()
    (data / "exceptions.jsonl").write_text('{"never": {"app": "com.example.one"}}\n{"never": {"app": "com.exa')
    out = cli(tmp_path, "never", "add", "--app", "com.apple.dt.Xcode", "--name", "Xcode", "--json")
    assert out.returncode == 0, out.stdout + out.stderr
    settings, rules = load_config(EXAMPLE)
    Policy(settings, rules, data).never_here("com.figma.Desktop", "Figma", "")
    apps = [e["never"]["app"] for e in jsonl.read(data / "exceptions.jsonl")]
    assert apps == ["com.example.one", "com.apple.dt.Xcode", "com.figma.Desktop"]
    assert "\n\n" not in (data / "exceptions.jsonl").read_text()  # a whole last line gets no blank one after it
