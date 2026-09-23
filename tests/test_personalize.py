"""Rules made and changed from the command line: the file edits, sites,
schedules, on/off, and removing what was learned. No model."""

import json
import shutil
import subprocess
import sys
from datetime import datetime

import pytest

from seenot_desktop.config import Config, apply_assignments
from seenot_desktop.decide import Reading, RuleVerdict
from seenot_desktop.policy import Policy
from seenot_desktop.rules import Rule, Settings, in_window, site_pattern


@pytest.fixture
def cfg(tmp_path):
    shutil.copyfile("rules.example.toml", tmp_path / "rules.toml")
    return Config(tmp_path / "rules.toml")


def rule(cfg, id):
    return next(r for r in cfg.load()[1] if r.id == id)


def test_sites_match_the_site_its_subdomains_and_paths_only():
    import re

    home = site_pattern("youtube.com/")
    shorts = site_pattern("https://www.youtube.com/shorts")
    douyin = site_pattern("douyin.com")
    assert re.search(home, "https://www.youtube.com/") and re.search(home, "https://youtube.com/?bp=x")
    assert not re.search(home, "https://www.youtube.com/watch?v=1")
    assert re.search(shorts, "https://m.youtube.com/shorts/abc") and not re.search(shorts, "https://youtube.com/shortsville")
    assert re.search(douyin, "https://www.douyin.com/video/1")
    assert not re.search(douyin, "https://www.google.com/search?q=douyin.com")
    assert not re.search(douyin, "https://notdouyin.com/")
    with pytest.raises(ValueError):
        site_pattern("not a site")


def test_when_windows():
    mon_10 = datetime(2026, 9, 21, 10, 0)  # a Monday
    sat_01 = datetime(2026, 9, 26, 1, 0)
    assert in_window((), mon_10)
    assert in_window(("mon-fri 09:00-18:00",), mon_10) and not in_window(("weekends",), mon_10)
    assert in_window(("fri 22:00-02:00",), sat_01) and not in_window(("sat 22:00-02:00",), sat_01)
    assert in_window(("sun-tue",), mon_10)  # ranges wrap round the week


def test_edit_keeps_everything_else(cfg):
    before = cfg.text()
    cfg.edit("rules", "social", {"threshold": 0.4, "sites": ["weibo.com"]})
    after = cfg.text()
    assert rule(cfg, "social").threshold == 0.4 and rule(cfg, "social").sites == ("weibo.com",)
    # Comments and other rules untouched; the old file kept.
    assert after.count("#") == before.count("#")
    assert rule(cfg, "livestream").patterns[3].startswith("twitch")
    assert cfg.path.with_name("rules.toml.bak").read_text() == before


def test_multiline_values_are_replaced_whole(cfg):
    cfg.edit("rules", "feeds", {"sites": ["youtube.com/"]})
    assert rule(cfg, "feeds").sites == ("youtube.com/",)
    cfg.edit("rules", "livestream", unset=["patterns"])
    assert rule(cfg, "livestream").patterns == ()


def test_a_broken_edit_is_never_saved(cfg):
    before = cfg.text()
    for bad in ({"threshold": 3.0}, {"when": ["someday"]}, {"kind": "block"}, {"sites": ["no spaces allowed"]}):
        with pytest.raises(ValueError):
            cfg.edit("rules", "social", bad)
    with pytest.raises(ValueError):
        cfg.edit("rules", "social", {"colour": "red"})
    assert cfg.text() == before


def test_add_remove_and_settings(cfg):
    cfg.add("rules", {"id": "news", "kind": "deny", "description": "news", "when": ["weekdays"]})
    assert rule(cfg, "news").when == ("weekdays",)
    with pytest.raises(ValueError):
        cfg.add("rules", {"id": "news", "kind": "deny", "description": "again"})
    cfg.remove("rules", "feeds")
    assert "feeds" not in {r.id for r in cfg.load()[1]}
    cfg.edit_settings({"budgets": True, "allow_sites": ["github.com", "gitlab.com"]})
    s = cfg.load()[0]
    assert s.budgets and s.allowed_url("https://gitlab.com/x") and not s.allowed_url("https://example.com")


def test_emptied_list_drops_the_key(cfg):
    cfg.edit("rules", "videos", {"exceptions": ["a lecture"]})
    cfg.edit("rules", "videos", {"exceptions": []})
    assert "exceptions = " not in cfg.text()


def test_assignments():
    cur = {"sites": ["a.com"], "threshold": 0.2}
    assert apply_assignments(Rule, cur, ["sites+=b.com", "threshold=0.3", "note="]) == (
        {"sites": ["a.com", "b.com"], "threshold": 0.3}, ["note"])
    assert apply_assignments(Rule, cur, ["sites-=a.com", "enabled=off"])[0] == {"sites": [], "enabled": False}
    with pytest.raises(ValueError):
        apply_assignments(Rule, cur, ["threshold+=1"])


def reading(rules, **p_hit):
    return Reading(sensitive=0.0, page_kind="single_item", page_probs={}, purpose="entertain", purpose_probs={},
                   latency_ms=0.0, rules=[RuleVerdict(r.id, "", p_hit.get(r.id, 0.0), {}) for r in rules])


def test_off_and_out_of_hours_rules_are_neither_asked_nor_fire(tmp_path):
    rules = [Rule("a", "deny", "a", enabled=False), Rule("b", "deny", "b", when=("00:00-00:01",)), Rule("c", "deny", "c")]
    p = Policy(Settings(), rules, tmp_path)
    assert [r.id for r in p.rules] == ["c"] or datetime.now().strftime("%H:%M") == "00:00"
    got = p.decide({"url": "https://x.com/1", "visible_text": ["x"]}, reading(rules, a=0.9, b=0.9, c=0.9))
    assert [d.rule for d in got] == ["c"]


def test_a_removed_exception_is_forgotten(tmp_path):
    rules = [Rule("a", "deny", "a")]
    p = Policy(Settings(), rules, tmp_path)
    p.mark_fine("a", "https://x.com/1", "A talk")
    p.add_exception_text("a", "lectures")
    assert p.rules[0].exceptions == ('the page "A talk"', "lectures")
    with (tmp_path / "exceptions.jsonl").open("a") as f:
        f.write(json.dumps({"rule": "a", "title": "A talk", "url": "https://x.com/1", "removed": True}) + "\n")
    p.reload_exceptions()
    assert p.rules[0].exceptions == ("lectures",)
    got = p.decide({"url": "https://x.com/1", "visible_text": ["x"]}, reading(rules, a=0.9))
    assert got[0].action == "intervene"


def test_cli_round_trip(tmp_path):
    """The commands an agent would run, end to end, with --json."""
    def run(*args):
        out = subprocess.run([sys.executable, "-m", "seenot_desktop.cli", *args, "--rules", str(tmp_path / "r.toml"),
                              "--data", str(tmp_path / "d")], capture_output=True, text=True)
        return out

    assert run("rules", "add", "news", "--what", "news sites", "--minutes", "15", "--site", "nytimes.com").returncode == 0
    assert run("rules", "set", "news", "when+=weekdays", "threshold=0.3").returncode == 0
    assert run("rules", "off", "shortvideo").returncode == 0
    rows = json.loads(run("rules", "list", "--json").stdout)["rules"]
    news = next(r for r in rows if r["id"] == "news")
    assert news["kind"] == "time_cap" and news["when"] == ["weekdays"] and news["threshold"] == 0.3
    assert not next(r for r in rows if r["id"] == "shortvideo")["enabled"]
    bad = run("rules", "set", "news", "when=someday")
    assert bad.returncode == 2 and "someday" in bad.stderr
    err = json.loads(run("rules", "show", "nope", "--json").stdout)["error"]
    assert err["code"] == "not_found" and "news" in err["message"]
    assert run("rules", "show", "nope").returncode == 3

    # A dry run shows the diff and saves nothing.
    before = (tmp_path / "r.toml").read_text()
    dry = json.loads(run("rules", "set", "news", "threshold=0.5", "--dry-run", "--json").stdout)
    assert dry["dry_run"] and "+threshold = 0.5" in dry["diff"] and (tmp_path / "r.toml").read_text() == before
    dry = json.loads(run("never", "add", "--site", "example.com", "--dry-run", "--json").stdout)
    assert dry["appends"] and not (tmp_path / "d" / "exceptions.jsonl").exists()

    # The whole config round-trips through export / apply, all or nothing.
    cfg = json.loads(run("config", "export").stdout)
    cfg["rules"] = [r for r in cfg["rules"] if r["id"] != "videos"]
    cfg["rules"].append({"id": "games", "kind": "deny", "description": "video games"})
    next(r for r in cfg["rules"] if r["id"] == "social")["minutes_per_day"] = 10
    (tmp_path / "want.json").write_text(json.dumps(cfg))
    out = json.loads(run("config", "apply", str(tmp_path / "want.json"), "--json").stdout)
    # Without --prune, a rule left out of the list is kept.
    assert {(c["op"], c["id"]) for c in out["changes"]} == {("add", "games"), ("change", "social")}
    out = json.loads(run("config", "apply", str(tmp_path / "want.json"), "--prune", "--json").stdout)
    assert {(c["op"], c["id"]) for c in out["changes"]} == {("remove", "videos")}
    assert json.loads(run("config", "export").stdout)["rules"] == cfg["rules"]
    # Undo brings the previous file back.
    assert run("config", "undo").returncode == 0
    assert "videos" in {r["id"] for r in json.loads(run("config", "export").stdout)["rules"]}


def test_question_limit(tmp_path):
    """Rules on at the same moment + allow classes + 3 shared questions <= max_questions,
    checked over the whole week, and a change past it is refused whole."""
    from seenot_desktop.rules import OverLimit, capacity, parse_config

    def cfg(n, when=None, limit=10):
        rules = "".join(f'[[rules]]\nid = "r{i}"\nkind = "deny"\ndescription = "x"\n'
                        + (f'when = ["{when[i % len(when)]}"]\n' if when else "") for i in range(n))
        return f"[settings]\nmax_questions = {limit}\n\n" + rules

    assert capacity(*parse_config(cfg(7)))["peak"] == 10
    with pytest.raises(OverLimit):
        parse_config(cfg(8))
    # Rules at different times don't add up.
    s, rules = parse_config(cfg(14, when=["mon-fri 09:00-18:00", "mon-fri 18:00-23:00"]))
    assert capacity(s, rules)["peak"] == 10
    with pytest.raises(OverLimit):
        parse_config(cfg(14, when=["mon-fri 09:00-18:00", "mon-fri 17:00-23:00"]))

    path = tmp_path / "r.toml"
    path.write_text(cfg(7))
    c = Config(path)
    with pytest.raises(OverLimit):
        c.add("rules", {"id": "extra", "kind": "deny", "description": "x"})
    with pytest.raises(OverLimit):
        c.apply({"rules": [{"id": f"n{i}", "kind": "deny", "description": "x"} for i in range(9)]}, prune=True)
    with pytest.raises(OverLimit):
        c.apply({"rules": [{"id": "n0", "kind": "deny", "description": "x"}]})
    assert path.read_text() == cfg(7)
    # Switching one off makes room.
    with c.batch():
        c.edit("rules", "r0", {"enabled": False})
        c.add("rules", {"id": "extra", "kind": "deny", "description": "x"})
    assert capacity(*c.load())["peak"] == 10
