"""The watch loop's timing: when it asks the model, with a fake screen and clock."""

import types

from qualm import watcher as w
from qualm.policy import Policy
from qualm.rules import Settings
from qualm.state import ScreenState


def run(monkeypatch, tmp_path, screens, fail_first=False):
    """screens: one ScreenState per 1-second tick. Returns the ticks the model was asked on."""
    clock = {"t": 0.0}
    feed = iter(screens)
    asked = []
    front = types.SimpleNamespace(processIdentifier=lambda: -1)
    workspace = types.SimpleNamespace(frontmostApplication=lambda: front)
    monkeypatch.setattr(w, "NSWorkspace", types.SimpleNamespace(sharedWorkspace=lambda: workspace))
    monkeypatch.setattr(w, "make_client", lambda: None)
    monkeypatch.setattr(w.time, "monotonic", lambda: clock["t"])
    watcher = w.Watcher(Policy(Settings(), [], tmp_path), lambda ev: None, interval=0, debounce=0.5, recheck=30, presence=False)

    def capture(skip=()):
        try:
            return next(feed)
        except StopIteration:
            watcher.stop.set()
            return screens[-1]

    def judge(client, s):
        asked.append(clock["t"])
        return not (fail_first and len(asked) == 1)

    def sleep(_):
        clock["t"] += 1.0

    monkeypatch.setattr(w, "capture", capture)
    monkeypatch.setattr(w.time, "sleep", sleep)
    watcher._judge = judge
    watcher.run()
    return asked


def page(url, text=("hello",)):
    return ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title=url, url=url, text=list(text))


def test_an_unchanged_screen_is_asked_once(monkeypatch, tmp_path):
    assert run(monkeypatch, tmp_path, [page("a")] * 120) == [1.0]


def test_a_new_screen_is_asked_after_the_debounce(monkeypatch, tmp_path):
    assert run(monkeypatch, tmp_path, [page("a")] * 5 + [page("b")] * 5) == [1.0, 6.0]


def test_new_text_on_the_same_screen_is_asked_at_most_every_recheck(monkeypatch, tmp_path):
    screens = [page("a", (f"line {i}",)) for i in range(100)]  # text changes every second
    assert run(monkeypatch, tmp_path, screens) == [1.0, 31.0, 61.0, 91.0]


def test_a_failed_ask_is_retried(monkeypatch, tmp_path):
    asked = run(monkeypatch, tmp_path, [page("a")] * 40, fail_first=True)
    assert asked[:2] == [1.0, 31.0]


def judged(monkeypatch, tmp_path, screens):
    """Like run(), but through the real _judge with a fake model whose answer
    hits the shortvideo rule. Returns (model calls, events)."""
    from qualm.decide import Reading, RuleVerdict
    from qualm.rules import Rule

    calls, events = [], []

    def fake_ask(client, state, rules, lang, allow):
        calls.append(state["url"])
        return Reading(0.0, "single_item", {"single_item": 0.9}, "entertain", {"entertain": 0.9},
                       [RuleVerdict("shortvideo", "violates", 0.9, {})], 100.0)

    clock = {"t": 0.0}
    feed = iter(screens)
    front = types.SimpleNamespace(processIdentifier=lambda: -1)
    workspace = types.SimpleNamespace(frontmostApplication=lambda: front)
    monkeypatch.setattr(w, "NSWorkspace", types.SimpleNamespace(sharedWorkspace=lambda: workspace))
    monkeypatch.setattr(w, "make_client", lambda: None)
    monkeypatch.setattr(w, "ask", fake_ask)
    monkeypatch.setattr(w.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(w.time, "sleep", lambda _: clock.__setitem__("t", clock["t"] + 1.0))
    policy = Policy(Settings(), [Rule("shortvideo", "deny", "short videos", threshold=0.5)], tmp_path)
    watcher = w.Watcher(policy, events.append, interval=0, debounce=0.5, recheck=30, shots=False, presence=False)

    def capture(skip=()):
        try:
            return next(feed)
        except StopIteration:
            watcher.stop.set()
            return screens[-1]

    monkeypatch.setattr(w, "capture", capture)
    watcher.run()
    return calls, events


def test_a_popup_waits_until_you_have_stayed(monkeypatch, tmp_path):
    calls, events = judged(monkeypatch, tmp_path, [page("a")] * 10)
    assert [d.action for d in events[0].decisions] == ["intervene"]


def test_a_popup_is_dropped_if_you_leave_first(monkeypatch, tmp_path):
    calls, events = judged(monkeypatch, tmp_path, [page("a")] * 2 + [page("b")] * 10)
    first = events[0]
    assert first.screen.url == "a" and first.decisions[0].action == "skip" and "left within" in first.decisions[0].reason
    assert events[1].screen.url == "b" and events[1].decisions[0].action == "intervene"


def test_going_back_to_a_page_uses_the_cache(monkeypatch, tmp_path):
    calls, events = judged(monkeypatch, tmp_path, [page("a")] * 6 + [page("b")] * 6 + [page("a")] * 6)
    assert calls == ["a", "b"] and events[-1].reading.cached


def test_another_panel_is_never_judged(monkeypatch, tmp_path):
    for title in ("Qualm", "SeeNot"):  # a second copy, or one from before the rename
        panel = ScreenState(app="python3", bundle_id="", window_title=title, text=["feeds, Weibo hot search"])
        assert run(monkeypatch, tmp_path, [panel] * 5) == []


def test_presence(monkeypatch):
    from qualm import presence as pr

    p = pr.Presence(idle_s=120)
    monkeypatch.setattr(pr, "screen_locked", lambda: False)
    monkeypatch.setattr(pr, "idle_seconds", lambda: 10)
    assert not p.away("Safari")
    monkeypatch.setattr(pr, "idle_seconds", lambda: 600)
    monkeypatch.setattr(pr, "display_kept_awake_by", lambda app: app == "Google Chrome")
    assert p.away("Safari")  # idle, nothing playing
    assert not p.away("Google Chrome")  # idle, but a video is playing in the front app
    monkeypatch.setattr(pr, "screen_locked", lambda: True)
    assert p.away("Google Chrome")


def test_owner_matching_ignores_keep_awake_tools(monkeypatch):
    from qualm import presence as pr

    out = """Listed by owning process:
   pid 1534(Caffeine): [0x1] 00:00:03 PreventUserIdleDisplaySleep named: "Caffeine prevents sleep"
   pid 99(Google Chrome Helper (Renderer)): [0x2] 00:01:00 PreventUserIdleDisplaySleep named: "Video Wake Lock"
"""
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=out))
    assert pr.display_kept_awake_by("Google Chrome") and not pr.display_kept_awake_by("Safari")


def test_a_focus_session_started_elsewhere_reaches_the_running_policy(tmp_path):
    from qualm.policy import start_focus

    policy = Policy(Settings(), [], tmp_path)
    watcher = w.Watcher(policy, lambda ev: None, presence=False)
    assert policy.focusing() is None
    start_focus(tmp_path, "write the report", 30)  # what `qualm focus` does
    watcher._reload_rules()
    assert policy.focusing()["intent"] == "write the report"
