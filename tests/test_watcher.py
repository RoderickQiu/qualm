"""The watch loop's timing: when it asks the model, with a fake screen and clock."""

import types

from seenot_desktop import watcher as w
from seenot_desktop.policy import Policy
from seenot_desktop.rules import Settings
from seenot_desktop.state import ScreenState


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
    watcher = w.Watcher(Policy(Settings(), [], tmp_path), lambda ev: None, interval=0, debounce=0.5, recheck=30)

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
