"""What capture() keeps of a window title."""

from qualm.state import _title


def test_browser_additions_are_dropped_from_titles():
    assert _title("(1) Notifications | LinkedIn - High memory usage - 1.4 GB - Google Chrome", "Google Chrome") \
        == "Notifications | LinkedIn"
    assert _title("Make Something | Y Combinator - Google Chrome (Incognito)", "Google Chrome") == "Make Something | Y Combinator"
    assert _title("Page - Google Chrome - Work", "Google Chrome") == "Page"


def test_other_titles_are_kept():
    assert _title("state.py — qualm", "Cursor") == "state.py — qualm"
    assert _title("Google Chrome", "Google Chrome") == "Google Chrome"
    assert _title("Qualm review", "Google Chrome") == "Qualm review"


# -- the AX walk and capture(), on made-up trees ------------------------------

import types

import pytest

from qualm import ocr
from qualm import state as st
from qualm.state import ScreenState, _walk


def node(role, children=(), **attrs):
    return {"AXRole": role, "AXChildren": list(children), **attrs}


def text(value):
    return node("AXStaticText", AXValue=value)


@pytest.fixture
def fake_ax(monkeypatch):
    monkeypatch.setattr(st, "_ax", lambda n, attr: n.get(attr) if isinstance(n, dict) else None)


AUTOFILL = node("AXPopover", [text("Apple Account"), text("Continue with Touch ID"), text("Tianrun Qiu")])


def test_safari_autofill_left_open_is_not_the_page(fake_ax):
    window = node("AXWindow", [AUTOFILL, node("AXGroup", [node("AXWebArea", [
        node("AXHeading", AXTitle="Calculus, lecture 3"), text("The definition of a limit")],
        AXURL="https://www.bilibili.com/video/BV1")])])
    s = ScreenState(app="Safari", bundle_id="com.apple.Safari")
    _walk(window, s)
    assert s.url == "https://www.bilibili.com/video/BV1"
    assert s.headings == ["Calculus, lecture 3"] and s.text == ["The definition of a limit"]


def test_text_found_before_the_page_is_dropped_when_it_turns_up(fake_ax):
    # Not a popover this time: a toolbar's text, read before the web area.
    window = node("AXWindow", [node("AXToolbar", [text("Sign in to your Apple Account for account.apple.com.")]),
                               node("AXGroup", [node("AXWebArea", [text("Hacker News front page")], AXURL="https://news.ycombinator.com/")])])
    s = ScreenState(app="Safari", bundle_id="com.apple.Safari")
    _walk(window, s)
    assert s.text == ["Hacker News front page"]


def test_autofill_lines_are_dropped_where_there_is_no_page(fake_ax):
    window = node("AXWindow", [node("AXGroup", [text("Continue with Touch ID"), text("Apple 账户"),
                                                text("Sign in to your Apple Account for account.apple.com."),
                                                text("Keynote slides for Monday")])])
    s = ScreenState(app="Some App", bundle_id="com.example.app")
    _walk(window, s)
    assert s.text == ["Keynote slides for Monday"]


def test_a_real_apple_sign_in_page_keeps_its_text(fake_ax):
    page = node("AXWebArea", [text("Apple Account"), text("Continue with Touch ID")], AXURL="https://account.apple.com/sign-in")
    s = ScreenState(app="Safari", bundle_id="com.apple.Safari")
    _walk(node("AXWindow", [page]), s)
    assert s.text == ["Apple Account", "Continue with Touch ID"]  # on the page itself, it's the page


def fake_front(monkeypatch, window, bundle_id="com.example.player", name="Player", pid=4242):
    app = types.SimpleNamespace(localizedName=lambda: name, bundleIdentifier=lambda: bundle_id, processIdentifier=lambda: pid)
    ws = types.SimpleNamespace(frontmostApplication=lambda: app)
    monkeypatch.setattr(st, "NSWorkspace", types.SimpleNamespace(sharedWorkspace=lambda: ws))
    monkeypatch.setattr(st, "AXIsProcessTrusted", lambda: True)
    monkeypatch.setattr(st, "AXUIElementCreateApplication", lambda p: {"AXFocusedWindow": window})
    monkeypatch.setattr(st, "AXUIElementSetAttributeValue", lambda *a: None)
    monkeypatch.setattr(st, "_frame", lambda w: [0, 0, 800, 600])
    monkeypatch.setattr(st, "_browser_url", lambda b: "")
    monkeypatch.setattr(st, "_switched_on", set())


def test_a_tree_switched_on_just_now_gets_one_more_look(fake_ax, monkeypatch):
    area = node("AXWebArea", [], AXURL="https://example.com/")
    window = node("AXWindow", [area], AXTitle="Example")
    slept = []

    def sleep(s):  # the tree fills in while we wait, as Chromium's does
        slept.append(s)
        area["AXChildren"] = [text("Example Domain body text")]

    fake_front(monkeypatch, window, "com.google.Chrome", "Google Chrome")
    monkeypatch.setattr(st.time, "sleep", sleep)
    assert st.capture().text == ["Example Domain body text"] and slept == [st.FIRST_WAIT_S]
    area["AXChildren"] = []
    assert st.capture().text == [] and len(slept) == 1  # once per process, not on every empty page


def test_a_window_that_draws_its_text_is_read_from_a_picture(fake_ax, monkeypatch):
    fake_front(monkeypatch, node("AXWindow", [node("AXGroup")], AXTitle="Movie.mp4"))
    monkeypatch.setattr(st.time, "sleep", lambda s: None)
    asked = []
    monkeypatch.setattr(ocr, "window_text", lambda pid, frame, title: asked.append(pid) or ["弹幕 · 点赞 12.3万", "Movie.mp4", "Next up"])
    s = st.capture()
    assert s.text == ["弹幕 · 点赞 12.3万", "Next up"] and s.ocr and asked == [4242]  # the title isn't repeated
    assert s.as_record()["ocr"] is True


def test_windows_that_are_never_pictured(fake_ax, monkeypatch):
    monkeypatch.setattr(st.time, "sleep", lambda s: None)
    monkeypatch.setattr(ocr, "window_text", lambda *a: pytest.fail("pictured"))
    # A terminal or editor: its text is in a text area (anything typed there stays unread).
    fake_front(monkeypatch, node("AXWindow", [node("AXTextArea")], AXTitle="zsh"), "com.apple.Terminal", "Terminal")
    assert not st.capture().ocr
    # A browser, even on an empty page; and a window with enough text of its own.
    fake_front(monkeypatch, node("AXWindow", [], AXTitle="New Tab"), "com.apple.Safari", "Safari")
    assert not st.capture().ocr
    fake_front(monkeypatch, node("AXWindow", [text("A settings pane with enough words")], AXTitle="Settings"))
    assert not st.capture().ocr


def test_records_without_ocr_are_unchanged():
    s = ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title="t", text=["x"])
    assert "ocr" not in s.as_record() and "ocr" not in str(s.to_state())
    assert ScreenState(**s.as_record()) == s  # labels and logs still load
