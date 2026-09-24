"""Scoring a rule from the command line: `rules test`, `label`, `tune`,
`allow test`, `except add --from` and `review`, on made-up screens with a
fake model. No model server, no screen."""

import json
import shutil
import sys
from types import SimpleNamespace as NS

import pytest

from qualm import agent
from qualm.config import EXAMPLE
from qualm.decide import Reading, RuleVerdict, shifted
from qualm.policy import Policy
from qualm.rules import AllowClass, Rule, Settings, question_key

MONDAY, SUNDAY = "2026-09-21T10:00:00", "2026-09-20T16:00:00"


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for k in ("QUALM_SHIFT", "QUALM_BACKEND"):
        monkeypatch.delenv(k, raising=False)


def screen(jid, title, url="", p_hit=None, kind="single_item", at=MONDAY, allow=None, bundle="com.google.Chrome", **extra):
    """One judgement as the app logs it (before it recorded wording and model, unless given)."""
    return {"id": jid, "at": at, "screen": {"app": "Chrome", "bundle_id": bundle, "window_title": title, "url": url},
            "decisions": extra.pop("decisions", []), "opened_on_purpose": False,
            "state": {"window_title": title, "url": url, "visible_text": [title]},
            "page_kind": kind, "purpose": "entertain", "page_probs": {}, "purpose_probs": {"entertain": 0.7},
            "sensitive": 0.01, "p_hit": p_hit or {}, "allow": allow if allow is not None else {"shopping": 0.05},
            **extra}


def write(tmp_path, *screens):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "judgements.jsonl").write_text("".join(json.dumps(s) + "\n" for s in screens))
    return data


class Fake:
    """Answers every question from the screen's title: SCORES for the rule, ALLOW for allow classes."""

    def __init__(self, backend="kev", scores=None, allow=None, down=False):
        self.backend, self.scores, self.allow, self.down, self.asked = backend, scores or {}, allow or {}, down, []

    def system_one(self, state, questions):
        if self.down:
            from typesafe_sdk import TypeSafeAPIConnectionError

            raise TypeSafeAPIConnectionError("Connection refused")
        title = state["window_title"]
        self.asked.append((title, sorted(questions)))
        p = next((v for k, v in self.scores.items() if k in title), 0.05)
        a = next((v for k, v in self.allow.items() if k in title), 0.05)
        return NS(answers={k: NS(probabilities={"violates": p, "in_scope": p}, noul=a) for k in questions})


@pytest.fixture
def qualm(tmp_path, monkeypatch, capsys):
    """`qualm ARGS --json` in this process, with `fake` as the model; returns (exit code, parsed output)."""
    import qualm.decide as decide
    from qualm.cli import main

    shutil.copyfile(EXAMPLE, tmp_path / "rules.toml")
    state = {"fake": Fake()}
    monkeypatch.setattr(decide, "make_client", lambda settings=None: state["fake"])

    def run(*args, json_out=True):
        argv = ["qualm", *args, "--rules", str(tmp_path / "rules.toml"), "--data", str(tmp_path / "data")]
        monkeypatch.setattr(sys, "argv", argv + (["--json"] if json_out else []))
        code = 0
        try:
            main()
        except SystemExit as e:
            code = e.code
        out = capsys.readouterr().out
        return code, json.loads(out) if json_out else out

    run.state = state
    return run


def test_rules_test_counts_check_ins_and_says_why_per_screen(tmp_path, qualm):
    write(tmp_path,
          screen("s1", "A thread : Reddit", "https://www.reddit.com/r/a/1", at=SUNDAY),
          screen("s2", "Travel notes : Xiaohongshu", "https://www.xiaohongshu.com/explore/1"),
          screen("s3", "Amazon.com: Earbuds", "https://www.amazon.com/dp/1", allow={"shopping": 0.9}),
          screen("s4", "Weixin", bundle="com.tencent.xinWeChat", p_hit={"social": 0.4}),
          screen("s5", "Lecture 1", "https://www.youtube.com/watch?v=1"))
    qualm.state["fake"] = Fake(scores={"Reddit": 0.8, "Xiaohongshu": 0.6, "Amazon": 0.9, "Weixin": 0.4})
    code, out = qualm("rules", "test", "social")
    assert code == 0 and out["would_fire"] == 3  # was always 0 for a check-in rule
    does = {x["id"]: (x["does"], x["why"]) for x in out["screens"]}
    assert does["s1"][0] == "checks in" and does["s3"] == ("allow", "shopping is never flagged")

    # What the user changes shows up: hours, a window marked fine, a site never judged.
    assert qualm("rules", "set", "social", "when+=mon-fri")[0] == 0
    assert qualm("except", "add", "social", "--from", "s4")[0] == 0
    assert qualm("never", "add", "--site", "xiaohongshu.com")[0] == 0
    qualm.state["fake"].asked.clear()
    code, out = qualm("rules", "test", "social")
    does = {x["id"]: (x["does"], x["why"]) for x in out["screens"]}
    assert does["s1"] == ("nothing", "outside its hours: mon-fri")
    assert does["s2"] == ("allow", "you said never here")
    assert does["s4"] == ("allow", "you marked this window fine")
    assert out["would_fire"] == 0 and not qualm.state["fake"].asked  # the same questions: all from the cache

    # An allow class added since: asked once per screen, and it lets the chat through.
    qualm.state["fake"].allow = {"Weixin": 0.9}
    assert qualm("allow", "add", "work_chat", "--what", "a work chat")[0] == 0
    code, out = qualm("rules", "test", "social")
    assert {x["id"]: x["why"] for x in out["screens"]}["s4"] == "work_chat is never flagged"
    assert {q for _, qs in qualm.state["fake"].asked for q in qs} == {"allow_work_chat"}


def test_hosted_scores_are_shifted_and_never_mixed_with_the_local_models(tmp_path, qualm):
    from qualm.rules import load_config

    write(tmp_path, screen("s1", "A thread : Reddit", "https://www.reddit.com/r/a/1"))
    social = next(r for r in load_config(tmp_path / "rules.toml")[1] if r.id == "social")
    (tmp_path / "data" / "trials.jsonl").write_text(json.dumps(  # from before trials said which model: Kev's then
        {"rule": "social", "q": question_key(social, "en"), "jid": "s1", "p_hit": 0.5}) + "\n")
    qualm.state["fake"] = Fake("jev", scores={"Reddit": 0.8})
    code, out = qualm("rules", "test", "social")
    assert out["backend"] == "jev" and out["screens"][0]["p_hit"] == round(shifted(0.8, 2.0), 3) == 0.351
    assert out["screens"][0]["does"] == "checks in"  # 0.351 >= 0.3, as the live app would see it
    qualm.state["fake"] = Fake("kev", scores={"Reddit": 0.6})
    code, out = qualm("rules", "test", "social")
    asked = {q for _, qs in qualm.state["fake"].asked for q in qs}
    assert out["screens"][0]["p_hit"] == 0.5 and "rule" not in asked  # Kev's own score, not Jev's


def test_no_screens_yet_is_not_a_model_problem(tmp_path, qualm):
    for cmd in (("rules", "test", "social"), ("allow", "test", "chat")):
        code, out = qualm(*cmd)
        assert code == 3 and out["error"]["code"] == "no_screens" and "Qualm running" in out["error"]["message"]
    assert not (tmp_path / "data").exists()
    write(tmp_path, screen("s1", "A thread : Reddit", "https://www.reddit.com/r/a/1"))
    qualm.state["fake"] = Fake(down=True)
    code, out = qualm("rules", "test", "social")
    assert code == 5 and out["error"]["code"] == "unreachable" and "is Qualm running" in out["error"]["message"]


def test_a_new_rule_judges_the_whole_page_and_steps_in_on_its_own_allow_class(tmp_path, qualm):
    code, out = qualm("rules", "add", "news", "--what", "news sites and headlines")
    added = out["added"]
    assert added["target"] == "page" and added["allow_intentional"] and out["overrides_allow_added"] == []
    code, out = qualm("rules", "add", "old_news", "--what", "news", "--content-only", "--no-on-purpose-ok")
    assert out["added"]["target"] == "content" and not out["added"]["allow_intentional"]
    # Rules already in the file keep their meaning.
    assert qualm("rules", "show", "shortvideo")[1]["target"] == "content"

    code, out = qualm("rules", "add", "shopping", "--what", "online shopping")
    assert code == 2 and "allow class" in out["error"]["message"]
    code, out = qualm("rules", "add", "evening_shopping", "--what", "online shopping", "--check-in")
    assert out["overrides_allow_added"] == ["shopping"] and out["added"]["overrides_allow"] == ["shopping"]
    code, out = qualm("rules", "set", "news", "what=news sites and shops")
    assert out["overrides_allow_added"] == ["shopping"]

    # rules test shows it, and so does the live policy.
    write(tmp_path, screen("s1", "Amazon.com: Earbuds", "https://www.amazon.com/dp/1", allow={"shopping": 0.9}))
    qualm.state["fake"] = Fake(scores={"Amazon": 0.9})
    row = qualm("rules", "test", "evening_shopping")[1]["screens"][0]
    assert row["does"] == "checks in" and row["overrides"] == ["shopping"]
    settings = Settings(allow=(AllowClass("shopping", "an online store", threshold=0.35),))
    rule = Rule("shop", "deny", "online shopping", overrides_allow=("shopping",))
    r = Reading(0.0, "single_item", {}, "task", {}, [RuleVerdict("shop", "", 0.9, {})], 0.0, allow={"shopping": 0.87})
    st = {"url": "https://www.amazon.com/dp/1", "visible_text": ["Earbuds"]}
    assert [d.action for d in Policy(settings, [rule], tmp_path).decide(st, r)] == ["intervene"]
    assert [d.action for d in Policy(settings, [Rule("shop", "deny", "shop")], tmp_path).decide(st, r)] == ["allow"]


def test_the_suggestion_is_never_worse_than_the_threshold_you_have(tmp_path):
    from qualm.review import _pick, _pr, _score

    # Right on every answer at 0.15 today: the old pick walked down to 0.115, into the no answers.
    pts = [(p, True) for p in (0.9, 0.5, 0.21, 0.2, 0.19, 0.18)] + [(p, False) for p in (0.12, 0.11, 0.1, 0.05)]
    t = _pick(pts)
    assert _score(pts, t) <= _score(pts, 0.15) and _pr(pts, 0.15) == (1.0, 1.0)
    # 90% right let it take two no answers for free; now it sits in the gap between them and the yes ones.
    pts = [(0.38 + i * 0.02, True) for i in range(20)] + [(p, False) for p in (0.101, 0.106, 0.12, 0.05, 0.03, 0.01)]
    t = _pick(pts)
    assert 0.12 < t < 0.38 and _pr(pts, t) == (1.0, 1.0)


def test_tune_uses_this_wording_and_model_the_way_the_live_policy_scores(tmp_path, qualm, monkeypatch):
    yes = [screen(f"y{i}", f"Feed {i} : Reddit", f"https://www.reddit.com/r/{i}", p_hit={"social": 0.5}) for i in range(3)]
    no = [screen(f"n{i}", f"Docs {i}", f"https://example.com/{i}", p_hit={"social": 0.05}) for i in range(3)]
    shop = screen("shop", "Amazon.com: Earbuds", "https://www.amazon.com/dp/1", p_hit={"social": 0.9}, allow={"shopping": 0.9})
    write(tmp_path, *yes, *no, shop)
    assert qualm("rules", "label", "social", "--yes", *[s["id"] for s in yes], "--no", "shop", *[s["id"] for s in no])[0] == 0
    code, t = qualm("rules", "tune", "social")
    # Scored live, before Qualm recorded the wording: used while nothing better exists.
    assert (t["scored_live"], t["scored_by_test"], t["yes"], t["no"]) == (7, 0, 3, 4)
    assert t["precision"] == 1.0  # the store scored 0.9, but the shopping class lets it through: never a hit
    assert t["suggested"] is None and t["keep_current"]

    # Hosted, and a new wording tested on it: tuned on those scores only, shifted like the live app's.
    rules = tmp_path / "rules.toml"
    rules.write_text(rules.read_text().replace('max_wait_s = 60', 'max_wait_s = 60\nbackend = "jev"'))
    assert qualm("rules", "set", "social", "what=scrolling Reddit")[0] == 0
    qualm.state["fake"] = Fake("jev", scores={"Feed 0": 0.9, "Feed 1": 0.7, "Feed 2": 0.6, "Docs": 0.3})
    qualm("rules", "test", "social")
    code, t = qualm("rules", "tune", "social", "--precision", "0.8")
    assert t["backend"] == "jev" and t["scored_by_test"] == 7 and t["scored_live"] == 0
    # Between 0.055 and 0.169, Jev's 0.3 and 0.6 shifted as the live app shifts them; unshifted it was 0.45.
    assert 0.055 < t["suggested"] < 0.169 and t["recall"] == 0.33 and t["suggested_recall"] == 1.0
    q = "QUALM_HOME=/tmp/second uv run --project /src/qualm qualm"  # the command as this terminal runs it
    monkeypatch.setattr(agent, "command", lambda: q)
    text = qualm("rules", "tune", "social", "--precision", "0.8", json_out=False)[1]
    assert f"apply it: {q} rules tune social --precision 0.8 --apply" in text

    # The dashboard shows the same numbers.
    from qualm.review import tuning
    from qualm.rules import load_config

    settings, all_rules = load_config(rules)
    assert next(r for r in tuning(tmp_path / "data", all_rules, settings) if r["rule"] == "social") == t


def test_a_rewording_is_not_tuned_on_the_old_wordings_scores(tmp_path):
    from qualm.review import save_review
    from qualm.trial import run, tune

    settings = Settings()
    old, new = Rule("videos", "check_in", "videos on YouTube"), Rule("videos", "check_in", "bingeing TV series")
    judged = [screen(f"j{i}", f"Clip {i}", f"https://youtube.com/{i}", p_hit={"videos": 0.6}, backend="kev",
                     w={"videos": question_key(old, "en")}, decisions=[{"action": "intervene", "rule": "videos"}])
              for i in range(4)]
    # Logged before Qualm recorded the wording; the log recorded the old one after them.
    legacy = [screen(f"l{i}", f"Old clip {i}", f"https://youtube.com/l{i}", p_hit={"videos": 0.6}, at=SUNDAY)
              for i in range(3)]
    data = write(tmp_path, *judged, *legacy)
    for j in judged:
        save_review(data, j["id"], verdict="right")  # "right" about what the old wording did
    for j in legacy:
        save_review(data, j["id"], rules={"videos": "yes"})  # "is this videos?": kept for any wording
    t = tune(data, new, settings)
    assert t["dropped"]["other_wording"] == 4 + 3 and t["scored_live"] == 0  # the old wording: left out
    assert tune(data, old, settings)["scored_live"] == 4 + 3  # the wording they were scored with: all of them
    run(Fake(scores={"Old clip": 0.1}), data, new, settings, last=2)  # the new wording, tested
    t = tune(data, new, settings)
    assert t["dropped"]["other_wording"] == 4 + 1 and t["scored_by_test"] == 2 and t["scored_live"] == 0


def test_answers_from_before_the_upgrade_count_until_the_rule_is_reworded(tmp_path, qualm):
    """Judgements from before Qualm recorded wording and model count as the
    current wording and Kev's, unless the log or rules.toml's kept versions
    show a rewording since; one `rules test` doesn't drop them."""
    yes = [screen(f"y{i}", f"Feed {i} : Reddit", f"https://www.reddit.com/r/{i}", p_hit={"social": 0.5}, at=SUNDAY)
           for i in range(4)]
    no = [screen(f"n{i}", f"Docs {i}", f"https://example.com/{i}", p_hit={"social": 0.05}, at=SUNDAY) for i in range(4)]
    write(tmp_path, *yes, *no)
    assert qualm("rules", "label", "social", "--yes", *[s["id"] for s in yes], "--no", *[s["id"] for s in no])[0] == 0
    assert qualm("rules", "tune", "social")[1]["scored_live"] == 8
    qualm.state["fake"] = Fake(scores={"Docs 3": 0.04, "Docs 2": 0.03})
    assert qualm("rules", "test", "social", "--last", "2")[1]["complete"]  # the same wording, on two of them
    t = qualm("rules", "tune", "social")[1]
    assert (t["scored_by_test"], t["scored_live"], t["dropped"]["other_wording"]) == (2, 6, 0)

    # Scores from before trials recorded the model are Kev's: reused, not asked again.
    trials = tmp_path / "data" / "trials.jsonl"
    rows = [json.loads(line) for line in trials.open()]
    trials.write_text("".join(json.dumps({k: v for k, v in r.items() if k not in ("backend", "raw", "asked")}) + "\n"
                              for r in rows))
    qualm.state["fake"].asked.clear()
    assert qualm("rules", "test", "social", "--last", "2")[0] == 0 and not qualm.state["fake"].asked
    assert qualm("rules", "tune", "social")[1]["scored_by_test"] == 2

    # Reworded after them (rules.toml keeps the version before): they were the old wording's.
    assert qualm("rules", "set", "social", "what=scrolling Reddit")[0] == 0
    t = qualm("rules", "tune", "social")[1]
    assert (t["scored_live"], t["scored_by_test"], t["dropped"]["other_wording"]) == (0, 0, 8)


def test_judgements_say_which_wording_and_model_scored_them(tmp_path):
    rule = Rule("social", "check_in", "social media")
    p = Policy(Settings(), [rule], tmp_path)
    r = Reading(0.0, "single_item", {}, "entertain", {}, [RuleVerdict("social", "", 0.4, {})], 0.0)
    p.log_judgement({"app": "Chrome", "url": "https://x.com"}, r, [], state={"url": "https://x.com"})
    logged = json.loads((tmp_path / "judgements.jsonl").read_text())
    assert logged["backend"] == "kev" and logged["w"] == {"social": question_key(rule, "en")}


def test_except_from_a_screen_or_a_pop_up_is_what_not_this_one_saves(tmp_path, qualm):
    write(tmp_path, screen("page", "A thread : Reddit", "https://www.reddit.com/r/a/1", p_hit={"social": 0.7}),
          screen("chat", "Weixin", bundle="com.tencent.xinWeChat", p_hit={"social": 0.4}))
    popup = {"at": MONDAY, "type": "intervention", "id": "d1", "rule": "social", "reason": "",
             "screen": {"app": "Chrome", "bundle_id": "com.google.Chrome", "window_title": "Another", "url": "https://x.com/2"}}
    (tmp_path / "data" / "decisions.jsonl").write_text(json.dumps(popup) + "\n")
    dry = qualm("except", "add", "social", "--from", "page", "--dry-run")[1]
    assert dry["appends"][0]["line"]["url"] == "https://www.reddit.com/r/a/1"
    assert not (tmp_path / "data" / "exceptions.jsonl").exists()
    for jid in ("page", "chat", "d1"):
        assert qualm("except", "add", "social", "--from", jid)[0] == 0
    saved = [json.loads(line) for line in (tmp_path / "data" / "exceptions.jsonl").open()]
    assert [{k: v for k, v in e.items() if k != "at"} for e in saved] == [
        {"rule": "social", "url": "https://www.reddit.com/r/a/1", "title": "A thread : Reddit"},
        {"rule": "social", "url": "", "title": "Weixin", "app": "com.tencent.xinWeChat", "p_hit": 0.4},
        {"rule": "social", "url": "https://x.com/2", "title": "Another"}]
    answers = [json.loads(line) for line in (tmp_path / "data" / "decisions.jsonl").open()][1:]
    assert [(a["id"], a["response"]) for a in answers] == [("d1", "fine")]  # only the pop-up gets an answer

    p = Policy(Settings(), [Rule("social", "check_in", "social media", threshold=0.3)], tmp_path / "data")
    chat = {"window_title": "Weixin", "visible_text": ["hi"]}
    for score, action in ((0.45, "allow"), (0.55, "intervene")):
        r = Reading(0.0, "single_item", {}, "entertain", {}, [RuleVerdict("social", "", score, {})], 0.0)
        assert p.decide(chat, r, "com.tencent.xinWeChat")[0].action == action
    assert qualm("except", "add", "social", "--from", "nope")[1]["error"]["code"] == "not_found"


def test_review_has_dates_json_and_the_misses_you_flagged(tmp_path, qualm):
    write(tmp_path, screen("a", "Lecture", "https://youtube.com/1", p_hit={"social": 0.05}, at="2026-09-20T09:00:00"),
          screen("b", "Feed : Reddit", "https://reddit.com/", p_hit={"social": 0.6}, at="2026-09-22T21:33:13",
                 decisions=[{"action": "intervene", "rule": "social", "reason": "p_hit 0.60 >= 0.30"}]))
    assert qualm("review", "--fix", "a", "social=yes", json_out=False)[0] == 0
    text = qualm("review", "--rule", "social", json_out=False)[1]
    assert "#a 2026-09-20 09:00:00" in text and "you said it missed: social" in text  # 0.05 is far under the threshold
    code, out = qualm("review", "--misses")
    assert [(j["id"], j["missed"]) for j in out["judgements"]] == [("a", ["social"])]
    code, out = qualm("review", "--since", "2026-09-22")
    j = out["judgements"][0]
    assert j["id"] == "b" and j["at"] == "2026-09-22T21:33:13" and j["decisions"][0]["action"] == "intervene"
    assert j["p_hit"] == {"social": 0.6} and j["you"] is None and j["missed"] is None


def test_except_from_during_a_focus_session_is_saved_for_good(tmp_path, qualm):
    from qualm.policy import start_focus

    write(tmp_path, screen("page", "A thread : Reddit", "https://www.reddit.com/r/a/1", p_hit={"social": 0.7}))
    start_focus(tmp_path / "data", "write the report", 30)  # the pop-up's "It's part of the task" lasts the session
    code, text = qualm("except", "add", "social", "--from", "page", json_out=False)
    assert code == 0 and "let through from now on" in text and "the model reads" not in text
    saved = [json.loads(line) for line in (tmp_path / "data" / "exceptions.jsonl").open()]
    assert [e["url"] for e in saved] == ["https://www.reddit.com/r/a/1"]


def test_rules_test_steps_in_on_an_allow_classs_own_sites_as_the_app_would(tmp_path):
    from qualm.trial import run

    settings = Settings(allow=(AllowClass("music", "a music player", sites=("open.spotify.com",)),))
    data = write(tmp_path, screen("j1", "Daily Mix - Spotify", "https://open.spotify.com/playlist/1"))
    # Off for now, being tried out: it still steps in on the class's own sites, as it will when on.
    rule = Rule("limit_music", "check_in", "listening to music", overrides_allow=("music",), enabled=False)
    rows = run(Fake(scores={"Spotify": 0.9}, allow={"Spotify": 0.97}), data, rule, settings, last=5)
    assert [(r["does"], r["overrides"]) for r in rows] == [("checks in", ["music"])]
    rows = run(Fake(scores={"Spotify": 0.9}), data, Rule("social", "check_in", "social media"), settings, last=5)
    assert [(r["does"], r["why"]) for r in rows] == [("allow", "music is never flagged")]


def test_except_from_a_pop_up_of_the_rules_own_app_or_another_rule(tmp_path, qualm):
    from qualm.rules import load_config

    write(tmp_path, screen("s1", "A thread : Reddit", "https://www.reddit.com/r/a/1"))
    assert qualm("rules", "add", "games", "--what", "playing video games", "--app", "com.valvesoftware.steam")[0] == 0
    steam = {"app": "Steam", "bundle_id": "com.valvesoftware.steam", "window_title": "Steam", "url": ""}
    pops = [{"at": MONDAY, "type": "intervention", "id": "dgame", "rule": "games", "reason": "", "screen": steam},
            {"at": MONDAY, "type": "intervention", "id": "dshort", "rule": "shortvideo", "reason": "",
             "screen": {**steam, "app": "Chrome", "url": "https://www.youtube.com/shorts/1"}},
            {"at": MONDAY, "type": "response", "id": "dshort", "rule": "shortvideo", "response": "back"}]
    log = tmp_path / "data" / "decisions.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in pops))
    # An app the rule names steps in without a score: let through at any, as Not this one does.
    code, out = qualm("except", "add", "games", "--from", "dgame")
    assert code == 0 and out["added"]["app"] == "com.valvesoftware.steam"
    settings, rules = load_config(tmp_path / "rules.toml")
    reading = Reading(0.0, "other", {}, "", {}, [], 0.0)
    assert Policy(settings, rules, tmp_path / "data").decide(
        {"app": "Steam", "window_title": "Steam"}, reading, "com.valvesoftware.steam")[0].reason == "you marked this window fine"
    # Another rule's pop-up is refused; its own rule's doesn't change how it ended (Take me back).
    code, out = qualm("except", "add", "social", "--from", "dshort")
    assert code == 2 and "was a shortvideo pop-up" in out["error"]["message"]
    assert qualm("except", "add", "shortvideo", "--from", "dshort")[0] == 0
    answers = [(e["id"], e["response"]) for e in map(json.loads, log.open()) if e["type"] == "response"]
    assert answers == [("dshort", "back"), ("dgame", "fine")]


def test_except_remove_takes_away_one_page_as_the_app_does(tmp_path, qualm):
    from qualm.rules import load_config

    urls = [f"https://www.instagram.com/p/{x}/" for x in ("AAA", "BBB", "CCC")]
    write(tmp_path, screen("s1", "Instagram", urls[0]), screen("s2", "Instagram", urls[0]))
    exceptions = tmp_path / "data" / "exceptions.jsonl"
    exceptions.write_text("".join(json.dumps({"rule": "social", "url": u, "title": "Instagram"}) + "\n" for u in urls)
                          + json.dumps({"rule": "social", "app": "com.tencent.xinWeChat", "title": "Instagram",
                                        "p_hit": 0.4}) + "\n")

    def listed():
        return sorted(x.get("url") or x["app"] for x in qualm("except", "list", "social")[1])

    def let_through():
        settings, rules = load_config(tmp_path / "rules.toml")
        policy = Policy(settings, rules, tmp_path / "data")
        return sorted([u for u in urls if policy.marked_fine("social", u, "com.google.Chrome", "Instagram", 0.9)]
                      + ["com.tencent.xinWeChat"] * bool(policy.marked_fine("social", "", "com.tencent.xinWeChat",
                                                                            "Instagram", 0.1)))

    # Titles repeat across pages: by title is refused while it's several pages', by address takes one.
    code, out = qualm("except", "remove", "social", "Instagram")
    assert code == 2 and "give the address" in out["error"]["message"]
    assert qualm("except", "remove", "social", urls[0])[0] == 0
    assert listed() == let_through() == ["com.tencent.xinWeChat", *urls[1:]]
    assert qualm("except", "remove", "social", urls[1])[0] == 0  # was "no exception" once the first one went
    assert listed() == let_through() == ["com.tencent.xinWeChat", urls[2]]
    assert "(`except list`): 2" in qualm("rules", "show", "social", json_out=False)[1]
    # The same page from a second screen isn't saved twice.
    assert qualm("except", "add", "social", "--from", "s1")[0] == 0
    code, out = qualm("except", "add", "social", "--from", "s2")
    assert code == 0 and out["already"] and "was already let through" in qualm(
        "except", "add", "social", "--from", "s2", json_out=False)[1]
    assert sum(urls[0] in line for line in exceptions.read_text().splitlines()) == 3  # the first, its removal, s1


def test_review_finds_pop_ups_by_their_decision_id_and_says_how_they_ended(tmp_path, qualm):
    from qualm.policy import Decision

    pop = screen("b", "Feed : Reddit", "https://reddit.com/", p_hit={"social": 0.6}, at="2026-09-22T21:33:13",
                 decisions=[{"action": "intervene", "rule": "social", "reason": "p_hit 0.60 >= 0.30"}])
    write(tmp_path, *[screen(f"s{i}", f"Page {i}", f"https://example.com/{i}", at=f"2026-09-01T09:{i:02d}:00")
                      for i in range(40)], pop)
    (tmp_path / "data" / "decisions.jsonl").write_text("".join(json.dumps(e) + "\n" for e in (
        {"at": "2026-09-22T21:33:12", "type": "intervention", "id": "17d7f938abcd", "rule": "social", "reason": "",
         "screen": pop["screen"]},
        {"at": "2026-09-22T21:33:20", "type": "response", "id": "17d7f938abcd", "rule": "", "response": "never"})))
    code, out = qualm("review", "--id", "17d7f938abcd")
    assert [j["id"] for j in out["judgements"]] == ["b"]
    assert out["judgements"][0]["popup"] == {"id": "17d7f938abcd", "rule": "social", "ended": "never"}
    assert "[pop-up 17d7f938abcd: you said Never here]" in qualm("review", "--last", "1", json_out=False)[1]
    # Nothing is cut without saying so; a date shows everything from then.
    code, out = qualm("review")
    assert (out["matching"], out["shown"]) == (41, 30) and "--last N" in out["note"]
    code, out = qualm("review", "--since", "2026-9-1")
    assert (out["matching"], out["shown"]) == (41, 41) and "note" not in out
    assert qualm("review", "--since", "2026-09-22 21:00")[1]["matching"] == 1
    assert qualm("review", "--since", "yesterday")[0] == 0
    code, out = qualm("review", "--since", "last week")
    assert code == 2 and "today or yesterday" in out["error"]["message"]
    # From now on judgements keep the decision id of their pop-up.
    p = Policy(Settings(), [Rule("social", "check_in", "social media")], tmp_path)
    p.log_judgement({"app": "Chrome"}, None, [Decision("intervene", "social", "why", "d2")])
    assert json.loads((tmp_path / "judgements.jsonl").read_text())["decisions"][0]["id"] == "d2"


def test_review_since_a_date_is_capped_and_says_how_to_get_more(tmp_path, qualm):
    write(tmp_path, *[screen(f"s{i:03d}", f"Page {i}", f"https://example.com/{i}", p_hit={"social": 0.5},
                             at=f"2026-09-22T{8 + i // 60:02d}:{i % 60:02d}:00") for i in range(250)])
    # A busy day is over 1,000 judgements: the newest 100, and how to get more or fewer.
    code, out = qualm("review", "--since", "2026-09-22")
    assert (out["matching"], out["shown"], out["truncated"]) == (250, 100, True)
    assert out["judgements"][-1]["id"] == "s249"
    assert out["note"] == "the newest 100 of 250 matching since 2026-09-22; --last N shows more, --rule R or --acted narrows it"
    # Only what wasn't asked for: --since given, --last cut it.
    note = qualm("review", "--since", "2026-09-22", "--last", "50", "--rule", "social")[1]["note"]
    assert "--since" not in note and "--rule" not in note and "--last N shows more" in note
    code, out = qualm("review")
    assert (out["shown"], out["truncated"]) == (30, True) and "--since DATE only those from then" in out["note"]
    code, out = qualm("review", "--since", "2026-09-22", "--last", "300")
    assert (out["shown"], out["truncated"]) == (250, False) and "note" not in out


def test_review_misses_leave_out_what_was_let_through_by_design(tmp_path, qualm):
    let = lambda jid, reason: screen(jid, jid, f"https://reddit.com/{jid}", p_hit={"social": 0.5},
                                     decisions=[{"action": "allow", "rule": "social", "reason": reason}])
    write(tmp_path, let("sess", "your session until 20:15, for “catch up”"), let("purp", "opened on purpose"),
          let("shop", "shopping is never flagged"))
    assert qualm("rules", "label", "social", "--yes", "sess", "purp", "shop")[0] == 0
    assert [(j["id"], j["missed"]) for j in qualm("review", "--misses")[1]["judgements"]] == [("shop", ["social"])]


def test_tune_leaves_out_answers_from_outside_the_rules_hours(tmp_path, qualm):
    evening = "2026-09-21T21:00:00"
    yes = [screen(f"y{i}", f"Feed {i} : Reddit", f"https://www.reddit.com/r/{i}", p_hit={"social": 0.5}) for i in range(3)]
    no = [screen(f"n{i}", f"Docs {i}", f"https://example.com/{i}", p_hit={"social": 0.05}) for i in range(3)]
    late = [screen(f"e{i}", f"Late {i} : Reddit", f"https://www.reddit.com/r/e{i}", p_hit={"social": 0.5}, at=evening)
            for i in range(3)]
    write(tmp_path, *yes, *no, *late)
    ids = lambda xs: [s["id"] for s in xs]
    assert qualm("rules", "label", "social", "--yes", *ids(yes), *ids(late), "--no", *ids(no))[0] == 0
    assert qualm("rules", "set", "social", "when=20:00-24:00")[0] == 0
    t = qualm("rules", "tune", "social")[1]
    # At 10:00 no threshold changes what it does, so those answers can't say which one is right.
    assert (t["yes"], t["no"], t["dropped"]["outside_hours"]) == (3, 0, 6) and t["recall"] == 1.0
    text = qualm("rules", "tune", "social", json_out=False)[1]
    assert "6 from outside its hours (20:00-24:00)" in text and "more screens from inside its hours" in text


def test_an_evening_rules_daytime_test_asks_for_no_answers_tune_leaves_out(tmp_path, qualm):
    write(tmp_path, *[screen(f"d{i}", f"Feed {i} : Reddit", f"https://www.reddit.com/r/{i}") for i in range(4)])
    assert qualm("rules", "add", "eve", "--what", "reading social media and news", "--when", "20:00-24:00")[0] == 0
    code, out = qualm("rules", "test", "eve")
    assert code == 0 and out["outside_hours"] == 4
    text = qualm("rules", "test", "eve", json_out=False)[1]
    assert "All of these are from outside its hours (20:00-24:00)" in text and "rules label" not in text
    # Some inside, some out: label, and only those from inside.
    write(tmp_path, *[screen(f"d{i}", f"Feed {i} : Reddit", f"https://www.reddit.com/r/{i}") for i in range(4)],
          screen("e1", "Late : Reddit", "https://www.reddit.com/r/late", at="2026-09-21T21:00:00"))
    text = qualm("rules", "test", "eve", json_out=False)[1]
    assert "qualm rules label eve --yes" in text and "tune leaves out the 4 from outside them" in text


def test_tune_with_too_few_answers_says_so_and_nothing_else(tmp_path, qualm):
    yes = [screen(f"y{i}", f"Feed {i}", f"https://www.reddit.com/r/{i}", p_hit={"social": 0.5}) for i in range(2)]
    no = [screen(f"n{i}", f"Docs {i}", f"https://example.com/{i}", p_hit={"social": 0.05}) for i in range(5)]
    write(tmp_path, *yes, *no)
    assert qualm("rules", "label", "social", "--yes", *[s["id"] for s in yes], "--no", *[s["id"] for s in no])[0] == 0
    t = qualm("rules", "tune", "social")[1]
    assert (t["precision"], t["recall"], t["suggested"], t["enough_answers"]) == (1.0, 1.0, None, False)
    text = qualm("rules", "tune", "social", json_out=False)[1]
    assert "too few answers to tune it: it needs at least 3 yes and 3 no" in text and "no threshold reaches" not in text


def test_tune_keeps_a_threshold_that_is_right_whatever_it_is(tmp_path, qualm):
    yes = [screen(f"y{i}", f"Feed {i}", f"https://www.reddit.com/r/{i}", p_hit={"social": 0.5}) for i in range(3)]
    no = [screen(f"n{i}", f"News {i}", f"https://cnn.com/{i}", p_hit={"social": 0.5}) for i in range(3)]
    write(tmp_path, *yes, *no)
    assert qualm("rules", "set", "social", "sites+=reddit.com")[0] == 0
    assert qualm("never", "add", "--site", "cnn.com")[0] == 0
    assert qualm("rules", "label", "social", "--yes", *[s["id"] for s in yes], "--no", *[s["id"] for s in no])[0] == 0
    t = qualm("rules", "tune", "social")[1]
    assert (t["precision"], t["recall"], t["suggested"], t["keep_current"]) == (1.0, 1.0, None, True)
    assert "no threshold reaches" not in qualm("rules", "tune", "social", json_out=False)[1]


def test_a_rules_test_cut_short_keeps_what_the_model_scored(tmp_path, qualm):
    write(tmp_path, *[screen(f"s{i}", f"Page {i}", f"https://example.com/{i}") for i in range(5)])

    class Tired(Fake):
        def system_one(self, state, questions):
            self.down = len(self.asked) == 2
            return super().system_one(state, questions)

    qualm.state["fake"] = Tired()
    code, out = qualm("rules", "test", "social")
    assert code == 5 and out["complete"] is False and [x["id"] for x in out["screens"]] == ["s4", "s3"]
    assert "stopped answering after 2 of 5 screens" in out["error"]["message"]
    assert "running" not in out["error"]["message"]  # it answered: the app isn't the problem
    qualm.state["fake"] = Tired()
    assert qualm("allow", "test", "shopping")[1]["error"]["message"].startswith("the model on this Mac stopped")
    # Down from the start: the scores from before, and not "stopped after 2" but how to check it.
    qualm.state["fake"] = Fake(down=True)
    for cmd in (("rules", "test", "social"), ("allow", "test", "shopping")):
        code, out = qualm(*cmd)
        message = out["error"]["message"]
        assert code == 5 and out["complete"] is False, cmd
        assert "didn't answer" in message and "is Qualm running?" in message and " status`" in message
        assert "stopped answering" not in message and "run the same command again" not in message
    assert [x["id"] for x in qualm("rules", "test", "social")[1]["screens"]] == ["s4", "s3"]
    assert "not complete: the model on this Mac didn't answer" in qualm("rules", "test", "social", json_out=False)[1]
    qualm.state["fake"] = Fake()
    code, out = qualm("rules", "test", "social")
    assert code == 0 and out["complete"] and len(out["screens"]) == 5 and len(qualm.state["fake"].asked) == 3


def test_a_torn_usage_file_is_skipped_and_named(tmp_path, qualm, capsys):
    write(tmp_path, screen("s1", "A thread : Reddit", "https://www.reddit.com/r/a/1"))
    usage = tmp_path / "data" / "usage.json"
    usage.write_text('{"day": "2026-09-24", "coun')
    assert qualm("rules", "test", "social")[0] == 0 and qualm("rules", "tune", "social")[0] == 0
    from qualm.policy import Usage

    assert Usage(usage).counts == {} and str(usage) in capsys.readouterr().err


def test_overrides_allow_only_where_the_rules_words_name_a_class(tmp_path, qualm):
    for id, what, want in (("vids", "watching entertainment videos, but not music or chat", []),
                           ("feeds2", "social feeds, except shopping; no chats", []),
                           ("discord", "chatting on Discord about games", ["chat"]),
                           ("einkaufen", "Online-Einkaufen: Produktseiten, Warenkorb und Kasse", [])):
        code, out = qualm("rules", "add", id, "--what", what)
        assert out["overrides_allow_added"] == want, id
        # Every class that can still cancel it, for an agent to judge in any language.
        assert [c["id"] for c in out["can_be_cancelled_by"]] == [c for c in ("shopping", "music", "chat") if c not in want]
        assert f"rules set {id} overrides_allow+=" in out["overrides_allow_note"]
    # English wording added to a rule in another language is read too.
    code, out = qualm("rules", "set", "einkaufen", "description_en=online shopping: product pages and carts")
    assert out["overrides_allow_added"] == ["shopping"] and out["rule"]["overrides_allow"] == ["shopping"]
    assert [c["id"] for c in out["can_be_cancelled_by"]] == ["music", "chat"]
    assert "can_be_cancelled_by" not in qualm("rules", "set", "einkaufen", "threshold=0.3")[1]


def test_words_that_leave_a_class_out_never_make_a_rule_override_it(tmp_path, qualm, monkeypatch):
    from qualm.personalize import overlaps
    from qualm.rules import load_config

    settings, _ = load_config(tmp_path / "rules.toml")
    for what in ("watching entertainment videos other than music or chat", "anything but chat", "non-chat apps",
                 "videos, unless it is chat", "videos instead of chat", "videos rather than music",
                 "social media (but chat is fine)", "videos, chat excluded", "videos except for music, and chat",
                 "online shopping, apart from groceries", "videos besides music", "刷短视频，不包括 chat"):
        assert overlaps(what, settings) == [], what
    # Set nothing, and say which classes the words name, with the command, for the user to choose.
    q = "QUALM_HOME=/tmp/second uv run --project /src/qualm qualm"  # as this terminal runs it: another Qualm folder
    monkeypatch.setattr(agent, "command", lambda: q)
    code, out = qualm("rules", "add", "vids2", "--what", "watching entertainment videos other than music or chat",
                      "--check-in")
    assert out["overrides_allow_added"] == [] and out["added"]["overrides_allow"] == []
    assert out["may_cancel"] == [{"id": c, "command": f"{q} rules set vids2 overrides_allow+={c}"} for c in ("music", "chat")]
    # The chat a friend shared a video in stays a chat: the chat allow class lets it through.
    write(tmp_path, screen("w1", "Weixin", bundle="com.tencent.xinWeChat", allow={"chat": 0.8}))
    qualm.state["fake"] = Fake(scores={"Weixin": 0.36})
    row = qualm("rules", "test", "vids2")[1]["screens"][0]
    assert (row["does"], row["why"], row["overrides"]) == ("allow", "chat is never flagged", [])
    text = qualm("rules", "test", "vids2", json_out=False)[1]
    assert f"{q} rules label vids2 --yes" in text and f"{q} rules tune vids2 --apply" in text
    # A rewording says it too; one that only names a class still overrides it.
    text = qualm("rules", "set", "vids2", "what=online shopping, not groceries", json_out=False)[1]
    assert "(“not”)" in text and f"`{q} rules set vids2 overrides_allow+=shopping`" in text
    code, out = qualm("rules", "set", "vids2", "what=online shopping and chats")
    assert out["may_cancel"] == [] and out["overrides_allow_added"] == ["shopping", "chat"]
    # A "not" in the user's own words counts for the English the model reads, whichever one is new.
    code, out = qualm("rules", "add", "wanggou", "--what", "网购，不包括买书", "description_en=online shopping")
    assert out["added"]["overrides_allow"] == [] and [m["id"] for m in out["may_cancel"]] == ["shopping"]
    code, out = qualm("rules", "set", "wanggou", "description_en=online shopping and music")
    assert out["overrides_allow_added"] == [] and [m["id"] for m in out["may_cancel"]] == ["shopping", "music"]


def test_an_allow_class_a_rule_overrides_can_go_without_breaking_the_file(tmp_path, qualm):
    import re

    rules = tmp_path / "rules.toml"
    assert qualm("rules", "add", "limit_shopping", "--what", "online shopping")[0] == 0
    fix = "rules set limit_shopping overrides_allow-=shopping"
    code, out = qualm("allow", "remove", "shopping")
    assert code == 2 and f"qualm {fix}`" in out["error"]["message"]
    # Taken out by hand: the file still loads (there's nothing to override), and the fix it named works.
    rules.write_text("".join(b for b in re.split(r"(?m)^(?=\[\[)", rules.read_text())
                             if not (b.startswith("[[allow]]") and 'id = "shopping"' in b)))
    assert qualm("rules", "list")[0] == 0
    assert "allow class" not in qualm("rules", "show", "limit_shopping")[1]["summary"]
    assert qualm(*fix.split())[0] == 0
    # A class that isn't there is refused as a mistake in the change.
    code, out = qualm("rules", "set", "social", "overrides_allow+=chats")
    assert code == 2 and out["error"]["message"] == ("rule 'social': overrides_allow chats: there's no allow class "
                                                     "'chats' (there are: music, chat)")


def test_an_allow_class_cant_take_a_rules_id_and_is_told_why(tmp_path, qualm):
    assert qualm("rules", "add", "games", "--what", "playing video games")[0] == 0
    code, out = qualm("allow", "add", "games", "--what", "a video game or game launcher")
    assert code == 2 and "is the name of a rule" in out["error"]["message"]
    assert "share their ids" in out["error"]["message"] and "games_ok" in out["error"]["message"]
    assert qualm("allow", "add", "games_ok", "--what", "a video game or game launcher")[0] == 0
