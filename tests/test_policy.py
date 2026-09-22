"""The policy, with made-up readings: no model, no screen."""

import json

import pytest

from seenot_desktop.decide import Reading, RuleVerdict
from seenot_desktop.policy import Policy
from seenot_desktop.rules import Rule, Settings, load_config

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
    assert settings.lang == "en" and {r.id for r in rules} >= {"shortvideo", "feeds", "stocks"}
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
