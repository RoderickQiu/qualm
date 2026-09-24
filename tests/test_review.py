"""The dashboard's data: answers, what counts as a pop-up, the pages the
server sends, exceptions, a rules file that doesn't load, the status chip,
a tab left open across an update, and `qualm week`. Made-up logs, no model,
no screen."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest

from qualm import review
from qualm.config import EXAMPLE
from qualm.policy import Decision, Policy
from qualm.rules import Settings, load_config

NOW = datetime.now().replace(microsecond=0)
P_HIT = {"shortvideo": 0.05, "feeds": 0.05, "livestream": 0.05, "videos": 0.05, "social": 0.05}


def judgement(jid, url, rule=None, p=0.85, ago=timedelta(minutes=5), title="", state="what the model read"):
    at = (NOW - ago).isoformat(timespec="seconds")
    decisions = [{"action": "intervene", "rule": rule, "reason": f"p_hit {p:.2f} >= 0.30"}] if rule else []
    return {"id": jid, "at": at, "screen": {"app": "Safari", "bundle_id": "com.apple.Safari", "window_title": title or url,
                                           "url": url, "text": ["the whole capture"], "ax_trusted": True},
            "decisions": decisions, "thresholds": {k: 0.3 for k in P_HIT}, "came_from": None, "opened_on_purpose": False,
            "state": {"url": url, "visible_text": [state]}, "page_kind": "single_item", "purpose": "entertain",
            "page_probs": {"single_item": 0.9}, "purpose_probs": {"entertain": 0.9}, "sensitive": 0.0,
            "p_hit": P_HIT | ({rule: p} if rule else {}), "answers": {}, "latency_ms": 500, "cached": False, "allow": {}}


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def home(tmp_path):
    shutil.copyfile(EXAMPLE, tmp_path / "rules.toml")
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture
def dash(home):
    """The dashboard's server on a free port; get(path) and post(path, body) -> (status, json),
    sent as the page sends them: with its version (page= or X-Qualm-Page)."""
    server = review.start_server(home / "data", home / "rules.toml", 0)
    port = server.server_address[1]
    server.server_close()
    server = review.start_server(home / "data", home / "rules.toml", port)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(path, body=None, page=server.page_version):
        if body is None and path.startswith("/api/") and page is not None:
            path += ("&" if "?" in path else "?") + "page=" + page
        headers = {"Host": f"127.0.0.1:{port}"} | ({"Content-Type": "application/json"} if body is not None else {}) | (
            {"X-Qualm-Page": page} if body is not None and page is not None else {})
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read()) if path.startswith("/api/") else r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield call
    server.shutdown()


def test_a_new_verdict_replaces_the_answers_given_with_the_old_one(tmp_path):
    j = judgement("a", "https://x.com/1", "social")
    review.save_review(tmp_path, "a", verdict="should_not_block", rules={r: "no" for r in P_HIT})
    review.save_review(tmp_path, "a", rules={"feeds": "yes"})  # an answer on its own adds to the verdict
    assert review.load_reviews(tmp_path)["a"]["rules"]["feeds"] == "yes"
    review.save_review(tmp_path, "a", verdict="right")  # you changed your mind
    got = review.load_reviews(tmp_path)["a"]
    assert got["verdict"] == "right" and got["rules"] == {}
    assert review.rule_answers(j, got)["social"] is True  # no stale "no" left to count it as a false pop-up


def test_switching_a_rule_off_keeps_its_past_popups(home, dash):
    write(home / "data" / "judgements.jsonl", [judgement("a", "https://x.com/1", "social")])
    assert dash("/api/rule/enabled", {"rule": "social", "on": False}) == (200, {"ok": True})
    _, d = dash("/api/data")
    card = d["queue"]["cards"][0]
    assert card["j"]["outcome"] == "intervene" and d["rows"]["items"][0]["outcome"] == "intervene"
    social = next(r for r in d["rules"] if r["id"] == "social")
    assert not social["enabled"] and social["name"] == "Social media"  # still offered when answering its old pop-ups
    # A rule that's gone from rules.toml doesn't count.
    assert review.outcome(card["j"], {"feeds"}) == "none"


def test_only_popups_that_showed_are_counted():
    at = NOW.isoformat(timespec="seconds")
    later = (NOW + timedelta(seconds=40)).isoformat(timespec="seconds")
    answered = (NOW + timedelta(seconds=90)).isoformat(timespec="seconds")
    reddit = {"url": "https://www.reddit.com/r/popular/", "window_title": "popular"}
    events = [
        # One screen hit two rules: the app showed the first only (older logs have both).
        {"at": at, "type": "intervention", "id": "d1", "rule": "feeds", "screen": reddit},
        {"at": at, "type": "intervention", "id": "d2", "rule": "social", "screen": reddit},
        # Judged again while that pop-up was still open: never shown.
        {"at": later, "type": "intervention", "id": "d3", "rule": "feeds", "screen": reddit},
        {"at": answered, "type": "response", "id": "d1", "rule": "feeds", "response": "back"},
        {"at": answered, "type": "intervention", "id": "d4", "rule": "social", "screen": {"url": "https://x.com/"}},
    ]
    assert [e["id"] for e in review.popups(events)] == ["d1", "d4"]


def test_a_focus_nudge_and_the_panel_after_it_are_one_popup(home):
    def at(s):
        return (NOW - timedelta(minutes=10) + timedelta(seconds=s)).isoformat(timespec="seconds")

    tiktok = {"url": "https://www.tiktok.com/@a/video/1", "window_title": "TikTok"}
    x = {"url": "https://x.com/home", "window_title": "Home / X"}
    events = [
        # A nudge answered "Not now", still there: the panel, answered "Take me back".
        {"at": at(0), "type": "intervention", "id": "n1", "rule": "shortvideo", "screen": tiktok},
        {"at": at(5), "type": "response", "id": "n1", "rule": "shortvideo", "response": "not now"},
        {"at": at(30), "type": "intervention", "id": "n2", "rule": "shortvideo", "screen": tiktok},
        {"at": at(40), "type": "response", "id": "n2", "rule": "shortvideo", "response": "back"},
        # A nudge let go, then its panel; and a nudge answered "Not now", then left before its panel came.
        {"at": at(100), "type": "intervention", "id": "n3", "rule": "feeds", "screen": x},
        {"at": at(120), "type": "intervention", "id": "n4", "rule": "feeds", "screen": x},
        {"at": at(125), "type": "response", "id": "n4", "rule": "feeds", "response": "back"},
        {"at": at(300), "type": "intervention", "id": "n5", "rule": "feeds", "screen": x},
        {"at": at(303), "type": "response", "id": "n5", "rule": "feeds", "response": "not now"},
        # Back on the page after "Take me back", a minute later: a pop-up of its own.
        {"at": at(360), "type": "intervention", "id": "n6", "rule": "shortvideo", "screen": tiktok},
    ]
    assert [e["id"] for e in review.popups(events)] == ["n2", "n4", "n5", "n6"]
    write(home / "data" / "decisions.jsonl", events)
    ins = review.insights(home / "data", load_config(home / "rules.toml")[1])
    ended = {k: sum(ins["outcomes"][d][k] for d in ins["days"]) for k in ("back", "snooze", "open")}
    # That "Not now" is going back, not "I need it": had you stayed, its panel would have come.
    assert ended == {"back": 3, "snooze": 0, "open": 1} and ins["unlocks"] == []  # the list of unlocks agrees


def test_the_watcher_logs_one_popup_per_screen_and_says_whether_the_model_answers(monkeypatch, tmp_path):
    from qualm import watcher as w
    from qualm.state import ScreenState
    from test_watcher import judged, page

    p = Policy(Settings(), [], tmp_path)
    watcher = w.Watcher(p, lambda ev: None, shots=False, presence=False)
    screen = ScreenState(app="Chrome", bundle_id="com.google.Chrome", window_title="popular", url="https://www.reddit.com/r/popular/")
    two = [Decision("intervene", "feeds", "matches URL pattern", "d1"), Decision("intervene", "social", "p_hit 0.70 >= 0.30", "d2")]
    watcher._finish({"ev": w.Event(screen, {}, None, two), "shot": ""}, shown=True)
    logged = [json.loads(line) for line in (tmp_path / "decisions.jsonl").open()]
    assert [(e["type"], e["rule"]) for e in logged] == [("intervention", "feeds")]

    judged(monkeypatch, tmp_path / "up", [page("a")] * 3)
    status = json.loads((tmp_path / "up" / "status.json").read_text())
    assert status["model"] == "ok" and status["pid"] == os.getpid()
    judged(monkeypatch, tmp_path / "down", [page("https://example.com/")] * 3, model_down=True)
    assert json.loads((tmp_path / "down" / "status.json").read_text())["model"] == "TimeoutError"


def test_the_page_gets_pages_of_the_log_not_all_of_it(home, dash, monkeypatch):
    rows = [judgement(f"j{i:03}", f"https://site{i}.example/", "social", ago=timedelta(minutes=i)) for i in range(150)]
    rows.append(judgement("old", "https://old.example/", "social", ago=timedelta(days=review.WINDOW_DAYS + 3)))
    write(home / "data" / "judgements.jsonl", rows)
    status, d = dash("/api/data")
    assert d["queue"]["total"] == 150 and len(d["queue"]["cards"]) == review.QUEUE_PAGE  # the old one waits for "older"
    assert d["rows"]["total"] == 150 and len(d["rows"]["items"]) == review.ROWS_PAGE
    # What the model read comes with the cards, never with the rows; the whole capture with neither.
    assert d["queue"]["cards"][0]["j"]["state"]["visible_text"] == ["what the model read"]
    assert "text" not in d["queue"]["cards"][0]["j"]["screen"] and "state" not in json.dumps(d["rows"])
    _, more = dash("/api/data?qn=40&rn=150&older=1")
    assert len(more["queue"]["cards"]) == 40 and more["rows"]["total"] == 151
    # The 15 s refresh: nothing changed, nothing sent; a change, everything again.
    q = "&qn=20&rn=100&quiet=0&q=&f=all&older=0"
    v = dash("/api/data?v=" + q)[1]["v"]
    assert dash(f"/api/data?v={v}{q}")[1] == {"same": True}
    assert dash("/api/pause", {"minutes": 30}) == (200, {"ok": True})  # a small answer, not the payload
    assert "queue" in dash(f"/api/data?v={v}{q}")[1]
    # Search and filters happen on the server.
    _, found = dash("/api/data?q=site12")
    assert {r["where"] for r in found["rows"]["items"]} == {f"https://site{i}.example/" for i in (12, *range(120, 130))}


def test_opening_a_reviewed_screen_changes_nothing_until_you_answer(home, dash):
    write(home / "data" / "judgements.jsonl", [judgement("a", "https://x.com/1", "social")])
    review.save_review(home / "data", "a", verdict="right")
    before = (home / "data" / "reviews.jsonl").read_text()
    _, got = dash("/api/screen?key=" + urllib.request.quote("https://x.com/1"))
    assert got["card"]["ids"] == ["a"] and got["card"]["review"]["verdict"] == "right" and got["card"]["j"]["state"]
    assert (home / "data" / "reviews.jsonl").read_text() == before
    assert dash("/api/data")[1]["queue"]["total"] == 0


def test_exceptions_from_the_page_go_to_rules_toml_and_can_be_removed(home, dash):
    assert dash("/api/exception", {"rule": "videos", "text": "  a conference   talk "}) == (200, {"ok": True})
    assert next(r for r in load_config(home / "rules.toml")[1] if r.id == "videos").exceptions == ("a conference talk",)
    code, err = dash("/api/exception", {"rule": "videos", "text": "a conference talk"})
    assert code == 400 and "already" in err["error"]
    code, err = dash("/api/exception", {"rule": "videos", "text": "y" * 5000})
    assert code == 400 and "200 characters" in err["error"]
    # Typed on an older dashboard, in exceptions.jsonl: shown with the rule, and removable here too.
    write(home / "data" / "exceptions.jsonl", [{"rule": "social", "text": "group chats", "at": "2026-09-01T10:00:00"}])
    rules = {r["id"]: r for r in dash("/api/data")[1]["rules"]}
    assert rules["videos"]["exceptions"] == ["a conference talk"] and rules["social"]["exceptions"] == ["group chats"]
    assert dash("/api/exception/remove", {"rule": "videos", "text": "a conference talk"})[0] == 200
    assert dash("/api/exception/remove", {"rule": "social", "text": "group chats"})[0] == 200
    rules = {r["id"]: r for r in dash("/api/data")[1]["rules"]}
    assert rules["videos"]["exceptions"] == [] and rules["social"]["exceptions"] == []
    # Errors read as sentences, not str(KeyError) in quotes.
    code, err = dash("/api/threshold", {"rule": "nope", "value": "0.3"})
    assert code == 400 and err["error"].startswith("no rule 'nope'")


def test_a_rules_file_that_does_not_load_keeps_the_history(home, dash):
    write(home / "data" / "judgements.jsonl", [judgement("a", "https://x.com/1", "social")])
    with (home / "rules.toml").open("a", encoding="utf-8") as f:
        f.write('\n[[rules]]\nid = "短视频"\nkind = "deny"\ndescription = "刷短视频"\n')
    status, d = dash("/api/data")
    assert status == 200 and "短视频" in d["problem"] and d["rules_file"] == str(home / "rules.toml")
    assert d["queue"]["total"] == 1 and d["rows"]["total"] == 1 and d["rules"] == []
    assert d["backend"] == ""  # unknown: the page says nothing about where screens are read


def test_the_page_knows_where_screens_are_read(home, dash):
    from qualm.config import Config

    assert dash("/api/data")[1]["backend"] == "kev"
    Config(home / "rules.toml").edit_settings({"backend": "jev"})
    assert dash("/api/data")[1]["backend"] == "jev"


def test_the_status_chip_says_whether_qualm_is_watching(tmp_path, monkeypatch):
    js = [judgement("a", "https://x.com/1")]
    off = {"rules": "", "hosted_off": False}  # rules.toml loads: nothing to say about it
    assert review.app_status(tmp_path, js) == {"running": False, "model": "", "accessibility": True} | off
    review.write_status(tmp_path, pid=os.getpid(), model="starting")
    review.write_status(tmp_path, model="ok")
    assert review.app_status(tmp_path, js) | {"accessibility": None} == {"running": True, "model": "ok", "accessibility": None} | off
    stamp = (tmp_path / review.STATUS_FILE).stat().st_mtime_ns
    review.write_status(tmp_path, model="ok")  # every reading says so: written once
    assert (tmp_path / review.STATUS_FILE).stat().st_mtime_ns == stamp
    (tmp_path / review.STATUS_FILE).write_text(json.dumps({"pid": 999999, "model": "ok"}))  # a Qualm that quit
    blind = [judgement("b", "")]
    blind[0]["screen"]["ax_trusted"] = False
    assert review.app_status(tmp_path, blind) == {"running": False, "model": "ok", "accessibility": False} | off


def test_a_second_watcher_quitting_leaves_qualm_running(tmp_path):
    other = subprocess.Popen([sys.executable, "-c", "pass"])  # `qualm watch` on the same folder, then Ctrl-C
    other.wait()
    review.write_status(tmp_path, pid=os.getpid(), model="starting")  # the app
    review.write_status(tmp_path, model="ok")
    (tmp_path / review.STATUS_FILE).write_text(json.dumps({"pid": other.pid, "model": "ok"}))
    assert not review.app_status(tmp_path, [])["running"]
    assert review.app_status(tmp_path, [], here=True)["running"]  # the app's own page knows it runs
    review.write_status(tmp_path, model="ok")  # the app's next reading: nothing changed, but the file isn't its own
    assert json.loads((tmp_path / review.STATUS_FILE).read_text())["pid"] == os.getpid()
    assert review.app_status(tmp_path, [])["running"]
    # Two watching at once take turns only when something changes, not on every reading.
    (tmp_path / review.STATUS_FILE).write_text(json.dumps({"pid": os.getppid(), "model": "ok"}))
    review.write_status(tmp_path, model="ok")
    assert json.loads((tmp_path / review.STATUS_FILE).read_text())["pid"] == os.getppid()


def test_hosted_trouble_and_accessibility_are_told_as_the_menu_tells_them(home, dash, monkeypatch):
    from qualm import localmodel
    from qualm.config import Config

    review.write_status(home / "data", pid=os.getpid(), model="TypeSafeAuthenticationError")
    assert dash("/api/data")[1]["status"]["trouble"] == ""  # the model on this Mac: no key to turn down
    Config(home / "rules.toml").edit_settings({"backend": "jev"})
    status = dash("/api/data")[1]["status"]
    assert status["trouble"] == "key" and status["running"] and isinstance(status["local"], bool)
    for model, kind in (("TypeSafeRateLimitError", "quota"), ("TypeSafeAPIConnectionError", "down"),
                        ("no TypeSafe API key: `qualm setup` stores one in the keychain", "nokey"), ("ok", "")):
        review.write_status(home / "data", model=model)
        assert dash("/api/data")[1]["status"]["trouble"] == kind
    # The model on this Mac is offered only where it can run, as the menu offers it.
    review.write_status(home / "data", model="TypeSafeRateLimitError")
    monkeypatch.setattr(localmodel, "unsupported", lambda: "The local model needs macOS 14 or later.")
    assert dash("/api/data")[1]["status"]["local"] is False
    monkeypatch.setattr(localmodel, "unsupported", lambda: None)
    monkeypatch.delenv("QUALM_BACKEND", raising=False)
    assert dash("/api/data")[1]["status"]["local"] is True
    page = review.PAGE_FILE.read_text(encoding="utf-8")
    assert all(f"  {kind}: [" in page for kind in ("nokey", "key", "quota", "down"))  # each has its words
    assert "quit Qualm from the menu bar and open it again" not in page and "without a restart" in page  # the app re-checks it


def test_a_tab_opened_before_an_update_reloads_and_changes_nothing(home, dash):
    session = (home / "data" / "session.json")
    status, page = dash("/")
    assert status == 200 and "__PAGE_VERSION__" not in page and review.PAGE_FILE.read_text(encoding="utf-8") != page
    version = page.split('const PAGE_VERSION = "')[1][:12]
    assert dash(f"/api/data?page={version}", page=None)[0] == 200
    # A page from before versions asks with no query; one from an older Qualm with another version.
    for path in ("/api/data", "/api/data?v=&page=0123456789ab", "/api/screen?key=x&page=0123456789ab"):
        assert dash(path, page=None) == (409, {"error": review.UPDATED, "reload": True})
    assert dash("/api/pause", {"minutes": 30}, page=None) == (409, {"error": review.UPDATED, "reload": True})
    assert dash("/api/pause", {"minutes": 30}, page="0123456789ab")[0] == 409 and not session.exists()
    assert dash("/api/pause", {"minutes": 30}) == (200, {"ok": True}) and session.exists()
    assert "queue" in dash("/api/data")[1]


def test_the_rules_tab_says_address_patterns_in_words(home, dash):
    from qualm.personalize import summary

    rules = {r["id"]: r for r in dash("/api/data")[1]["rules"]}
    live = rules["livestream"]
    assert "always on certain pages of live.bilibili.com, douyu.com, huya.com, twitch.tv and kick.com." in live["summary"]
    assert "\\" not in live["summary"] and "(?!" not in live["summary"] and len(live["patterns"]) == 5
    assert "m.weibo.cn/, certain pages of facebook.com and xiaohongshu.com." in rules["feeds"]["summary"]
    settings, starters = load_config(home / "rules.toml")
    assert "/twitch\\.tv/(?!" in summary(next(r for r in starters if r.id == "livestream"), settings)  # the CLI's, as written


def test_a_pattern_that_names_no_site_plainly_is_shown_as_it_is():
    from qualm.personalize import _pattern_words

    assert _pattern_words([r"twitch.tv/\w+"]) == ["certain pages of twitch.tv"]  # a plain dot names it too
    # A file isn't a site, and a choice of hosts says too little: the pattern itself, not "1 pattern".
    assert _pattern_words([r"index\.php\?id=\d+"]) == ["/index\\.php\\?id=\\d+/"]
    assert _pattern_words([r"(?:youtube|youtu)\.be/\w+", r"^https?://(www\.)?kick\.com/\w+"]) == \
        ["certain pages of kick.com", "/(?:youtube|youtu)\\.be/\\w+/"]


def test_the_page_names_commands_this_macs_terminal_can_run(home, dash, monkeypatch):
    from qualm import agent

    monkeypatch.setattr(agent, "command", lambda: "uv run --project /src/qualm qualm")
    d = dash("/api/data")[1]
    assert d["command"] == "uv run --project /src/qualm qualm" and d["undo"] == ""  # rules.toml loads
    page = review.PAGE_FILE.read_text(encoding="utf-8")
    assert "<code>qualm " not in page  # every command goes through cmd(), with D.command
    assert "t.dropped.outside_hours" in page  # the rules card leaves out answers from outside a rule's hours, as tune says
    # A broken file with nothing kept: no `config undo` to offer; and what the running app judges with.
    (home / "rules.toml").write_text("[settings]\nmax_wait_s = \"60\"\n")
    review.write_status(home / "data", rules="the rules it last loaded", hosted_off=True)
    d = dash("/api/data")[1]
    assert d["problem"] and d["undo"] == "" and d["starts_on"] == "the starter rules"
    assert d["status"]["rules"] == "the rules it last loaded" and d["status"]["hosted_off"] is True


def test_the_page_says_where_undo_goes_and_what_the_app_starts_on_as_the_menu_does(home, dash):
    from qualm import app
    from qualm.config import Config, undo_to

    # An older Qualm's folder: rules.toml and rules.toml.bak, no backups/, then a hand edit that breaks it.
    good = (home / "rules.toml").read_text()
    (home / "rules.toml.bak").write_text(good)
    (home / "rules.toml").write_text(good + "\n[setting]\nfoo = 1\n")
    d = dash("/api/data")[1]
    assert d["problem"] and d["undo"] == "the version before the last change Qualm saved" == undo_to(home / "rules.toml")
    assert d["starts_on"] == "an earlier version it saved" == app.fallback_config(home / "rules.toml")[2]
    assert "go back to the version before the last change Qualm saved" in d["problem"]  # the error says the same
    # Once Qualm has saved it, a broken hand edit goes back to that save, and the app starts on it.
    (home / "rules.toml").write_text(good)
    Config(home / "rules.toml").edit_settings({"max_wait_s": 90}, [])
    (home / "rules.toml").write_text((home / "rules.toml").read_text() + "\n[setting]\nfoo = 1\n")
    d = dash("/api/data")[1]
    assert d["undo"] == "the version Qualm last saved" and d["starts_on"] == "the version it last saved"
    # A kept version that doesn't load isn't one to go back to: undo would only fail, so nobody offers it.
    for p in [*(home / "backups").iterdir(), home / "rules.toml.bak"]:
        p.write_text("broken = [")
    d = dash("/api/data")[1]
    assert d["undo"] == "" and d["starts_on"] == "the starter rules" and "config undo" not in d["problem"]


def test_a_rule_in_other_letters_is_named_by_its_words(home, dash):
    from qualm.config import Config

    Config(home / "rules.toml").add("rules", {"id": "duanshipin", "kind": "deny", "description": "刷短视频，比如抖音"})
    assert {r["id"]: r["name"] for r in dash("/api/data")[1]["rules"]}["duanshipin"] == "刷短视频"  # as the menu has it
    p = Policy(Settings(), load_config(home / "rules.toml")[1], home / "data")
    p.log_intervention(Decision("intervene", "duanshipin", "x", "d1"), {"window_title": "抖音"}, None)
    assert review.week(home / "data", load_config(home / "rules.toml")[1])["this_week"]["by_rule"] == {"刷短视频": 1}


def test_week_has_the_insights_numbers_for_the_cli(home):
    data = home / "data"
    p = Policy(Settings(), load_config(home / "rules.toml")[1], data)
    p.log_intervention(Decision("intervene", "social", "x", "d1"), {"window_title": "Reddit"}, None)
    p.log_response("d1", "back", "social")
    p.log_intervention(Decision("intervene", "shortvideo", "x", "d2"), {"window_title": "Shorts"}, None)
    p.snooze("shortvideo", 10, "a recipe", "d2")
    w = review.week(data, load_config(home / "rules.toml")[1])
    assert w["this_week"]["popups"] == 2 and w["this_week"]["went_back_pct"] == 50
    assert w["this_week"]["by_rule"] == {"Social media": 1, "Short videos": 1}
    assert w["time_asked_for"][0]["reason"] == "a recipe"
    out = subprocess.run([sys.executable, "-m", "qualm.cli", "week", "--json", "--rules", str(home / "rules.toml"),
                          "--data", str(data)], capture_output=True, text=True)
    assert out.returncode == 0 and json.loads(out.stdout)["this_week"]["popups"] == 2
    text = subprocess.run([sys.executable, "-m", "qualm.cli", "week", "--rules", str(home / "rules.toml"),
                           "--data", str(home / "empty")], capture_output=True, text=True).stdout
    assert "0 pop-ups" in text and "Nothing yet" in text


def test_the_demo_weeks_keep_what_their_answers_saved(tmp_path):
    # experiments/demo_data.py logged "Not this one" and "Never here" without the exceptions
    # they save, so agents trying it told the user those answers were lost.
    from qualm.policy import host_of

    demo = os.path.join(os.path.dirname(__file__), os.pardir, "experiments", "demo_data.py")
    for seed in (7, 8):
        data = tmp_path / str(seed)
        subprocess.run([sys.executable, demo, "--out", str(data), "--seed", str(seed)], check=True, capture_output=True)
        events = [json.loads(line) for line in (data / "decisions.jsonl").open()]
        shown = {e["id"]: e for e in events if e["type"] == "intervention"}
        said = [(e, shown[e["id"]]) for e in events if e.get("response") in ("fine", "never")]
        assert {e["response"] for e, _ in said} == {"fine", "never"}
        p = Policy(*load_config(EXAMPLE), data)
        for e, d in said:
            s = d["screen"]
            if e["response"] == "fine":
                assert p.marked_fine(d["rule"], s["url"], s["bundle_id"], s["window_title"], None) == "you marked this page fine"
            else:
                assert p.precheck(s["bundle_id"], s["url"], s["window_title"], s["app"]).reason == "you said never here"
                assert not [x for x in shown.values() if x["at"] > e["at"] and host_of(x["screen"]["url"]) == host_of(s["url"])]


def test_the_pages_tuning_is_what_rules_tune_says(home, dash):
    from qualm.trial import tune_all

    store = judgement("shop", "https://shop.example/item/1", "social", p=0.9) | {"allow": {"shopping": 0.9}}
    rows = [*(judgement(f"y{i}", f"https://reddit.com/r/{i}", "social", p=0.6) for i in range(3)),
            *(judgement(f"n{i}", f"https://docs.example/{i}") for i in range(3)), store]
    write(home / "data" / "judgements.jsonl", rows)
    for r in rows:
        review.save_review(home / "data", r["id"], rules={"social": "yes" if r["id"].startswith("y") else "no"})
    settings, rules = load_config(home / "rules.toml")
    page = next(t for t in dash("/api/data")[1]["tuning"] if t["rule"] == "social")
    assert page == next(t for t in tune_all(home / "data", rules, settings) if t["rule"] == "social")
    assert page["precision"] == 1.0  # the store is a shopping page, which only the log's whole line says


def test_the_page_sees_a_rewording_that_only_unanswered_judgements_show(home, dash):
    from qualm.trial import tune_all

    rows = [*(judgement(f"y{i}", f"https://reddit.com/r/{i}", "social", p=0.6) for i in range(3)),
            *(judgement(f"n{i}", f"https://docs.example/{i}") for i in range(3))]
    # Logged after a hand edit changed the wording, and never answered: the older scores are the old wording's.
    later = judgement("u1", "https://example.com/u", ago=timedelta(minutes=1)) | {"backend": "kev", "w": {"social": "old"}}
    write(home / "data" / "judgements.jsonl", [*rows, later])
    for r in rows:
        review.save_review(home / "data", r["id"], rules={"social": "yes" if r["id"].startswith("y") else "no"})
    settings, rules = load_config(home / "rules.toml")
    page = next(t for t in dash("/api/data")[1]["tuning"] if t["rule"] == "social")
    assert page == next(t for t in tune_all(home / "data", rules, settings) if t["rule"] == "social")
    assert (page["yes"], page["dropped"]["other_wording"], page["suggested"]) == (0, 6, None)


def test_an_exception_from_the_page_keeps_one_saved_at_the_same_moment(home):
    from qualm.config import Config, locked

    started = threading.Event()

    def agent():  # `qualm except add` at the same moment, holding rules.toml while it changes it
        with locked(home / "rules.toml"):
            started.set()
            time.sleep(0.3)
            Config(home / "rules.toml").edit("rules", "videos", {"exceptions": ["a lecture"]})

    t = threading.Thread(target=agent)
    t.start()
    started.wait()
    review.add_exception(home / "rules.toml", home / "data", "videos", "a conference talk")
    t.join()
    videos = next(r for r in load_config(home / "rules.toml")[1] if r.id == "videos")
    assert videos.exceptions == ("a lecture", "a conference talk")
