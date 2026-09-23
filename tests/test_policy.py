"""The policy, with made-up readings: no model, no screen."""

import json

import pytest

from qualm.decide import Reading, RuleVerdict
from qualm.policy import Policy
from qualm.rules import Rule, Settings, load_config

RULES = [
    Rule("shortvideo", "deny", "short videos", threshold=0.15, patterns=(r"youtube\.com/shorts/",), allow_intentional=True),
    Rule("feeds", "deny", "feeds", threshold=0.5, target="page", feed_hit=True),
    Rule("livestream", "deny", "live", threshold=0.5, allow_learning=True),
    Rule("social", "time_cap", "social", threshold=0.2, minutes_per_day=20, allow_intentional=True),
    Rule("stocks", "time_cap", "stocks", threshold=0.1, minutes_per_day=10, visits_per_day=2),
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
    assert see(policy, "https://www.youtube.com/shorts/a", reading()) == [("allow", "shortvideo")]
    # Swiping on to the next short: drift, not intent.
    assert see(policy, "https://www.youtube.com/shorts/b", reading()) == [("intervene", "shortvideo")]


def test_link_from_chat_app_is_intentional(policy):
    see(policy, "", reading(page="single_item", purpose="task"), app="com.tinyspeck.slackmacgap")
    assert see(policy, "https://www.youtube.com/shorts/a", reading()) == [("allow", "shortvideo")]


def test_link_from_an_allowed_url_is_intentional(policy):
    assert policy.precheck("com.apple.Safari", "https://docs.python.org/3/").action == "allow"
    assert see(policy, "https://www.youtube.com/shorts/a", reading()) == [("allow", "shortvideo")]


def test_precheck_skips_unmonitored_apps_and_pause(policy):
    assert policy.precheck("com.1password.1password", "").action == "skip"
    assert policy.precheck("com.apple.Safari", "https://a.com") is None
    policy.pause(5)
    assert policy.precheck("com.apple.Safari", "https://a.com").reason == "paused"


def test_time_cap_counts_then_intervenes_over_budget(policy):
    assert see(policy, "https://weibo.com/1", reading(social=0.5)) == [("count", "social")]
    policy.tick(0.0)
    for t in range(1, 20 * 60 // 5 + 2):
        policy.tick(t * 5.0)  # 5 s per tick: MAX_TICK_S caps longer gaps
    assert see(policy, "https://weibo.com/2", reading(social=0.5)) == [("intervene", "social")]


def test_visits_limit(policy):
    other = reading(page="work", purpose="task")
    for i in range(2):
        assert see(policy, f"https://quote.com/{i}", reading(stocks=0.5)) == [("count", "stocks")]
        see(policy, "app://editor", other, app="com.microsoft.VSCode")
    got = policy.decide({"url": "https://quote.com/9"}, reading(stocks=0.5))
    assert actions(got) == [("intervene", "stocks")] and "visit 3 of 2" in got[0].reason


def test_snooze_allows_the_rule_for_a_while(policy):
    policy.snooze("shortvideo", 10, "research for a talk")
    assert see(policy, "https://a.com/x", reading(shortvideo=0.9)) == [("allow", "shortvideo")]


def test_not_this_one_allows_the_url_and_teaches_the_model(policy, tmp_path):
    policy.mark_fine("shortvideo", "https://a.com/x", "How to fix a bike chain")
    assert see(policy, "https://a.com/x", reading(shortvideo=0.9)) == [("allow", "shortvideo")]
    assert see(policy, "https://a.com/y", reading(shortvideo=0.9)) == [("intervene", "shortvideo")]
    rule = next(r for r in policy.rules if r.id == "shortvideo")
    assert rule.exceptions == ('the page "How to fix a bike chain"',)
    # It survives a restart.
    again = Policy(SETTINGS, RULES, tmp_path)
    assert see(again, "https://a.com/x", reading(shortvideo=0.9)) == [("allow", "shortvideo")]


def test_interventions_and_responses_are_logged(policy, tmp_path):
    d = policy.decide({"url": "https://a.com/x"}, reading(shortvideo=0.9))[0]
    policy.log_intervention(d, {"app": "Safari"}, reading(shortvideo=0.9))
    policy.log_response(d.id, "back", d.rule)
    events = [json.loads(line) for line in (tmp_path / "decisions.jsonl").open()]
    assert [e["type"] for e in events] == ["intervention", "response"] and events[0]["id"] == events[1]["id"]


def test_example_rules_load():
    settings, rules = load_config("rules.example.toml")
    assert settings.lang == "en" and {r.id for r in rules} >= {"shortvideo", "feeds", "social"}
    assert all(0 < r.threshold < 1 for r in rules)


def test_entertainment_feed_hits_a_feed_rule_on_any_site(policy):
    assert see(policy, "https://new-site.example/", reading(page="feed", feeds=0.0)) == [("intervene", "feeds")]
    assert see(policy, "https://news.example/", reading(page="feed", purpose="task", feeds=0.0)) == []


def test_scrolling_a_feed_counts_toward_a_time_cap(policy):
    assert ("count", "social") in see(policy, "https://weibo.com/", reading(page="feed", social=0.5))


def test_budgets_off_makes_time_caps_step_in_at_once(tmp_path):
    p = Policy(Settings(budgets=False), RULES, tmp_path)
    assert see(p, "https://weibo.com/1", reading(social=0.5)) == [("intervene", "social")]


def test_every_judgement_is_logged_but_private_ones_without_content(policy, tmp_path):
    screen = {"app": "Safari", "bundle_id": "com.apple.Safari", "window_title": "t", "url": "https://a.com", "text": ["x"]}
    r = reading(shortvideo=0.9)
    policy.log_judgement(screen, r, policy.decide(screen, r))
    policy.log_judgement(screen, r, policy.decide(screen, reading(sensitive=0.9)))
    logged = [json.loads(line) for line in (tmp_path / "judgements.jsonl").open()]
    assert logged[0]["screen"]["text"] == ["x"] and logged[0]["p_hit"]["shortvideo"] == 0.9
    assert "text" not in logged[1]["screen"] and "p_hit" not in logged[1]


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
        lines.append(json.dumps({"id": f"j{i}", "p_hit": {"social": p}, "decisions": []}))
        save_review(tmp_path, f"j{i}", rules={"social": y})
    (tmp_path / "judgements.jsonl").write_text("\n".join(lines) + "\n")
    row = next(t for t in tuning(tmp_path, RULES) if t["rule"] == "social")
    assert row["yes"] == 3 and row["no"] == 3 and row["suggested"] == 0.35 and row["suggested_recall"] == 1.0

    rules = tmp_path / "rules.toml"
    rules.write_text(open("rules.example.toml", encoding="utf-8").read(), encoding="utf-8")
    set_threshold(rules, "social", 0.35)
    assert next(r for r in load_config(rules)[1] if r.id == "social").threshold == 0.35
    assert next(r for r in load_config(rules)[1] if r.id == "videos").threshold == 0.25


def test_a_screen_with_nothing_but_its_name_does_not_pop_up(policy):
    got = policy.decide({"app": "WhatsApp", "window_title": "WhatsApp"}, reading(stocks=0.9), "net.whatsapp.WhatsApp")
    assert actions(got) == [("skip", "stocks")] and "too little on screen" in got[0].reason


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
    shop = reading(stocks=0.9)
    shop.allow = {"shopping": 0.8}
    got = p.decide({"url": "https://store.example/polo"}, shop)
    assert actions(got) == [("allow", "stocks")] and got[0].reason == "shopping is never flagged"
    short = reading(shortvideo=0.9)
    short.allow = {"shopping": 0.8}
    assert see(p, "https://www.youtube.com/shorts/x", short) == [("intervene", "shortvideo")]


def test_explanations_in_words(policy):
    from qualm.explain import reason
    from qualm.policy import Decision

    rule = RULES[0]  # shortvideo, threshold 0.15
    r = reading(shortvideo=0.9)
    assert reason(Decision("intervene", "shortvideo", "p_hit 0.90 >= 0.15"), r, rule, "en").startswith("Your shortvideo rule, a clear match: short videos.")
    assert "a close call" in reason(Decision("intervene", "shortvideo", "x"), reading(shortvideo=0.16), rule, "en")
    assert reason(Decision("intervene", "shortvideo", "matches URL pattern"), r, rule, "en").startswith("This address is on your list")
    assert "for entertainment" in reason(Decision("intervene", "shortvideo", "x"), r, rule, "en")


def test_snoozes_are_counted_for_the_growing_wait(policy):
    assert policy.snoozes_in_last_hour() == 0
    policy.snooze("shortvideo", 10, "a")
    policy.snooze("shortvideo", 10, "b")
    assert policy.snoozes_in_last_hour() == 2


def test_time_away_does_not_count(policy):
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


def test_example_allow_classes_load():
    settings, _ = load_config("rules.example.toml")
    ids = {c.id for c in settings.allow}
    assert ids == {"shopping", "music"} and any(c.matches("", "https://music.youtube.com/x") for c in settings.allow)
