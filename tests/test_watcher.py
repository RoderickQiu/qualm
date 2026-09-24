"""The watch loop's timing: when it asks the model, with a fake screen and clock."""

import json
import types

import pytest

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


def test_a_video_counts_as_watching_whatever_language_the_app_is_named_in(monkeypatch):
    from qualm import presence as pr

    out = 'pid 812(Safari): [0x3] 00:02:00 PreventUserIdleDisplaySleep named: "WebKit Media Playback"\n'
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=out))
    assert not pr.display_kept_awake_by("Safari浏览器")  # the name a Chinese Mac shows
    assert pr.display_kept_awake_by("Safari浏览器", "Safari")  # and its executable, which pmset names


def test_a_focus_session_started_elsewhere_reaches_the_running_policy(tmp_path):
    from qualm.policy import start_focus

    policy = Policy(Settings(), [], tmp_path)
    watcher = w.Watcher(policy, lambda ev: None, presence=False)
    assert policy.focusing() is None
    start_focus(tmp_path, "write the report", 30)  # what `qualm focus` does
    watcher._reload_rules()
    assert policy.focusing()["intent"] == "write the report"


def test_a_planned_pause_beginning_shows_in_the_status_line(monkeypatch, tmp_path):
    """It begins without a write to session.json: the menu's line said "watching" all weekend."""
    import time

    from qualm.policy import pause_for

    t = time.time()
    policy, said = Policy(Settings(), [], tmp_path), []
    watcher = w.Watcher(policy, lambda ev: None, on_status=said.append, shots=False, presence=False)
    pause_for(tmp_path, 60, start=t + 3600)  # `qualm pause --from ...`
    watcher._reload_rules()
    assert said == ["watching"]
    monkeypatch.setattr(w.time, "time", lambda: t + 3601)
    monkeypatch.setattr("qualm.policy.time.time", lambda: t + 3601)
    watcher._reload_rules()
    assert said[-1].startswith("paused until ") and watcher._rejudge
    watcher._reload_rules()
    assert len(said) == 2  # said once
    monkeypatch.setattr("qualm.policy.time.time", lambda: t + 3600 + 3601)  # and when it ends
    watcher._reload_rules()
    assert said[-1] == "watching"


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
        return True


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


class FakeBrowser:
    """A running app for go_back(): one tab's history, the keys it was sent,
    whether it was hidden. `applescript`: it answers Chrome's AppleScript;
    `ax_url`: Accessibility reads its page's address; `comes_forward`: it's
    the app in front once brought forward, and `stays` keys later."""

    def __init__(self, history, applescript=True, ax_url=True, title="Page", sets_url=True, comes_forward=True,
                 stays=99):
        self.history, self.applescript, self.ax_url, self.title = list(history), applescript, ax_url, title
        self.sets_url, self.keys, self.hidden, self.windows = sets_url, [], False, 2
        self.comes_forward, self.stays, self.front, self.activations = comes_forward, stays, False, 0

    def processIdentifier(self):
        return 4242

    def activateWithOptions_(self, options):
        self.activations += 1
        self.front = self.comes_forward
        return self.comes_forward

    def hide(self):
        self.hidden = True

    def osa(self, script):
        import re

        if "System Events" in script:
            key = re.search(r'keystroke "(.)"', script).group(1)
            assert self.front, "a key pressed with another app in front"
            self.keys.append(key)
            self.front = len(self.keys) < self.stays  # you switched away after this one
            if key == "[" and len(self.history) > 1:
                self.history.pop()
            elif key == "t":
                self.history.append("about:newtab")
            return ""
        if not self.applescript:
            return None  # no Automation permission, or no dictionary
        if "go back" in script:
            if len(self.history) > 1:
                self.history.pop()
        elif "set URL" in script:
            if not self.sets_url:
                return None
            self.history.append("chrome://newtab/")
        return self.history[-1] if "get URL" in script else ""


def take_me_back(monkeypatch, app, bundle_id, url, title="Page", browser=False):
    """go_back() on a screen in `app`; returns (what it did, windows closed).
    `browser`: its window looks like a browser's (state.browser_window)."""
    closed = []
    monkeypatch.setattr(w, "browser_window", lambda pid: browser)
    monkeypatch.setattr(w, "NSRunningApplication",
                        types.SimpleNamespace(runningApplicationsWithBundleIdentifier_=lambda b: [app]))
    monkeypatch.setattr(w, "_osa", app.osa)
    monkeypatch.setattr(w, "page_address", lambda pid, by_title=False: (
        app.title if by_title else app.history[-1] if app.ax_url else None))
    monkeypatch.setattr(w, "close_window", lambda *a: closed.append(a[2]) or app.windows > 1)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    other = types.SimpleNamespace(processIdentifier=lambda: 1)  # the app you're in when the browser isn't in front
    monkeypatch.setattr(w, "NSWorkspace", types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(
        frontmostApplication=lambda: app if app.front else other)))
    done = []
    how = w.go_back(ScreenState(app="Browser", bundle_id=bundle_id, window_title=title, url=url), done=done.append)
    assert done == [how]
    return how, closed


SHORT = "https://www.youtube.com/shorts/abc"


def test_take_me_back_never_closes_a_browser_window(monkeypatch):
    # Vivaldi, Opera, Chrome Beta: Chrome's AppleScript, not the close button (a window of 23 tabs was lost).
    for bundle_id in ("com.vivaldi.Vivaldi", "com.operasoftware.Opera", "com.google.Chrome.beta", "org.chromium.Chromium"):
        app = FakeBrowser(["https://docs.python.org/3/", SHORT])
        assert take_me_back(monkeypatch, app, bundle_id, SHORT) == ("left", [])
        assert app.history == ["https://docs.python.org/3/"] and not app.hidden
    # Safari: Cmd-[ with its address read back through AppleScript, brought forward once.
    app = FakeBrowser(["https://docs.python.org/3/", "https://www.youtube.com/", SHORT])
    assert take_me_back(monkeypatch, app, "com.apple.Safari", SHORT) == ("left", [])
    assert app.keys == ["[", "["] and app.activations == 1 and not app.hidden


def test_take_me_back_in_firefox_checks_it_left_and_opens_a_new_tab_if_not(monkeypatch):
    # A tab opened from a link has no history: Cmd-[ does nothing, so a new tab (it popped up every 30 s).
    app = FakeBrowser([SHORT], applescript=False)
    assert take_me_back(monkeypatch, app, "org.mozilla.firefox", SHORT) == ("new tab", [])
    assert app.keys == ["[", "t"]
    app = FakeBrowser(["https://news.example/", "https://www.youtube.com/", SHORT], applescript=False)
    assert take_me_back(monkeypatch, app, "app.zen-browser.zen", SHORT)[0] == "left"
    assert app.keys == ["[", "["] and app.history == ["https://news.example/"]  # off the site, not one step


def test_take_me_back_falls_back_to_keys_without_automation(monkeypatch):
    app = FakeBrowser(["https://docs.python.org/3/", SHORT], applescript=False)
    assert take_me_back(monkeypatch, app, "com.google.Chrome", SHORT) == ("left", [])
    assert app.keys == ["["]
    # A Chromium browser whose AppleScript won't open a new-tab page: Cmd-T does.
    app = FakeBrowser([SHORT], sets_url=False)
    assert take_me_back(monkeypatch, app, "company.thebrowser.Browser", SHORT) == ("new tab", [])
    assert app.keys == ["[", "t"]


def test_take_me_back_in_a_browser_it_cannot_read_goes_by_the_title(monkeypatch):
    app = FakeBrowser([SHORT], applescript=False, ax_url=False, title="Funny cat #shorts")
    assert take_me_back(monkeypatch, app, "com.kagi.kfmac", "", "Funny cat #shorts")[0] == "new tab"
    assert app.keys == ["[", "t"]


def test_take_me_back_in_a_web_app_closes_its_window_or_hides_it(monkeypatch):
    # YouTube installed from Chrome as an app, Slack: a Back there stays in the app (it popped up every 30 s).
    pwa = "com.google.Chrome.app.agimnkijcaahngcdmfeangaknmldooml"
    app = FakeBrowser(["https://www.youtube.com/shorts/prev", SHORT])
    assert take_me_back(monkeypatch, app, pwa, SHORT) == ("closed", ["Page"])
    app = FakeBrowser(["https://app.slack.com/client/T1/C1"])
    app.windows = 1
    assert take_me_back(monkeypatch, app, "com.tinyspeck.slackmacgap", "https://app.slack.com/client/T1/C1") == ("hidden", ["Page"])
    assert app.hidden and app.keys == [] and app.activations == 0
    # A Chrome web app is an app, whatever its window looks like.
    app = FakeBrowser(["https://www.youtube.com/shorts/prev", SHORT])
    assert take_me_back(monkeypatch, app, pwa, SHORT, browser=True) == ("closed", ["Page"]) and app.keys == []


def test_take_me_back_in_a_browser_it_doesnt_list_presses_one_back(monkeypatch):
    # Dia, Comet, Yandex, Whale before they were listed: the window of 23 tabs was closed.
    for bundle_id in ("company.thebrowser.dia", "ai.perplexity.comet", "ru.yandex.desktop.yandex-browser", "com.naver.Whale"):
        assert bundle_id in w.BROWSERS
    app = FakeBrowser(["https://news.example/", "https://www.youtube.com/", SHORT], applescript=False)
    assert take_me_back(monkeypatch, app, "com.example.NewBrowser", SHORT, browser=True) == ("left", [])
    assert app.keys == ["["] and app.activations == 1 and not app.hidden  # one Back, not off the site
    # Nothing to go back to: it stays, and Cmd-T (Show Fonts in many apps) isn't pressed.
    app = FakeBrowser([SHORT], applescript=False)
    assert take_me_back(monkeypatch, app, "com.example.NewBrowser", SHORT, browser=True) == ("stayed", [])
    assert app.keys == ["["]
    # You went elsewhere before it pressed anything: nothing is pressed.
    app = FakeBrowser([SHORT], applescript=False, comes_forward=False)
    assert take_me_back(monkeypatch, app, "com.example.NewBrowser", SHORT, browser=True) == ("stayed", [])
    assert app.keys == []


def test_take_me_back_by_keys_stops_when_you_go_elsewhere(monkeypatch):
    history = ["https://news.example/", "https://www.youtube.com/"] + [f"https://www.youtube.com/shorts/{i}" for i in range(20)]
    # You switched to your editor after the first Back: nothing more is pressed, nor is the browser brought back.
    app = FakeBrowser(history, applescript=False, stays=1)
    assert take_me_back(monkeypatch, app, "org.mozilla.firefox", history[-1]) == ("stayed", [])
    assert app.keys == ["["] and app.activations == 1
    # The browser never came to the front (it quit, its window closed): no key at all.
    app = FakeBrowser(history, applescript=False, comes_forward=False)
    assert take_me_back(monkeypatch, app, "com.google.Chrome", history[-1]) == ("stayed", [])
    assert app.keys == [] and app.activations == 1
    # Staying in front: brought forward once, however many steps back (12, then a new tab).
    app = FakeBrowser(history, applescript=False)
    assert take_me_back(monkeypatch, app, "org.mozilla.firefox", history[-1])[0] == "new tab"
    assert app.keys == ["["] * w.MAX_BACK + ["t"] and app.activations == 1


def test_take_me_back_in_an_app_closes_its_window_as_before(monkeypatch):
    app = FakeBrowser([])
    assert take_me_back(monkeypatch, app, "com.tencent.xinWeChat", "", "Photos and Videos") == ("closed", ["Photos and Videos"])


def shots_watcher(monkeypatch, tmp_path, policy, ask):
    """A Watcher that keeps screenshots, with screencapture writing a dummy
    file instead of looking at the screen, and `ask` for the model."""
    taken = []

    def run(args, **kw):
        if args[0] == "screencapture":
            taken.append(args[-1])
            open(args[-1], "wb").write(b"jpg")
        return types.SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(w.subprocess, "run", run)
    monkeypatch.setattr(w, "ask", ask)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    ms = iter(range(1_790_000_000_000, 1_800_000_000_000, 1000))  # a new name per picture, as a second apart
    monkeypatch.setattr(w.time, "time_ns", lambda: next(ms) * 1_000_000)
    events = []
    watcher = w.Watcher(policy, events.append, presence=False)
    watcher.dwell = 0
    return watcher, events, taken


def shots(tmp_path):
    folder = tmp_path / "shots"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_no_screenshot_is_kept_while_the_model_is_down(monkeypatch, tmp_path):
    from qualm.rules import Rule

    def down(*a):
        raise ConnectionError("starting")

    policy = Policy(Settings(), [Rule("shortvideo", "deny", "short videos", sites=("tiktok.com",))], tmp_path)
    watcher, events, taken = shots_watcher(monkeypatch, tmp_path, policy, down)
    bank = ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title="Sign in - Online Banking",
                       url="https://secure.examplebank.com/login", text=["User ID", "Password"], frame=[0, 0, 800, 600])
    assert watcher._judge(types.SimpleNamespace(backend="kev"), bank) is False
    assert taken and shots(tmp_path) == [] and not (tmp_path / "judgements.jsonl").exists()
    # A rule's own site still steps in, without a picture: nothing said the page wasn't private.
    tiktok = ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title="dance", url="https://www.tiktok.com/@a/video/1",
                         frame=[0, 0, 800, 600])
    watcher._judge(types.SimpleNamespace(backend="kev"), tiktok)
    assert events[-1].decisions[0].action == "intervene" and shots(tmp_path) == []
    # No model at all (hosted, no key yet): no picture is even taken.
    taken.clear()
    watcher._judge(None, bank)
    assert taken == []


def test_a_screenshot_is_kept_only_with_a_reading_that_is_not_private(monkeypatch, tmp_path):
    from qualm.decide import Reading

    answer = {"sensitive": 0.0}

    def ask_(client, state, rules, lang, allow):
        return Reading(answer["sensitive"], "single_item", {}, "task", {}, [], 100.0)

    policy = Policy(Settings(allow_sites=("github.com",)), [], tmp_path)
    watcher, events, taken = shots_watcher(monkeypatch, tmp_path, policy, ask_)
    client = types.SimpleNamespace(backend="kev")
    page = lambda url: ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title=url, url=url, text=["hello"],
                                   frame=[0, 0, 800, 600])
    watcher._judge(client, page("https://example.com/a"))
    logged = [json.loads(line) for line in (tmp_path / "judgements.jsonl").read_text().splitlines()]
    assert shots(tmp_path) == [logged[-1]["shot"]]
    answer["sensitive"] = 0.9
    watcher._judge(client, page("https://bank.example/"))
    assert len(shots(tmp_path)) == 1  # the private page's picture is gone
    watcher._judge(client, page("https://github.com/me/repo"))  # let through before the model is asked
    assert len(taken) == 2 and len(shots(tmp_path)) == 1


def test_a_rule_naming_the_app_steps_in_without_the_model(monkeypatch, tmp_path):
    from qualm.rules import Rule

    policy = Policy(Settings(), [Rule("games", "deny", "video games", apps=("Steam",))], tmp_path)
    watcher, events, taken = shots_watcher(monkeypatch, tmp_path, policy, lambda *a: pytest.fail("asked the model"))
    steam = ScreenState(app="Steam", bundle_id="com.valvesoftware.steam", window_title="Steam", frame=[0, 0, 800, 600])
    assert watcher._judge(types.SimpleNamespace(backend="kev"), steam) is True
    assert [(d.action, d.rule) for d in events[-1].decisions] == [("intervene", "games")] and taken == []


def test_a_rule_naming_the_app_leaves_the_other_rules_to_the_model(monkeypatch, tmp_path):
    from qualm.decide import Reading, RuleVerdict
    from qualm.rules import Rule

    # "Check me in when I open Safari" silenced the feeds rule there: the model wasn't asked at all.
    feeds = Rule("feeds", "deny", "recommendation feeds", threshold=0.5, target="page")
    safari = Rule("safari_time", "check_in", "using Safari", apps=("Safari",))
    policy = Policy(Settings(), [feeds, safari], tmp_path)
    policy.start_session("safari_time", 30, "reading the news", "d0")
    asked = []

    def ask(client, state, rules, lang, allow):
        asked.append([r.id for r in rules])
        return Reading(0.01, "feed", {"feed": 0.9}, "entertain", {"entertain": 0.9},
                       [RuleVerdict(r.id, "violates", 0.9, {}) for r in rules], 100.0)

    watcher, events, _ = shots_watcher(monkeypatch, tmp_path, policy, ask)
    watcher.shots = False
    feed = ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title="9GAG - Hot", url="https://9gag.com/hot",
                       text=["Hot", "Trending", "Fresh"])
    assert watcher._judge(types.SimpleNamespace(backend="kev"), feed) is True
    assert asked == [["feeds"]]  # the app's own rule is a hit without the question
    assert [(d.action, d.rule) for d in events[-1].decisions] == [("intervene", "feeds"), ("allow", "safari_time")]
    # The model down: the app's rule still steps in, and the others are asked again later.
    policy.end_session("safari_time", "done")
    monkeypatch.setattr(w, "ask", lambda *a: (_ for _ in ()).throw(ConnectionError()))
    watcher._cache.clear()
    assert watcher._judge(types.SimpleNamespace(backend="kev"), feed) is False
    assert [(d.action, d.rule, d.panel) for d in events[-1].decisions] == [("intervene", "safari_time", "check_in")]


def test_an_error_every_pass_prints_its_traceback_once_whatever_its_text(monkeypatch, tmp_path):
    clock, printed = {"t": 0.0, "n": 0}, []
    policy = Policy(Settings(), [], tmp_path)
    front = types.SimpleNamespace(processIdentifier=lambda: -1)
    monkeypatch.setattr(w, "NSWorkspace", types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(frontmostApplication=lambda: front)))
    monkeypatch.setattr(w, "make_client", lambda settings=None: types.SimpleNamespace(backend="kev"))
    monkeypatch.setattr(w.time, "monotonic", lambda: clock["t"])
    watcher = w.Watcher(policy, lambda ev: None, interval=0, presence=False)

    def capture(skip=()):
        clock["n"] += 1
        if clock["n"] % 2:
            raise KeyError(f"AXChildren@{clock['n']:#x}")  # a new address every time
        raise ValueError("an odd tree")  # and another error in between

    def sleep(s):
        clock["t"] += s
        if clock["t"] >= 600:
            watcher.stop.set()

    monkeypatch.setattr(w, "capture", capture)
    monkeypatch.setattr(w.time, "sleep", sleep)
    monkeypatch.setattr(w.traceback, "print_exc", lambda: printed.append(clock["t"]))
    checked = []
    monkeypatch.setattr(w, "bound_log", lambda: checked.append(clock["t"]))
    watcher.run()
    assert clock["n"] == 120  # every 5 s for 10 minutes, still watching
    # Each at once the first time; then, taking turns, at most once a minute.
    assert printed[:2] == [0, 5] and len(printed) <= 12
    assert all(b - a >= w.TRACEBACK_EVERY_S for a, b in zip(printed[1:], printed[2:]))
    assert len(checked) == 10  # the log's size, once a minute


def test_the_login_log_is_set_aside_when_it_grows(tmp_path):
    import subprocess
    import sys

    logs = tmp_path / "Library" / "Logs" / "Qualm"
    logs.mkdir(parents=True)
    log = logs / "com.qualm.app.log"
    script = ("import sys; from pathlib import Path; from qualm import paths, watcher; paths.LOGS = Path(sys.argv[1]); "
              "sys.stderr.write('x' * 3000 + '\\n'); watcher.bound_log(2000); sys.stderr.write('after\\n')")
    for where in (log, tmp_path / "mine.log"):  # the login item's log; a file of your own is left alone
        with open(where, "a") as f:  # launchd opens it for appending
            subprocess.run([sys.executable, "-c", script, str(logs)], stdout=f, stderr=f, check=True)
    assert log.read_text() == "after\n" and len((logs / "com.qualm.app.log.1").read_text()) == 3001
    assert (tmp_path / "mine.log").read_text() == "x" * 3000 + "\nafter\n"


def test_this_should_have_been_blocked_is_about_the_screen_in_front(monkeypatch, tmp_path):
    from qualm.decide import Reading, RuleVerdict
    from qualm.review import load_reviews, save_review
    from qualm.rules import Rule

    policy = Policy(Settings(allow_sites=("github.com",)), [Rule("shortvideo", "deny", "short videos", threshold=0.5)], tmp_path)
    watcher, events, _ = shots_watcher(monkeypatch, tmp_path, policy, lambda *a: Reading(
        0.0, "single_item", {}, "entertain", {}, [RuleVerdict("shortvideo", "violates", 0.9, {})], 100.0))
    watcher.dwell = 4.0
    watcher.shots = False
    video = ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title="A video", url="https://v.example/1")
    watcher._changed_at = w.time.monotonic()
    watcher._front_sig = video.signature()
    watcher._judge(types.SimpleNamespace(backend="kev"), video)
    ev = watcher.current()
    assert ev is not None and ev.screen is video and not events  # still waiting out the 4 s
    save_review(tmp_path, ev.id, verdict="should_block")
    watcher._settle_pending(video, w.time.monotonic() + 5)
    logged = json.loads((tmp_path / "judgements.jsonl").read_text().splitlines()[-1])
    assert logged["id"] == ev.id and load_reviews(tmp_path)[ev.id]["verdict"] == "should_block"
    # A page let through without asking the model can be flagged too.
    repo = ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title="repo", url="https://github.com/a/b")
    watcher._front_sig = repo.signature()
    watcher._judge(types.SimpleNamespace(backend="kev"), repo)
    assert watcher.current().screen is repo
    watcher._front_sig = ("com.apple.Safari", "elsewhere", "https://other.example/")
    assert watcher.current() is None  # moved on, not judged yet


def test_an_error_in_one_pass_never_stops_the_watcher(monkeypatch, tmp_path):
    statuses = []
    policy = Policy(Settings(allow_urls=("*broken",)), [], tmp_path)  # "nothing to repeat": it killed the thread
    clock = {"t": 0.0}
    front = types.SimpleNamespace(processIdentifier=lambda: -1)
    monkeypatch.setattr(w, "NSWorkspace", types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(frontmostApplication=lambda: front)))
    monkeypatch.setattr(w, "make_client", lambda settings=None: types.SimpleNamespace(backend="kev"))
    monkeypatch.setattr(w.time, "monotonic", lambda: clock["t"])
    watcher = w.Watcher(policy, lambda ev: None, statuses.append, interval=0, debounce=0.5, presence=False)
    asked = []

    def sleep(_):
        clock["t"] += 1.0
        if clock["t"] == 8:
            policy.settings.allow_urls = ()  # fixed in rules.toml
        if clock["t"] >= 20:
            watcher.stop.set()

    monkeypatch.setattr(w, "capture", lambda skip=(): page("https://a.example/"))
    monkeypatch.setattr(w.time, "sleep", sleep)
    monkeypatch.setattr(w, "ask", lambda *a: asked.append(clock["t"]) or w.NO_READING)
    monkeypatch.setattr(w.traceback, "print_exc", lambda: None)
    watcher.run()
    assert any("couldn't judge the screen (error: nothing to repeat" in s and "still watching" in s for s in statuses)
    assert asked  # and it went on judging once the pattern was fixed
