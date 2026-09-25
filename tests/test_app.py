"""The menu bar app and the setup window, built off-screen: no window is ever
ordered in, no status item made, no prompt asked, no key stored, no model."""

import json
import subprocess
import sys
import types

import pytest
from AppKit import NSApplication, NSButton, NSEvent, NSTextField

from qualm import about, app, autostart, keychain, localmodel, onboard, paths, ui, updates
from qualm import setup as s
from qualm.decide import Reading, RuleVerdict
from qualm.explain import cut, headline, reason
from qualm.policy import Decision
from qualm.rules import Rule
from qualm.state import ScreenState
from qualm.watcher import Event

NSApplication.sharedApplication().setActivationPolicy_(2)  # no Dock icon, never active


class OffscreenPanel(ui.QualmPanel):
    """A panel that never goes on screen: ordering in flips a flag."""

    def makeKeyAndOrderFront_(self, sender):
        self._shown = True

    def orderFrontRegardless(self):
        self._shown = True

    def orderOut_(self, sender):
        self._shown = False

    def isVisible(self):
        return getattr(self, "_shown", False)


class OffscreenAbout(about.AboutPanel):
    """The About window, never on screen."""

    def makeKeyAndOrderFront_(self, sender):
        self._shown = getattr(self, "_shown", 0) + 1

    def isVisible(self):
        return bool(getattr(self, "_shown", 0))


def offscreen_hud(width, height, title, hud=ui.hud):
    real, ui.QualmPanel = ui.QualmPanel, OffscreenPanel  # swapped only while it builds
    try:
        return hud(width, height, title)
    finally:
        ui.QualmPanel = real


class Item:
    """A status item's stand-in: the menu and a button, no menu bar."""

    def __init__(self):
        self.button_ = types.SimpleNamespace(setImage_=lambda i: setattr(self, "image", i),
                                             setTitle_=lambda t: None, setToolTip_=lambda t: setattr(self, "tip", t))
        self.image = self.tip = self.menu = None

    def setAutosaveName_(self, name):
        pass

    def setVisible_(self, on):
        pass

    def setMenu_(self, menu):
        self.menu = menu

    def button(self):
        return self.button_


class Dimmer:
    windows = []

    def show(self, block=False):
        pass

    def hide(self, animate=True):
        pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A first run's rules in a throwaway home, and everything that would reach the screen or the system faked."""
    monkeypatch.setenv("QUALM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("QUALM_BACKEND", raising=False)
    # A pop-up asks the model which part of the screen carried it: never the model server this Mac runs on :8009.
    monkeypatch.setenv("KEV_URL", "http://127.0.0.1:9")
    monkeypatch.chdir(tmp_path)
    w = types.SimpleNamespace(access=True, key=None, check=None, stored=[], asked=[], opened=[], backs=[])
    monkeypatch.setattr(ui, "hud", offscreen_hud)
    monkeypatch.setattr(ui, "Dimmer", Dimmer)
    monkeypatch.setattr(app, "NSStatusBar", types.SimpleNamespace(
        systemStatusBar=lambda: types.SimpleNamespace(statusItemWithLength_=lambda n: Item())))
    monkeypatch.setattr(app, "NSApp", types.SimpleNamespace(activateIgnoringOtherApps_=lambda on: None))
    monkeypatch.setattr(app, "subprocess", types.SimpleNamespace(run=lambda argv, **kw: w.opened.append(argv)))
    monkeypatch.setattr(app, "go_back", lambda screen, fine=None, done=None: w.backs.append(screen.url))
    monkeypatch.setattr(localmodel, "ManagedServer", lambda *a, **kw: types.SimpleNamespace(
        start=lambda: None, stop=lambda: None, proc=None, ready=types.SimpleNamespace(is_set=lambda: True)))
    monkeypatch.setattr(localmodel, "apple_silicon", lambda: True)
    monkeypatch.setattr(keychain, "api_key", lambda: w.key)
    monkeypatch.setattr(keychain, "store", lambda key: (w.stored.append(key), setattr(w, "key", key)))
    monkeypatch.setattr(s, "accessibility", lambda: w.access)
    monkeypatch.setattr(s, "ask_accessibility", lambda: w.asked.append("accessibility"))
    monkeypatch.setattr(s, "check_key", lambda key: w.check)
    monkeypatch.setattr(autostart, "installed", lambda: False)
    monkeypatch.setenv("QUALM_UPDATE_FEED", "http://127.0.0.1:9/appcast.xml")  # never GitHub from a test
    s.apply(s.Choices("kev", ["shortvideo", "feeds", "livestream", "videos", "social"], login=False, shim=False))
    return w


def controller():
    from qualm.policy import Policy
    from qualm.rules import load_config

    ctrl = app.Controller.alloc().init().setup(Policy(*load_config(paths.rules_file()), paths.data_dir()),
                                               str(paths.rules_file()))
    ctrl.timer.invalidate()
    return ctrl


def menu_titles(menu):
    return [str(menu.itemAtIndex_(i).title()) for i in range(menu.numberOfItems())
            if not menu.itemAtIndex_(i).isHidden() and not menu.itemAtIndex_(i).isSeparatorItem()]


def item(menu, title):
    for i in range(menu.numberOfItems()):
        it = menu.itemAtIndex_(i)
        if str(it.title()).startswith(title):
            return it
        if it.submenu() is not None and (found := item(it.submenu(), title)) is not None:
            return found
    return None


def pop_up(ctrl, rule, url, panel="", reason="p_hit 0.70 >= 0.25"):
    screen = ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title="A page", url=url)
    reading = Reading(0.0, "single_item", {"single_item": 0.9}, "entertain", {"entertain": 0.9},
                      [RuleVerdict(rule, "violates", 0.7, {})], 800.0)
    d = Decision("intervene", rule, reason, f"d-{rule}", panel)
    ctrl.policy.log_intervention(d, screen.as_record(), reading)
    ctrl.handle(Event(screen, {"url": url, "headings": ["h"]}, reading, [d]))
    return d


def press_return(panel):
    panel.sendEvent_(NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        10, (0, 0), 0, 0, panel.windowNumber(), None, "\r", "\r", False, 36))


# -- words ---------------------------------------------------------------------


def test_short_answers_count_in_any_script():
    # 工作 (work) and 学习 (study) are whole answers; so is one CJK character.
    assert all(app.enough(a) for a in ("工作", "学", "hw", "ok"))
    assert not any(app.enough(a) for a in ("", "x"))


def test_text_is_cut_at_a_word_with_an_ellipsis():
    assert cut("comparing three pairs of noise-cancelling earbuds for my commute", 40) == "comparing three pairs of…"
    assert cut("short enough", 40) == "short enough"
    assert cut("Friday deadline, then the slides", 18) == "Friday deadline…"  # no ", …"
    assert cut("为了写论文查资料顺便看看这个视频", 12) == "为了写论文…"  # a CJK character is two wide
    assert cut("hello world foo", 12) == "hello world…"  # it ends at a word already: nothing more dropped
    # The only space is in a prefix: cut mid-word (by width) rather than keep only the prefix.
    assert cut("You're here to: 准备明天下午组会要讲的实验报告和", 46) == "You're here to: 准备明天下午组会要讲的实验报…"


def test_wrapped_text_is_measured_as_it_wraps():
    # fittingSize measured this a line short at 442 pt, and "up." was cut off.
    head = ui.label("", 21, 0.3, width=442, wrap=True)
    text = "Your 5 more minutes for “the keynote everyone's talking about at the offsite” are up."
    h = ui.fit(head, text, 442)
    assert h >= head.cell().cellSizeForBounds_(((0, 0), (442, 10000))).height and h >= 3 * 24


def test_rules_in_other_scripts_are_named_by_their_words():
    assert s.rule_name("duanshipin", "刷短视频和直播，特别是抖音上的") == "刷短视频和直播"  # not "Duanshipin"
    assert s.rule_name("akhbar", "مواقع الأخبار والعناوين") == "مواقع الأخبار والعناوين"
    assert s.rule_name("late_news", "news sites — after 10pm") == "Late news"  # English: the id, as before
    assert s.rule_name("shortvideo", "anything") == "Short videos"
    rule = Rule("duanshipin", "deny", "刷短视频和直播，特别是抖音上的")
    assert headline(Decision("intervene", "duanshipin", "p_hit 0.9 >= 0.2"), rule, "en")[0] == "刷短视频和直播"


def test_your_own_rules_are_quoted_not_fitted_into_the_sentence():
    news = Rule("news", "check_in", "news sites and headlines")
    d = Decision("intervene", "news", "p_hit 0.9 >= 0.2")
    assert headline(d, news, "en") == ("News", "This looks like it's under your rule: news sites and headlines.")
    assert reason(Decision("intervene", "news", "x", "d", "check_in"), None, news, "en").startswith(
        "This looks like it's under your rule: news sites and headlines. Say what for")
    # The starters keep their wording.
    short = Rule("shortvideo", "deny", "short videos made for endless swiping, such as TikTok")
    assert headline(d, short, "en")[1] == "This looks like short videos made for endless swiping."
    long = Rule("doom", "deny", "anything that is basically news or politics or outrage that I end up doomscrolling "
                                "late at night when I should be sleeping")
    head = headline(d, long, "en")[1]
    assert head.endswith("…") and len(head) < 140


def test_hosted_trouble_is_read_from_the_watchers_status():
    assert app.model_trouble("model unreachable: TypeSafeAuthenticationError") == "key"
    assert app.model_trouble("model unreachable: TypeSafePermissionDeniedError") == "key"
    assert app.model_trouble("no TypeSafe API key: `qualm setup` stores one in the keychain") == "nokey"  # none to turn down
    assert app.model_trouble("model unreachable: TypeSafeRateLimitError") == "quota"
    assert app.model_trouble("model unreachable: TypeSafeAPIConnectionError") == "down"
    assert app.model_trouble("watching") == ""


def test_a_key_check_that_couldnt_reach_typesafe_isnt_called_a_bad_key():
    assert onboard.key_trouble("TypeSafeAPIConnectionError: Connection error") == \
        "Couldn't reach TypeSafe. Check your internet connection and try again."
    assert onboard.key_trouble("TypeSafeAuthenticationError: 401").startswith("That key didn't work")


# -- the menu bar --------------------------------------------------------------


def test_missing_accessibility_stays_in_the_menu_until_granted(world):
    world.access = False
    ctrl = controller()
    menu = ctrl.status_item.menu
    ctrl.set_status("Google Chrome: nothing")  # a judgement doesn't hide it
    assert menu_titles(menu)[0] == "Qualm can't read windows: allow Accessibility…"
    assert "can't read windows" in ctrl.status_item.tip
    item(menu, "Qualm can't read windows").target().grantAccess_(None)
    assert world.asked == ["accessibility"]
    world.access = True
    ctrl.tick_(None)  # granted: gone by the next tick, no restart
    assert menu_titles(menu)[0] == "Google Chrome: nothing" and ctrl.status_item.tip == "Qualm is watching"


def test_hosted_trouble_shows_with_its_fix_and_a_notice_once(world):
    world.key = "ts-key"
    ctrl = controller()
    ctrl._use("jev")
    ctrl.set_status("model unreachable: TypeSafeAuthenticationError")
    menu = ctrl.status_item.menu
    assert menu_titles(menu)[:2] == ["Your TypeSafe key doesn't work: add a TypeSafe key…",
                                     "hosted model: TypeSafe turned down the key"]
    assert ctrl.notice.isVisible() and str(ctrl.notice_fix.title()) == "Add a TypeSafe key…"
    ctrl.notice.orderOut_(None)
    ctrl.set_status("model unreachable: TypeSafeAuthenticationError")
    assert not ctrl.notice.isVisible()  # told once
    for _ in range(app.MODEL_ERRORS):
        ctrl.set_status("model unreachable: TypeSafeInternalServerError")
    assert menu_titles(menu)[0] == "TypeSafe isn't answering: use the model on this Mac"
    ctrl.fixModel_(None)
    assert ctrl.policy.settings.backend == "kev" and "model" not in ctrl.problems


def test_hosted_chosen_from_the_cli_or_an_agent_stops_the_local_model(world, monkeypatch):
    from qualm.config import Config
    from qualm.rules import load_config

    stopped = []
    monkeypatch.setattr(localmodel, "ManagedServer", lambda *a, **kw: types.SimpleNamespace(
        start=lambda: None, stop=lambda: stopped.append(1), proc=None, failing=True, hint="",
        status="no internet, or Hugging Face is blocked here; retrying in 15 s",
        ready=types.SimpleNamespace(is_set=lambda: False)))
    ctrl = controller()
    ctrl.ensure_server()
    ctrl._refresh()
    assert ctrl.status_item.tip.startswith("Qualm isn't watching yet: no internet")
    # `qualm settings set backend=jev` (or an agent): rules.toml changes, the watcher reloads it.
    Config(paths.rules_file()).edit_settings({"backend": "jev"}, [])
    ctrl.policy.reload(*load_config(paths.rules_file()))
    ctrl.tick_(None)
    assert stopped == [1] and ctrl.server is None  # no more retries, no 5 GB download nobody wants
    assert ctrl.status_item.tip == "Qualm is watching" and str(ctrl.status_line.title()) == "model: hosted by TypeSafe"
    ctrl.tick_(None)
    assert stopped == [1]


def test_a_key_can_be_added_from_the_menu_and_is_checked_first(world):
    ctrl = controller()
    menu = ctrl.status_item.menu
    ctrl.menuWillOpen_(menu)
    hosted = item(menu, "Hosted by TypeSafe")
    assert hosted.isEnabled() and str(hosted.title()) == "Hosted by TypeSafe (Jev)…"  # no key yet: it asks for one
    ctrl.pickModel_(hosted)
    assert ctrl.key_panel.isVisible() and ctrl.policy.settings.backend == "kev"
    ctrl._key_checked("ts-bad", "TypeSafeAuthenticationError: 401", ctrl._key_check)
    assert world.stored == [] and str(ctrl.key_status.stringValue()).startswith("That key didn't work")
    ctrl._key_checked("ts-good", None, ctrl._key_check)
    assert world.stored == ["ts-good"] and ctrl.policy.settings.backend == "jev" and not ctrl.key_panel.isVisible()
    ctrl.menuWillOpen_(menu)
    assert item(menu, "Replace the TypeSafe key…") is not None


def test_cancel_drops_a_key_check_still_on_its_way(world, monkeypatch):
    checks = []
    monkeypatch.setattr(app, "threading", types.SimpleNamespace(Thread=lambda target, args, daemon: (
        types.SimpleNamespace(start=lambda: checks.append((target, args))))))
    monkeypatch.setattr(app, "AppHelper", types.SimpleNamespace(callAfter=lambda f, *a: f(*a)))
    ctrl = controller()
    ctrl.openKey_(None)
    ctrl.key_field.setStringValue_("ts-typo")
    ctrl.keySave_(None)
    ctrl.keyCancel_(None)  # while it says "Checking the key with TypeSafe…"
    target, args = checks.pop()
    target(*args)  # TypeSafe answers after all
    assert world.stored == [] and ctrl.policy.settings.backend == "kev"
    ctrl.openKey_(None)
    ctrl.key_field.setStringValue_("ts-good")
    ctrl.keySave_(None)  # not stuck "busy" on the dropped check
    target, args = checks.pop()
    target(*args)
    assert world.stored == ["ts-good"] and ctrl.policy.settings.backend == "jev"


def test_a_keychain_that_wont_save_is_said_once_in_words(world, monkeypatch):
    def refuse(key):
        raise RuntimeError("couldn't save the key in the keychain: security: SecKeychainItemCreateFromContent "
                           "(<default>): User interaction is not allowed.")

    monkeypatch.setattr(keychain, "store", refuse)
    ctrl = controller()
    ctrl.openKey_(None)
    ctrl._key_checked("ts-abc", None, ctrl._key_check)
    status = ctrl.key_status
    assert str(status.stringValue()).startswith("TypeSafe accepted the key, but macOS wouldn't save it")
    assert "security:" not in str(status.stringValue()) and "User interaction" in str(status.toolTip())
    assert status.cell().cellSizeForBounds_(((0, 0), (status.frame().size.width, 10000))).height <= \
        status.frame().size.height  # not cut off


def test_no_saved_key_isnt_called_a_turned_down_one(world):
    world.key = "ts-key"
    ctrl = controller()
    ctrl._use("jev")
    world.key = None  # removed, or the keychain won't give it
    ctrl.set_status("no TypeSafe API key: `qualm setup` stores one in the keychain")
    menu = ctrl.status_item.menu
    assert menu_titles(menu)[:2] == ["No TypeSafe key is saved: add a TypeSafe key…", "hosted model: no TypeSafe key is saved"]
    ctrl.menuWillOpen_(menu)
    assert item(menu, "Add a TypeSafe key…") is not None and item(menu, "Replace the TypeSafe key…") is None


def test_hosted_trouble_never_offers_a_local_model_that_cant_run(world, monkeypatch):
    monkeypatch.setattr(localmodel, "unsupported", lambda: "The local model needs macOS 14 or later.")
    world.key = "ts-key"
    ctrl = controller()
    ctrl._use("jev")
    for _ in range(app.MODEL_ERRORS):
        ctrl.set_status("model unreachable: TypeSafeAPIConnectionError")
    line = item(ctrl.status_item.menu, "TypeSafe isn't answering")
    assert str(line.title()) == "TypeSafe isn't answering: check your internet connection" and not line.isEnabled()
    assert "this Mac" not in str(ctrl.notice_text.stringValue()) and ctrl.notice_fix.isHidden()


def test_the_icon_shows_the_first_download(world, monkeypatch):
    names = []
    real = app.NSImage
    monkeypatch.setattr(app, "NSImage", types.SimpleNamespace(imageWithSystemSymbolName_accessibilityDescription_=(
        lambda name, d: (names.append(name), real.imageWithSystemSymbolName_accessibilityDescription_(name, d))[1])))
    ctrl = controller()
    ready = types.SimpleNamespace(is_set=lambda: False)
    ctrl.server = types.SimpleNamespace(ready=ready, failing=False, hint="", status="downloading the model: 1.2 of 4.5 GB…")
    ctrl._refresh()
    ctrl.server.status = "loading the model…"
    ctrl._refresh()
    assert names[-2:] == ["arrow.down.circle", "hourglass"]  # not the eye: nothing is judged yet


def test_a_broken_rules_file_never_raises_from_the_menu(world, monkeypatch):
    from qualm import agent

    monkeypatch.setattr(agent, "command", lambda: "/Applications/Qualm.app/Contents/MacOS/Qualm -m qualm")
    ctrl = controller()
    path = paths.rules_file()
    path.write_text(path.read_text().replace("allow_intentional = true", "allow_intentional = tru", 1))
    menu = ctrl.status_item.menu
    ctrl.menuWillOpen_(menu)
    watch = item(menu, "Watch for").submenu()
    # The mistake itself, not the file's path; and a command a stock shell can run.
    assert menu_titles(watch)[0].startswith("rules.toml doesn't load: Invalid value (at line")
    assert menu_titles(watch)[2] == "Or undo the last change in Terminal: /Applications/Qualm.app/Contents/MacOS/Qualm " \
                                    "-m qualm config undo"
    assert not item(watch, "Short videos").isEnabled()
    ctrl.toggleRule_(item(watch, "Short videos"))  # raised TOMLDecodeError before
    assert str(ctrl.status_line.title()) == "rules.toml has a mistake: nothing was changed"
    text = str(ctrl.notice_text.stringValue())
    assert text.count(str(path.name)) == 1 and ".." not in text and "Correct it in the file" not in text
    assert "run `/Applications/Qualm.app/Contents/MacOS/Qualm -m qualm config undo` in Terminal" in text
    assert menu_titles(menu)[0].startswith("rules.toml has a mistake at line ")  # until it loads again
    path.write_text(path.read_text().replace("allow_intentional = tru", "allow_intentional = true", 1))
    ctrl.set_status("rules reloaded")  # the watcher's, once the fixed file loads
    assert "rules" not in ctrl.problems and not menu_titles(menu)[0].startswith("rules.toml")


def test_a_broken_rules_file_with_nothing_kept_offers_no_undo(world):
    """A hand-written rules.toml, nothing saved by Qualm: undo would say "nothing to undo", so nothing offers it."""
    import shutil

    from qualm.rules import load_config

    ctrl = controller()
    path = paths.rules_file()
    shutil.rmtree(path.parent / "backups")
    path.with_name("rules.toml.bak").unlink(missing_ok=True)
    path.write_text(path.read_text().replace("allow_intentional = true", "allow_intentional = tru", 1))
    ctrl.menuWillOpen_(ctrl.status_item.menu)
    watch = item(ctrl.status_item.menu, "Watch for").submenu()
    assert menu_titles(watch)[1] == "Fix it with your AI agent…" and not any("undo" in t for t in menu_titles(watch))
    with pytest.raises(ValueError) as e:
        load_config(path)
    ctrl.rules_broken(str(e.value), tell=True)
    text = str(ctrl.notice_text.stringValue())
    assert "undo" not in text and text.endswith("Ask your AI agent to fix it.")


def test_a_rules_file_that_doesnt_load_never_stops_the_app(world, monkeypatch, capsys):
    from qualm import cli
    from qualm.config import Config

    path = paths.rules_file()
    Config(path).edit_settings({"backend": "jev"}, [])
    Config(path).edit("rules", "social", {"enabled": False}, [])  # the last save: kept as backups/rules.toml.saved
    path.write_text(path.read_text() + "\n[setting]\nfoo = 1\n")  # a hand edit (or an older Qualm's file)
    monkeypatch.setattr(s, "needed", lambda: False)
    started = []
    monkeypatch.setattr(app, "run_app", lambda policy, rules_path, budget, **kw: started.append((policy, kw)))
    cli.main(["app"])  # exited 2 before: launchd restarted it over and over, and nothing showed why
    policy, kw = started[0]
    assert policy.rule("social").enabled is False  # the last saved change too, not only the version before it
    assert policy.settings.backend == "jev" and kw["broken"][1] == "the version it last saved"
    assert "! " in capsys.readouterr().out
    ctrl = app.Controller.alloc().init().setup(policy, str(path))
    ctrl.timer.invalidate()
    ctrl.rules_broken(*kw["broken"])
    assert menu_titles(ctrl.status_item.menu)[0] == "rules.toml has a mistake: fix it with your AI agent…"
    assert ctrl.notice.isVisible() and ("Until it's fixed, Qualm uses the version it last saved, and sends "
                                        "nothing to TypeSafe: only a rule's own sites and apps step in.") in \
        str(ctrl.notice_text.stringValue())
    status = json.loads((policy.data_dir / "status.json").read_text())  # the dashboard's banner says the same
    assert status["rules"] == "the version it last saved" and status["hosted_off"] is True
    ctrl.menuWillOpen_(ctrl.status_item.menu)
    watch = item(ctrl.status_item.menu, "Watch for").submenu()
    assert "Social media" in menu_titles(watch) and not item(watch, "Social media").isEnabled()  # off, but listed
    ctrl.notice.orderOut_(None)
    ctrl.set_status(f"rules.toml not reloaded: {kw['broken'][0]}")
    assert not ctrl.notice.isVisible()  # the same mistake: told once
    # The last save doesn't load either: an earlier version, said as such (it's from before a change).
    path.with_name("backups").joinpath("rules.toml.saved").write_text("broken = [")
    assert app.fallback_config(path)[2] == "an earlier version it saved"
    # Nothing loads, not even a saved version: the starter rules, in memory; the file is left as it is.
    for p in (path.parent / "backups").iterdir():
        p.write_text("broken = [")
    path.with_name("rules.toml.bak").write_text("broken = [")
    before = path.read_text()
    settings, rules, using = app.fallback_config(path)
    assert using == "the starter rules" and settings.backend == "jev" and path.read_text() == before
    # Then there's nothing for `config undo` to go back to: neither the notice nor the menu offers it.
    ctrl.rules_broken("rules.toml doesn't load (line 3): [setting] is unknown", using, tell=True)
    ctrl.menuWillOpen_(ctrl.status_item.menu)
    assert "config undo" not in str(ctrl.notice_text.stringValue()) and \
        not any("config undo" in t for t in menu_titles(watch))
    # Fixed: the notice saying otherwise goes, and so does the banner's line.
    monkeypatch.setattr(ui, "fade", lambda window, alpha, seconds=0.18, then=None: then and then())  # no run loop here
    path.write_text(before.replace("\n[setting]\nfoo = 1\n", ""))
    ctrl.set_status("rules reloaded")
    assert not ctrl.notice.isVisible() and "rules" not in ctrl.problems
    assert json.loads((policy.data_dir / "status.json").read_text())["rules"] == ""


def test_an_unreadable_rules_file_doesnt_stop_the_app_either(world, monkeypatch, capsys):
    from qualm import cli

    path = paths.rules_file()
    monkeypatch.setattr(s, "needed", lambda: False)
    started = []
    monkeypatch.setattr(app, "run_app", lambda policy, rules_path, budget, **kw: started.append((policy, kw)))
    path.chmod(0)
    try:
        cli.main(["app"])  # raised PermissionError: the login item's launchd started it again and again
    finally:
        path.chmod(0o644)
    policy, kw = started[0]
    error, using = kw["broken"]
    assert using == "the version it last saved" and "can't be read (Permission denied)" in error
    assert "chmod u+rw" in error and "Traceback" not in capsys.readouterr().out
    assert app.rules_mistake(error).startswith("can't be read (Permission denied)")  # the menu's words, no path


def test_a_broken_edit_to_no_monitor_never_gets_those_apps_read(world, monkeypatch):
    from qualm import cli, watcher as w
    from qualm.config import Config

    path = paths.rules_file()
    Config(path).edit_settings({"backend": "jev"}, [])
    # One hand edit adds a bank and WeChat to no_monitor, and misses the comma between them.
    path.write_text(path.read_text().replace('"com.bitwarden.desktop",', '"com.bitwarden.desktop",\n'
                                             '    "com.mybank.mac" "com.tencent.xinWeChat",', 1))
    monkeypatch.setattr(s, "needed", lambda: False)
    started = []
    monkeypatch.setattr(app, "run_app", lambda policy, rules_path, budget, **kw: started.append((policy, kw)))
    cli.main(["app"])
    policy, kw = started[0]
    assert kw["broken"] and policy.settings.backend == "jev"
    assert policy.precheck("com.mybank.mac", "") == Decision("skip", reason="app not monitored")
    assert policy.precheck("com.tencent.xinWeChat", "", "WeChat", "WeChat").reason == "app not monitored"
    # And while it doesn't load, nothing at all goes to TypeSafe: what else it meant can't be told.
    made = []
    monkeypatch.setattr(w, "make_client", lambda settings=None: made.append(1) or types.SimpleNamespace(backend="jev"))
    watcher = w.Watcher(policy, lambda ev: None, rules_path=str(path), shots=False, presence=False)
    watcher.rules_broken = bool(kw["broken"])  # as run_app sets it
    assert watcher._client(None) is None and made == []
    path.write_text(path.read_text().replace('"com.mybank.mac" "com.tencent.xinWeChat"',
                                             '"com.mybank.mac", "com.tencent.xinWeChat"'))
    path.touch()
    watcher._rules_mtime = 0.0
    watcher._reload_rules()  # fixed: it loads, and the model is asked again
    assert not watcher.rules_broken and watcher._client(None) is not None and made == [1]
    assert policy.settings.never_read("com.mybank.mac")
    # Broken again while running: the old rules stay, but the apps the new edit lists aren't read either.
    path.write_text(path.read_text().replace('"com.tencent.xinWeChat",', '"com.tencent.xinWeChat",\n    "Signal" '
                                                                          '"Telegram",', 1))
    watcher._rules_mtime = 0.0
    watcher._reload_rules()
    assert watcher.rules_broken and watcher._client(None) is None and made == [1]
    assert policy.settings.never_read("", "Signal") and policy.settings.never_read("", "Telegram")


def test_the_menu_names_rules_as_you_do(world):
    ctrl = controller()
    ctrl.policy.usage.add("videos", seconds=20 * 60)
    ctrl._refresh()
    assert str(ctrl.usage_line.title()) == "Entertainment videos 20 min today"
    screen = ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title="Home", url="https://x.com/home")
    ctrl.handle(Event(screen, {}, None, [Decision("allow", "social", "you marked this page fine")]))
    assert str(ctrl.status_line.title()) == "Safari: let through (Social media)"  # was "Safari: allow social"


def test_a_pause_of_days_and_one_planned_for_later_show_the_day(world):
    from datetime import datetime

    from qualm.policy import pause_for

    ctrl = controller()
    menu = ctrl.status_item.menu
    ctrl.policy.pause(3 * 24 * 60)  # `qualm pause --until 'mon 09:00'` on a Thursday
    until = ctrl.policy.paused_until
    day = datetime.fromtimestamp(until).strftime("%a %H:%M")
    ctrl._refresh()
    assert ctrl.status_item.tip == f"Qualm is paused until {day}"  # was "until 09:00 tomorrow"
    assert f"Resume (paused until {day})" in menu_titles(menu)
    ctrl.resume_(None)
    # The weekend, planned on a weekday: watching till then, the plan said, and one click calls it off.
    pause_for(ctrl.policy.data_dir, 60, start=until)
    ctrl.policy.reload_session()
    ctrl._refresh()
    assert not ctrl.policy.paused() and ctrl.status_item.tip.startswith("Qualm is watching; a pause is planned from " + day)
    assert "Pause" in menu_titles(menu) and item(menu, "Cancel the pause from " + day) is not None
    item(menu, "Cancel the pause from").target().cancelPlanned_(None)
    assert ctrl.policy.pause_later is None and not any(t.startswith("Cancel the pause") for t in menu_titles(menu))


# -- the pop-up ----------------------------------------------------------------


def test_the_pop_up_ignores_keys_for_a_moment(world):
    ctrl = controller()
    pop_up(ctrl, "shortvideo", "https://www.youtube.com/shorts/abc", reason="matches URL pattern")
    press_return(ctrl.panel)  # already on its way to the page
    assert world.backs == [] and ctrl.current is not None
    ctrl.panel.hush(0)
    press_return(ctrl.panel)
    assert world.backs == ["https://www.youtube.com/shorts/abc"]


def test_quiet_links_wait_with_i_need_it_on_a_rules_own_site(world):
    from AppKit import NSColor

    ctrl = controller()
    pop_up(ctrl, "shortvideo", "https://www.youtube.com/shorts/abc", reason="matches URL pattern")
    assert not ctrl.fine.isEnabled() and not ctrl.never.isEnabled()
    assert ctrl.fine.contentTintColor() == NSColor.quaternaryLabelColor()  # and look it
    ctrl.fine_(ctrl.fine)
    assert ctrl.mode == "ask"  # nothing marked fine while the wait runs
    assert "for any rule" in str(ctrl.never.toolTip())
    ctrl._fixed_wait = 0
    ctrl._set_need()
    ctrl.fine_(ctrl.fine)
    assert str(ctrl.headline.stringValue()) == "Got it: this page won't pop up again. Other pages on the rule's list still will."


def test_the_quiet_link_does_what_it_said_when_the_pop_up_showed(world):
    import json

    from qualm.policy import end_focus, start_focus

    data, page = paths.data_dir(), "https://www.reddit.com/r/SaaS/comments/1"
    ctrl = controller()
    start_focus(data, "research competitors", 30)
    ctrl.policy.reload_session()
    pop_up(ctrl, "social", page)  # the corner nudge first, in a focus session
    pop_up(ctrl, "social", page)
    assert str(ctrl.fine.title()) == "It's part of the task"
    end_focus(data)  # the session ends before the click
    ctrl.policy.reload_session()
    ctrl.fine_(ctrl.fine)
    assert not (data / "exceptions.jsonl").exists()  # not a permanent "Not this one"
    assert str(ctrl.headline.stringValue()) == "Your focus session had already ended, so nothing was changed."
    # "Not this one", shown before a session started, is saved as it said.
    ctrl = controller()
    pop_up(ctrl, "shortvideo", "https://example.com/clip/1")
    assert str(ctrl.fine.title()) == "Not this one"
    start_focus(data, "write the report", 30)
    ctrl.policy.reload_session()
    ctrl.fine_(ctrl.fine)
    saved = [json.loads(line) for line in (data / "exceptions.jsonl").read_text().splitlines()]
    assert [(e["rule"], e["url"]) for e in saved] == [("shortvideo", "https://example.com/clip/1")]


def test_the_screen_check_is_only_for_what_the_model_said(world, monkeypatch):
    # "Which part of the screen triggered it" is two more model calls on the rule's question:
    # not where the rule's own app or site stepped in without the model's say.
    import threading

    from qualm.config import Config

    real, checked = threading.Thread, []

    def thread(target=None, args=(), daemon=None, **kw):
        if getattr(target, "__name__", "") == "_find_evidence":
            checked.append(args[0].rule)
            return types.SimpleNamespace(start=lambda: None)
        return real(target=target, args=args, daemon=daemon, **kw)

    Config(paths.rules_file()).edit("rules", "videos", {"apps": ["com.valvesoftware.steam"]}, [])
    monkeypatch.setattr(threading, "Thread", thread)
    ctrl = controller()
    pop_up(ctrl, "social", "https://www.reddit.com/r/a/1")  # the model's score
    assert checked == ["social"] and "Checking which part" in ctrl.evidence_text
    ctrl = controller()
    pop_up(ctrl, "shortvideo", "https://www.youtube.com/shorts/abc", reason="matches URL pattern")
    assert checked == ["social"] and ctrl.evidence_text == ""
    ctrl = controller()
    steam = ScreenState(app="Steam", bundle_id="com.valvesoftware.steam", window_title="Steam", url="")
    others = Reading(0.0, "other", {"other": 0.9}, "entertain", {"entertain": 0.9}, [RuleVerdict("feeds", "", 0.1, {})], 1.0)
    ctrl.handle(Event(steam, {"app": "Steam", "headings": ["Store"]}, others,  # the other rules were asked
                      [Decision("intervene", "videos", "the app is on this rule's list", "d-steam")]))
    assert ctrl.panel.isVisible() and checked == ["social"] and ctrl.evidence_text == ""


def test_quiet_links_wait_on_a_rules_own_apps_too(world):
    from qualm.config import Config

    Config(paths.rules_file()).edit("rules", "videos", {"apps": ["Steam"]}, [])
    ctrl = controller()
    screen = ScreenState(app="Steam", bundle_id="com.valvesoftware.steam", window_title="Steam", url="")
    d = Decision("intervene", "videos", "the app is on this rule's list", "d-steam")
    ctrl.handle(Event(screen, {}, None, [d]))
    assert not ctrl.fine.isEnabled() and "This app is on the rule's list" in str(ctrl.fine.toolTip())
    ctrl.fine_(ctrl.fine)
    assert ctrl.mode == "ask"  # "Not this one" isn't a way past the wait
    ctrl._fixed_wait = 0
    ctrl._set_need()
    ctrl.fine_(ctrl.fine)
    assert str(ctrl.headline.stringValue()) == "Got it. I'll let this window through."


def test_never_here_shows_the_whole_domain(world):
    ctrl = controller()
    pop_up(ctrl, "feeds", "https://www.xiaohongshu.com/explore", reason="an entertainment feed")
    assert str(ctrl.never.title()) == "Never on xiaohongshu.com"
    assert ctrl.never.frame().size.width >= ctrl.never.fittingSize().width  # on a row of its own if need be
    assert ctrl.never.frame().origin.y > ctrl.need.frame().origin.y + ctrl.need.frame().size.height


def test_the_local_models_log_is_one_click_away(world, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "LOGS", tmp_path / "Logs")
    ctrl = controller()
    menu = ctrl.status_item.menu
    ctrl.menuWillOpen_(menu)
    assert "Show the local model's log" not in menu_titles(ctrl.model_menu)  # nothing logged yet
    localmodel.log_file().parent.mkdir(parents=True)
    localmodel.log_file().write_text("qualm: error offline ConnectError\n")
    ctrl.menuWillOpen_(menu)
    item(menu, "Show the local model's log").target().openModelLog_(None)
    assert world.opened[-1] == ["open", "-t", str(localmodel.log_file())]


def test_a_long_title_is_cut_and_the_site_stays(world):
    ctrl = controller()
    title = "【独家首发】2026年度最火爆搞笑短视频合集第十七期：看完这些你一定会笑出声来，不信你就试试看吧！全程高能无尿点 - 哔哩哔哩"
    screen = ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title=title,
                         url="https://www.bilibili.com/video/BV1xK4y1a7Zz")
    ctrl.handle(Event(screen, {}, None, [Decision("intervene", "shortvideo", "matches URL pattern", "d-long")]))
    place = str(ctrl.place.stringValue())
    assert place.startswith("【独家首发】2026") and place.endswith("… — bilibili.com")  # was cut at the tail, host and all
    assert ctrl.place.fittingSize().width <= ctrl.place.frame().size.width and ctrl.place.toolTip() == title
    ctrl.panel.orderOut_(None)
    short = ScreenState(app="Safari", bundle_id="com.apple.Safari", window_title="A video", url="https://www.bilibili.com/")
    ctrl.handle(Event(short, {}, None, [Decision("intervene", "shortvideo", "matches URL pattern", "d-short")]))
    assert str(ctrl.place.stringValue()) == "A video — bilibili.com" and not ctrl.place.toolTip()


def test_the_focus_nudge_and_prompt_keep_your_words(world):
    from qualm.policy import start_focus

    ctrl = controller()
    ctrl.startFocus_(None)
    ctrl.focus_field.setStringValue_("x")
    ctrl.focusStart_(None)  # turned down, and it says why where it's seen
    assert ctrl.focus_prompt.isVisible() and str(ctrl.focus_hint.stringValue()).startswith("A few words first")
    start_focus(ctrl.policy.data_dir, "写论文第三章：实验结果与讨论部分的修改和补充材料整理", 25)
    ctrl.policy.reload_session()
    pop_up(ctrl, "social", "https://www.reddit.com/r/x")
    assert str(ctrl.nudge_title.stringValue()).startswith("You're here to: 写论文第三章")  # was "You're here to…"


def test_a_new_session_after_times_up_has_the_check_ins_words(world):
    ctrl = controller()
    policy = ctrl.policy
    policy.start_session("videos", 5, "the keynote", "s1")
    policy.extend_session("videos", "s1")
    pop_up(ctrl, "videos", "https://www.bilibili.com/video/BV1", panel="times_up",
           reason="your 5 more minutes for “the keynote” are up")
    assert str(ctrl.body.stringValue()) == "Done takes you back."
    ctrl.need_(ctrl.need)  # no more time on this one: "New session"
    assert ctrl.mode == "checkin" and "Done" not in str(ctrl.body.stringValue())
    assert str(ctrl.body.stringValue()).startswith("This looks like watching entertainment videos")


def test_a_one_letter_answer_asks_for_more_where_its_seen(world):
    ctrl = controller()
    pop_up(ctrl, "social", "https://x.com/home", panel="check_in")
    ctrl.why.setStringValue_("x")
    ctrl.need_(ctrl.need)
    assert ctrl.mode == "checkin" and "A few words first" in str(ctrl.context.stringValue())
    ctrl.why.setStringValue_("工作")
    ctrl.need_(ctrl.need)
    assert ctrl.mode == "done" and ctrl.policy.sessions["social"].purpose == "工作"


# -- one copy, the setup window ------------------------------------------------


def test_one_copy_at_a_time_and_a_crashed_copy_doesnt_block(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_held", None)
    holder = subprocess.Popen([sys.executable, "-c", "import sys, time; from pathlib import Path; from qualm import app; "
                               "app.already_running(Path(sys.argv[1])); print('held', flush=True); time.sleep(30)",
                               str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert app.already_running(tmp_path) == holder.pid
    finally:
        holder.kill()  # it crashes: the system drops its lock
        holder.wait()
    assert app.already_running(tmp_path) is None
    assert (tmp_path / app.LOCK).read_text() == str(__import__("os").getpid())


def test_an_older_copy_without_the_lock_is_found(tmp_path, monkeypatch, capsys):
    import http.server
    import os
    import threading

    home = tmp_path / "home"
    monkeypatch.setenv("QUALM_HOME", str(home))
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)  # never an alert on screen
    old = "/Users/me/qualm/.venv/bin/python3 /Users/me/qualm/.venv/bin/qualm app"
    procs = [(os.getpid(), 1, 5, "python -m pytest"), (4242, 1, 20000, old), (4343, 1, 9000, old + " --demo")]
    monkeypatch.setattr(app, "_processes", lambda: procs)
    homes = {4242: home}
    monkeypatch.setattr(app, "_home_of", lambda pid: homes[pid])
    monkeypatch.setenv("QUALM_DASHBOARD_PORT", "9")  # nothing answers there
    assert app.older_copy(home, tmp_path / "data") == 4242
    homes[4242] = tmp_path / "another"  # a second Qualm folder's copy: not this one's
    assert app.older_copy(home, tmp_path / "data") is None
    procs[1] = (4242, 1, 1, old)  # started after this one: it finds this one's lock and goes by itself
    assert app.older_copy(home, tmp_path / "data") is None

    class OldPage(http.server.BaseHTTPRequestHandler):  # a dashboard from before /api/hello
        def do_GET(self):
            self.send_response(404 if self.path.startswith("/api/") else 200)
            self.end_headers()
            self.wfile.write(b"<title>Qualm review</title>")

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OldPage)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("QUALM_DASHBOARD_PORT", str(server.server_address[1]))
        monkeypatch.setattr(app, "_processes", lambda: [])
        assert app.older_copy(home, tmp_path / "data") == 0
    finally:
        server.shutdown()
    monkeypatch.setattr(app, "already_running", lambda home=None: None)
    monkeypatch.setattr(app, "older_copy", lambda *a: 4242)
    with pytest.raises(SystemExit) as out:
        app.run_app(None, str(tmp_path / "rules.toml"), 700)
    assert out.value.code == 0  # launchd's KeepAlive doesn't start it again
    assert "An older Qualm is running (pid 4242): quit it from its menu bar icon first." in capsys.readouterr().out


def test_the_setup_window_saves_a_key_only_for_the_hosted_model(world, monkeypatch):
    applied = []
    monkeypatch.setattr(s, "apply", lambda c, say=print: applied.append((c.backend, c.key)))
    monkeypatch.setattr(onboard, "screen_recording", lambda: False)
    monkeypatch.setattr(localmodel, "memory", lambda: localmodel.Memory(32, 8, 0, 0))
    monkeypatch.setattr(onboard, "threading", types.SimpleNamespace(Thread=lambda target, args, daemon: (
        types.SimpleNamespace(start=lambda: target(*args)))))
    monkeypatch.setattr(onboard, "AppHelper", types.SimpleNamespace(callAfter=lambda f, *a: f(*a)))
    ob = onboard.Onboarding.alloc().init().setup(lambda: None, show=False)
    ob.timer.invalidate()
    ob._go(1)
    ob.key.setStringValue_("sk-half-pasted")
    ob._choose("kev")  # typed a key, then picked this Mac
    for _ in range(4):
        ob.next_(None)
    assert applied == [("kev", None)]
    assert not ob.login.state()  # from a checkout, as `qualm setup` asks it: login off unless ticked


def test_the_setup_window_greys_out_this_mac_on_macos_13(world, monkeypatch):
    monkeypatch.setattr(onboard, "screen_recording", lambda: False)
    monkeypatch.setattr(localmodel.platform, "mac_ver", lambda: ("13.6.1", ("", "", ""), "arm64"))  # MLX has no build
    ob = onboard.Onboarding.alloc().init().setup(lambda: None, show=False)
    ob.timer.invalidate()
    assert 0.4 < ob.card_local.alphaValue() < 0.5  # greyed out, and picking it does nothing


def test_the_setup_window_says_what_this_mac_and_this_copy_cant_do(world, monkeypatch):
    from pathlib import Path

    from AppKit import NSColor

    monkeypatch.setattr(onboard, "screen_recording", lambda: False)
    monkeypatch.setattr(localmodel, "unsupported", lambda: "The local model needs macOS 14 or later.")
    monkeypatch.setattr(paths, "bundle", lambda: Path("/Volumes/Qualm/Qualm.app"))  # opened from the disk image
    monkeypatch.setattr(autostart, "temporary_bundle", lambda: True)
    monkeypatch.setattr(onboard, "threading", types.SimpleNamespace(Thread=lambda target, args, daemon: (
        types.SimpleNamespace(start=lambda: None))))
    ob = onboard.Onboarding.alloc().init().setup(lambda: None, show=False)
    ob.timer.invalidate()
    ob._go(1)
    ob.card_local.on_pick()  # greyed out, and a click does nothing
    assert ob.backend == "jev" and ob.card_local.alphaValue() < 1 and "macOS 14" in str(ob.card_local.toolTip())
    with pytest.raises(ValueError, match="macOS 14"):
        s.apply(s.Choices("kev", ["social"], login=False, shim=False))
    ob.key.setStringValue_("ts-abc")
    ob.next_(None)
    assert str(ob.model_error.stringValue()) == "Checking the key…"
    assert ob.model_error.textColor() != NSColor.systemRedColor()  # progress, not an error
    ob._key_checked("TypeSafeAuthenticationError: 401")
    assert ob.model_error.textColor() == NSColor.systemRedColor()
    ob._go(4)  # said before Start Qualm refuses, and inside the window, above its buttons
    assert str(ob.status.stringValue()).startswith("Qualm is running from its disk image: drag it to Applications")
    assert ob.login.state()  # Qualm.app: ticked to begin with
    ob.window.contentView().layoutSubtreeIfNeeded()
    assert ob.pages[4].subviews()[0].frame().origin.y >= 0
    monkeypatch.setattr(onboard.platform, "mac_ver", lambda: ("15.1", ("", "", ""), "arm64"))
    assert onboard.screen_pane() == "Screen & System Audio Recording"
    monkeypatch.setattr(onboard.platform, "mac_ver", lambda: ("14.6", ("", "", ""), "arm64"))
    assert onboard.screen_pane() == "Screen Recording"


def test_not_this_one_says_it_lets_this_page_through(world):
    ctrl = controller()
    pop_up(ctrl, "shortvideo", "https://example.com/clip/1")  # the model's hit, off the rule's own sites
    ctrl.fine_(ctrl.fine)
    assert str(ctrl.headline.stringValue()) == "Got it: this page won't pop up again."


# -- updates -------------------------------------------------------------------


LATEST = {"version": "0.2.0", "build": 70, "page": "https://github.com/RoderickQiu/qualm/releases/tag/v0.2.0",
          "download": "", "notes": "", "published": ""}


def test_a_checkout_says_once_that_a_newer_version_is_out(world):
    ctrl = controller()
    menu = ctrl.status_item.menu
    assert ctrl.sparkle is None and "Check for Updates…" in menu_titles(menu)
    assert not item(menu, "Qualm 0.2.0").isHidden() if item(menu, "Qualm 0.2.0") else True
    ctrl._feed_checked({"update": True, "latest": LATEST, "version": "0.1.0b1"}, None, told=False)
    assert menu_titles(menu)[0] == "Qualm 0.2.0 is out: see what's new…"
    assert ctrl.notice.isVisible() and "Qualm 0.2.0 is available" in str(ctrl.notice_title.stringValue())
    assert "git pull" in str(ctrl.notice_text.stringValue())
    ctrl._hide_notice()
    ctrl.notice.orderOut_(None)
    ctrl._feed_checked({"update": True, "latest": LATEST, "version": "0.1.0b1"}, None, told=False)
    assert not ctrl.notice.isVisible()  # told once per version; the menu line stays
    updates.save_state(ctrl.policy.data_dir, latest=LATEST)
    item(menu, "Qualm 0.2.0").target().installUpdate_(None)
    assert world.opened[-1] == ["open", LATEST["page"]]
    assert menu_titles(menu)[0] != "Qualm 0.2.0 is out: see what's new…"  # looked at: gone


def test_asking_says_up_to_date_or_why_it_couldnt_check(world):
    ctrl = controller()
    ctrl._feed_checked({"update": False, "latest": LATEST, "version": "0.2.0"}, None, told=True)
    assert str(ctrl.notice_title.stringValue()) == "Qualm is up to date"
    ctrl._feed_checked(None, OSError("HTTP Error 404: Not Found"), told=True)
    assert str(ctrl.notice_title.stringValue()) == "Couldn't check for updates"
    assert "404" in str(ctrl.notice_text.stringValue())


def test_the_daily_check_can_be_turned_off_from_the_menu(world):
    ctrl = controller()
    ctrl.menuWillOpen_(ctrl.status_item.menu)
    toggle = item(ctrl.status_item.menu, "Check for updates automatically")
    assert toggle.state() == 1
    toggle.target().toggleAutoUpdates_(toggle)
    assert toggle.state() == 0 and updates.load_state(ctrl.policy.data_dir)["auto"] is False
    ctrl._next_update_check = 0
    ctrl._check_updates_due()
    assert not ctrl._checking  # off: no check


def test_after_an_update_the_app_says_how_to_give_accessibility_again(world, monkeypatch):
    class Updater:
        def automaticallyChecksForUpdates(self):
            return True

    fake = types.SimpleNamespace(updater=lambda: Updater(), checkForUpdates_=lambda sender: world.asked.append("sparkle"))
    monkeypatch.setattr(updates, "start_sparkle", lambda available, attended: fake)
    monkeypatch.setattr(updates, "build", lambda: 60)
    data = paths.data_dir()
    updates.save_state(data, ran="51")
    world.access = False  # macOS: a new ad-hoc signature is a new app
    ctrl = controller()
    assert "Qualm is now" in str(ctrl.notice_title.stringValue()) and ctrl.notice.isVisible()
    assert "remove Qualm" in str(ctrl.notice_text.stringValue())
    assert updates.load_state(data)["ran"] == "60"
    ctrl.notice.orderOut_(None)
    controller()  # the next start: nothing new to say
    ctrl._update_found("0.3.0")  # Sparkle's gentle reminder
    assert menu_titles(ctrl.status_item.menu)[0] == "Qualm 0.3.0 is available: install…"
    item(ctrl.status_item.menu, "Qualm 0.3.0").target().installUpdate_(None)
    assert world.asked[-1] == "sparkle"  # Sparkle's own window, with Install Update


def test_the_menu_says_who_made_qualm_and_links_to_its_website(world, monkeypatch):
    monkeypatch.setattr(about, "AboutPanel", OffscreenAbout)
    monkeypatch.setattr(about, "NSApp", types.SimpleNamespace(activateIgnoringOtherApps_=lambda on: None))
    monkeypatch.setattr(about, "subprocess", types.SimpleNamespace(run=lambda argv, **kw: world.opened.append(argv)))
    ctrl = controller()
    titles = menu_titles(ctrl.status_item.menu)
    assert titles[-3:] == ["About Qualm", "Check for Updates…", "Quit Qualm"]  # the website is in About
    item(ctrl.status_item.menu, "About Qualm").target().openAbout_(None)
    window = ctrl.about.window
    assert window._shown == 1 and str(window.title()) == "About Qualm"
    words = []

    def collect(view):
        for v in view.subviews():
            if isinstance(v, NSButton):
                words.append(str(v.title()))
            elif isinstance(v, NSTextField):
                words.append(str(v.stringValue()))
            collect(v)

    collect(window.contentView())
    assert "Made by" in words and "Tianrun Qiu" in words and "Visit qualm.r-q.name" in words
    assert any(w.startswith("Version ") for w in words)
    ctrl.about.website.target().openLink_(ctrl.about.website)
    assert world.opened[-1] == ["open", "https://qualm.r-q.name"]
    maker = next(v for v in window.contentView().subviews()[0].views()[4].views()
                 if isinstance(v, NSButton) and str(v.title()) == "Tianrun Qiu")
    maker.target().openLink_(maker)
    assert world.opened[-1] == ["open", "https://r-q.name"]
    ctrl.openAbout_(None)
    assert ctrl.about.window is window and window._shown == 2  # the same window, shown again


def test_claude_codes_skill_is_a_switch_in_the_menu(world, monkeypatch, claude_config_dir):
    from qualm import agent

    ctrl = controller()
    menu = ctrl.status_item.menu
    ctrl.menuWillOpen_(menu)
    assert "Qualm skill for Claude Code" not in menu_titles(menu)  # another Qualm folder: the main install's
    monkeypatch.setattr(paths, "custom_home", lambda: False)
    ctrl.menuWillOpen_(menu)
    switch = item(menu, "Qualm skill for Claude Code")
    assert not switch.isHidden() and switch.state() == 0
    switch.target().toggleSkill_(switch)
    assert switch.state() == 1 and agent.skill_state() == "current"
    switch.target().toggleSkill_(switch)
    assert switch.state() == 0 and not agent.skill_file().exists()
    import shutil

    shutil.rmtree(claude_config_dir)  # no Claude Code on this Mac: nothing to switch
    ctrl.menuWillOpen_(menu)
    assert switch.isHidden()


def test_open_at_login_is_in_the_menu_from_a_checkout_too(world, monkeypatch):
    import sys
    from pathlib import Path

    login = {"on": False}
    monkeypatch.setattr(autostart, "installed", lambda: login["on"])
    monkeypatch.setattr(autostart, "install", lambda quiet=False: login.update(on=True))
    monkeypatch.setattr(autostart, "uninstall", lambda quiet=False: login.update(on=False))
    ctrl = controller()
    menu = ctrl.status_item.menu
    ctrl.menuWillOpen_(menu)
    switch = item(menu, "Open at login")
    assert not switch.isHidden() and switch.state() == 0
    switch.target().toggleLogin_(switch)
    assert login["on"] and switch.state() == 1
    python = Path(sys.executable).resolve().name
    assert str(ctrl.notice_title.stringValue()) == "Qualm opens at login" and ctrl.notice.isVisible()
    assert f"it starts as {python}, not from your terminal" in str(ctrl.notice_text.stringValue())
    switch.target().toggleLogin_(switch)
    assert not login["on"] and switch.state() == 0


def test_open_at_login_says_in_full_why_it_couldnt(world, monkeypatch):
    def refuse(quiet=False):
        raise RuntimeError(autostart.MOVE_FIRST)

    monkeypatch.setattr(autostart, "install", refuse)
    ctrl = controller()
    switch = item(ctrl.status_item.menu, "Open at login")
    switch.target().toggleLogin_(switch)
    assert str(ctrl.notice_text.stringValue()) == autostart.MOVE_FIRST  # not cut to a menu line
    monkeypatch.setattr(autostart, "install", lambda quiet=False: sys.exit("Your rules and data are still here"))
    switch.target().toggleLogin_(switch)  # install's own refusals exit: the app mustn't
    assert "still here" in str(ctrl.notice_text.stringValue())
