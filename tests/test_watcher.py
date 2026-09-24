"""The watch loop's timing: when it asks the model, with a fake screen and clock."""

import types

from qualm import watcher as w
from qualm.policy import Policy
from qualm.rules import Settings
from qualm.state import ScreenState


def run(monkeypatch, tmp_path, screens, fail_first=False, wake=None, captures=None):
    """screens: one ScreenState per 1-second tick. Returns the ticks the model was asked on."""
    clock = {"t": 0.0}
    feed = iter(screens)
    asked = []
    front = types.SimpleNamespace(processIdentifier=lambda: -1)
    workspace = types.SimpleNamespace(frontmostApplication=lambda: front)
    monkeypatch.setattr(w, "NSWorkspace", types.SimpleNamespace(sharedWorkspace=lambda: workspace))
    monkeypatch.setattr(w, "make_client", lambda settings=None: types.SimpleNamespace(backend="kev"))
    monkeypatch.setattr(w.time, "monotonic", lambda: clock["t"])
    watcher = w.Watcher(Policy(Settings(), [], tmp_path), lambda ev: None, interval=0, debounce=0.5, recheck=30,
                        presence=False, wake=wake(clock) if wake else None)

    def capture(skip=()):
        if captures is not None:
            captures.append(clock["t"])
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


def test_qualms_own_windows_are_never_judged(monkeypatch, tmp_path):
    # Another Qualm's setup window lists Douyin, TikTok and Shorts: judged, it
    # popped up as short video (2026-09-23). Neither it nor Qualm.app is read.
    setup = ScreenState(app="Qualm", bundle_id="python3", window_title="Set up Qualm", text=["Short videos: TikTok, Shorts"])
    app = ScreenState(app="Qualm", bundle_id="com.qualm.app", window_title="Anything", text=["Douyin"])
    assert run(monkeypatch, tmp_path, [setup] * 5 + [app] * 5) == []


class IdleWake:
    """events.py's Event, with no event ever coming: each nap lasts the whole idle time."""

    def __init__(self, clock):
        self.clock = clock

    def wait(self, seconds):
        self.clock["t"] += seconds

    def clear(self):
        pass


def test_with_events_an_idle_screen_is_read_every_few_seconds(monkeypatch, tmp_path):
    seen = []
    # screens[i] is what a read returns; each read after the first costs 3 s of idle nap.
    screens = [page("a")] * 3 + [page("b")] * 3
    asked = run(monkeypatch, tmp_path, screens, wake=IdleWake, captures=seen)
    # a: asked 1 s after it appeared (the settle keeps the 1 s pace), then idle 3 s naps;
    # b: seen at the next read, asked on the one after (the debounce), fast again meanwhile.
    assert asked == [1.0, 8.0]
    assert seen[:4] == [0.0, 1.0, 4.0, 7.0]


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


def judged(monkeypatch, tmp_path, screens, model_down=False):
    """Like run(), but through the real _judge with a fake model whose answer
    hits the shortvideo rule (or, model_down, which times out). Returns (model calls, events)."""
    from qualm.decide import Reading, RuleVerdict
    from qualm.rules import Rule

    calls, events = [], []

    def fake_ask(client, state, rules, lang, allow):
        calls.append(state["url"])
        if model_down:
            raise TimeoutError("swapped out")
        return Reading(0.0, "single_item", {"single_item": 0.9}, "entertain", {"entertain": 0.9},
                       [RuleVerdict("shortvideo", "violates", 0.9, {})], 100.0)

    clock = {"t": 0.0}
    feed = iter(screens)
    front = types.SimpleNamespace(processIdentifier=lambda: -1)
    workspace = types.SimpleNamespace(frontmostApplication=lambda: front)
    monkeypatch.setattr(w, "NSWorkspace", types.SimpleNamespace(sharedWorkspace=lambda: workspace))
    monkeypatch.setattr(w, "make_client", lambda settings=None: types.SimpleNamespace(backend="kev"))
    monkeypatch.setattr(w, "ask", fake_ask)
    monkeypatch.setattr(w.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(w.time, "sleep", lambda _: clock.__setitem__("t", clock["t"] + 1.0))
    policy = Policy(Settings(), [Rule("shortvideo", "deny", "short videos", threshold=0.5, sites=("tiktok.com",))], tmp_path)
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


def test_a_rules_own_site_still_steps_in_while_the_model_is_down(monkeypatch, tmp_path):
    calls, events = judged(monkeypatch, tmp_path, [page("https://www.tiktok.com/@a/video/1")] * 10, model_down=True)
    assert events and events[0].reading is None
    assert [(d.action, d.reason) for d in events[0].decisions] == [("intervene", "matches URL pattern")]
    calls, events = judged(monkeypatch, tmp_path / "b", [page("https://example.com/")] * 10, model_down=True)
    assert calls and not events  # no site of its own: nothing to go on


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


def test_starting_focus_rejudges_the_screen_you_are_on(monkeypatch, tmp_path):
    from qualm.policy import start_focus

    clock = {"t": 0.0}
    asked = []
    front = types.SimpleNamespace(processIdentifier=lambda: -1)
    monkeypatch.setattr(w, "NSWorkspace", types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(frontmostApplication=lambda: front)))
    monkeypatch.setattr(w, "make_client", lambda settings=None: types.SimpleNamespace(backend="kev"))
    monkeypatch.setattr(w.time, "monotonic", lambda: clock["t"])
    watcher = w.Watcher(Policy(Settings(), [], tmp_path), lambda ev: None, interval=0, debounce=0.5, recheck=30, presence=False)

    def sleep(_):
        clock["t"] += 1.0
        if clock["t"] == 10:
            start_focus(tmp_path, "write", 30)  # from the CLI, mid-page
        if clock["t"] >= 20:
            watcher.stop.set()

    monkeypatch.setattr(w, "capture", lambda skip=(): page("a"))
    monkeypatch.setattr(w.time, "sleep", sleep)
    watcher._judge = lambda client, s: asked.append(clock["t"]) or True
    watcher.run()
    assert asked == [1.0, 11.0]


def test_a_check_in_session_on_an_unchanged_page_ends_with_times_up(monkeypatch, tmp_path):
    """The page never changes, so only the policy's re-check can say the time is up."""
    import time as real_time

    from qualm.decide import Reading, RuleVerdict
    from qualm.rules import Rule

    clock, seen = {"t": 0.0}, []
    front = types.SimpleNamespace(processIdentifier=lambda: -1)
    monkeypatch.setattr(w, "NSWorkspace", types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(frontmostApplication=lambda: front)))
    monkeypatch.setattr(w, "make_client", lambda settings=None: types.SimpleNamespace(backend="kev"))
    monkeypatch.setattr(w, "ask", lambda client, state, rules, lang, allow: Reading(
        0.0, "single_item", {"single_item": 0.9}, "entertain", {"entertain": 0.9}, [RuleVerdict("videos", "in_scope", 0.9, {})], 100.0))
    monkeypatch.setattr(w.time, "monotonic", lambda: clock["t"])
    policy = Policy(Settings(), [Rule("videos", "check_in", "entertainment videos", threshold=0.5)], tmp_path)

    def on_event(ev):
        d = ev.decisions[0]
        seen.append(d.panel or d.action)
        if d.panel == "check_in":
            policy.start_session("videos", 5, "the match", d.id)  # the user answers the pop-up

    watcher = w.Watcher(policy, on_event, interval=0, debounce=0.5, recheck=30, shots=False, presence=False)

    def sleep(_):
        clock["t"] += 1.0
        if clock["t"] == 20:
            policy.sessions["videos"].until = real_time.time() - 1  # five minutes later
        if clock["t"] >= 30:
            watcher.stop.set()

    monkeypatch.setattr(w, "capture", lambda skip=(): page("https://www.bilibili.com/video/BV1"))
    monkeypatch.setattr(w.time, "sleep", sleep)
    watcher.run()
    assert seen == ["check_in", "allow", "times_up"]


class FakeTab:
    """A tab with a history; back() moves one step."""

    def __init__(self, history):
        self.history, self.blanked = list(history), False

    def url(self):
        return self.history[-1]

    def back(self):
        if len(self.history) > 1:
            self.history.pop()

    def blank(self):
        self.blanked = True


def test_take_me_back_leaves_the_site_not_just_one_page():
    tab = FakeTab(["https://docs.python.org/3/", "https://www.xiaohongshu.com/explore", "https://www.xiaohongshu.com/user/1",
                   "https://www.xiaohongshu.com/explore/abc"])
    assert w.leave(tab, "https://www.xiaohongshu.com/explore/abc", poll=0, patience=0) == "left"
    assert tab.url() == "https://docs.python.org/3/"


def test_take_me_back_stops_at_a_page_of_the_same_site_that_was_fine():
    lecture = "https://www.youtube.com/watch?v=lecture"
    tab = FakeTab(["https://news.example/", lecture, "https://www.youtube.com/", "https://www.youtube.com/shorts/x"])
    w.leave(tab, "https://www.youtube.com/shorts/x", fine=lambda u: u == lecture, poll=0, patience=0)
    assert tab.url() == lecture


def test_take_me_back_with_no_history_opens_a_new_tab_page():
    tab = FakeTab(["https://www.xiaohongshu.com/explore"])
    assert w.leave(tab, "https://www.xiaohongshu.com/explore", poll=0, patience=0) == "new tab" and tab.blanked


def test_take_me_back_does_nothing_if_you_already_left():
    tab = FakeTab(["https://www.xiaohongshu.com/explore", "https://mail.example/"])
    assert w.leave(tab, "https://www.xiaohongshu.com/explore", poll=0, patience=0) == "left"
    assert tab.url() == "https://mail.example/" and not tab.blanked


def test_take_me_back_closes_the_photo_viewer_not_the_chats():
    chats, viewer = ("Weixin", [228.0, 65.0, 1284.0, 853.0]), ("Photos and Videos", [221.0, 57.0, 825.0, 901.0])
    assert w.judged_window([viewer, chats], "Photos and Videos", [221.0, 57.0, 825.0, 901.0]) == 0
    assert w.judged_window([viewer, chats], "Photos and Videos", []) == 0  # no frame: by title
    assert w.judged_window([viewer, chats], "Photos and Videos", [221.5, 56.0, 825.0, 901.0]) == 0  # moved a pixel


def test_take_me_back_hides_an_app_with_one_window_or_an_unknown_one():
    chats = ("Weixin", [228.0, 65.0, 1284.0, 853.0])
    assert w.judged_window([chats], "Weixin", chats[1]) is None
    assert w.judged_window([chats, ("Weixin", [0.0, 0.0, 400.0, 300.0])], "Weixin", []) is None  # two alike
    assert w.judged_window([chats, ("Settings", [0.0, 0.0, 400.0, 300.0])], "Photos and Videos", [9.0, 9.0, 9.0, 9.0]) is None
