"""The policy, with made-up readings: no model, no screen."""

import json
import re
import time

import pytest

from qualm.config import EXAMPLE
from qualm.decide import Reading, RuleVerdict
from qualm.policy import Decision, Policy
from qualm.rules import Rule, Settings, load_config

RULES = [
    Rule("shortvideo", "deny", "short videos", threshold=0.15, patterns=(r"youtube\.com/shorts/",), allow_intentional=True),
    Rule("feeds", "deny", "feeds", threshold=0.5, target="page", feed_hit=True),
    Rule("livestream", "deny", "live", threshold=0.5, allow_learning=True),
    Rule("social", "check_in", "social", threshold=0.2, allow_intentional=True),
]
SETTINGS = Settings(no_monitor=("com.1password.1password",), allow_urls=(r"^https://docs\.",))


def reading(page="single_item", purpose="entertain", sensitive=0.0, **p_hit):
    return Reading(
        sensitive=sensitive, page_kind=page, page_probs={page: 0.9}, purpose=purpose,
        purpose_probs={purpose: 0.9}, latency_ms=1.0,
        rules=[RuleVerdict(r.id, "", p_hit.get(r.id, 0.0), {}) for r in RULES],
    )


@pytest.fixture
def policy(tmp_path):
    return Policy(SETTINGS, RULES, tmp_path)


def actions(decisions):
    return [(d.action, d.rule) for d in decisions]


def see(policy, url, r, app="com.apple.Safari"):
    return actions(policy.decide({"url": url, "app": "Safari"}, r, app))


def test_low_p_hit_is_nothing(policy):
    assert see(policy, "https://a.com/x", reading(shortvideo=0.1)) == []


def test_threshold_hit_intervenes(policy):
    assert see(policy, "https://a.com/x", reading(shortvideo=0.2)) == [("intervene", "shortvideo")]


def test_pattern_hits_even_when_model_misses(policy):
    assert see(policy, "https://www.youtube.com/shorts/abc", reading(shortvideo=0.01)) == [("intervene", "shortvideo")]


def test_sensitive_page_is_never_judged(policy):
    assert see(policy, "https://bank.com", reading(sensitive=0.9, shortvideo=0.99)) == [("skip", "")]


def test_content_rule_ignores_feeds_but_page_rule_does_not(policy):
    got = see(policy, "https://a.com/", reading(page="feed", shortvideo=0.9, feeds=0.9))
    assert got == [("intervene", "feeds")]


def test_learning_exempts_only_rules_that_allow_it(policy):
    got = see(policy, "https://live.com/talk", reading(purpose="learn", livestream=0.9, shortvideo=0.9))
    assert ("allow", "livestream") in got and ("intervene", "shortvideo") in got


def test_opened_from_search_is_intentional_but_the_next_one_is_not(policy):
    see(policy, "https://google.com/search?q=x", reading(page="search", purpose="task"))
    assert see(policy, "https://clips.example/v/a", reading(shortvideo=0.9)) == [("allow", "shortvideo")]
    # Swiping on to the next one: drift, not intent.
    assert see(policy, "https://clips.example/v/b", reading(shortvideo=0.9)) == [("intervene", "shortvideo")]


def test_the_rules_own_sites_count_even_when_opened_on_purpose(policy):
    see(policy, "https://google.com/search?q=x", reading(page="search", purpose="task"))
    assert see(policy, "https://www.youtube.com/shorts/a", reading()) == [("intervene", "shortvideo")]


def test_link_from_chat_app_is_intentional(policy):
    see(policy, "https://mail.example/inbox", reading(page="work", purpose="task"))  # the browser, earlier
    see(policy, "", reading(page="single_item", purpose="task"), app="com.tinyspeck.slackmacgap")
    assert see(policy, "https://clips.example/v/a", reading(shortvideo=0.9)) == [("allow", "shortvideo")]


def test_switching_back_to_a_page_already_open_is_not_opening_it(policy):
    # Reels in the browser, then the editor, then Cmd-Tab back to the same
    # tab: the editor didn't open it (a running day, 2026-09-23).
    assert see(policy, "https://clips.example/v/a", reading(shortvideo=0.9)) == [("intervene", "shortvideo")]
    see(policy, "", reading(page="work", purpose="task"), app="com.microsoft.VSCode")
    assert see(policy, "https://clips.example/v/a", reading(shortvideo=0.9)) == [("intervene", "shortvideo")]
    # An app with no address shows no sign of a link followed: switching to it isn't one.
    see(policy, "", reading(page="work", purpose="task"), app="com.microsoft.VSCode")
    got = policy.decide({"app": "Instagram", "window_title": "Instagram", "visible_text": ["reel"]},
                        reading(shortvideo=0.9), "com.burbn.instagram")
    assert actions(got) == [("intervene", "shortvideo")]
    # A new page in a browser seen before, from the editor: a link followed.
    see(policy, "", reading(page="work", purpose="task"), app="com.microsoft.VSCode")
    assert see(policy, "https://clips.example/v/b", reading(shortvideo=0.9)) == [("allow", "shortvideo")]


def test_link_from_an_allowed_url_is_intentional(policy):
    assert policy.precheck("com.apple.Safari", "https://docs.python.org/3/").action == "allow"
    assert see(policy, "https://clips.example/v/a", reading(shortvideo=0.9)) == [("allow", "shortvideo")]


def test_precheck_skips_unmonitored_apps_and_pause(policy):
    assert policy.precheck("com.1password.1password", "").action == "skip"
    assert policy.precheck("com.apple.Safari", "https://a.com") is None
    policy.pause(5)
    assert policy.precheck("com.apple.Safari", "https://a.com").reason == "paused"


def test_a_focus_session_makes_check_ins_step_in_and_is_logged(policy, tmp_path):
    from qualm.policy import end_focus, start_focus

    assert policy.decide({"url": "https://weibo.com/1"}, reading(social=0.5))[0].panel == "check_in"
    start_focus(tmp_path, "write the report", 50)
    policy.reload_session()
    assert policy.focusing()["intent"] == "write the report"
    d = policy.decide({"url": "https://weibo.com/2"}, reading(social=0.5))
    assert actions(d) == [("intervene", "social")] and d[0].panel == ""
    jid = policy.log_judgement({"app": "Safari"}, None, [])
    logged = [json.loads(line) for line in (tmp_path / "judgements.jsonl").open()]
    assert logged[-1]["id"] == jid and logged[-1]["focus"] == "write the report"
    end_focus(tmp_path)
    policy.reload_session()
    assert policy.focusing() is None
    kinds = [json.loads(line)["type"] for line in (tmp_path / "decisions.jsonl").open()]
    assert kinds == ["focus", "focus_end"]


def test_an_expired_focus_or_pause_is_gone(tmp_path):
    from qualm.policy import read_session, write_session

    write_session(tmp_path, paused_until=1.0, focus={"intent": "x", "started": 0.0, "until": 1.0})
    assert read_session(tmp_path) == {"paused_until": 0.0, "pause_later": None, "focus": None}


def test_focus_needs_words_and_a_sane_length(tmp_path):
    from qualm.policy import start_focus

    with pytest.raises(ValueError):
        start_focus(tmp_path, "   ", 30)
    with pytest.raises(ValueError):
        start_focus(tmp_path, "read", 0)


def decide(policy, url, r):
    return policy.decide({"url": url, "app": "Safari"}, r, "com.apple.Safari")


def test_check_in_session_then_times_up_then_one_extension(policy, tmp_path):
    d = decide(policy, "https://weibo.com/1", reading(social=0.5))
    assert actions(d) == [("intervene", "social")] and d[0].panel == "check_in"
    c = policy.start_session("social", 5, "reply to a friend", d[0].id)
    assert policy.rejudge_due()  # the page you're on is in the session now
    # The session covers the rule, on any site.
    for url in ("https://weibo.com/1", "https://x.com/home"):
        d = decide(policy, url, reading(social=0.5))
        assert actions(d) == [("allow", "social")] and "reply to a friend" in d[0].reason
    now = time.time()
    policy.tick(now)
    policy.tick(now + 5)
    assert policy.usage.get("social") == (5.0, 1) and c.seconds == 5.0
    # Time's up while you're on it: judged again, and it says so.
    c.until = now - 1
    policy.tick(now + 10)
    assert policy.rejudge_due(now + 10)
    d = decide(policy, "https://weibo.com/1", reading(social=0.5))
    assert d[0].panel == "times_up" and d[0].reason == "your 5 minutes for “reply to a friend” are up"
    policy.tick(now + 15)
    assert "social" in policy.sessions  # kept while "time's up" waits for your answer
    assert policy.extend_session("social", d[0].id) is not None
    assert actions(decide(policy, "https://weibo.com/1", reading(social=0.5))) == [("allow", "social")]
    c.until = time.time() - 1
    d = decide(policy, "https://weibo.com/1", reading(social=0.5))
    assert "5 more minutes" in d[0].reason
    assert policy.extend_session("social", d[0].id) is None  # one extension per session (settings.extensions)
    policy.end_session("social", "done", d[0].id)
    assert decide(policy, "https://weibo.com/1", reading(social=0.5))[0].panel == "check_in"
    events = [json.loads(line) for line in (tmp_path / "decisions.jsonl").open()]
    assert [e.get("event") for e in events if e["type"] == "session"] == ["start", "extend", "end"]
    assert [e["response"] for e in events if e["type"] == "response"] == ["session", "snooze", "back"]
    end = events[-2]
    assert end["how"] == "done" and end["minutes"] == 10 and end["extended"] == 1


def test_the_check_in_wait_grows_with_sessions_today_and_coming_back_soon(policy):
    assert policy.check_in_wait(5) == 0  # the first session of the day is free
    assert policy.check_in_wait(15) == 5 and policy.check_in_wait(30) == 10  # longer costs a little
    policy.start_session("social", 5, "a")
    assert policy.check_in_wait(5) == 5
    policy.end_session("social", "done")
    assert policy.check_in_wait(5) == 10  # back within 20 minutes: doubled
    policy.last_end = time.time() - 25 * 60
    assert policy.check_in_wait(5) == 5
    for _ in range(6):
        policy.start_session("social", 5, "a")
    assert policy.check_in_wait(30) == 60  # max_wait_s


def test_a_session_that_runs_out_while_you_are_elsewhere_just_ends(policy, tmp_path):
    c = policy.start_session("social", 5, "a")
    decide(policy, "https://weibo.com/1", reading(social=0.5))
    decide(policy, "https://docs.example/x", reading(page="work", purpose="task"))
    c.until = time.time() - 1
    policy.tick()
    assert "social" not in policy.sessions
    end = [json.loads(line) for line in (tmp_path / "decisions.jsonl").open()][-1]
    assert end["event"] == "end" and end["how"] == "time"


def test_a_running_session_and_the_wait_survive_a_restart(policy, tmp_path):
    policy.start_session("social", 15, "the match")
    again = Policy(SETTINGS, RULES, tmp_path)
    assert actions(decide(again, "https://weibo.com/1", reading(social=0.5))) == [("allow", "social")]
    assert again.check_in_wait(5) == 5


def test_old_budget_files_say_how_to_migrate(tmp_path):
    from qualm.config import Config
    from qualm.rules import parse_config

    old = ('[settings]\n# false while testing\nbudgets = false\nlang = "en"\n\n'
           '[[rules]]\nid = "social"\nkind = "time_cap"\ndescription = "social"\nminutes_per_day = 20\n'
           '# on purpose is fine\nallow_intentional = true\n')
    with pytest.raises(ValueError, match="config migrate"):
        parse_config(old)
    path = tmp_path / "rules.toml"
    path.write_text(old)
    assert Config(path).migrate() == ["settings: budgets removed", "social: kind check_in, minutes_per_day removed"]
    text = path.read_text()
    assert "budgets" not in text and "false while testing" not in text and "# on purpose is fine" in text
    assert parse_config(text)[1][0].kind == "check_in"


def test_snooze_allows_the_rule_for_a_while(policy):
    policy.snooze("shortvideo", 10, "research for a talk")
    assert see(policy, "https://a.com/x", reading(shortvideo=0.9)) == [("allow", "shortvideo")]


def test_not_this_one_allows_the_url_and_adds_nothing_the_model_reads(policy, tmp_path):
    policy.mark_fine("shortvideo", "https://a.com/x", "How to fix a bike chain")
    assert see(policy, "https://a.com/x", reading(shortvideo=0.9)) == [("allow", "shortvideo")]
    assert see(policy, "https://a.com/y", reading(shortvideo=0.9)) == [("intervene", "shortvideo")]
    # The title as words raised other pages' scores (YouTube's home page 0.20 -> 0.34 on social).
    assert next(r for r in policy.rules if r.id == "shortvideo").exceptions == ()
    # It survives a restart.
    again = Policy(SETTINGS, RULES, tmp_path)
    assert see(again, "https://a.com/x", reading(shortvideo=0.9)) == [("allow", "shortvideo")]
    # Counted per site, for the pop-up's "Change rules with your AI agent" hint: Short after Short adds up.
    assert again.mark_fine("shortvideo", "https://www.a.com/y", "Another one") == 2
    assert again.mark_fine("shortvideo", "https://b.com/y", "Elsewhere") == 1


def test_word_exceptions_already_saved_still_count(policy, tmp_path):
    with (tmp_path / "exceptions.jsonl").open("a") as f:
        f.write(json.dumps({"rule": "social", "url": "", "title": "Weixin"}) + "\n")  # before score bars
        f.write(json.dumps({"rule": "social", "text": "a lecture on YouTube"}) + "\n")  # typed on the review page
        f.write(json.dumps({"rule": "social", "url": "https://x.com/a/status/1", "title": "Launch day"}) + "\n")
    policy.reload_exceptions()
    assert next(r for r in policy.rules if r.id == "social").exceptions == ('the page "Weixin"', "a lecture on YouTube")


def test_not_this_one_in_an_app_lets_that_window_through_below_its_score(policy, tmp_path):
    chats = {"app": "WeChat", "window_title": "Weixin", "visible_text": ["File Transfer", "wow"]}
    viewer = chats | {"window_title": "Photos and Videos"}
    wechat = "com.tencent.xinWeChat"
    assert policy.mark_fine("feeds", "", "Weixin", bundle_id=wechat, p_hit=0.55) == 1
    assert actions(policy.decide(chats, reading(feeds=0.6), wechat)) == [("allow", "feeds")]
    assert actions(policy.decide(chats, reading(feeds=0.9), wechat)) == [("intervene", "feeds")]  # well above it
    assert actions(policy.decide(viewer, reading(feeds=0.6), wechat)) == [("intervene", "feeds")]  # another window
    assert actions(policy.decide(chats, reading(feeds=0.6), "com.other")) == [("intervene", "feeds")]  # another app
    # Not read as words: an app's name in an exception made the model flag it more.
    assert next(r for r in policy.rules if r.id == "feeds").exceptions == ()
    assert policy.mark_fine("feeds", "", "Weixin", bundle_id=wechat, p_hit=0.7) == 2  # the bar only rises
    assert actions(Policy(SETTINGS, RULES, tmp_path).decide(chats, reading(feeds=0.75), wechat)) == [("allow", "feeds")]
    # `except remove feeds Weixin` undoes it.
    with (tmp_path / "exceptions.jsonl").open("a") as f:
        f.write(json.dumps({"rule": "feeds", "title": "Weixin", "removed": True}) + "\n")
    policy.reload_exceptions()
    assert actions(policy.decide(chats, reading(feeds=0.6), wechat)) == [("intervene", "feeds")]


def test_interventions_and_responses_are_logged(policy, tmp_path):
    d = policy.decide({"url": "https://a.com/x"}, reading(shortvideo=0.9))[0]
    policy.log_intervention(d, {"app": "Safari"}, reading(shortvideo=0.9))
    policy.log_response(d.id, "back", d.rule)
    events = [json.loads(line) for line in (tmp_path / "decisions.jsonl").open()]
    assert [e["type"] for e in events] == ["intervention", "response"] and events[0]["id"] == events[1]["id"]


def test_example_rules_load():
    settings, rules = load_config(EXAMPLE)
    assert settings.lang == "en" and {r.id for r in rules} >= {"shortvideo", "feeds", "social"}
    assert all(0 < r.threshold < 1 for r in rules)


def test_starter_patterns_catch_channels_and_home_feeds_but_not_site_pages():
    # A fresh user's audit: Twitch channels opened with ?sr=a or ?referrer=raid were missed,
    # /downloads and /jobs popped up, and no Western home feed was on the list. Kick's own
    # pages (its sitemap and scripts) aren't channels; any other single name there is one.
    by_id = {r.id: r for r in load_config(EXAMPLE)[1]}
    hits = {"livestream": ["https://www.twitch.tv/speedrunner_live", "https://www.twitch.tv/speedrunner_live?referrer=raid",
                           "https://m.twitch.tv/xqcow", "https://kick.com/xqc?ref=home", "https://kick.com/adinross/"],
            "feeds": ["https://www.threads.com/", "https://www.reddit.com/?feed=home", "https://m.youtube.com/",
                      "https://www.facebook.com/watch/?ref=tab"],
            "shortvideo": ["https://www.facebook.com/reel/1203948576234512"]}
    misses = {"livestream": ["https://www.twitch.tv/downloads", "https://www.twitch.tv/jobs", "https://www.twitch.tv/videos/2245012345",
                             "https://www.twitch.tv/directory/following", "https://www.twitch.tv/tsoding/videos",
                             "https://kick.com/categories/just-chatting", "https://www.sidekick.com/pricing",
                             "https://kick.com/login", "https://kick.com/events", "https://kick.com/drops",
                             "https://kick.com/advertising-policy", "https://kick.com/xqc/videos"],
              "feeds": ["https://www.facebook.com/watch/?v=123456", "https://www.facebook.com/marketplace/",
                        "https://www.instagram.com/direct/inbox/", "https://www.reddit.com/r/learnpython/comments/1/x/"]}
    for rid, urls in hits.items():
        assert [u for u in urls if not by_id[rid].matches_url(u)] == [], rid
    for rid, urls in misses.items():
        assert [u for u in urls if by_id[rid].matches_url(u)] == [], rid


def test_a_login_page_never_pops_up_on_the_starters_while_the_model_is_down(tmp_path):
    # Logged out, Facebook's and Instagram's home pages are their login forms. As feeds sites
    # they stepped in with no model: a pop-up over a password field while the model downloaded
    # or timed out. With the model up, it finds their feeds (and the login form).
    from qualm.watcher import NO_READING

    p = Policy(*load_config(EXAMPLE), tmp_path)
    for url, title in (("https://www.facebook.com/", "Facebook – log in or sign up"), ("https://www.instagram.com/", "Instagram")):
        login = {"app": "Google Chrome", "window_title": title, "url": url, "visible_text": ["Password", "Log in"]}
        assert p.precheck("com.google.Chrome", url, title, "Google Chrome") is None
        assert p.decide(login, NO_READING, "com.google.Chrome") == []
    home = {"app": "Google Chrome", "window_title": "Threads", "url": "https://www.threads.com/"}
    assert actions(p.decide(home, NO_READING, "com.google.Chrome")) == [("intervene", "feeds")]


def test_starter_descriptions_name_sites_as_examples_not_as_their_scope():
    # Worded "entertainment videos on Bilibili or YouTube", the videos rule kept Netflix,
    # Prime Video and Vimeo at 0.12-0.15 on Kev-4B; without the names they scored 0.50-0.67.
    for r in load_config(EXAMPLE)[1]:
        assert not re.search(r"\bon [A-Z]", r.description.split("such as", 1)[0]), r.id


def test_entertainment_feed_hits_a_feed_rule_on_any_site(policy):
    assert see(policy, "https://new-site.example/", reading(page="feed", feeds=0.0)) == [("intervene", "feeds")]
    assert see(policy, "https://news.example/", reading(page="feed", purpose="task", feeds=0.0)) == []


def test_a_feed_only_barely_for_entertainment_is_not_an_entertainment_feed(policy):
    # Apple Ads' "recommendations" page, in real use: feed 0.39, entertain 0.41.
    r = reading(page="feed", feeds=0.0)
    r.purpose_probs = {"entertain": 0.41, "task": 0.37, "learn": 0.22}
    assert see(policy, "https://app-ads.apple.com/cm/app/1/recommendations", r) == []


def test_scrolling_a_feed_needs_a_check_in(policy):
    assert ("intervene", "social") in see(policy, "https://weibo.com/", reading(page="feed", social=0.5))


def test_every_judgement_is_logged_but_private_ones_without_content(policy, tmp_path):
    screen = {"app": "Safari", "bundle_id": "com.apple.Safari", "window_title": "t", "url": "https://a.com", "text": ["x"]}
    r = reading(shortvideo=0.9)
    policy.log_judgement(screen, r, policy.decide(screen, r))
    s = reading(sensitive=0.9)
    policy.log_judgement({**screen, "url": "https://www.bank.com/login?u=me"}, s, policy.decide(screen, s), shot="x.jpg")
    logged = [json.loads(line) for line in (tmp_path / "judgements.jsonl").open()]
    assert logged[0]["screen"]["text"] == ["x"] and logged[0]["p_hit"]["shortvideo"] == 0.9
    # Private: the site and the score, for audits; never the title, the text, the path or a screenshot.
    assert logged[1]["screen"] == {"app": "Safari", "bundle_id": "com.apple.Safari", "site": "bank.com"}
    assert logged[1]["sensitive"] == 0.9 and "p_hit" not in logged[1] and "shot" not in logged[1]


def test_review_answers_and_tuning(tmp_path):
    from qualm.review import rule_answers, save_review, set_threshold, tuning

    j = {"id": "a", "p_hit": {"shortvideo": 0.9, "social": 0.1, "stocks": 0.5},
         "decisions": [{"action": "intervene", "rule": "shortvideo", "reason": ""},
                       {"action": "allow", "rule": "stocks", "reason": "opened on purpose"}]}
    # "Right" confirms the pop-up and every quiet rule; the exempted one stays open.
    save_review(tmp_path, "a", verdict="right")
    from qualm.review import load_reviews
    assert rule_answers(j, load_reviews(tmp_path)["a"]) == {"shortvideo": True, "social": False}
    save_review(tmp_path, "a", rules={"social": "yes"})
    assert rule_answers(j, load_reviews(tmp_path)["a"])["social"] is True

    # Threshold suggestions need 3 yes and 3 no.
    lines = []
    for i, (p, y) in enumerate([(0.9, "yes"), (0.7, "yes"), (0.4, "yes"), (0.3, "no"), (0.1, "no"), (0.05, "no")]):
        lines.append(json.dumps({"id": f"j{i}", "at": "2026-09-23T20:00:00", "screen": {"url": f"https://x.com/{i}"},
                                 "state": {"url": f"https://x.com/{i}", "visible_text": ["x"]}, "page_kind": "single_item",
                                 "p_hit": {"social": p}, "decisions": []}))
        save_review(tmp_path, f"j{i}", rules={"social": y})
    (tmp_path / "judgements.jsonl").write_text("\n".join(lines) + "\n")
    row = next(t for t in tuning(tmp_path, RULES) if t["rule"] == "social")
    assert row["yes"] == 3 and row["no"] == 3 and row["suggested"] == 0.35 and row["suggested_recall"] == 1.0

    rules = tmp_path / "rules.toml"
    rules.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    set_threshold(rules, "social", 0.35)
    assert next(r for r in load_config(rules)[1] if r.id == "social").threshold == 0.35
    assert next(r for r in load_config(rules)[1] if r.id == "videos").threshold == 0.25


def test_a_screen_with_nothing_but_its_name_does_not_pop_up(policy):
    got = policy.decide({"app": "WhatsApp", "window_title": "WhatsApp"}, reading(social=0.9), "net.whatsapp.WhatsApp")
    assert actions(got) == [("skip", "social")] and "too little on screen" in got[0].reason


def test_never_here_for_an_app_and_a_site(policy, tmp_path):
    assert policy.never_here("net.whatsapp.WhatsApp", "WhatsApp", "") == "WhatsApp"
    assert policy.precheck("net.whatsapp.WhatsApp", "").reason == "you said never here"
    policy.never_here("com.apple.Safari", "Safari", "https://www.example.com/a/b")
    assert policy.precheck("com.apple.Safari", "https://example.com/other").action == "allow"
    assert policy.precheck("com.apple.Safari", "https://other.com/") is None
    assert Policy(SETTINGS, RULES, tmp_path).precheck("net.whatsapp.WhatsApp", "") is not None  # survives restart


def test_an_allow_class_stops_every_rule_but_not_a_url_pattern(tmp_path):
    from qualm.rules import AllowClass

    p = Policy(Settings(allow=(AllowClass("shopping", "an online store", threshold=0.35),)), RULES, tmp_path)
    shop = reading(social=0.9)
    shop.allow = {"shopping": 0.8}
    got = p.decide({"url": "https://store.example/polo"}, shop)
    assert actions(got) == [("allow", "social")] and got[0].reason == "shopping is never flagged"
    short = reading(shortvideo=0.9)
    short.allow = {"shopping": 0.8}
    assert see(p, "https://www.youtube.com/shorts/x", short) == [("intervene", "shortvideo")]


def test_explanations_in_words(policy):
    from qualm.explain import headline, label, reason
    from qualm.policy import Decision

    rule = RULES[0]  # shortvideo, threshold 0.15
    r = reading(shortvideo=0.9)
    # Rules by the name the menu uses, never the raw id.
    assert reason(Decision("intervene", "shortvideo", "p_hit 0.90 >= 0.15"), r, rule, "en").startswith("A clear match for your “Short videos” rule.")
    assert "A close call" in reason(Decision("intervene", "shortvideo", "x"), reading(shortvideo=0.16), rule, "en")
    assert reason(Decision("intervene", "shortvideo", "matches URL pattern"), r, rule, "en").startswith("This address is on your “Short videos” list.")
    assert "for entertainment" in reason(Decision("intervene", "shortvideo", "x"), r, rule, "en")
    long = Rule("shortvideo", "deny", "short videos made for endless swiping, such as Douyin, TikTok")
    assert label(long) == "short videos made for endless swiping"
    assert headline(Decision("intervene", "shortvideo", "x"), long, "en") == ("Short videos", "This looks like short videos made for endless swiping.")
    videos = Rule("videos", "check_in", "watching entertainment videos: comedy, gaming")
    assert headline(Decision("intervene", "videos", "x", "d", "check_in"), videos, "en") == ("Entertainment videos · check in", "What are you here for?")
    up = Decision("intervene", "videos", "your 15 minutes for “the match” are up", "d", "times_up")
    assert headline(up, videos, "en") == ("Entertainment videos · time's up", "Your 15 minutes for “the match” are up.")
    shopping = Rule("online_shopping", "check_in", "online shopping")
    assert reason(Decision("intervene", "online_shopping", "x"), None, shopping, "en") == "It matches your “Online shopping” rule."
    focus = {"intent": "write the report", "until": time.time() + 20 * 60 + 5}
    assert headline(Decision("intervene", "videos", "x"), videos, "en", focus) == ("Focus · 20 min left", "You're here to: write the report.")


def test_the_context_line_is_neutral_and_echoes_what_you_asked_for(policy):
    from qualm.explain import context

    assert context([]) == ""
    assert context(["2026-09-23T14:20:05"]) == "2nd time today · last at 14:20"
    assert context([], (time.time() - 60, 10, "a recipe")) == "Your 10 minutes for “a recipe” are up"
    policy.snooze("social", 10, "a recipe", "d1")
    assert policy.last_snooze("social")[1:] == (10, "a recipe")
    policy.log_intervention(Decision("intervene", "social", "x", "d2"), {}, reading())
    policy.log_intervention(Decision("intervene", "social", "x", "d3"), {}, reading())
    assert len(policy.popups_today("social", but="d3")) == 1


def test_snoozes_are_counted_for_the_growing_wait(policy):
    assert policy.snoozes_in_last_hour() == 0
    policy.snooze("shortvideo", 10, "a")
    policy.snooze("shortvideo", 10, "b")
    assert policy.snoozes_in_last_hour() == 2


def test_time_away_does_not_count(policy):
    policy.start_session("social", 15, "a")
    see(policy, "https://weibo.com/1", reading(social=0.5))
    policy.tick(0.0)
    policy.tick(5.0, away=True)
    policy.tick(10.0)
    assert policy.usage.get("social")[0] == 5.0


def test_own_review_page_is_never_judged_wherever_it_is_open(policy):
    assert policy.precheck("com.todesktop.230313mzl4w4u92", "vscode-file://x", "Qualm review — qualm").action == "skip"
    # Its title before the rename.
    assert policy.precheck("com.todesktop.230313mzl4w4u92", "vscode-file://x", "SeeNot review — seenot").action == "skip"


def test_a_work_tool_is_left_alone_even_when_unsure(policy):
    r = reading(page="work", purpose="task", shortvideo=0.9)
    r.page_probs = {"work": 0.4, "single_item": 0.3}
    assert see(policy, "vscode-file://x", r) == [("allow", "shortvideo")]


def test_a_known_music_site_or_app_is_allowed_without_the_model(tmp_path):
    from qualm.rules import AllowClass

    music = AllowClass("music", "a music player", patterns=(r"^https://music\.youtube\.com",), apps=("com.netease.163music",))
    p = Policy(Settings(allow=(music,)), RULES, tmp_path)
    assert p.precheck("com.google.Chrome", "https://music.youtube.com/").reason == "music is never flagged"
    assert p.precheck("com.netease.163music", "").reason == "music is never flagged"
    assert p.precheck("com.google.Chrome", "https://www.youtube.com/") is None


def test_a_rule_can_step_in_on_an_allow_classs_own_sites_and_apps(tmp_path):
    from qualm.rules import AllowClass

    music = AllowClass("music", "a music player", sites=("open.spotify.com",), apps=("com.spotify.client",))
    feeds = Rule("feeds", "deny", "feeds", threshold=0.5)
    spotify = {"app": "Spotify", "window_title": "Spotify Premium", "headings": ["Made for you"]}
    r = Reading(0.0, "single_item", {}, "entertain", {}, [RuleVerdict("feeds", "", 0.9, {}),
                                                         RuleVerdict("limit_music", "", 0.8, {})], 1.0)
    # A rule naming the app, or one that steps in on the class anyway: the model is asked there.
    for rule in (Rule("no_music", "deny", "music apps", apps=("com.spotify.client",)),
                 Rule("limit_music", "check_in", "listening to music", overrides_allow=("music",))):
        p = Policy(Settings(allow=(music,)), [feeds, rule], tmp_path / rule.id)
        assert p.precheck("com.spotify.client", "", "Spotify Premium", "Spotify") is None
        site = p.precheck("com.google.Chrome", "https://open.spotify.com/album/1")  # only the class-wide rule claims it
        assert site is None if rule.overrides_allow else site.reason == "music is never flagged"
        got = [(d.action, d.rule, d.reason) for d in p.decide(spotify, r, "com.spotify.client")]
        assert ("allow", "feeds", "music is never flagged") in got  # still the class's for every other rule
        assert got[-1][:2] == ("intervene", rule.id)
    # With no such rule on, the class's own apps and sites are let through before the model, as before.
    p = Policy(Settings(allow=(music,)), [feeds], tmp_path / "plain")
    assert p.precheck("com.spotify.client", "", "Spotify Premium", "Spotify").reason == "music is never flagged"


def test_a_rule_on_a_whole_domain_leaves_an_allow_classs_own_subdomains_to_it(tmp_path):
    settings, _ = load_config(EXAMPLE)
    no_youtube = Rule("no_youtube", "deny", "watching YouTube", sites=("youtube.com",), target="page")
    no_qq = Rule("no_qq", "deny", "Tencent sites", sites=("qq.com", "163.com"), target="page")
    songs = ("https://music.youtube.com/watch?v=abc", "https://y.qq.com/n/ryqq/player", "https://music.163.com/#/song?id=1")
    p = Policy(settings, [no_youtube, no_qq], tmp_path / "a")
    # Let through before the model, as before rules could name those places.
    assert [p.precheck("com.google.Chrome", url).reason for url in songs] == ["music is never flagged"] * 3
    assert p.precheck("com.google.Chrome", "https://www.youtube.com/shorts/1") is None  # the rest of YouTube isn't
    # With a rule on that steps in on music pages anyway, the model is asked there; the domain rules still let them through.
    limit = Rule("limit_music", "check_in", "listening to music", overrides_allow=("music",))
    p = Policy(settings, [no_youtube, no_qq, limit], tmp_path / "b")
    assert p.precheck("com.google.Chrome", songs[0]) is None
    r = Reading(0.0, "single_item", {}, "entertain", {}, [RuleVerdict("limit_music", "", 0.9, {})], 1.0,
                allow={"music": 0.95})
    got = [(d.action, d.rule, d.reason) for d in p.decide({"url": songs[0], "app": "Chrome"}, r, "com.google.Chrome")]
    assert ("allow", "no_youtube", "music is never flagged") in got and got[-1][:2] == ("intervene", "limit_music")
    # A rule naming that very site (or a page of it) steps in there.
    for site in ("music.youtube.com", "music.youtube.com/watch"):
        own = Rule("no_yt_music", "deny", "YouTube Music", sites=(site,))
        p = Policy(settings, [no_youtube, own], tmp_path / site.replace("/", "_"))
        assert p.precheck("com.google.Chrome", songs[0]) is None
        got = [(d.action, d.rule) for d in p.decide({"url": songs[0], "app": "Chrome"}, r, "com.google.Chrome")]
        assert got == [("allow", "no_youtube"), ("intervene", "no_yt_music")]


def test_search_results_are_left_alone_but_a_rules_own_sites_are_not(tmp_path):
    rules = [*RULES, Rule("no_shopping", "deny", "shopping", threshold=0.3, target="page"),
             Rule("tiktok", "deny", "TikTok", sites=("tiktok.com",))]
    p = Policy(Settings(), rules, tmp_path)
    r = Reading(0.0, "search", {"search": 0.9}, "entertain", {"entertain": 0.6}, [
        RuleVerdict("shortvideo", "", 0.3, {}), RuleVerdict("social", "", 0.35, {}), RuleVerdict("no_shopping", "", 0.8, {}),
        RuleVerdict("tiktok", "", 0.9, {})], 1.0)
    got = actions(p.decide({"url": "https://www.google.com/search?q=tiktok+dance+trends", "app": "Chrome",
                            "window_title": "tiktok dance trends - Google Search"}, r, "com.google.Chrome"))
    # Searching is the choice: content and check-in rules wait for what you open. A rule
    # that judges the page itself does judge it (a store's results are the store).
    assert got == [("allow", "shortvideo"), ("allow", "social"), ("intervene", "no_shopping"), ("allow", "tiktok")]
    assert p.decide({"url": "https://www.google.com/search?q=x"}, r, "com.google.Chrome")[0].reason == "search results"
    got = actions(p.decide({"url": "https://www.tiktok.com/search?q=dance", "app": "Chrome"}, r, "com.google.Chrome"))
    assert ("intervene", "tiktok") in got


def test_a_pop_up_says_when_its_on_the_rules_own_site_or_app(tmp_path):
    from qualm.watcher import NO_READING

    games = Rule("games", "deny", "playing video games", apps=("com.valvesoftware.steam",))
    shorts = Rule("shortvideo", "deny", "short videos", threshold=0.2, sites=("youtube.com/shorts",))
    p = Policy(Settings(), [games, shorts], tmp_path)
    assert [d.own for d in p.decide({"app": "Steam", "window_title": "Steam"}, NO_READING, "com.valvesoftware.steam")] == [True]
    assert [d.own for d in p.decide({"url": "https://www.youtube.com/shorts/x"}, NO_READING, "com.apple.Safari")] == [True]
    model = Reading(0.0, "single_item", {}, "entertain", {}, [RuleVerdict("shortvideo", "", 0.9, {})], 1.0)
    assert [d.own for d in p.decide({"url": "https://example.com/clip", "visible_text": ["a clip"]}, model,
                                    "com.apple.Safari")] == [False]


def test_example_allow_classes_load():
    settings, _ = load_config(EXAMPLE)
    ids = {c.id for c in settings.allow}
    assert ids == {"shopping", "music", "chat"} and any(c.matches("", "https://music.youtube.com/x") for c in settings.allow)


def test_insights_join_pop_ups_to_answers_and_focus_sessions(tmp_path):
    from qualm.policy import start_focus
    from qualm.review import insights

    p = Policy(Settings(), RULES, tmp_path)
    start_focus(tmp_path, "write the report", 50)
    p.log_intervention(Decision("intervene", "social", "x", "d1"), {"window_title": "Reddit"}, reading())
    p.log_response("d1", "back", "social")
    p.log_intervention(Decision("intervene", "shortvideo", "x", "d2"), {"window_title": "Shorts"}, reading())
    p.snooze("shortvideo", 10, "a recipe", "d2")
    got = insights(tmp_path, RULES)
    today = got["days"][-1]
    assert got["outcomes"][today]["back"] == 1 and got["outcomes"][today]["snooze"] == 1
    assert [x["ended"] for x in got["today"]] == ["back", "snooze"]
    assert got["unlocks"][-1]["reason"] == "a recipe"
    f = got["focus"][-1]
    assert f["intent"] == "write the report" and f["running"] and f["popups"] == 2 and f["back"] == 1 and f["minutes"] == 0
    assert sum(map(sum, got["heat"])) == 2


def test_review_fixes_session_and_ordinals(tmp_path):
    from qualm.explain import ordinal
    from qualm.policy import read_session, start_focus

    # A new focus session closes the running one in the log.
    start_focus(tmp_path, "a", 50)
    start_focus(tmp_path, "b", 25)
    kinds = [(e["type"], e["intent"]) for e in map(json.loads, (tmp_path / "decisions.jsonl").open())]
    assert kinds == [("focus", "a"), ("focus_end", "a"), ("focus", "b")]
    # A hand-written or broken session file counts as nothing set.
    for bad in ('{"paused_until": null}', "[1, 2]", '{"focus": {"intent": 3}}', "{half"):
        (tmp_path / "session.json").write_text(bad)
        assert read_session(tmp_path) == {"paused_until": 0.0, "pause_later": None, "focus": None}
    assert [ordinal(n) for n in (2, 3, 11, 21, 22, 112)] == ["2nd", "3rd", "11th", "21st", "22nd", "112th"]


def test_the_dashboard_answers_only_to_its_own_host(tmp_path):
    import threading
    import urllib.request

    from qualm.review import start_server

    rules = tmp_path / "rules.toml"
    rules.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    server = start_server(tmp_path, rules, 0)
    port = server.server_address[1]
    server.server_close()
    server = start_server(tmp_path, rules, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def get(host):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/data?v=", headers={"Host": host})
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert get(f"127.0.0.1:{port}") == 200 and get(f"localhost:{port}") == 200
        assert get(f"evil.example:{port}") == 403
    finally:
        server.shutdown()


def test_one_page_two_check_in_rules_asks_once(tmp_path):
    videos = Rule("videos", "check_in", "videos", threshold=0.2)
    p = Policy(Settings(), [*RULES, videos], tmp_path)
    r = reading(social=0.5)
    r.rules.append(RuleVerdict("videos", "", 0.5, {}))
    d = p.decide({"url": "https://www.xiaohongshu.com/video/1"}, r)
    assert [(x.action, x.panel) for x in d] == [("intervene", "check_in")]
    p.start_session(d[0].rule, 5, "a friend's video", d[0].id)
    d = p.decide({"url": "https://www.xiaohongshu.com/video/1"}, r)
    assert actions(d) == [("allow", "social"), ("allow", "videos")] and p.counting == {"social"}


def test_times_up_on_screen_is_never_ended_under_you_and_a_lost_one_asks_again(policy):
    c = policy.start_session("social", 5, "a")
    decide(policy, "https://weibo.com/1", reading(social=0.5))
    now = time.time()
    c.until = now - 1
    policy.tick(now)  # still here: asked
    policy.hold("social", True)  # the app shows "time's up"
    policy.tick(now + 600)  # a slow answer
    assert "social" in policy.sessions
    policy.hold("social", False)  # closed without an answer that ends it
    policy.rejudge_due(now + 600)
    policy.tick(now + 601)
    assert "social" not in policy.sessions and policy.rejudge_due(now + 601)


def test_a_session_that_ran_out_while_qualm_was_off_ended_at_its_time(policy, tmp_path):
    c = policy.start_session("social", 5, "a")
    c.until = time.time() - 30 * 60  # e.g. the Mac slept
    policy.tick()
    assert policy.last_end == c.until and policy.check_in_wait(5) == 5  # not doubled: it ended 30 min ago


def test_pages_judged_fine_are_remembered_for_take_me_back(policy):
    decide(policy, "https://www.youtube.com/watch?v=lecture", reading(purpose="learn"))
    decide(policy, "https://www.youtube.com/shorts/x", reading(shortvideo=0.9))
    assert policy.page_fine("https://www.youtube.com/watch?v=lecture")
    assert not policy.page_fine("https://www.youtube.com/shorts/x") and not policy.page_fine("https://never.seen/")


def test_going_back_and_returning_counts_toward_the_wait(policy, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("qualm.policy.time.time", lambda: now[0])
    assert policy.returns("shortvideo") == 0
    policy.log_response("a", "back", "shortvideo")
    policy.log_response("b", "fine", "shortvideo")  # not a going-back
    policy.log_response("c", "back", "feeds")
    assert policy.returns("shortvideo") == 1 and policy.returns("feeds") == 1
    now[0] += 1801  # half an hour later: forgotten
    assert policy.returns("shortvideo") == 0


def test_the_headline_rotates_but_focus_words_do_not():
    from qualm.explain import DENY_WORDS, headline

    rule = Rule(id="shortvideo", kind="deny", description="short videos made for endless swiping, such as TikTok")
    d = Decision("intervene", "shortvideo", "p_hit 0.9 >= 0.15", "x")
    heads = [headline(d, rule, "en", n=n)[1] for n in range(len(DENY_WORDS) + 1)]
    assert len(set(heads[:-1])) == len(DENY_WORDS) and heads[-1] == heads[0]
    assert all("short videos made for endless swiping" in h for h in heads)
    focus = {"intent": "write the report", "until": time.time() + 600}
    assert {headline(d, rule, "en", focus, n=n)[1] for n in range(3)} == {"You're here to: write the report."}


def test_allow_test_says_which_pop_ups_a_kind_of_page_would_stop(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    from qualm.rules import AllowClass
    from qualm.trial import run_allow

    monkeypatch.delenv("QUALM_SHIFT", raising=False)
    screens = [("a", "Weixin", ["intervene"]), ("b", "Weixin", []), ("c", "小红书", ["intervene"])]
    with (tmp_path / "judgements.jsonl").open("w") as f:
        for jid, title, acts in screens:
            f.write(json.dumps({"id": jid, "at": "2026-09-23T20:00:00", "screen": {"app": "x", "window_title": title},
                                "state": {"window_title": title, "visible_text": [jid]},
                                "decisions": [{"action": a, "rule": "social"} for a in acts]}) + "\n")

    class Client:
        backend, asked = "kev", 0

        def system_one(self, state, questions):
            Client.asked += 1
            return NS(answers={"a": NS(noul=0.8 if state["window_title"] == "Weixin" else 0.1)})

    chat = AllowClass("chat", "a chat conversation", threshold=0.5)
    rows = run_allow(Client(), tmp_path, chat, Settings(), last=10)
    assert [(r["id"], r["is_it"], r["stepped_in"]) for r in rows] == [
        ("b", True, []), ("a", True, ["social"]), ("c", False, ["social"])]
    run_allow(Client(), tmp_path, chat, Settings(), last=10)
    assert Client.asked == 3  # the same question on the same screen is asked once


def test_the_agent_prompt_names_the_pop_ups_you_said_were_wrong(tmp_path):
    from qualm import agent

    lines = [
        {"at": "2026-09-23T20:30:00", "type": "intervention", "id": "d1", "rule": "social",
         "screen": {"app": "WeChat", "window_title": "Weixin"}},
        {"at": "2026-09-23T20:30:05", "type": "response", "id": "d1", "rule": "social", "response": "fine"},
        {"at": "2026-09-23T20:31:00", "type": "intervention", "id": "d2", "rule": "feeds",
         "screen": {"app": "Chrome", "window_title": "Home", "url": "https://www.youtube.com/"}},
        {"at": "2026-09-23T20:31:05", "type": "response", "id": "d2", "rule": "feeds", "response": "back"},
    ]
    (tmp_path / "decisions.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    from datetime import datetime

    got = agent.wrong_popups(tmp_path, now=datetime(2026, 9, 23, 21, 0))
    assert got == ['- 20:30 social popped up in WeChat, window "Weixin"; I said not this one (decision d1)']
    assert agent.wrong_popups(tmp_path, now=datetime(2026, 9, 25, 21, 0)) == []  # older than a day
    p = agent.prompt(tmp_path)
    assert "guide` first" in p and "rules.toml by hand" in p and p.endswith("What I want: ")


def test_password_managers_are_never_read_whatever_the_settings(tmp_path):
    p = Policy(Settings(), RULES, tmp_path)  # no no_monitor at all
    for bundle_id, name in (("com.apple.Passwords", "Passwords"), ("org.keepassxc.keepassxc", "KeePassXC"),
                            ("me.proton.pass.electron", "Proton Pass")):
        assert p.precheck(bundle_id, "", name, name).reason == "app not monitored"
    assert p.precheck("com.apple.Safari", "https://a.com", "", "Safari") is None


def test_app_lists_match_a_bundle_id_or_a_name_in_any_case(tmp_path):
    p = Policy(Settings(no_monitor=("Figma", "COM.TINYSPECK.SLACKMACGAP")), RULES, tmp_path)
    assert p.precheck("com.figma.Desktop", "", "Design", "Figma").reason == "app not monitored"
    assert p.precheck("com.tinyspeck.slackmacgap", "", "general", "Slack").reason == "app not monitored"
    with (tmp_path / "exceptions.jsonl").open("a") as f:  # `never add --app xcode`, with no Xcode installed
        f.write(json.dumps({"never": {"app": "xcode", "name": "xcode"}}) + "\n")
    p.reload_exceptions()
    assert p.precheck("com.apple.dt.Xcode", "", "main.swift", "Xcode").reason == "you said never here"


def test_a_rule_can_name_apps_and_steps_in_without_the_model(tmp_path):
    from qualm.watcher import NO_READING

    games = Rule("games", "deny", "playing video games", apps=("com.valvesoftware.steam", "TV"))
    p = Policy(Settings(), [games], tmp_path)
    steam = {"app": "Steam", "window_title": "Steam"}  # a title and nothing else: too little for the model
    got = p.decide(steam, NO_READING, "com.valvesoftware.steam")
    assert [(d.action, d.rule, d.reason) for d in got] == [("intervene", "games", "the app is on this rule's list")]
    assert actions(p.decide({"app": "tv", "window_title": "Severance"}, NO_READING, "com.apple.TV")) == [("intervene", "games")]
    assert actions(p.decide({"app": "Safari", "window_title": "Steam"}, NO_READING, "com.apple.Safari")) == []
    late = Policy(Settings(), [Rule("games", "deny", "games", apps=("TV",), when=("00:00-00:01",))], tmp_path / "b")
    assert late.decide({"app": "TV", "window_title": "x"}, NO_READING, "com.apple.TV") == [] \
        or time.strftime("%H:%M") == "00:00"  # outside its hours


def test_apps_in_rules_toml_are_checked():
    from qualm.rules import parse_config

    ok = '[[rules]]\nid = "games"\nkind = "deny"\ndescription = "games"\napps = ["com.valvesoftware.steam"]\n'
    assert parse_config(ok)[1][0].apps == ("com.valvesoftware.steam",)
    with pytest.raises(ValueError, match="apps must be a list"):
        parse_config(ok.replace('["com.valvesoftware.steam"]', '"Steam"'))


def test_part_of_the_task_lasts_until_the_focus_session_ends(policy, tmp_path):
    from qualm.policy import end_focus, start_focus

    start_focus(tmp_path, "research competitors", 30)
    policy.reload_session()
    page = "https://www.reddit.com/r/SaaS/comments/1"
    assert see(policy, page, reading(social=0.9)) == [("intervene", "social")]
    policy.mark_fine("social", page, "Competitor X review : r/SaaS")  # "It's part of the task"
    got = policy.decide({"url": page, "app": "Safari"}, reading(social=0.9), "com.apple.Safari")
    assert [(d.action, d.reason) for d in got] == [("allow", "you said it's part of the task")]
    assert not (tmp_path / "exceptions.jsonl").exists()  # nothing permanent
    end_focus(tmp_path)
    policy.reload_session()
    assert see(policy, page, reading(social=0.9)) == [("intervene", "social")]  # a check-in again


def test_part_of_the_task_clicked_after_the_session_ended_saves_nothing(policy, tmp_path):
    from qualm.policy import end_focus, start_focus

    focus = start_focus(tmp_path, "research competitors", 30)
    policy.reload_session()
    page = "https://www.reddit.com/r/SaaS/comments/1"
    assert see(policy, page, reading(social=0.9)) == [("intervene", "social")]
    end_focus(tmp_path)  # the session ends with the pop-up still open
    policy.reload_session()
    # What the pop-up offered: it holds nothing now, and says so (0).
    assert policy.mark_fine("social", page, "Competitor X review : r/SaaS", "d1", focus=focus) == 0
    assert not (tmp_path / "exceptions.jsonl").exists()
    assert see(policy, page, reading(social=0.9)) == [("intervene", "social")]


def test_take_me_back_that_could_not_leave_is_not_a_return(policy):
    from qualm.policy import RECHECK_S

    policy.log_response("d1", "back", "shortvideo")
    policy.rejudge_in(RECHECK_S)
    policy.rejudge_in(600)  # something else due later: kept
    policy.went_back("shortvideo", "left")
    assert policy.returns("shortvideo") == 1
    policy.went_back("shortvideo", "stayed")
    assert policy.returns("shortvideo") == 0
    assert not policy.rejudge_due(time.time() + RECHECK_S + 2)  # no pop-up every 30 s on the same tab
    assert policy.rejudge_due(time.time() + 601)


def test_rules_test_asks_what_the_live_app_asks(tmp_path):
    from types import SimpleNamespace as NS

    from qualm.rules import rule_question
    from qualm.trial import run

    rule = Rule("social", "check_in", "browsing social media", threshold=0.3)
    live = Policy(Settings(), [rule], tmp_path)
    live.add_exception_text("social", "a lecture on YouTube")
    live.mark_fine("social", "https://x.com/a/status/1", "Launch day thread / X")
    j = {"id": "j1", "at": "2026-09-23T10:00:00", "screen": {"app": "Safari", "window_title": "YouTube"},
         "state": {"app": "Safari", "window_title": "YouTube"}, "decisions": []}
    (tmp_path / "judgements.jsonl").write_text(json.dumps(j) + "\n")
    asked = []

    class Client:
        def system_one(self, state, questions):
            asked.append(questions["rule"].instructions)
            return NS(answers={"rule": NS(probabilities={"in_scope": 0.2})})

    run(Client(), tmp_path, rule, Settings(), last=5)
    assert asked == [rule_question(live.rules[0], "en").instructions]
    assert "a lecture on YouTube" in asked[0] and "Launch day" not in asked[0]


def test_not_this_one_holds_on_a_window_its_rule_names_by_app(tmp_path):
    from qualm.watcher import NO_READING

    p = Policy(Settings(), [Rule("games", "deny", "playing video games", apps=("com.valvesoftware.steam",))], tmp_path)
    steam = {"app": "Steam", "window_title": "Steam"}
    assert actions(p.decide(steam, NO_READING, "com.valvesoftware.steam")) == [("intervene", "games")]
    p.mark_fine("games", "", "Steam", "d1", "com.valvesoftware.steam", None)  # no score: the model wasn't asked
    assert [(d.action, d.reason) for d in p.decide(steam, NO_READING, "com.valvesoftware.steam")] == [
        ("allow", "you marked this window fine")]
    store = {"app": "Steam", "window_title": "Steam Store"}
    assert actions(p.decide(store, NO_READING, "com.valvesoftware.steam")) == [("intervene", "games")]  # only that window
