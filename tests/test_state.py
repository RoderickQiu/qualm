"""What capture() keeps of a window title."""

from qualm.state import _title


CHROME = "com.google.Chrome"


def test_browser_additions_are_dropped_from_titles():
    assert _title("(1) Notifications | LinkedIn - High memory usage - 1.4 GB - Google Chrome", "Google Chrome", CHROME) \
        == "Notifications | LinkedIn"
    assert _title("Make Something | Y Combinator - Google Chrome (Incognito)", "Google Chrome") == "Make Something | Y Combinator"
    assert _title("Page - Google Chrome - Work", "Google Chrome") == "Page"


def test_other_titles_are_kept():
    assert _title("state.py — qualm", "Cursor") == "state.py — qualm"
    assert _title("Google Chrome", "Google Chrome") == "Google Chrome"
    assert _title("Qualm review", "Google Chrome") == "Qualm review"
    assert _title("Chapter 3 - Limits - Calculus", "Safari") == "Chapter 3 - Limits - Calculus"
    assert _title("Top 10 - 2026 - Charts", "Google Chrome", CHROME) == "Top 10 - 2026 - Charts"
    # Only Chromium adds a memory label: elsewhere a size is the product's or the file's.
    assert _title("iPhone 16 Pro - Desert Titanium - 256 GB", "Safari", "com.apple.Safari") == \
        "iPhone 16 Pro - Desert Titanium - 256 GB"
    assert _title("ubuntu-24.04-desktop-amd64.iso - Downloads - 5.7 GB", "Finder", "com.apple.finder") == \
        "ubuntu-24.04-desktop-amd64.iso - Downloads - 5.7 GB"


def test_chromes_memory_label_is_dropped_in_its_other_languages():
    # Chrome's own strings (its locale.pak), after _clean turned the no-break spaces into spaces.
    for title in ("Funny cats - YouTube - Memory usage - 312 MB - Google Chrome",
                  "Funny cats - YouTube - High memory usage - 1,2 GB - Google Chrome",
                  "Funny cats - YouTube – Hohe Arbeitsspeichernutzung – 1,2 GB – Google Chrome",
                  "Funny cats - YouTube - 内存用量高 - 1.2 GB - Google Chrome",
                  "Funny cats - YouTube - メモリを大量に使用 - 1.2 GB - Google Chrome",
                  "Funny cats - YouTube - Utilisation élevée de la mémoire - 1,2 Go - Google Chrome",
                  "Funny cats - YouTube – голямо количество използвана памет: 1,2 ГБ - Google Chrome"):
        assert _title(title, "Google Chrome", CHROME) == "Funny cats - YouTube", title
    # The number changing is no longer a new screen.
    assert _title("Seite – Hohe Arbeitsspeichernutzung – 1,3 GB", "Google Chrome", CHROME) == "Seite"


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
    # A browser's page, even an empty one, or its own new-tab page; and a window with enough text of its own.
    fake_front(monkeypatch, node("AXWindow", [node("AXWebArea", [], AXURL="")], AXTitle="Loading"), "com.apple.Safari", "Safari")
    assert not st.capture().ocr
    fake_front(monkeypatch, node("AXWindow", [node("AXWebArea", [], AXURL="chrome://newtab/")], AXTitle="New Tab"),
               "com.google.Chrome", "Google Chrome")
    assert not st.capture().ocr
    fake_front(monkeypatch, node("AXWindow", [text("A settings pane with enough words")], AXTitle="Settings"))
    assert not st.capture().ocr


def test_a_browser_with_no_web_tree_is_read_from_a_picture(fake_ax, monkeypatch):
    # Firefox with its accessibility tree off: a title and a toolbar, no page; every rule would skip it.
    fake_front(monkeypatch, node("AXWindow", [node("AXToolbar", [node("AXButton")])], AXTitle="Funny cat #shorts - YouTube"),
               "org.mozilla.firefox", "Firefox")
    monkeypatch.setattr(st.time, "sleep", lambda s: None)
    monkeypatch.setattr(ocr, "window_text", lambda pid, frame, title: ["Funny cat compilation", "1.2M views"])
    s = st.capture()
    assert s.ocr and s.text == ["Funny cat compilation", "1.2M views"]


def test_records_without_ocr_are_unchanged():
    s = ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title="t", text=["x"])
    assert "ocr" not in s.as_record() and "ocr" not in str(s.to_state())
    assert ScreenState(**s.as_record()) == s  # labels and logs still load


def test_a_page_inside_the_browsers_own_web_area_is_found(fake_ax):
    # Vivaldi's window is a web page itself, with the tab's page inside it.
    ui = node("AXWebArea", [node("AXGroup", [text("Tabs: Funny cat, Docs")]), node("AXGroup", [node("AXWebArea", [
        node("AXHeading", AXTitle="Funny cat #shorts"), text("Subscribe to the channel")],
        AXURL="https://www.youtube.com/shorts/abc")])], AXURL="chrome-extension://mpognobbkildjkofajifpdfhcoklimli/browser.html")
    s = ScreenState(app="Vivaldi", bundle_id="com.vivaldi.Vivaldi")
    _walk(node("AXWindow", [ui]), s)
    assert s.url == "https://www.youtube.com/shorts/abc"
    assert s.headings == ["Funny cat #shorts"] and s.text == ["Subscribe to the channel"]


def test_the_largest_web_area_is_the_page_not_a_sidebar(fake_ax, monkeypatch):
    monkeypatch.setattr(st, "_frame", lambda n: n.get("frame", []))
    chat = node("AXWebArea", [text("Mum: dinner at 7?")], AXURL="https://web.whatsapp.com/", frame=[0, 0, 360, 900])
    tab = node("AXWebArea", [text("Funny cat compilation")], AXURL="https://www.youtube.com/watch?v=1", frame=[360, 0, 1080, 900])
    s = ScreenState(app="Opera", bundle_id="com.operasoftware.Opera")
    _walk(node("AXWindow", [chat, node("AXGroup", [tab])]), s)  # the sidebar comes first, and shallower
    assert s.url == "https://www.youtube.com/watch?v=1" and s.text == ["Funny cat compilation"]


def test_a_long_side_panel_of_the_browsers_own_leaves_the_page_its_text(monkeypatch):
    # Chrome's History side panel is a web area of Chrome's own: 1600 items used up the walk before the page.
    calls = []
    monkeypatch.setattr(st, "_ax", lambda n, attr: calls.append(attr) or n.get(attr))
    items = [node("AXGroup", [node("AXLink", [text(f"History item {i} - some page title")])]) for i in range(1600)]
    panel = node("AXGroup", [node("AXWebArea", [node("AXGroup", items)], AXURL="chrome://history-side-panel.top-chrome/")])
    page = node("AXWebArea", [node("AXHeading", AXTitle="Funny cat compilation"), text("1.2M views · 3 days ago")],
                AXURL="https://www.reddit.com/r/funny/")
    toolbar = node("AXToolbar", [text("Back Forward Reload")])
    s = ScreenState(app="Google Chrome", bundle_id="com.google.Chrome")
    _walk(node("AXWindow", [toolbar, panel, node("AXGroup", [node("AXGroup", [page])])]), s)
    assert s.url == "https://www.reddit.com/r/funny/"
    assert s.headings == ["Funny cat compilation"] and s.text == ["1.2M views · 3 days ago"]
    assert len(calls) < 60  # and the panel isn't walked at all
    # With no page outside it, the browser's own web areas are looked into, for so long.
    calls.clear()
    s = ScreenState(app="Vivaldi", bundle_id="com.vivaldi.Vivaldi")
    _walk(node("AXWindow", [node("AXWebArea", [node("AXGroup", items), page], AXURL="chrome://vivaldi-webui/")]), s)
    assert s.url == "https://www.reddit.com/r/funny/" and s.headings == ["Funny cat compilation"]
    # A list there (its tab bar, a panel) is walked last: the page deeper down is found first.
    calls.clear()
    s = ScreenState(app="Vivaldi", bundle_id="com.vivaldi.Vivaldi")
    _walk(node("AXWindow", [node("AXWebArea", [node("AXGroup", items), node("AXGroup", [page])],
                                 AXURL="chrome://vivaldi-webui/")]), s)
    assert s.url == "https://www.reddit.com/r/funny/" and len(calls) < 60
    # Past UI_NODES of Vivaldi's own that isn't a list, the walk stops looking.
    calls.clear()
    s = ScreenState(app="Vivaldi", bundle_id="com.vivaldi.Vivaldi")
    groups = [node("AXGroup", items[i:i + 20]) for i in range(0, 1600, 20)]
    panels = [node("AXGroup", groups[i:i + 20]) for i in range(0, 80, 20)]
    _walk(node("AXWindow", [node("AXWebArea", [*panels, node("AXGroup", [node("AXGroup", [page])])],
                                 AXURL="chrome://vivaldi-webui/")]), s)
    assert s.url == "" and len(calls) < 4 * st.UI_NODES


@pytest.mark.parametrize("tabs", [50, 400, 700])
def test_hundreds_of_tabs_leave_the_page_its_budget(monkeypatch, tabs):
    # Chrome's tab strip, and Vivaldi's, which is HTML inside its own web area:
    # every tab is met before the page, breadth first, and 400 used up the walk.
    calls = []
    monkeypatch.setattr(st, "_ax", lambda n, attr: calls.append(attr) or n.get(attr))
    page = node("AXWebArea", [node("AXHeading", AXTitle="Funny cat compilation"), text("1.2M views · 3 days ago")],
                AXURL="https://www.youtube.com/shorts/abc")
    strip = node("AXGroup", [node("AXTabGroup", [
        node("AXRadioButton", [text(f"Tab title number {i}"), node("AXImage"), node("AXButton", AXTitle="Close")])
        for i in range(tabs)])])
    body = node("AXGroup", [node("AXGroup", [node("AXGroup", [page])])])
    s = ScreenState(app="Google Chrome", bundle_id="com.google.Chrome")
    _walk(node("AXWindow", [strip, node("AXToolbar", [text("youtube.com/shorts/abc")]), body]), s)
    assert s.url == "https://www.youtube.com/shorts/abc" and s.headings == ["Funny cat compilation"]
    assert s.text == ["1.2M views · 3 days ago"] and len(calls) < 60  # the tabs aren't walked at all
    calls.clear()
    bar = node("AXGroup", [node("AXGroup", [node("AXGroup", [text(f"Tab {i} title"), node("AXImage"),
                                                                node("AXButton", AXTitle="Close tab")])
                                             for i in range(tabs)])])
    buttons = node("AXGroup", [node("AXButton", AXTitle=f"b{i}") for i in range(20)])
    content = node("AXGroup", [node("AXGroup", [node("AXGroup", [page])])])
    ui = node("AXWebArea", [bar, buttons, content], AXURL="chrome-extension://mpognobbkildjkofajifpdfhcoklimli/browser.html")
    s = ScreenState(app="Vivaldi", bundle_id="com.vivaldi.Vivaldi")
    _walk(node("AXWindow", [ui]), s)
    assert s.url == "https://www.youtube.com/shorts/abc" and s.headings == ["Funny cat compilation"]
    assert len(calls) < 150


def test_a_browser_qualm_doesnt_list_is_known_by_its_window(fake_ax, monkeypatch):
    page = node("AXWebArea", [node("AXHeading", AXTitle="Funny cat")], AXURL="https://www.youtube.com/shorts/abc")
    tabs = node("AXTabGroup", [node("AXRadioButton", AXTitle=f"Tab {i}") for i in range(3)])
    address = node("AXToolbar", [node("AXTextField", AXValue="youtube.com/shorts/abc")])
    windows = {
        "tabs": node("AXWindow", [node("AXGroup", [tabs]), node("AXGroup", [page])]),
        "address field": node("AXWindow", [address, node("AXGroup", [page])]),
        # Slack or another web app: its tabs, if any, are the page's own.
        "web app": node("AXWindow", [node("AXWebArea", [node("AXTabGroup", []), node("AXTextField", AXValue="youtube.com")],
                                          AXURL="https://app.slack.com/client/T1")]),
        "no web page": node("AXWindow", [tabs, node("AXWebArea", [], AXURL="file:///Users/me/notes.html")]),
        "another site in the field": node("AXWindow", [node("AXTextField", AXValue="search.example"), node("AXGroup", [page])]),
    }
    looks = {}
    for name, window in windows.items():
        monkeypatch.setattr(st, "AXUIElementCreateApplication", lambda pid, w=window: {"AXFocusedWindow": w})
        looks[name] = st.browser_window(4242)
    assert looks == {"tabs": True, "address field": True, "web app": False, "no web page": False,
                     "another site in the field": False}


def test_take_me_back_reads_the_address_through_accessibility(fake_ax, monkeypatch):
    page = node("AXWebArea", [], AXURL="https://www.youtube.com/shorts/abc")
    window = node("AXWindow", [node("AXGroup", [page])], AXTitle="Funny cat - YouTube")
    monkeypatch.setattr(st, "AXUIElementCreateApplication", lambda pid: {"AXFocusedWindow": window})
    assert st.page_address(4242) == "https://www.youtube.com/shorts/abc"
    assert st.page_address(4242, title=True) == "Funny cat - YouTube"
    page["AXURL"] = None
    assert st.page_address(4242) is None


def test_password_managers_are_never_read(fake_ax, monkeypatch):
    window = node("AXWindow", [text("github.com  me@example.com")], AXTitle="Passwords")
    fake_front(monkeypatch, window, "com.apple.Passwords", "Passwords")
    monkeypatch.setattr(ocr, "window_text", lambda *a: pytest.fail("pictured"))
    s = st.capture()  # no no_monitor given at all
    assert (s.app, s.window_title, s.text, s.url) == ("Passwords", "", [], "")
    fake_front(monkeypatch, window, "com.figma.Desktop", "Figma")
    assert st.capture(skip=("figma",)).window_title == ""  # no_monitor by name


def test_an_app_is_found_by_its_name_or_bundle_id():
    assert st.installed_app("safari") == ("com.apple.Safari", "Safari")
    assert st.installed_app("COM.APPLE.SAFARI")[0] == "com.apple.Safari"
    assert st.installed_app("No Such App 4242") is None
    assert st.installed_app("/System/Applications/Mail.app") == ("com.apple.mail", "Mail")  # dragged from Finder
    assert st.installed_app("/Applications/No Such App 4242.app") is None


def test_ocr_reads_the_languages_set_on_the_mac(monkeypatch):
    import Foundation

    def prefer(*tags):
        monkeypatch.setattr(Foundation, "NSLocale", types.SimpleNamespace(preferredLanguages=lambda: list(tags)))
        ocr.languages.cache_clear()
        return ocr.languages()

    try:
        # Chinese and Korean first: they read Latin letters too, English first lost a mixed line's Chinese.
        assert prefer("en-HK", "zh-Hans-HK", "ko-KR") == ["zh-Hans", "ko-KR", "en-US"]
        # Chinese is always there and leads, so "打开 Settings 设置" reads on any Mac.
        assert prefer("de-AT") == ["zh-Hans", "de-DE", "en-US"]
        assert prefer("ru-RU", "uk-UA") == ["zh-Hans", "ru-RU", "uk-UA", "en-US"]
        assert prefer("ko-KR", "ja-JP") == ["zh-Hans", "ko-KR", "ja-JP", "en-US"]
        assert prefer("zh-Hant-TW", "en-US") == ["zh-Hant", "zh-Hans", "en-US"]
    finally:
        ocr.languages.cache_clear()
