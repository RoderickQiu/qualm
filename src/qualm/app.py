"""Menu bar app: the watcher on a thread, an intervention panel on top.

The panel is friction, not a lock: it floats over everything (full-screen
video included), dims the screens behind it and, by default, takes the
clicks there until it's answered ("Pop-ups block clicks behind them" in the
menu, or [settings] block_clicks), and offers a way out. It has
three faces:

- a deny rule steps in: "Take me back" is the default; "I need it" unlocks
  after a short wait that grows with each use and asks what for;
- a check-in rule, on arrival: what for, and 5, 15 or 30 minutes (5 is
  Return), after a wait that grows with each session today;
- the check-in's time is up: "Done" goes back; "5 more" once, after a wait;
  then only a new check-in.

"Not this one" teaches the rule an exception; "Never here" silences an app
or site. Each answer is logged. Still on the page RECHECK_S after going
back: it steps in again.

The menu starts a focus session ("I'm here to write the report, for 50
min"), pauses, and opens the dashboard. Pause and focus live in
data/session.json, so `qualm focus` and `qualm pause` do the same from a
terminal.

What stops Qualm from working (no Accessibility, a hosted model that turns
the key down or stops answering, a rules.toml that doesn't load) stays in the
icon and the menu's first line until it's fixed, with the fix one click away.
A rules.toml that doesn't load never stops the app: it starts on the last
saved version that loads, or the starter rules, never reads the apps the
broken file lists in no_monitor, and sends nothing to a hosted model until
the file loads (it can't tell what else was meant). One copy runs at a time: a
lock in the Qualm folder, dropped by the system when the copy ends, and a
look for an older copy that takes no lock.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSButton,
    NSColor,
    NSFont,
    NSImage,
    NSImageView,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSPasteboard,
    NSPasteboardTypeString,
    NSScrollView,
    NSSecureTextField,
    NSScreen,
    NSSegmentedControl,
    NSStatusBar,
    NSTextField,
    NSTextView,
    NSTimer,
    NSVariableStatusItemLength,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from . import ui
from .explain import cut
from .policy import (CHECK_IN_MINUTES, EXTEND_MINUTES, EXTEND_WAIT_S, RECHECK_S, Decision, Policy, clock, end_focus,
                     host_of, start_focus, write_session)
from .watcher import BROWSERS, PANEL_TITLE, Event, Watcher, describe, go_back

SNOOZE_MINUTES = 10
FOCUS_SNOOZE_MINUTES = 5  # in a focus session, "I need it" is a short break
# "I need it" unlocks after a wait that doubles with each snooze in the last
# hour: 5, 10, 20, 40, 60 s (max_wait_s). Research on one sec (PNAS 2023) found
# the option to back out and a short wait both cut use; the message alone didn't.
FIRST_WAIT_S = 5
FOCUS_LENGTHS = (25, 50, 90)  # minutes offered by the focus prompt
FOCUS_HINT = "Every rule steps in at once, and the pop-up reminds you of this."
# In a focus session a hit first gets a corner nudge (no dim, focus not
# taken); still on such a page NUDGE_S later, the full panel. Frequent full
# alerts were found disruptive in focus (HANDOFF, next steps: graded friction).
NUDGE_S = 20
NUDGE_W, NUDGE_H = 400.0, 122.0
W, PAD = 560.0, 28.0  # panel width and margin
BADGE = 46.0
KEY_GUARD_S = 0.6  # the pop-up ignores keys this long after it takes the keyboard
NOTICE_W, NOTICE_S = 400.0, 30.0  # the corner notice: width, and how long it stays
MODEL_ERRORS = 3  # hosted failures in a row (not a turned-down key) before it's called down
LOCK = "app.lock"  # in the Qualm folder: held by the copy that's running
ACTION_WORDS = {"intervene": "stepped in", "allow": "let through", "skip": "skipped"}  # the menu's status line
# CJK scripts say "what for" in one or two characters: 学, 工作.
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")


def enough(answer: str) -> bool:
    """An answer to "what for?": two characters, or one in a CJK script."""
    return len(answer) >= 2 or bool(CJK.search(answer))


def model_trouble(status: str) -> str:
    """What a watcher status says about the hosted model: "nokey" (none saved,
    or the keychain wouldn't give it), "key" (turned down), "quota" (its usage
    limit), "down" (any other failure), or ""."""
    if status.startswith("no TypeSafe API key"):
        return "nokey"
    if not status.startswith("model unreachable"):
        return ""
    if "Authentication" in status or "PermissionDenied" in status:
        return "key"
    return "quota" if "RateLimit" in status else "down"


# Per kind of trouble: the status line, the notice's title, and what it means.
TROUBLE = {
    "nokey": ("hosted model: no TypeSafe key is saved", "No TypeSafe key is saved",
              "The hosted model needs one, so Qualm can't judge what's on screen."),
    "key": ("hosted model: TypeSafe turned down the key", "Your TypeSafe key doesn't work",
            "TypeSafe turned it down, so Qualm can't judge what's on screen."),
    "quota": ("hosted model: TypeSafe's usage limit is reached", "TypeSafe's usage limit is reached",
              "Until it resets, Qualm can't judge what's on screen with the hosted model."),
    "down": ("hosted model: TypeSafe isn't answering", "TypeSafe isn't answering",
             f"Qualm couldn't reach the hosted model {MODEL_ERRORS} times in a row, so it can't judge what's "
             "on screen."),
}


LOAD_WORDS = re.compile(r" (doesn't load|is empty|isn't UTF-8 text|can't be read)\b")


def rules_mistake(error: str) -> str:
    """What's wrong with rules.toml, from its load error, for the menu and the
    notice: without the file's path and the advice rules.py adds to it, and
    with commands a stock shell can run: "doesn't load (line 38): [settings]:
    no_monitor has an empty item"."""
    from . import agent

    if m := LOAD_WORDS.search(error):
        error = error[m.start() + 1:]
    error = error.split(". Correct it in the file")[0].strip()  # rules.correct_it
    return error.replace("`qualm ", f"`{agent.command()} ")


class Controller(NSObject):
    @objc.python_method
    def setup(self, policy: Policy, rules_path: str, demo: str | None = None):
        self.policy, self.rules_path, self.demo = policy, rules_path, demo
        self.server = None  # the local model server (localmodel.ManagedServer)
        self.watcher = None  # set by run_app, to judge the screen again after a switch
        self.last_reading = None  # (backend, seconds) of the last model answer, for the Model menu
        self.current: tuple[Decision, Event] | None = None
        self.last: Event | None = None  # the latest judged screen, for "This should have been blocked"
        # The panel: "ask" -> "why" (what do you need it for) -> "done" (a short
        # "got it"); "checkin" -> "done"; "timesup" -> "done" or "checkin".
        self.mode = "ask"
        self.evidence_text = self.context_text = self.hint_text = ""
        # The pop-up's page or app is on the rule's own list ("site", "app" or ""); it's a browser whose
        # address couldn't be read.
        self._own, self._no_never = "", False
        self._focus = None  # the focus session the pop-up was shown in
        # What stops Qualm from working, until it's fixed: "access", "model" or "rules" -> the menu's first line.
        self.problems: dict[str, str] = {}
        self._model_fix = None  # what the model's line does when clicked
        self._model_errors = 0  # hosted failures in a row
        self._noticed: set[str] = set()  # kinds of trouble already told in a notice: once each
        self._broken = ""  # the rules.toml mistake last told in a notice
        self._broken_notice = 0  # which notice told it (self._notices), to take it down once the file loads
        self._using = "the rules it last loaded"  # what Qualm judges with while rules.toml doesn't load
        self.dimmer = ui.Dimmer()
        self._build_menu()
        self._build_panel()
        self._build_focus_prompt()
        self._build_nudge()
        self._build_notice()
        self._nudged: dict[tuple[str, str], float] = {}  # (rule, site or app) -> when nudged
        self._check_access()
        self._refresh()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            5.0, self, "tick:", None, True)
        return self

    # -- menu bar ------------------------------------------------------------

    @objc.python_method
    def _item(self, title, action=None, key="", tag=0):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if action:
            item.setTarget_(self)
            item.setTag_(tag)
        else:
            item.setEnabled_(False)
        return item

    @objc.python_method
    def _build_menu(self):
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        # A stable identity across launches, so macOS and menu bar managers
        # (Thaw, Bartender) remember where you put it instead of treating each
        # launch as a new item.
        self.status_item.setAutosaveName_("Qualm")
        self.status_item.setVisible_(True)
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        # Problems first, hidden while there are none.
        self.access_line = self._item("Qualm can't read windows: allow Accessibility…", "grantAccess:")
        self.model_line = self._item("", "fixModel:")
        self.rules_line = self._item("", "openAgent:")
        self.problem_sep = NSMenuItem.separatorItem()
        self.status_line = self._item("starting…")
        self.usage_line = self._item("")
        self.focus_line = self._item("")
        self.focus_item = self._item("Start a focus session…", "startFocus:", "f")
        self.end_focus_item = self._item("End focus session", "endFocus:")
        pause_menu = NSMenu.alloc().init()
        for title, minutes in (("For 15 minutes", 15), ("For 1 hour", 60), ("Until tomorrow", -1)):
            pause_menu.addItem_(self._item(title, "pauseFor:", tag=minutes))
        self.pause_item = self._item("Pause")
        self.pause_item.setEnabled_(True)
        self.pause_item.setSubmenu_(pause_menu)
        self.resume_item = self._item("Resume", "resume:")
        self.planned_item = self._item("", "cancelPlanned:")  # a pause planned for later: `qualm pause --from`
        self.block_item = self._item("Pop-ups block clicks behind them", "toggleBlock:")

        # Where the model runs: both choices, the one in use ticked; switching
        # is one click once both are set up (a key for hosted).
        self.model_menu = NSMenu.alloc().init()
        self.model_menu.setAutoenablesItems_(False)
        self.model_local = self._item("On this Mac (Kev)", "pickModel:", tag=0)
        self.model_hosted = self._item("Hosted by TypeSafe (Jev)", "pickModel:", tag=1)
        self.model_key = self._item("Add a TypeSafe key…", "openKey:")
        self.model_note = self._item("")
        self.model_log = self._item("Show the local model's log", "openModelLog:")  # what it said when it failed
        for item in (self.model_local, self.model_hosted, NSMenuItem.separatorItem(), self.model_key,
                     NSMenuItem.separatorItem(), self.model_note, self.model_log):
            self.model_menu.addItem_(item)
        self.model_item = self._item("Model")
        self.model_item.setEnabled_(True)
        self.model_item.setSubmenu_(self.model_menu)
        # Each rule on or off.
        self.rules_menu = NSMenu.alloc().init()
        self.rules_menu.setAutoenablesItems_(False)
        self.rules_item = self._item("Watch for")
        self.rules_item.setEnabled_(True)
        self.rules_item.setSubmenu_(self.rules_menu)
        # From Qualm.app only: a checkout's login item would start without Accessibility.
        from . import paths

        self.login_item = self._item("Open at login", "toggleLogin:") if paths.bundle() else None
        menu.setDelegate_(self)
        for item in (self.access_line, self.model_line, self.rules_line, self.problem_sep, self.status_line,
                     self.usage_line,
                     NSMenuItem.separatorItem(),
                     self.focus_line, self.focus_item, self.end_focus_item, self.pause_item, self.resume_item,
                     self.planned_item, NSMenuItem.separatorItem(),
                     self.model_item, self.rules_item, self.block_item, *([self.login_item] if self.login_item else []),
                     NSMenuItem.separatorItem(),
                     self._item("This should have been blocked", "flagMiss:"),
                     self._item("Open dashboard", "openReview:", "d"),
                     self._item("Change rules with your AI agent…", "openAgent:"),
                     self._item("Edit rules file…", "openRules:"), NSMenuItem.separatorItem(),
                     self._item("Quit Qualm", "quit:", "q")):
            menu.addItem_(item)
        self.status_item.setMenu_(menu)

    @objc.python_method
    def _refresh(self):
        """Icon and menu from the policy's state: focus, pause, check-in sessions."""
        focus, paused = self.policy.focusing(), self.policy.paused()
        server = self.server if self.server is not None and not self.server.ready.is_set() else None
        problem = self.problems.get("access") or self.problems.get("model") or self.problems.get("rules")
        for item, kind in ((self.access_line, "access"), (self.model_line, "model"), (self.rules_line, "rules")):
            item.setHidden_(kind not in self.problems)
        self.problem_sep.setHidden_(not self.problems)
        # SF Symbols, so menu bar managers (Thaw, Bartender) can show the item;
        # they list it as "python3" because it isn't an app bundle.
        # The local model not up yet: an arrow while it downloads (the first time), an hourglass while it loads.
        fetching = server and server.status.startswith(("downloading", "installing", "building"))
        name = ("pause.circle" if paused else "eye.trianglebadge.exclamationmark" if problem
                else "scope" if focus else "eye.slash" if server and server.failing
                else "arrow.down.circle" if fetching else "hourglass" if server else "eye")
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "Qualm")
        image.setTemplate_(True)
        button = self.status_item.button()
        button.setImage_(image)
        left = max(1, round((focus["until"] - time.time()) / 60)) if focus else 0
        button.setTitle_(f" {left}m" if focus else "")
        later = self.policy.pause_later
        planned = f"a pause is planned from {clock(later['from'])} until {clock(later['until'])}" if later else ""
        if paused:
            tip = f"Qualm is paused until {clock(self.policy.paused_until)}" + (f"; {planned}" if planned else "")
        elif problem:
            tip = problem
        elif focus:
            tip = f"Focus: {focus['intent']} ({left} min left)"
        elif server:  # the local model isn't up yet (or keeps failing): nothing is judged
            tip = f"Qualm isn't watching yet: {server.status or 'starting the local model'}" + (
                f"\n{server.hint}" if server.hint else "")
        else:
            tip = "Qualm is watching" + (f"; {planned}" if planned else "")
        button.setToolTip_(tip)
        self.focus_line.setTitle_(f"Focus: {cut(focus['intent'], 40)} · {left} min left" if focus else "")
        self.focus_line.setHidden_(not focus)
        self.focus_item.setHidden_(bool(focus))
        self.end_focus_item.setHidden_(not focus)
        self.pause_item.setHidden_(paused)
        self.resume_item.setHidden_(not paused)
        self.resume_item.setTitle_(f"Resume (paused until {clock(self.policy.paused_until)})" if paused else "Resume")
        self.planned_item.setTitle_(f"Cancel the pause from {clock(later['from'])} to {clock(later['until'])}" if later else "")
        self.planned_item.setHidden_(not later)
        self.block_item.setState_(1 if self.policy.settings.block_clicks else 0)
        usage = self.policy.usage_summary(self._name)
        self.usage_line.setTitle_(usage)
        self.usage_line.setHidden_(not usage)

    @objc.python_method
    def _name(self, rule_id: str) -> str:
        """A rule as the menu names it (setup.rule_name), from its id."""
        from .setup import rule_name

        try:
            return rule_name(rule_id, self.policy.rule(rule_id).description)
        except StopIteration:  # gone from rules.toml since
            return rule_name(rule_id)

    def menuWillOpen_(self, menu):
        self._refresh_menus()

    @objc.python_method
    def _refresh_menus(self):
        """The Model and Watch for submenus, as they are right now (read when the menu opens)."""
        import os

        from . import autostart, keychain, localmodel
        from .decide import backend
        from .setup import rule_name

        name = backend(self.policy.settings)
        forced = bool(os.environ.get("QUALM_BACKEND"))
        has_key = bool(keychain.api_key())
        self.model_item.setTitle_(f"Model: {'on this Mac' if name == 'kev' else 'hosted by TypeSafe'}")
        self.model_local.setState_(1 if name == "kev" else 0)
        self.model_hosted.setState_(1 if name == "jev" else 0)
        self.model_local.setEnabled_(localmodel.unsupported() is None and not forced)
        self.model_hosted.setEnabled_(not forced)
        # Without a key, choosing hosted asks for one first.
        self.model_hosted.setTitle_("Hosted by TypeSafe (Jev)" + ("" if has_key else "…"))
        self.model_hosted.setToolTip_("About 0.2 s per reading; the text of each new screen is sent to TypeSafe."
                                      + ("" if has_key else " Needs a TypeSafe key: this asks for one."))
        self.model_local.setToolTip_("About 1 s per reading, 6-7 GB of memory; nothing leaves this Mac.")
        self.model_key.setTitle_("Replace the TypeSafe key…" if has_key else "Add a TypeSafe key…")
        if forced:
            note = f"Set by QUALM_BACKEND={os.environ['QUALM_BACKEND']}"
        elif self.last_reading:
            who, secs = self.last_reading
            note = f"Last reading: {secs:.1f} s ({'on this Mac' if who == 'kev' else 'hosted'})"
        else:
            note = "No reading yet"
        self.model_note.setTitle_(note)
        self.model_log.setHidden_(name != "kev" or not localmodel.log_file().exists())

        self.rules_menu.removeAllItems()
        from .rules import load_config

        try:
            every, broken = load_config(self.rules_path)[1], None  # the policy's `rules` are only those on now
        except Exception as e:  # a hand edit that doesn't load: nothing here can be saved until it's fixed
            every, broken = self.policy._base_rules, e  # what Qualm judges with meanwhile, those off too
        if broken is not None:
            from . import agent
            from .config import undo_to

            mistake = rules_mistake(str(broken))
            lines = [(f"rules.toml {cut(mistake, 70)}", None), ("Fix it with your AI agent…", "openAgent:")]
            if undo_to(self.rules_path):  # only when there's a version to go back to: no dead end
                lines.append((f"Or undo the last change in Terminal: {agent.command()} config undo", None))
            for title, action in lines:
                self.rules_menu.addItem_(self._item(title, action))
            self.rules_menu.itemAtIndex_(0).setToolTip_(mistake)
            self.rules_menu.addItem_(NSMenuItem.separatorItem())
        for r in every:
            item = self._item(rule_name(r.id, r.description), "toggleRule:")
            item.setRepresentedObject_(r.id)
            item.setState_(1 if r.enabled else 0)
            item.setEnabled_(broken is None)
            item.setToolTip_(r.description_en or r.description)
            self.rules_menu.addItem_(item)
        if self.login_item:
            self.login_item.setState_(1 if autostart.installed() else 0)

    @objc.python_method
    def _saved(self, change) -> bool:
        """One edit to rules.toml, then the policy reloaded from it; False (and why, in the menu) if refused."""
        from .config import Config
        from .rules import load_config

        try:
            change(Config(self.rules_path))
        except Exception as e:  # over the question limit, a file that doesn't load
            self._refused(e)
            return False
        self.policy.reload(*load_config(self.rules_path))
        if self.watcher is not None:
            self.watcher._rejudge = True  # the screen you're on, judged again under the change
        return True

    @objc.python_method
    def _refused(self, e: Exception) -> None:
        """A change from the menu that wasn't saved, and why, in full: a menu line gets cut, and missed."""
        from .rules import load_config

        try:
            load_config(self.rules_path)
        except Exception as broken:  # the file itself doesn't load: a hand edit
            self.set_status("rules.toml has a mistake: nothing was changed")
            self.rules_broken(str(broken), tell=True)
            return
        self.set_status(f"couldn't save: {e}")
        self._notice("Couldn't make that change", str(e))

    def pickModel_(self, sender):
        from . import keychain

        want = "kev" if sender.tag() == 0 else "jev"
        if want == "jev" and not keychain.api_key():
            self.openKey_(sender)  # a key first; saving it switches
            return
        self._use(want)
        self._refresh_menus()

    @objc.python_method
    def _use(self, want: str) -> bool:
        """The model on this Mac ("kev") or hosted ("jev"), saved in rules.toml. False if refused."""
        from .decide import backend

        if want == backend(self.policy.settings):
            return True
        if not self._saved(lambda c: c.edit_settings({"backend": "jev"} if want == "jev" else {},
                                                     [] if want == "jev" else ["backend"])):
            return False
        self.ensure_server()
        self._model_errors = 0
        self._set_trouble("")
        self.set_status("model: hosted by TypeSafe" if want == "jev" else "model: on this Mac")
        return True

    def toggleRule_(self, sender):
        from .rules import load_config

        rid = sender.representedObject()
        try:
            rule = next((r for r in load_config(self.rules_path)[1] if r.id == rid), None)
        except Exception as e:  # rules.toml doesn't load: say so, don't raise out of a menu action
            self._refused(e)
            self._refresh_menus()
            return
        if rule is None:
            return
        on = not rule.enabled
        if self._saved(lambda c: c.edit("rules", rid, {} if on else {"enabled": False}, ["enabled"] if on else [])):
            self.set_status(f"{self._name(rid)}: {'on' if on else 'off'}")
        self._refresh_menus()

    def toggleLogin_(self, sender):
        from . import autostart

        try:
            autostart.uninstall(quiet=True) if autostart.installed() else autostart.install(quiet=True)
        except Exception as e:
            self.set_status(f"couldn't change it: {e}")
        self._refresh_menus()

    @objc.python_method
    def ensure_server(self):
        """With the model on this Mac, its server: it starts one unless something
        answers already, and starts it again if it stops (localmodel.ManagedServer).
        Hosted, however it was chosen (the Model menu, `qualm settings`, an
        agent), stops it: no retries, no 5 GB download nobody wants now."""
        from .decide import backend
        from .localmodel import ManagedServer

        if self.demo:
            return
        if backend(self.policy.settings) != "kev":
            if self.server is not None:
                self.server.stop()  # only a server this app started; one from `qualm serve` is left running
                self.server = None
                self.set_status("model: hosted by TypeSafe")  # not the local server's last words
            return
        if self.server is not None:
            return
        server = ManagedServer(lambda text: AppHelper.callAfter(self._server_said, server, text))
        self.server = server
        server.start()

    @objc.python_method
    def _server_said(self, server, text):
        if server is self.server:  # not one stopped since (Model > Hosted): its last words are stale
            self.set_status(text)

    @objc.python_method
    def prune(self):
        """Old judgements and screenshots out (retention.py), once at start and once a day."""
        from . import retention

        self._pruned_on = datetime.now().date()

        def run():
            try:
                gone = retention.prune(self.policy.data_dir, self.policy.settings)
                if any(gone.values()):
                    print(f"  [retention: removed {gone}]", flush=True)
            except Exception as e:  # never let housekeeping stop the app
                print(f"  [retention failed: {e}]", flush=True)

        threading.Thread(target=run, daemon=True).start()

    def tick_(self, timer):
        if not self.demo and getattr(self, "_pruned_on", None) not in (None, datetime.now().date()):
            self.prune()
        self.ensure_server()  # also after [settings] backend changes to kev
        self._check_access()  # granted (or taken away) in System Settings: no restart needed
        self._refresh()
        if self.dimmer.windows and not self.panel.isVisible():
            self.dimmer.hide()  # never leave the screen dimmed (and, blocking, unclickable) without a pop-up

    def toggleBlock_(self, sender):
        from .config import Config

        on = not self.policy.settings.block_clicks
        try:
            # Saved in rules.toml like any setting (true is the default: no key).
            Config(self.rules_path).edit_settings({} if on else {"block_clicks": False}, ["block_clicks"] if on else [])
        except Exception as e:
            self._refused(e)
            return
        from .rules import load_config

        self.policy.reload(*load_config(self.rules_path))
        self._refresh()

    @objc.python_method
    def set_status(self, text):
        if text.startswith("model unreachable") and self.server and not self.server.ready.is_set():
            return  # still loading: its own status says so
        if kind := self._hosted_trouble(text):
            text = TROUBLE[kind][0]  # in words, not an exception's class name
        elif text.startswith("rules.toml not reloaded: "):  # the watcher's: a change that doesn't load
            self.rules_broken(text.split(": ", 1)[1])
            text = "rules.toml has a mistake: Qualm kept the rules it had"
        elif text == "rules reloaded":
            self.rules_broken(None)
        self.status_line.setTitle_(cut(text, 80))
        self.status_line.setToolTip_(text if len(text) > 80 else "")
        self._refresh()

    # -- what stops Qualm from working ---------------------------------------

    @objc.python_method
    def _check_access(self):
        """Accessibility, checked every tick: without it Qualm sees only app names."""
        from . import setup

        had = "access" in self.problems
        if setup.accessibility():
            self.problems.pop("access", None)
        else:
            self.problems["access"] = "Qualm can't read windows: allow Accessibility…"
        if had != ("access" in self.problems):
            self._refresh()

    def grantAccess_(self, sender):
        from . import setup

        setup.ask_accessibility()  # macOS's prompt, and the right pane of System Settings

    @objc.python_method
    def _hosted_trouble(self, status: str) -> str:
        """The hosted model's trouble in a watcher status, counted and shown; "" if none."""
        from .decide import backend

        kind = model_trouble(status)
        if not kind or backend(self.policy.settings) != "jev":
            return ""
        if kind == "down":
            self._model_errors += 1
            if self._model_errors < MODEL_ERRORS:
                return kind  # a blip, maybe: said in the status line, not yet a problem
        self._set_trouble(kind)
        return kind

    @objc.python_method
    def _set_trouble(self, kind: str) -> None:
        """The hosted model's problem in the icon and the menu, with its fix, and
        a notice the first time it happens. "" clears it."""
        from . import localmodel

        if not kind:
            if self.problems.pop("model", None) is not None:
                self._refresh()
            return
        # The model on this Mac only where it can run (Apple silicon, macOS 14), as in the Model menu.
        local = localmodel.unsupported() is None and not os.environ.get("QUALM_BACKEND")
        if kind in ("nokey", "key") or (kind == "quota" and not local):
            fix, self._model_fix = "Add a TypeSafe key…", lambda: self.openKey_(None)
        elif local:
            fix, self._model_fix = "Use the model on this Mac", lambda: self._use("kev")
        else:
            fix, self._model_fix = "Check your internet connection", None
        _, title, text = TROUBLE[kind]
        line = f"{title}: {fix[0].lower() + fix[1:]}"
        self.model_line.setTitle_(line)
        self.model_line.setEnabled_(self._model_fix is not None)
        self.problems["model"] = line
        self._refresh()
        if kind in self._noticed:
            return  # told once; the icon and the menu keep saying it
        self._noticed.add(kind)
        todo = {"nokey": "Add your key", "key": "Add a key that works", "quota": "Add a key with room left",
                "down": "Check your internet connection"}[kind]
        if local:
            need = localmodel.disk_needed_gb()
            todo += ", or use the model on this Mac" + (f" (its first start downloads about {need:.0f} GB)"
                                                        if need >= 0.5 else "")
        self._notice(title, f"{text} {todo}.", fix if self._model_fix else None, self._model_fix)

    def fixModel_(self, sender):
        if self._model_fix is not None:
            self._model_fix()

    @objc.python_method
    def rules_broken(self, error: str | None, using: str | None = None, tell: bool = False) -> None:
        """rules.toml doesn't load (`error`), or loads again (None): the icon, the
        menu's first line and a notice (once per mistake, or when `tell`). `using`:
        what Qualm judges with meanwhile, if not the rules it had."""
        from . import agent
        from .config import undo_to
        from .decide import backend
        from .explain import sentence
        from .review import write_status

        if error is None:
            self._using = "the rules it last loaded"
            if self.problems.pop("rules", None) is not None:
                self._broken = ""
                if self._broken_notice == self._notices and self.notice.isVisible():
                    self._hide_notice()  # it's fixed: the notice saying otherwise goes too
                write_status(self.policy.data_dir, rules="", hosted_off=False)
                self._refresh()
            return
        self._using = using or self._using
        mistake = rules_mistake(error)
        at = re.search(r"\((?:at )?line (\d+)", mistake)
        line = f"rules.toml has a mistake{f' at line {at.group(1)}' if at else ''}: fix it with your AI agent…"
        to = undo_to(self.rules_path)
        undo = "" if "config undo" in mistake or not to else \
            f", or run `{agent.command()} config undo` in Terminal to go back to {to}"
        # Meanwhile the watcher sends nothing to a hosted model: the file may list apps not to read.
        hosted = not self.demo and backend(self.policy.settings) == "jev"
        text = (f"{sentence(f'{self.rules_path} {mistake}')} Until it's fixed, Qualm uses {self._using}"
                + (", and sends nothing to TypeSafe: only a rule's own sites and apps step in" if hosted else "")
                + f". Ask your AI agent to fix it{undo}.")
        self.rules_line.setTitle_(line)
        self.rules_line.setToolTip_(text)
        self.problems["rules"] = line
        write_status(self.policy.data_dir, rules=self._using, hosted_off=hosted)  # the dashboard's banner says so
        self._refresh()
        if tell or mistake != self._broken:
            self._broken = mistake
            self._notice("Your rules file has a mistake", text, "Ask your AI agent…", lambda: self.openAgent_(None))
            self._broken_notice = self._notices

    def pauseFor_(self, sender):
        minutes = sender.tag()
        if minutes < 0:  # until tomorrow, 5 am
            now = datetime.now()
            morning = (now + timedelta(days=1 if now.hour >= 5 else 0)).replace(hour=5, minute=0, second=0)
            minutes = (morning - now).total_seconds() / 60
        self.policy.pause(minutes)
        self._refresh()

    def resume_(self, sender):
        self.policy.pause(0)
        self._refresh()

    def cancelPlanned_(self, sender):
        write_session(self.policy.data_dir, pause_later=None)
        self.policy.reload_session()
        self._refresh()

    def startFocus_(self, sender):
        self.focus_field.setStringValue_("")
        self._focus_say(FOCUS_HINT)
        self.focus_prompt.center()
        NSApp.activateIgnoringOtherApps_(True)
        self.focus_prompt.makeKeyAndOrderFront_(None)
        self.focus_prompt.makeFirstResponder_(self.focus_field)

    def endFocus_(self, sender):
        end_focus(self.policy.data_dir)
        self.policy.reload_session()
        self._refresh()

    def flagMiss_(self, sender):
        from .review import save_review

        # The screen in front now, even one still waiting out the 4 s before a
        # pop-up, or let through without asking the model.
        ev = self.watcher.current() if self.watcher is not None else self.last
        if ev is None:
            self.set_status("this screen hasn't been judged yet (just opened, or the model isn't answering)")
            return
        if any(d.reason in ("sensitive page", "app not monitored") for d in ev.decisions):
            self.set_status("this screen is private: Qualm keeps nothing of it to flag")
            return
        save_review(self.policy.data_dir, ev.id, verdict="should_block")
        title = cut(ev.screen.window_title, 40) or ev.screen.app
        self.set_status(f"flagged: {title}. Mark which rule on the dashboard.")

    def openReview_(self, sender):
        from .review import dashboard_url

        subprocess.run(["open", dashboard_url(self.policy.data_dir, self.policy.settings)], check=False)

    def openAgent_(self, sender):
        from . import agent

        if getattr(self, "agent_panel", None) is None:
            self._build_agent_panel()
        self.agent_text.setString_(agent.prompt(self.policy.data_dir))
        self.agent_copy.setTitle_("Copy prompt")
        self.agent_panel.center()
        NSApp.activateIgnoringOtherApps_(True)
        self.agent_panel.makeKeyAndOrderFront_(None)

    def agentCopy_(self, sender):
        board = NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(self.agent_text.string(), NSPasteboardTypeString)
        self.agent_copy.setTitle_("Copied")
        self.set_status("prompt copied: paste it into your AI agent")
        panel = self.agent_panel
        AppHelper.callLater(0.9, lambda: panel.orderOut_(None))

    def agentCancel_(self, sender):
        self.agent_panel.orderOut_(None)

    @objc.python_method
    def _build_agent_panel(self):
        """Why rules are changed through an agent, and the prompt to hand it."""
        w = 540.0
        body = ui.label("Rules are sentences a small model reads, and a change that reads right can make it worse: "
                        "naming the apps you want left alone can make it flag them more. An AI agent that runs "
                        "commands on this Mac (Claude Code, Codex, Cursor) scores each change on your recent "
                        "screens before it saves it.", 13, 0.0, NSColor.secondaryLabelColor(), width=w - 2 * PAD, wrap=True)
        body_h = ui.fit(body, str(body.stringValue()), w - 2 * PAD)
        hint = ui.label("Paste it into your agent, then say what you want in your own words.", 12, 0.0,
                        NSColor.tertiaryLabelColor(), width=w - 2 * PAD)
        box_h, buttons_h = 188.0, 60.0
        h = 30 + BADGE + 18 + body_h + 16 + box_h + 10 + hint.frame().size.height + buttons_h
        self.agent_panel, view = ui.hud(w, h, PANEL_TITLE)
        box, icon = ui.badge("agent", BADGE)
        text_x = PAD + BADGE + 16
        for v in (box, icon):
            v.setFrame_(NSMakeRect(PAD, h - 30 - BADGE, BADGE, BADGE))
        eyebrow = ui.label("CHANGE RULES", 11, 0.4, NSColor.secondaryLabelColor(), width=w - text_x - PAD)
        eyebrow.setFrameOrigin_((text_x, h - 30 - 14))
        title = ui.label("Ask your AI agent", 21, 0.3, width=w - text_x - PAD)
        title.setFrameOrigin_((text_x, h - 30 - 14 - 3 - title.frame().size.height))
        y = h - 30 - BADGE - 18 - body_h
        body.setFrame_(NSMakeRect(PAD, y, w - 2 * PAD, body_h))
        y -= 16 + box_h
        well = ui.well(NSMakeRect(PAD, y, w - 2 * PAD, box_h))
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD + 1, y + 1, w - 2 * PAD - 2, box_h - 2))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        self.agent_text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, w - 2 * PAD - 2, box_h - 2))
        self.agent_text.setEditable_(False)
        self.agent_text.setSelectable_(True)
        self.agent_text.setDrawsBackground_(False)
        self.agent_text.setTextContainerInset_((10, 9))
        self.agent_text.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11.5, 0.0))
        self.agent_text.setTextColor_(NSColor.labelColor())
        self.agent_text.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(self.agent_text)
        y -= 10 + hint.frame().size.height
        hint.setFrameOrigin_((PAD, y))
        self.agent_copy = NSButton.buttonWithTitle_target_action_("Copy prompt", self, "agentCopy:")
        self.agent_copy.setKeyEquivalent_("\r")
        self.agent_copy.setControlSize_(3)
        cancel = NSButton.buttonWithTitle_target_action_("Close", self, "agentCancel:")
        cancel.setKeyEquivalent_("\x1b")
        cancel.setControlSize_(3)
        self.agent_copy.setFrame_(NSMakeRect(w - PAD - 130, 12, 130, 36))
        cancel.setFrame_(NSMakeRect(w - PAD - 130 - 10 - 100, 12, 100, 36))
        for v in (box, icon, eyebrow, title, body, well, scroll, hint, cancel, self.agent_copy):
            view.addSubview_(v)

    def openRules_(self, sender):
        subprocess.run(["open", "-t", self.rules_path], check=False)

    def openModelLog_(self, sender):
        from .localmodel import log_file

        subprocess.run(["open", "-t", str(log_file())], check=False)

    def quit_(self, sender):
        self.policy.usage.save()
        if self.server:
            self.server.stop()
        NSApp.terminate_(self)

    # -- events from the watcher (main thread) --------------------------------

    @objc.python_method
    def handle(self, ev: Event):
        shown = [d for d in ev.decisions if d.action != "skip" or d.reason != "paused"]
        summary = ", ".join(ACTION_WORDS.get(d.action, d.action) + (f" ({self._name(d.rule)})" if d.rule else "")
                            for d in shown) or "nothing"
        if ev.reading is not None:
            self.last = ev
            if not ev.reading.cached:
                from .decide import backend

                self.last_reading = (backend(self.policy.settings), ev.reading.latency_ms / 1000)
                self._model_errors = 0
                self._set_trouble("")  # the model answered: whatever was wrong with it isn't any more
        self.set_status(f"{ev.screen.app.strip(chr(0x200e))}: {summary}")
        hit = next((d for d in ev.decisions if d.action == "intervene"), None)
        if hit and not self.panel.isVisible():
            if self._nudge_first(hit, ev):
                return
            self._show(hit, ev)

    # -- the focus nudge -----------------------------------------------------

    @objc.python_method
    def _build_nudge(self):
        self.nudge, view = ui.hud(NUDGE_W, NUDGE_H, PANEL_TITLE)
        box, icon = ui.badge("focus", 34)
        for v in (box, icon):
            v.setFrame_(NSMakeRect(16, NUDGE_H - 16 - 34, 34, 34))
        tx, tw = 16 + 34 + 12, NUDGE_W - (16 + 34 + 12) - 18
        self.nudge_title = ui.label("", 13, 0.3, width=tw)
        self.nudge_title.setFrameOrigin_((tx, NUDGE_H - 14 - 17))
        self.nudge_text = ui.label("", 12, 0.0, NSColor.secondaryLabelColor(), width=tw, wrap=True)
        self.nudge_text.setFrame_(NSMakeRect(tx, NUDGE_H - 14 - 17 - 4 - 32, tw, 32))  # two lines at most
        back = NSButton.buttonWithTitle_target_action_("Take me back", self, "nudgeBack:")
        back.sizeToFit()
        back.setFrameOrigin_((tx - 6, 12))  # the bezel's inset: its text lines up with the words above
        later = ui.link("Not now", self, "nudgeLater:")
        bf, lf = back.frame(), later.frame()
        later.setFrameOrigin_((bf.origin.x + bf.size.width + 10, bf.origin.y + (bf.size.height - lf.size.height) / 2))
        for v in (box, icon, self.nudge_title, self.nudge_text, back, later):
            view.addSubview_(v)
        self.nudge_ev = None

    @objc.python_method
    def _nudge_first(self, d: Decision, ev: Event) -> bool:
        """True if this hit gets the corner nudge instead of the panel."""
        focus = self.policy.focusing()
        if focus is None or d.panel or self.demo == "focus":
            return False
        key = (d.rule, host_of(ev.screen.url) or ev.screen.bundle_id)
        now = time.time()
        if now - self._nudged.get(key, 0) < 3 * NUDGE_S:
            return False  # nudged a moment ago and still here: the panel
        self._nudged[key] = now
        from .explain import looks_like

        rule = self.policy.rule(d.rule)
        self.nudge_title.setStringValue_(f"You're here to: {cut(focus['intent'], 46 - 16)}")  # your words, cut
        self.nudge_text.setStringValue_(cut(looks_like(rule, self.policy.settings.lang), 100))  # two lines at most
        self.nudge_ev = (d, ev)
        screen = NSScreen.mainScreen().visibleFrame()
        self.nudge.setFrameOrigin_((screen.origin.x + screen.size.width - NUDGE_W - 16,
                                    screen.origin.y + screen.size.height - NUDGE_H - 12))
        self.nudge.setAlphaValue_(0.0)
        self.nudge.orderFrontRegardless()  # shown, but the page keeps the keyboard
        ui.fade(self.nudge, 1.0)
        self.policy.rejudge_in(NUDGE_S)  # still here then: the full panel
        which = self.nudge_ev
        AppHelper.callLater(NUDGE_S, lambda: self._hide_nudge() if self.nudge_ev is which else None)
        return True

    @objc.python_method
    def _hide_nudge(self):
        self.nudge_ev = None
        nudge = self.nudge
        ui.fade(nudge, 0.0, 0.15, lambda: nudge.orderOut_(None))

    def nudgeBack_(self, sender):
        if self.nudge_ev is None:
            return
        d, ev = self.nudge_ev
        self._hide_nudge()
        self.policy.log_response(d.id, "back", d.rule)
        self._nudged.pop((d.rule, host_of(ev.screen.url) or ev.screen.bundle_id), None)
        if not self.demo:
            threading.Thread(target=go_back, args=(ev.screen, self.policy.page_fine,
                                                   lambda how: self.policy.went_back(d.rule, how)), daemon=True).start()

    def nudgeLater_(self, sender):
        if self.nudge_ev is not None:
            d, _ = self.nudge_ev
            self.policy.log_response(d.id, "not now", d.rule)
            self._hide_nudge()

    # -- the notice ----------------------------------------------------------

    @objc.python_method
    def _build_notice(self):
        """A note in the corner, like the focus nudge, for what a menu line would
        leave unseen: the hosted model stopping, a change that wasn't saved."""
        self.notice, view = ui.hud(NOTICE_W, 120, PANEL_TITLE)
        self.notice_box, self.notice_icon = ui.badge("warn", 34)
        tw = NOTICE_W - (16 + 34 + 12) - 18
        self.notice_title = ui.label("", 13, 0.3, width=tw)
        self.notice_text = ui.label("", 12, 0.0, NSColor.secondaryLabelColor(), width=tw, wrap=True)
        self.notice_fix = NSButton.buttonWithTitle_target_action_("", self, "noticeFix:")
        self.notice_close = ui.link("Not now", self, "noticeClose:")
        for v in (self.notice_box, self.notice_icon, self.notice_title, self.notice_text, self.notice_fix,
                  self.notice_close):
            view.addSubview_(v)
        self._notice_do, self._notices = None, 0

    @objc.python_method
    def _notice(self, title: str, text: str, fix: str | None = None, do=None):
        """Show a notice; `fix` titles a button that calls `do`. It goes by itself after NOTICE_S."""
        tx, tw = 16 + 34 + 12, NOTICE_W - (16 + 34 + 12) - 18
        self.notice_title.setStringValue_(cut(title, 60))
        th = ui.fit(self.notice_text, text, tw)
        h = 14 + 17 + 4 + th + 12 + 32 + 12
        for v in (self.notice_box, self.notice_icon):
            v.setFrame_(NSMakeRect(16, h - 16 - 34, 34, 34))
        self.notice_title.setFrameOrigin_((tx, h - 14 - 17))
        self.notice_text.setFrameOrigin_((tx, h - 14 - 17 - 4 - th))
        self.notice_fix.setHidden_(fix is None)
        x = tx - 6  # the bezel's inset: its text lines up with the words above
        if fix is not None:
            self.notice_fix.setTitle_(fix)
            self.notice_fix.sizeToFit()
            self.notice_fix.setFrameOrigin_((x, 12))
            x += self.notice_fix.frame().size.width + 10
        self.notice_close.setTitle_("Not now" if fix is not None else "Close")
        self.notice_close.sizeToFit()
        cf, bh = self.notice_close.frame(), self.notice_fix.frame().size.height
        self.notice_close.setFrameOrigin_((x if fix is not None else tx - 2, 12 + (bh - cf.size.height) / 2))
        self._notice_do = do
        screen = NSScreen.mainScreen().visibleFrame()
        self.notice.setFrame_display_(NSMakeRect(screen.origin.x + screen.size.width - NOTICE_W - 16,
                                                 screen.origin.y + screen.size.height - h - 12, NOTICE_W, h), True)
        self.notice.contentView().setFrame_(NSMakeRect(0, 0, NOTICE_W, h))
        self._notices += 1
        n = self._notices
        if not self.notice.isVisible():
            self.notice.setAlphaValue_(0.0)
            self.notice.orderFrontRegardless()  # shown, but whatever you're typing in keeps the keyboard
            ui.fade(self.notice, 1.0)
        AppHelper.callLater(NOTICE_S, lambda: self._hide_notice() if self._notices == n else None)

    @objc.python_method
    def _hide_notice(self):
        notice = self.notice
        ui.fade(notice, 0.0, 0.15, lambda: notice.orderOut_(None))

    def noticeFix_(self, sender):
        do, self._notice_do = self._notice_do, None
        self._hide_notice()
        if do is not None:
            do()

    def noticeClose_(self, sender):
        self._hide_notice()

    # -- the TypeSafe key ----------------------------------------------------

    @objc.python_method
    def _build_key_panel(self):
        """The hosted model's key, after setup: pasted, checked with TypeSafe, saved in the keychain."""
        from .onboard import HOSTED_COST

        w = 480.0
        self._key_busy, self._key_check = False, 0  # a check on its way; which one the window waits for
        body = ui.label("The hosted model is TypeSafe's Jev: about 0.2 s per check and almost no memory. The text of "
                        f"each new screen is sent to TypeSafe. It needs a TypeSafe account and API key; {HOSTED_COST}.",
                        13, 0.0, NSColor.secondaryLabelColor(), width=w - 2 * PAD, wrap=True)
        body_h = ui.fit(body, str(body.stringValue()), w - 2 * PAD)
        status_h, buttons_h = 34.0, 60.0
        h = 30 + BADGE + 18 + body_h + 16 + 30 + 8 + status_h + buttons_h
        self.key_panel, view = ui.hud(w, h, PANEL_TITLE)
        box, icon = ui.badge("key", BADGE)
        text_x = PAD + BADGE + 16
        for v in (box, icon):
            v.setFrame_(NSMakeRect(PAD, h - 30 - BADGE, BADGE, BADGE))
        eyebrow = ui.label("HOSTED MODEL", 11, 0.4, NSColor.secondaryLabelColor(), width=w - text_x - PAD)
        eyebrow.setFrameOrigin_((text_x, h - 30 - 14))
        self.key_title = ui.label("Add your TypeSafe key", 21, 0.3, width=w - text_x - PAD)
        self.key_title.setFrameOrigin_((text_x, h - 30 - 14 - 3 - self.key_title.frame().size.height))
        y = h - 30 - BADGE - 18 - body_h
        body.setFrame_(NSMakeRect(PAD, y, w - 2 * PAD, body_h))
        y -= 16 + 30
        self.key_field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(PAD, y, w - 2 * PAD, 30))
        self.key_field.setBezelStyle_(1)  # rounded
        self.key_field.setFont_(NSFont.systemFontOfSize_(14))
        self.key_field.setPlaceholderString_("TypeSafe API key")
        self.key_field.setTarget_(self)
        self.key_field.setAction_("keySave:")
        y -= 8 + status_h
        self.key_status = ui.label("", 12, 0.0, NSColor.secondaryLabelColor(), width=w - 2 * PAD, wrap=True)
        self.key_status.setFrame_(NSMakeRect(PAD, y, w - 2 * PAD, status_h))
        get = ui.link("Get a key", self, "keyGet:")
        get.setContentTintColor_(NSColor.linkColor())
        get.setFrameOrigin_((PAD - 2, 12 + (36 - get.frame().size.height) / 2))
        self.key_save = NSButton.buttonWithTitle_target_action_("Save and switch", self, "keySave:")
        self.key_save.setKeyEquivalent_("\r")
        self.key_save.setControlSize_(3)
        cancel = NSButton.buttonWithTitle_target_action_("Cancel", self, "keyCancel:")
        cancel.setKeyEquivalent_("\x1b")
        cancel.setControlSize_(3)
        self.key_save.setFrame_(NSMakeRect(w - PAD - 150, 12, 150, 36))
        cancel.setFrame_(NSMakeRect(w - PAD - 150 - 10 - 100, 12, 100, 36))
        for v in (box, icon, eyebrow, self.key_title, body, self.key_field, self.key_status, get, cancel,
                  self.key_save):
            view.addSubview_(v)

    def openKey_(self, sender):
        from . import keychain
        from .decide import backend

        if getattr(self, "key_panel", None) is None:
            self._build_key_panel()
        self.key_title.setStringValue_("Replace your TypeSafe key" if keychain.api_key() else "Add your TypeSafe key")
        self.key_save.setTitle_("Save" if backend(self.policy.settings) == "jev" else "Save and switch")
        self.key_field.setStringValue_("")
        self._key_drop()
        self._key_say("")
        self.key_panel.center()
        NSApp.activateIgnoringOtherApps_(True)
        self.key_panel.makeKeyAndOrderFront_(None)
        self.key_panel.makeFirstResponder_(self.key_field)

    @objc.python_method
    def _key_say(self, text: str, error: bool = False):
        self.key_status.setStringValue_(text)
        self.key_status.setToolTip_("")
        self.key_status.setTextColor_(NSColor.systemRedColor() if error else NSColor.secondaryLabelColor())

    def keyGet_(self, sender):
        from .onboard import KEY_URL

        subprocess.run(["open", KEY_URL], check=False)

    def keyCancel_(self, sender):
        self._key_drop()
        self.key_panel.orderOut_(None)

    @objc.python_method
    def _key_drop(self):
        """Forget a check still on its way (Cancel, or the window opened again): its answer saves nothing."""
        self._key_check += 1
        self._key_busy = False
        self.key_save.setEnabled_(True)

    def keySave_(self, sender):
        if self._key_busy or not self.key_panel.isVisible():
            return  # Return fires both the field and the button
        key = str(self.key_field.stringValue()).strip()
        if not key:
            self._key_say("Paste your TypeSafe API key first.", error=True)
            return
        self._key_busy = True
        self.key_save.setEnabled_(False)
        self._key_say("Checking the key with TypeSafe…")
        threading.Thread(target=self._check_key, args=(key, self._key_check), daemon=True).start()

    @objc.python_method
    def _check_key(self, key: str, check: int):
        from . import setup

        err = setup.check_key(key)
        AppHelper.callAfter(self._key_checked, key, err, check)

    @objc.python_method
    def _key_checked(self, key: str, err: str | None, check: int):
        """Checked: saved in the keychain, then the hosted model is the one in use.
        Nothing happens if the window was cancelled (or opened again) meanwhile."""
        from . import keychain
        from .onboard import key_trouble

        if check != self._key_check:
            return
        self._key_busy = False
        self.key_save.setEnabled_(True)
        if err:
            self._key_say(key_trouble(err), error=True)
            return
        try:
            keychain.store(key)
        except Exception as e:  # a locked keychain, most likely; the details go in the tooltip
            self._key_say("TypeSafe accepted the key, but macOS wouldn't save it in your keychain. Unlock your "
                          "login keychain (in Keychain Access), then try again.", error=True)
            self.key_status.setToolTip_(str(e))
            return
        self.key_field.setStringValue_("")
        self.key_panel.orderOut_(None)
        if self.watcher is not None:
            self.watcher.renew = self.watcher._rejudge = True  # the screen you're on, asked again with the new key
        self._model_errors = 0
        self._set_trouble("")
        if self._use("jev"):
            self.set_status("key saved: the model is hosted by TypeSafe")
        self._refresh_menus()

    # -- the panel -----------------------------------------------------------

    @objc.python_method
    def _build_panel(self):
        self.panel, view = ui.hud(W, 300, PANEL_TITLE)
        iw = W - 2 * PAD
        text_x = PAD + BADGE + 16
        self.badge_box, self.badge_icon = ui.badge("deny", BADGE)
        self.eyebrow = ui.label("", 11, 0.4, NSColor.secondaryLabelColor(), width=W - text_x - PAD)
        self.headline = ui.label("", 21, 0.3, width=W - text_x - PAD, wrap=True)
        self.place_icon = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 16, 16))
        self.place = ui.label("", 13, 0.23, NSColor.secondaryLabelColor(), width=iw - 22)
        self.body = ui.label("", 13, 0.0, NSColor.secondaryLabelColor(), width=iw, wrap=True)
        self.context = ui.label("", 12, 0.0, NSColor.tertiaryLabelColor(), width=iw, wrap=True)
        self.why = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD, 0, iw, 30))
        self.why.setBezelStyle_(1)  # rounded
        self.why.setFont_(NSFont.systemFontOfSize_(14))
        self.why.setPlaceholderString_("What's it for? A few words.")
        self.why.setTarget_(self)
        self.why.setAction_("need:")
        self.length = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            [f"{m} min" for m in CHECK_IN_MINUTES], 0, self, "lengthChanged:")
        self.length.setSelectedSegment_(0)
        self.length.sizeToFit()
        self.back = NSButton.buttonWithTitle_target_action_("Take me back", self, "back:")
        self.back.setKeyEquivalent_("\r")
        self.back.setControlSize_(3)  # large
        self.need = NSButton.buttonWithTitle_target_action_("I need it", self, "need:")
        self.need.setControlSize_(3)
        self.fine = ui.link("Not this one", self, "fine:")
        self.never = ui.link("Never here", self, "never:")
        for v in (self.badge_box, self.badge_icon, self.eyebrow, self.headline, self.place_icon, self.place,
                  self.body, self.context, self.why, self.length, self.back, self.need, self.fine, self.never):
            view.addSubview_(v)

    @objc.python_method
    def _layout(self):
        """Stack everything top-down for the current text and mode, keeping the panel's top edge where it was."""
        iw, text_x = W - 2 * PAD, PAD + BADGE + 16
        done = self.mode == "done"
        head_w = W - text_x - PAD
        eh = self.eyebrow.fittingSize().height if self.eyebrow.stringValue() else 0
        hh = ui.fit(self.headline, str(self.headline.stringValue()), head_w)
        header = max(BADGE, eh + 3 + hh)
        body_h = 0 if done else ui.fit(self.body, str(self.body.stringValue()), iw)
        ctx_text = "\n".join(t for t in (self.context_text, self.evidence_text, self.hint_text) if t)
        ctx_h = 0 if done else ui.fit(self.context, ctx_text, iw)
        self.context.setTextColor_(NSColor.secondaryLabelColor() if self.hint_text else NSColor.tertiaryLabelColor())
        own_row = False
        if not done:
            for b in (self.back, self.need):
                b.sizeToFit()
            bw, nw = max(self.back.frame().size.width, 130), max(self.need.frame().size.width, 120)
            nx = PAD - 2 + self.fine.frame().size.width + 12
            never_w = 0 if self._no_never else self.never.fittingSize().width
            # The links sit left of the buttons; a "Never on …" too long for the room
            # takes a row of its own rather than lose part of its domain.
            own_row = never_w > W - PAD - bw - 10 - nw - 12 - nx
        rows = [(30, None), (header, "header")]
        if not done:
            rows += [(18, None), (18, "place"), (10, None), (body_h, "body")]
            if ctx_h:
                rows += [(8, None), (ctx_h, "context")]
            if self.mode in ("why", "checkin"):
                rows += [(16, None), (30, "why")]
            if own_row:
                rows += [(14, None), (18, "links"), (10, None), (36, "buttons")]
            else:
                rows += [(20, None), (36, "buttons")]
        rows += [(22, None)]
        H = sum(h for h, _ in rows)
        y = {}
        top = 0.0
        for h, name in rows:
            if name:
                y[name] = H - top - h  # Cocoa's origin is bottom-left
            top += h
        hy = y["header"]
        self.badge_box.setFrame_(NSMakeRect(PAD, hy + header - BADGE, BADGE, BADGE))
        self.badge_icon.setFrame_(NSMakeRect(PAD, hy + header - BADGE, BADGE, BADGE))
        self.eyebrow.setFrame_(NSMakeRect(text_x, hy + header - eh, head_w, eh))
        self.headline.setFrame_(NSMakeRect(text_x, hy + header - eh - 3 - hh, head_w, hh))
        for v in (self.place_icon, self.place, self.body, self.context, self.why, self.length, self.back, self.need,
                  self.fine, self.never):
            v.setHidden_(done)
        self.never.setHidden_(done or self._no_never)
        if not done:
            self.place_icon.setFrame_(NSMakeRect(PAD, y["place"] + 1, 16, 16))
            has_icon = self.place_icon.image() is not None
            self.place.setFrame_(NSMakeRect(PAD + (22 if has_icon else 0), y["place"], iw - (22 if has_icon else 0), 18))
            self.body.setFrame_(NSMakeRect(PAD, y["body"], iw, body_h))
            self.context.setHidden_(not ctx_h)
            if ctx_h:
                self.context.setFrame_(NSMakeRect(PAD, y["context"], iw, ctx_h))
            self.why.setHidden_(self.mode not in ("why", "checkin"))
            self.length.setHidden_(self.mode != "checkin")
            if self.mode == "why":
                self.why.setFrame_(NSMakeRect(PAD, y["why"], iw, 30))
            elif self.mode == "checkin":
                lw = self.length.frame().size.width
                self.why.setFrame_(NSMakeRect(PAD, y["why"], iw - lw - 10, 30))
                self.length.setFrameOrigin_((W - PAD - lw, y["why"] + 2))
            by = y["buttons"]
            self.back.setFrame_(NSMakeRect(W - PAD - bw, by, bw, 36))
            self.need.setFrame_(NSMakeRect(W - PAD - bw - 10 - nw, by, nw, 36))
            ly = y["links"] if own_row else by + 9
            self.fine.setFrame_(NSMakeRect(PAD - 2, ly, self.fine.frame().size.width, 18))  # one baseline for both
            self.never.setFrame_(NSMakeRect(nx, ly, min(never_w, W - PAD - nx), 18))
        f = self.panel.frame()
        top_edge = f.origin.y + f.size.height
        self.panel.setFrame_display_animate_(NSMakeRect(f.origin.x, top_edge - H, W, H), True, self.panel.isVisible())
        self.panel.contentView().setFrame_(NSMakeRect(0, 0, W, H))

    @objc.python_method
    def _show(self, d: Decision, ev: Event):
        from .explain import context, headline, reason

        self.current = (d, ev)
        rule = self.policy.rule(d.rule)
        lang = self.policy.settings.lang
        focus = self.policy.focusing()
        shown = self.policy.popups_today(d.rule, but=d.id)
        eyebrow, head = headline(d, rule, lang, focus, n=len(shown))
        self.mode = {"check_in": "checkin", "times_up": "timesup"}.get(d.panel, "ask")
        kind = "focus" if focus else {"checkin": "checkin", "timesup": "timesup"}.get(self.mode, "deny")
        ui.recolor(self.badge_box, kind)
        ui.set_symbol(self.badge_icon, kind, BADGE)
        self.eyebrow.setStringValue_(eyebrow.upper())
        self.headline.setStringValue_(head)
        self.place_icon.setImage_(ui.app_icon(ev.screen.bundle_id))
        where = ev.screen.window_title or ev.screen.app.strip("‎")
        host = host_of(ev.screen.url)
        self._set_place(where, host if host and host not in where.lower() else "")
        from .decide import backend

        self.body.setStringValue_(reason(d, ev.reading, rule, lang, "Jev" if backend(self.policy.settings) == "jev" else "Kev"))
        self.headline.setToolTip_("The wording changes now and then, so it doesn't turn into wallpaper.")
        self.context_text = context(shown, self.policy.last_snooze(d.rule))
        self.hint_text = ""
        self.why.setStringValue_("")
        self.snooze_minutes = FOCUS_SNOOZE_MINUTES if focus else SNOOZE_MINUTES
        self.back.setTitle_("Back to it" if focus else "Take me back")
        # On one of the rule's own sites or apps, the quiet links wait with "I need it":
        # otherwise "Not this one" is a way past the wait there.
        self._own = ("site" if rule.matches_url(ev.screen.url)
                     else "app" if rule.matches_app(ev.screen.bundle_id, ev.screen.app.strip("‎")) else "")
        # Which part of the screen made the model say so: two more model calls. Not
        # where the rule's own site or app did it (the model's say didn't count).
        checking = ev.reading is not None and not self.demo and self.mode == "ask" and not self._own
        self.evidence_text = "Checking which part of the screen triggered it…" if checking else ""
        self._focus = focus  # what the quiet link offers stays what it does, if the session ends before the click
        self.fine.setTitle_("It's part of the task" if focus else "Not this one")
        self.fine.setToolTip_(f"This {self._own} is on the rule's list: this opens when the wait is over."
                              if self._own else "")
        self.fine.sizeToFit()
        place = host or ev.screen.app.strip("‎")
        # A browser whose address couldn't be read would be "Never in Google Chrome": every site, silenced.
        self._no_never = not host and ev.screen.bundle_id in BROWSERS
        self.never.setTitle_(cut(f"Never on {place}" if host else f"Never in {place}", 60))  # the whole domain
        self.never.setToolTip_(f"Qualm won't step in {'on' if host else 'in'} {place} again, for any rule, "
                               "sites a rule names included. Undo it on the dashboard, under Rules.")
        self.never.sizeToFit()
        if self.mode == "checkin":
            self._enter_checkin()
        elif self.mode == "timesup":
            self._enter_timesup()
        else:
            self.why.setPlaceholderString_("What's it for? A few words.")
            self._default(self.back)
            # Doubles with each "I need it" in the last hour and each return
            # after going back from this rule's pages in the last 30 minutes.
            self._start_countdown(min(self.policy.settings.max_wait_s, FIRST_WAIT_S * 2 ** (
                self.policy.snoozes_in_last_hour() + self.policy.returns(d.rule))))
        self._layout()
        if checking:
            threading.Thread(target=self._find_evidence, args=(d, ev), daemon=True).start()
        self.panel.center()
        self.panel.setAlphaValue_(0.0)
        self.dimmer.show(block=self.policy.settings.block_clicks)
        self.panel.hush(KEY_GUARD_S)  # a key already on its way to the page can't answer it
        NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeFirstResponder_(self.why if self.mode == "checkin" else None)
        ui.fade(self.panel, 1.0, 0.2)

    @objc.python_method
    def _set_place(self, where: str, host: str) -> None:
        """The page (or app) and its site on the line under the headline. A
        title too long for it is cut, never the site: "【独家首发】2026年度最火爆
        搞笑短视频合集…看完这些你一定会… — bilibili.com". The whole title in the tooltip."""
        room = W - 2 * PAD - (22 if self.place_icon.image() is not None else 0)  # as _layout sizes it
        tail = f" — {host}" if host else ""
        self.place.setStringValue_(where + tail)
        fits = self.place.fittingSize().width <= room
        if tail and not fits:
            lo, hi = 1, 2 * len(where)  # the widest cut of the title (cut counts CJK as 2) that fits with the site
            while lo < hi:
                mid = (lo + hi + 1) // 2
                self.place.setStringValue_(cut(where, mid) + tail)
                lo, hi = (mid, hi) if self.place.fittingSize().width <= room else (lo, mid - 1)
            self.place.setStringValue_(cut(where, lo) + tail)
        self.place.setToolTip_("" if fits else where)

    @objc.python_method
    def _default(self, button):
        """The button Return presses."""
        for b in (self.back, self.need):
            b.setKeyEquivalent_("\r" if b is button else "")
        self.panel.setDefaultButtonCell_(button.cell())

    @objc.python_method
    def _enter_checkin(self):
        """What for, and how long. Return starts 5 minutes once the wait is over."""
        from .explain import name, reason, session_context

        d, _ = self.current
        self.mode = "checkin"
        rule = self.policy.rule(d.rule)
        ui.recolor(self.badge_box, "checkin")
        ui.set_symbol(self.badge_icon, "checkin", BADGE)
        self.eyebrow.setStringValue_(f"{name(rule)} · check in".upper())
        self.headline.setStringValue_("What are you here for?")
        # From "time's up" too, where the body said "Done takes you back."
        self.body.setStringValue_(reason(replace(d, panel="check_in"), None, rule, self.policy.settings.lang))
        self.hint_text = ""
        seconds, n = self.policy.usage.get(d.rule)
        self.context_text = session_context(n, seconds / 60, self.policy.last_end)
        self.why.setPlaceholderString_("A few words: what's it for?")
        self.length.setSelectedSegment_(0)
        self.back.setTitle_("Take me back")
        # Each length's wait, fixed now: a session ended a moment ago counts.
        self._waits = {m: self.policy.check_in_wait(m) for m in CHECK_IN_MINUTES}
        self._default(self.need)
        self._start_countdown(None)

    @objc.python_method
    def _enter_timesup(self):
        d, _ = self.current
        self.policy.hold(d.rule, True)
        c = self.policy.sessions.get(d.rule)
        seconds, n = self.policy.usage.get(d.rule)
        stayed = f"{c.seconds / 60:.0f} min of it on these pages" if c else ""
        self.context_text = " · ".join(x for x in (stayed, f"{seconds / 60:.0f} min today") if x)
        self.back.setTitle_("Done")
        self._default(self.back)
        self._start_countdown(EXTEND_WAIT_S if self._can_extend() else 0)

    @objc.python_method
    def _can_extend(self) -> bool:
        c = self.policy.sessions.get(self.current[0].rule) if self.current else None
        return c is not None and c.extended < self.policy.settings.extensions

    @objc.python_method
    def _find_evidence(self, d: Decision, ev: Event):
        from .decide import make_client
        from .explain import evidence

        rule = next((r for r in self.policy.rules if r.id == d.rule), None)  # with the exceptions the model saw
        try:
            text = evidence(make_client(self.policy.settings), ev.state, rule, self.policy.settings.lang) if rule else ""
        except Exception:
            text = ""
        AppHelper.callAfter(self._set_evidence, d, text)

    @objc.python_method
    def _set_evidence(self, d: Decision, text: str):
        if self.current and self.current[0] is d and self.mode != "done":
            self.evidence_text = text
            self._layout()

    @objc.python_method
    def _start_countdown(self, wait: float | None):
        """The second button unlocks after `wait` seconds; None: the check-in's
        wait for the length picked, which can change while it counts."""
        self._fixed_wait = wait
        self._opened = time.monotonic()
        self._tick_countdown(self.current, self.mode)

    @objc.python_method
    def _left(self) -> int:
        wait = self._waits[self._picked()] if self._fixed_wait is None else self._fixed_wait
        return max(0, int(wait - (time.monotonic() - self._opened) + 0.999))

    @objc.python_method
    def _picked(self) -> int:
        return CHECK_IN_MINUTES[max(0, self.length.selectedSegment())]

    @objc.python_method
    def _tick_countdown(self, which, mode):
        if self.current is not which or self.mode != mode:
            return  # the panel closed or moved on
        left = self._set_need()
        if left > 0 or self.mode == "checkin":  # the length can change: keep watching
            AppHelper.callLater(0.5 if self.mode == "checkin" else 1.0, self._tick_countdown, which, mode)

    @objc.python_method
    def _need_title(self) -> str:
        if self.mode == "checkin":
            return f"Start {self._picked()} min"
        if self.mode == "timesup":
            return f"{EXTEND_MINUTES:g} more" if self._can_extend() else "New session"
        return "I need it" if self.snooze_minutes == SNOOZE_MINUTES else f"{self.snooze_minutes}-min break"

    @objc.python_method
    def _set_need(self) -> int:
        """The second button's title and state for the time left; relaid out only when it changes."""
        left = self._left()
        title = f"Wait {left} s" if left > 0 else self._need_title()
        self.need.setEnabled_(left <= 0)
        for link in (self.fine, self.never):  # greyed out clearly while they wait, not just a shade dimmer
            link.setEnabled_(left <= 0 or not self._own)
            link.setContentTintColor_(NSColor.secondaryLabelColor() if link.isEnabled()
                                      else NSColor.quaternaryLabelColor())
        if str(self.need.title()) != title:
            self.need.setTitle_(title)
            self._layout()
        return left

    def lengthChanged_(self, sender):
        if self.current is not None and self.mode == "checkin":
            self._set_need()

    @objc.python_method
    def _close(self):
        cur, self.current = self.current, None
        if cur is not None:
            self.policy.hold(cur[0].rule, False)
        panel = self.panel
        ui.fade(panel, 0.0, 0.15, lambda: panel.orderOut_(None) if self.current is None else None)
        self.dimmer.hide()
        return cur

    @objc.python_method
    def _confirm(self, text: str, seconds: float = 1.4):
        """A short "got it" in the panel, then it goes."""
        self.mode = "done"
        ui.recolor(self.badge_box, "done")
        ui.set_symbol(self.badge_icon, "done", BADGE)
        self.eyebrow.setStringValue_("")
        self.headline.setStringValue_(text)
        self._layout()
        which = self.current
        AppHelper.callLater(seconds, lambda: self._close() if self.current is which else None)

    def back_(self, sender):
        if self.current is None or self.mode == "done":
            return
        mode = self.mode
        d, ev = self._close()
        if mode == "timesup":
            if self.policy.end_session(d.rule, "done", d.id) is None:
                self.policy.log_response(d.id, "back", d.rule)
        else:
            self.policy.log_response(d.id, "back", d.rule)
        self.policy.rejudge_in(RECHECK_S)  # still here then (no Back in that app): step in again
        if not self.demo:
            threading.Thread(target=go_back, args=(ev.screen, self.policy.page_fine,
                                                   lambda how: self.policy.went_back(d.rule, how)), daemon=True).start()

    def need_(self, sender):
        if self.current is None or self.mode == "done":
            return
        d, ev = self.current
        if self.mode == "checkin":
            if self._left() > 0:
                return
            reason = str(self.why.stringValue()).strip()
            if not enough(reason):
                self._ask_more()
                return
            c = self.policy.start_session(d.rule, self._picked(), reason, d.id)
            self._confirm(f"Until {datetime.fromtimestamp(c.until):%H:%M}, for “{cut(reason, 60)}”.")
            return
        if self.mode == "timesup":
            if self._left() > 0:
                return
            if self._can_extend() and (c := self.policy.extend_session(d.rule, d.id)) is not None:
                self._confirm(f"{EXTEND_MINUTES:g} more, until {datetime.fromtimestamp(c.until):%H:%M}.")
                return
            # No more time on this one: a new check-in, with its wait.
            self.policy.hold(d.rule, False)
            self.policy.end_session(d.rule, "new")
            self._enter_checkin()
            self._layout()
            self.panel.makeFirstResponder_(self.why)
            return
        if self.mode == "ask":
            # Say what for first: a reason turns an impulse into a decision.
            self.mode = "why"
            self.need.setTitle_(f"Unlock {self.snooze_minutes} min")
            self.need.setEnabled_(True)
            self._default(self.need)  # Return now means "unlock", from the field or the button
            self._layout()
            self.panel.makeFirstResponder_(self.why)
            return
        reason = str(self.why.stringValue()).strip()
        if not enough(reason):
            self._ask_more()
            return
        self.policy.snooze(d.rule, self.snooze_minutes, reason, d.id)
        until = datetime.now() + timedelta(minutes=self.snooze_minutes)
        self._confirm(f"Until {until:%H:%M}, for “{cut(reason, 60)}”.")

    @objc.python_method
    def _ask_more(self):
        """An answer too short to count: said next to the field (a placeholder behind typed text is never seen)."""
        self.hint_text = "A few words first: what's it for?"
        self.why.setPlaceholderString_(self.hint_text)
        self._layout()
        self.panel.makeFirstResponder_(self.why)

    @objc.python_method
    def _quiet_locked(self) -> bool:
        """The quiet links on a rule's own site, while the wait runs (they're greyed out then)."""
        return bool(self._own) and self.mode != "why" and self._left() > 0

    def never_(self, sender):
        if self.current is None or self.mode == "done" or self._quiet_locked():
            return
        d, ev = self.current
        place = self.policy.never_here(ev.screen.bundle_id, ev.screen.app.strip("‎"), ev.screen.url, d.id)
        self.set_status(f"never again: {place} (undo it on the dashboard, under Rules)")
        self._confirm(f"I won't look at {place} again, for any rule. Undo it on the dashboard, under Rules.", 2.4)

    def fine_(self, sender):
        if self.current is None or self.mode == "done" or self._quiet_locked():
            return
        d, ev = self.current
        v = ev.reading.verdict(d.rule) if ev.reading is not None else None
        focus = self._focus
        times = self.policy.mark_fine(d.rule, ev.screen.url, ev.screen.window_title, d.id, ev.screen.bundle_id,
                                      v.p_hit if v else None, until_focus_ends=focus is not None, focus=focus)
        if focus and not times:  # clicked after that session ended: it held nothing
            self._confirm("Your focus session had already ended, so nothing was changed.", 2.4)
        elif focus:  # "It's part of the task": for this session only
            self._confirm("Got it. I'll let this through until your focus session ends.")
        elif times >= 2:
            # Clicks teach one place; a rule that keeps missing needs its wording tested.
            self._confirm("Got it. Still wrong here? In the menu bar: Change rules with your AI agent.", 3.0)
        elif self._own == "site":
            # The rule names this site: only this address is let through, not pages like it.
            self._confirm("Got it: this page won't pop up again. Other pages on the rule's list still will.", 2.4)
        elif ev.screen.url:
            self._confirm("Got it: this page won't pop up again.")
        else:
            self._confirm("Got it. I'll let this window through.")

    # -- the focus prompt ----------------------------------------------------

    @objc.python_method
    def _build_focus_prompt(self):
        w, h = 460.0, 250.0
        self.focus_prompt, view = ui.hud(w, h, PANEL_TITLE)
        box, icon = ui.badge("focus", BADGE)
        text_x = PAD + BADGE + 16
        for v in (box, icon):
            v.setFrame_(NSMakeRect(PAD, h - 30 - BADGE, BADGE, BADGE))
        eyebrow = ui.label("FOCUS SESSION", 11, 0.4, NSColor.secondaryLabelColor(), width=w - text_x - PAD)
        eyebrow.setFrameOrigin_((text_x, h - 30 - 14))
        title = ui.label("What are you here to do?", 21, 0.3, width=w - text_x - PAD)
        title.setFrameOrigin_((text_x, h - 30 - 14 - 3 - title.frame().size.height))
        self.focus_field = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD, 120, w - 2 * PAD, 32))
        self.focus_field.setBezelStyle_(1)
        self.focus_field.setFont_(NSFont.systemFontOfSize_(15))
        self.focus_field.setPlaceholderString_("e.g. write the report")
        self.focus_field.setTarget_(self)
        self.focus_field.setAction_("focusStart:")
        self.focus_length = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            [f"{m} min" for m in FOCUS_LENGTHS], 0, None, None)
        self.focus_length.setSelectedSegment_(1)
        self.focus_length.sizeToFit()
        self.focus_length.setFrameOrigin_((PAD, 76))
        self.focus_hint = hint = ui.label(FOCUS_HINT, 12, 0.0, NSColor.tertiaryLabelColor(), width=w - 2 * PAD)
        hint.setFrameOrigin_((PAD, 52))
        start = NSButton.buttonWithTitle_target_action_("Start", self, "focusStart:")
        start.setKeyEquivalent_("\r")
        start.setControlSize_(3)
        cancel = NSButton.buttonWithTitle_target_action_("Cancel", self, "focusCancel:")
        cancel.setKeyEquivalent_("\x1b")
        cancel.setControlSize_(3)
        start.setFrame_(NSMakeRect(w - PAD - 100, 12, 100, 36))
        cancel.setFrame_(NSMakeRect(w - PAD - 100 - 10 - 100, 12, 100, 36))
        for v in (box, icon, eyebrow, title, self.focus_field, self.focus_length, hint, start, cancel):
            view.addSubview_(v)

    def focusStart_(self, sender):
        if not self.focus_prompt.isVisible():
            return  # Return fires both the field and the Start button
        intent = str(self.focus_field.stringValue()).strip()
        if not enough(intent):  # said under the field: a placeholder behind typed text is never seen
            self._focus_say("A few words first: the pop-up reminds you of them.", True)
            self.focus_prompt.makeFirstResponder_(self.focus_field)
            return
        start_focus(self.policy.data_dir, intent, FOCUS_LENGTHS[max(0, self.focus_length.selectedSegment())])
        self.policy.reload_session()
        self.focus_prompt.orderOut_(None)
        self._refresh()

    def focusCancel_(self, sender):
        self.focus_prompt.orderOut_(None)

    @objc.python_method
    def _focus_say(self, text: str, asking: bool = False):
        self.focus_hint.setStringValue_(text)
        self.focus_hint.setTextColor_(NSColor.secondaryLabelColor() if asking else NSColor.tertiaryLabelColor())


DEMOS = ("deny", "feed", "checkin", "timesup", "focus", "prompt")


def _demo(ctrl: Controller, policy: Policy, kind: str) -> None:
    """One made-up moment, to see the panel (and take screenshots). Runs on
    a throwaway data folder, so it teaches your real rules nothing."""
    from .decide import Reading, RuleVerdict
    from .state import ScreenState

    if kind == "prompt":
        ctrl.startFocus_(None)
        return

    def pick(pred, fallback):
        return next((r for r in policy.rules if pred(r)), fallback)

    deny = pick(lambda r: r.kind == "deny", policy.rules[0])
    feed = pick(lambda r: r.feed_hit, deny)
    cap = pick(lambda r: r.kind == "check_in", deny)
    social = pick(lambda r: r.id == "social", cap)
    screens = {
        "deny": (deny, "p_hit 0.91 >= 0.15", ScreenState(
            app="Google Chrome", bundle_id="com.google.Chrome", window_title="Try Not To Smile Challenge #shorts - YouTube",
            url="https://www.youtube.com/shorts/demo")),
        "feed": (feed, "an entertainment feed", ScreenState(
            app="Google Chrome", bundle_id="com.google.Chrome", window_title="小红书 - 你的生活兴趣社区",
            url="https://www.xiaohongshu.com/explore")),
        "checkin": (cap, "p_hit 0.73 >= 0.25", ScreenState(
            app="Safari", bundle_id="com.apple.Safari", window_title="Top 10 Movie Fails of the Year",
            url="https://www.bilibili.com/video/BV1demo")),
        "timesup": (cap, "your 15 minutes for “the keynote everyone's talking about” are up", ScreenState(
            app="Safari", bundle_id="com.apple.Safari", window_title="Top 10 Movie Fails of the Year",
            url="https://www.bilibili.com/video/BV1demo")),
        "focus": (social, "p_hit 0.73 >= 0.30", ScreenState(
            app="Google Chrome", bundle_id="com.google.Chrome", window_title="What's the best mechanical keyboard? : r/MechanicalKeyboards",
            url="https://www.reddit.com/r/MechanicalKeyboards/comments/demo")),
    }
    rule, why, screen = screens[kind]
    if kind == "focus":
        start_focus(policy.data_dir, "write the pitch deck", 50)
        policy.reload_session()
    panel = {"checkin": "check_in", "timesup": "times_up"}.get(kind, "")
    if kind in ("checkin", "timesup"):
        # Two sessions earlier today, one ended 12 minutes ago: the wait shows.
        for purpose, ago in (("lunch break", 3 * 3600), ("the keynote everyone's talking about", 16 * 60)):
            c = policy.start_session(rule.id, 15, purpose, f"demo{ago}")
            c.started, c.until, c.seconds = time.time() - ago, time.time() - ago + 15 * 60, 14 * 60
            policy.usage.add(rule.id, seconds=14 * 60)
            if kind == "checkin" or ago > 3600:
                policy.end_session(rule.id, "time", now=time.time() - ago + 15 * 60)
        policy.rejudge_due(time.time() + 1)
    # An earlier pop-up today, so the context line shows.
    earlier = max(datetime.now() - timedelta(minutes=50), datetime.now().replace(hour=0, minute=1))
    with (policy.data_dir / "decisions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": earlier.isoformat(timespec="seconds"), "type": "intervention", "id": "earlier",
                            "rule": rule.id, "reason": why, "screen": {}}) + "\n")
    reading = Reading(0.02, "single_item", {"single_item": 0.9}, "entertain", {"entertain": 0.9},
                      [RuleVerdict(rule.id, "violates", 0.91 if kind == "deny" else 0.73, {})], 850.0)
    d = Decision("intervene", rule.id, why, "demo", panel)
    AppHelper.callLater(0.5, ctrl.handle, Event(screen, {}, reading if kind != "feed" else None, [d]))


_held = None  # the lock's file descriptor, open as long as this copy runs


def already_running(home: Path | None = None) -> int | None:
    """Take the one-copy lock in the Qualm folder. None once this process holds
    it; else the pid of the copy that does (0 if it can't be read). The system
    drops the lock when a copy ends, crashed or not, so a stale file never blocks."""
    import fcntl

    from . import paths

    global _held
    home = home or paths.home()
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        pid = os.read(fd, 32).decode(errors="ignore").strip()
        os.close(fd)
        return int(pid) if pid.isdigit() else 0
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _held = fd
    return None


# An app's command line: `qualm app` (a checkout, the `qualm` command), `python -m qualm app`, or Qualm.app with none.
APP_COMMAND = re.compile(r"(/qualm|-m qualm) app(?!\S)|\.app/Contents/MacOS/Qualm$")


def _processes() -> list[tuple[int, int, int, str]]:
    """(pid, parent pid, seconds running, command line) of each of your processes, from `ps`; [] if it can't be read."""
    try:
        out = subprocess.run(["ps", "-xww", "-o", "pid=,ppid=,etime=,command="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    procs = []
    for row in (line.split(None, 3) for line in out.splitlines()):
        if len(row) == 4 and row[0].isdigit() and row[1].isdigit():
            days, _, hms = row[2].rpartition("-")  # [[dd-]hh:]mm:ss
            secs = sum(int(x) * 60 ** i for i, x in enumerate(reversed(hms.split(":")))) + int(days or 0) * 86400
            procs.append((int(row[0]), int(row[1]), secs, row[3].strip()))
    return procs


def _home_of(pid: int) -> Path | None:
    """The Qualm folder a process of yours runs on (QUALM_HOME in its environment,
    else the default); None if its environment can't be read."""
    from . import paths

    try:
        out = subprocess.run(["ps", "-wwE", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not out.strip():
        return None
    m = re.search(r"(?:^| )QUALM_HOME=(.*?)(?= [A-Za-z_][A-Za-z0-9_]*=|$)", out.strip())
    return Path(m.group(1)).expanduser() if m and m.group(1) else paths.default_home()


def older_copy(home: Path, data_dir: Path, settings=None) -> int | None:
    """Another Qualm app on this Qualm folder that takes no lock: one from before
    app.lock, still running after an update. Its pid (0 if unknown), None if
    there's none. Found in the process list (`qualm app` or Qualm.app, not this
    one or what started it), or by its dashboard answering on this folder's port."""
    from .review import dashboard_url, hello

    procs = _processes()
    parents, mine, pid = {p: pp for p, pp, _, _ in procs}, set(), os.getpid()
    while pid and pid not in mine:  # this process and what started it (`uv run qualm app`)
        mine.add(pid)
        pid = parents.get(pid, 0)
    age = next((t for p, _, t, _ in procs if p == os.getpid()), 0)
    # Only copies started before this one: a newer one finds this one's lock and goes by itself.
    homes = {p: _home_of(p) for p, _, t, cmd in procs
             if p not in mine and t > age and APP_COMMAND.search(cmd) and "--demo" not in cmd}
    for p, h in homes.items():
        if h is not None and h.resolve() == home.resolve():
            return p
    said = hello(dashboard_url(data_dir, settings))
    if not said or not said.get("watching") or said.get("pid") == os.getpid():
        return None
    if said.get("older"):  # a page from before /api/hello doesn't say its folder
        if homes and None not in homes.values():
            return None  # every older copy runs on another folder: the page is one of theirs
        return next((p for p, h in homes.items() if h is None), 0)
    return said.get("pid", 0) if said.get("data") == str(Path(data_dir).resolve()) else None


def _say_running(pid: int, older: bool = False) -> None:
    """A second copy's goodbye: in the terminal, and in a small alert when it was opened from Finder or the Dock."""
    which = f" (pid {pid})" if pid else ""
    if older:  # it has no "already running" check of its own: this one steps aside
        title, text = "An older Qualm is running", "Quit it from its menu bar icon (Quit Qualm), then open Qualm again."
        print(f"An older Qualm is running{which}: quit it from its menu bar icon first.", flush=True)
    else:
        title = "Qualm is already running"
        text = "Its eye is in the menu bar. To start again, choose Quit Qualm there first."
        print(f"Qualm is already running{which}: its eye is in the menu bar.", flush=True)
    # Opened by LaunchServices (Finder, the Dock, `open`), not a terminal or the login item.
    if os.environ.get("XPC_SERVICE_NAME", "").startswith("application."):
        from AppKit import NSAlert

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(text)
        app.activateIgnoringOtherApps_(True)
        alert.runModal()


def fallback_config(rules_path: str | Path):
    """What the app starts on when rules.toml doesn't load: (settings, rules,
    that in words). The version Qualm last saved, else the newest one kept in
    backups/ or as rules.toml.bak that loads (read, never put back; said as
    an earlier version, which it is), else the starter rules. The
    model stays the one rules.toml names, where that line can be read: a
    hosted user isn't moved onto a 5 GB download. The apps the broken file
    lists in no_monitor, as far as they can be read, are never read either
    (and the watcher sends nothing to a hosted model until it loads)."""
    from .config import EXAMPLE, Config
    from .rules import listed_anyway, load_config, parse_config

    path, cfg = Path(rules_path), Config(Path(rules_path))
    for p in (cfg._saved(), *reversed(cfg._versions()), cfg._bak()):
        try:
            settings, rules = parse_config(p.read_bytes().decode("utf-8-sig"), p, advice=False)
            # Only .saved is the last save: the others are from before a change (the last one, or more).
            using = "the version it last saved" if p == cfg._saved() else "an earlier version it saved"
            break
        except (OSError, ValueError):
            continue
    else:
        (settings, rules), using = load_config(EXAMPLE), "the starter rules"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if m := re.search(r"""(?m)^\s*backend\s*=\s*["'](kev|jev)["']""", text):
        settings.backend = m.group(1)
    elif text.strip():  # no backend line: the model on this Mac
        settings.backend = "kev"
    settings.no_monitor = tuple(dict.fromkeys((*settings.no_monitor, *listed_anyway(text))))
    return settings, rules, using


def run_app(policy: Policy | None, rules_path: str, budget: int, demo: str | None = None, review: bool = True,
            data_dir: str | None = None, broken: tuple[str, str] | None = None) -> None:
    """The app. With no policy yet (a first run), the setup window comes
    first, and the rules it writes are loaded when it's done. `broken`:
    rules.toml doesn't load (why, and what `policy` holds instead), shown
    until it does. One copy at a time: a second one, or one started while
    an older copy runs, says so and exits (0, so the login item's launchd
    doesn't start it again); a demo watches nothing and never counts."""
    import atexit

    from . import paths

    if not demo and (other := already_running()) is not None:
        _say_running(other)
        sys.exit(0)
    data = Path(policy.data_dir if policy else data_dir or paths.data_dir())
    if not demo and (old := older_copy(paths.home(), data, policy.settings if policy else None)) is not None:
        _say_running(old, older=True)
        sys.exit(0)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    keep = []  # the controller and the setup window live as long as the app

    def start(policy: Policy):
        ctrl = Controller.alloc().init().setup(policy, str(Path(rules_path).resolve()), demo)
        keep.append(ctrl)
        if broken:  # the menu bar says so until a fixed rules.toml loads
            ctrl.rules_broken(*broken)
        atexit.register(lambda: ctrl.server and ctrl.server.stop())

        def on_event(ev: Event):
            print(f"[{datetime.now():%H:%M:%S}] {describe(ev)}", flush=True)
            AppHelper.callAfter(ctrl.handle, ev)

        def on_status(text: str):
            print(f"  [{text}]", flush=True)
            AppHelper.callAfter(ctrl.set_status, text)

        if "access" in ctrl.problems:  # the menu bar shows it too, until it's granted
            print("! No Accessibility permission: Qualm can only see app names. Grant it in System Settings >"
                  " Privacy & Security > Accessibility; Qualm picks it up without a restart.", flush=True)
        print("Qualm watching. Ctrl-C to stop.", flush=True)

        if demo:
            _demo(ctrl, policy, demo)
        else:
            ctrl.prune()
            ctrl.ensure_server()
            from .events import FrontWindowEvents

            wake = threading.Event()
            keep.append(FrontWindowEvents(wake).start())  # the front app and window's changes wake the watcher
            watcher = Watcher(policy, on_event, on_status, budget=budget, rules_path=rules_path, wake=wake)
            watcher.rules_broken = bool(broken)  # nothing goes to a hosted model until rules.toml loads
            ctrl.watcher = watcher
            threading.Thread(target=watcher.run, daemon=True).start()
        if review:
            from .review import dashboard_port, start_dashboard

            try:  # on a free port if another program holds this one; data/dashboard.json says which
                server = start_dashboard(policy.data_dir, Path(rules_path), dashboard_port(policy.settings))
                threading.Thread(target=server.serve_forever, daemon=True).start()
                print(f"dashboard: http://127.0.0.1:{server.server_address[1]}/", flush=True)
            except OSError as e:
                print(f"dashboard: couldn't start ({e})", flush=True)

    if policy is not None:
        start(policy)
    else:
        from .onboard import Onboarding
        from .rules import load_config

        keep.append(Onboarding.alloc().init().setup(
            lambda: start(Policy(*load_config(rules_path), data_dir or "data"))))
    AppHelper.runEventLoop(installInterrupt=True)
