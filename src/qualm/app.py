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
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
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
    NSScreen,
    NSSegmentedControl,
    NSStatusBar,
    NSTextField,
    NSTimer,
    NSVariableStatusItemLength,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from . import ui
from .policy import (CHECK_IN_MINUTES, EXTEND_MINUTES, EXTEND_WAIT_S, RECHECK_S, Decision, Policy, end_focus,
                     host_of, start_focus)
from .watcher import PANEL_TITLE, Event, Watcher, describe, go_back

SNOOZE_MINUTES = 10
FOCUS_SNOOZE_MINUTES = 5  # in a focus session, "I need it" is a short break
# "I need it" unlocks after a wait that doubles with each snooze in the last
# hour: 5, 10, 20, 40, 60 s (max_wait_s). Research on one sec (PNAS 2023) found
# the option to back out and a short wait both cut use; the message alone didn't.
FIRST_WAIT_S = 5
FOCUS_LENGTHS = (25, 50, 90)  # minutes offered by the focus prompt
# In a focus session a hit first gets a corner nudge (no dim, focus not
# taken); still on such a page NUDGE_S later, the full panel. Frequent full
# alerts were found disruptive in focus (HANDOFF, next steps: graded friction).
NUDGE_S = 20
NUDGE_W, NUDGE_H = 400.0, 122.0
W, PAD = 560.0, 28.0  # panel width and margin
BADGE = 46.0


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
        self.evidence_text = self.context_text = ""
        self.dimmer = ui.Dimmer()
        self._build_menu()
        self._build_panel()
        self._build_focus_prompt()
        self._build_nudge()
        self._nudged: dict[tuple[str, str], float] = {}  # (rule, site or app) -> when nudged
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
        self.block_item = self._item("Pop-ups block clicks behind them", "toggleBlock:")

        # Where the model runs: both choices, the one in use ticked; switching
        # is one click once both are set up (a key for hosted).
        self.model_menu = NSMenu.alloc().init()
        self.model_menu.setAutoenablesItems_(False)
        self.model_local = self._item("On this Mac (Kev)", "pickModel:", tag=0)
        self.model_hosted = self._item("Hosted by TypeSafe (Jev)", "pickModel:", tag=1)
        self.model_note = self._item("")
        for item in (self.model_local, self.model_hosted, NSMenuItem.separatorItem(), self.model_note):
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
        for item in (self.status_line, self.usage_line, NSMenuItem.separatorItem(),
                     self.focus_line, self.focus_item, self.end_focus_item, self.pause_item, self.resume_item,
                     NSMenuItem.separatorItem(),
                     self.model_item, self.rules_item, self.block_item, *([self.login_item] if self.login_item else []),
                     NSMenuItem.separatorItem(),
                     self._item("This should have been blocked", "flagMiss:"),
                     self._item("Open dashboard", "openReview:", "d"),
                     self._item("Edit rules…", "openRules:"), NSMenuItem.separatorItem(),
                     self._item("Quit Qualm", "quit:", "q")):
            menu.addItem_(item)
        self.status_item.setMenu_(menu)

    @objc.python_method
    def _refresh(self):
        """Icon and menu from the policy's state: focus, pause, check-in sessions."""
        focus, paused = self.policy.focusing(), self.policy.paused()
        # SF Symbols, so menu bar managers (Thaw, Bartender) can show the item;
        # they list it as "python3" because it isn't an app bundle.
        name = "pause.circle" if paused else "scope" if focus else "eye"
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "Qualm")
        image.setTemplate_(True)
        button = self.status_item.button()
        button.setImage_(image)
        left = max(1, round((focus["until"] - time.time()) / 60)) if focus else 0
        button.setTitle_(f" {left}m" if focus else "")
        if paused:
            until = datetime.fromtimestamp(self.policy.paused_until)
            tip = f"Qualm is paused until {until:%H:%M}" + (" tomorrow" if until.date() > datetime.now().date() else "")
        elif focus:
            tip = f"Focus: {focus['intent']} ({left} min left)"
        else:
            tip = "Qualm is watching"
        button.setToolTip_(tip)
        self.focus_line.setTitle_(f"Focus: {focus['intent'][:40]} · {left} min left" if focus else "")
        self.focus_line.setHidden_(not focus)
        self.focus_item.setHidden_(bool(focus))
        self.end_focus_item.setHidden_(not focus)
        self.pause_item.setHidden_(paused)
        self.resume_item.setHidden_(not paused)
        self.resume_item.setTitle_(f"Resume (paused until {datetime.fromtimestamp(self.policy.paused_until):%H:%M})" if paused else "Resume")
        self.block_item.setState_(1 if self.policy.settings.block_clicks else 0)
        usage = self.policy.usage_summary()
        self.usage_line.setTitle_(usage)
        self.usage_line.setHidden_(not usage)

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
        has_key = name == "jev" or bool(keychain.api_key())
        self.model_item.setTitle_(f"Model: {'on this Mac' if name == 'kev' else 'hosted by TypeSafe'}")
        self.model_local.setState_(1 if name == "kev" else 0)
        self.model_hosted.setState_(1 if name == "jev" else 0)
        self.model_local.setEnabled_(localmodel.apple_silicon() and not forced)
        self.model_hosted.setEnabled_(has_key and not forced)
        self.model_hosted.setTitle_("Hosted by TypeSafe (Jev)" + ("" if has_key else ": needs a key, `qualm setup`"))
        self.model_hosted.setToolTip_("About 0.2 s per reading; the text of each new screen is sent to TypeSafe.")
        self.model_local.setToolTip_("About 1 s per reading, 6-7 GB of memory; nothing leaves this Mac.")
        if forced:
            note = f"Set by QUALM_BACKEND={os.environ['QUALM_BACKEND']}"
        elif self.last_reading:
            who, secs = self.last_reading
            note = f"Last reading: {secs:.1f} s ({'on this Mac' if who == 'kev' else 'hosted'})"
        else:
            note = "No reading yet"
        self.model_note.setTitle_(note)

        self.rules_menu.removeAllItems()
        from .rules import load_config

        try:
            every = load_config(self.rules_path)[1]  # the policy keeps only the rules that are on
        except Exception:
            every = self.policy.rules
        for r in every:
            item = self._item(rule_name(r.id), "toggleRule:")
            item.setRepresentedObject_(r.id)
            item.setState_(1 if r.enabled else 0)
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
            self.set_status(f"couldn't save: {e}"[:80])
            return False
        self.policy.reload(*load_config(self.rules_path))
        if self.watcher is not None:
            self.watcher._rejudge = True  # the screen you're on, judged again under the change
        return True

    def pickModel_(self, sender):
        from .decide import backend

        want = "kev" if sender.tag() == 0 else "jev"
        if want == backend(self.policy.settings):
            return
        if not self._saved(lambda c: c.edit_settings({"backend": "jev"} if want == "jev" else {},
                                                     [] if want == "jev" else ["backend"])):
            return
        if want == "jev" and self.server is not None:
            self.server.stop()  # only a server this app started; one from `qualm serve` is left running
            self.server = None
        self.ensure_server()
        self.set_status("model: hosted by TypeSafe" if want == "jev" else "model: on this Mac")
        self._refresh_menus()

    def toggleRule_(self, sender):
        from .rules import load_config

        rid = sender.representedObject()
        rule = next((r for r in load_config(self.rules_path)[1] if r.id == rid), None)
        if rule is None:
            return
        on = not rule.enabled
        if self._saved(lambda c: c.edit("rules", rid, {} if on else {"enabled": False}, ["enabled"] if on else [])):
            from .setup import rule_name

            self.set_status(f"{rule_name(rid)}: {'on' if on else 'off'}")
        self._refresh_menus()

    def toggleLogin_(self, sender):
        from . import autostart

        try:
            autostart.uninstall(quiet=True) if autostart.installed() else autostart.install(quiet=True)
        except Exception as e:
            self.set_status(f"couldn't change it: {e}"[:80])
        self._refresh_menus()

    @objc.python_method
    def ensure_server(self):
        """With the model on this Mac, start its server unless something answers already."""
        from .decide import backend
        from .localmodel import ManagedServer

        from .localmodel import listening

        if self.demo or backend(self.policy.settings) != "kev":
            return
        if self.server is not None:
            s = self.server
            gone_theirs = s.proc is None and s.ready.is_set() and not listening(s.port)  # `qualm serve` stopped
            gone_ours = s.proc is not None and s.proc.poll() is not None  # ours crashed or was killed
            if not (gone_theirs or gone_ours):
                return
            self.server = None  # start one again
        self.server = ManagedServer(lambda text: AppHelper.callAfter(self.set_status, text))
        self.server.start()

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
            self.set_status(f"couldn't save: {e}"[:80])
            return
        from .rules import load_config

        self.policy.reload(*load_config(self.rules_path))
        self._refresh()

    @objc.python_method
    def set_status(self, text):
        if text.startswith("model unreachable") and self.server and not self.server.ready.is_set():
            return  # still loading: its own status says so
        self.status_line.setTitle_(text[:80])
        self._refresh()

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

    def startFocus_(self, sender):
        self.focus_field.setStringValue_("")
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

        ev = self.last
        if ev is None or ev.reading is None:
            self.set_status("nothing judged yet to flag")
            return
        save_review(self.policy.data_dir, ev.id, verdict="should_block")
        self.set_status(f"flagged: {ev.screen.window_title[:40] or ev.screen.app}. Mark which rule on the dashboard.")

    def openReview_(self, sender):
        from .review import PORT

        subprocess.run(["open", f"http://127.0.0.1:{PORT}/"], check=False)

    def openRules_(self, sender):
        subprocess.run(["open", "-t", self.rules_path], check=False)

    def quit_(self, sender):
        self.policy.usage.save()
        if self.server:
            self.server.stop()
        NSApp.terminate_(self)

    # -- events from the watcher (main thread) --------------------------------

    @objc.python_method
    def handle(self, ev: Event):
        shown = [d for d in ev.decisions if d.action != "skip" or d.reason != "paused"]
        summary = ", ".join(f"{d.action} {d.rule}".strip() for d in shown) or "nothing"
        if ev.reading is not None:
            self.last = ev
            if not ev.reading.cached:
                from .decide import backend

                self.last_reading = (backend(self.policy.settings), ev.reading.latency_ms / 1000)
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
        from .explain import label

        rule = self.policy.rule(d.rule)
        self.nudge_title.setStringValue_(f"You're here to: {focus['intent']}"[:60])
        self.nudge_text.setStringValue_(f"This looks like {label(rule, self.policy.settings.lang)}.")
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
            threading.Thread(target=go_back, args=(ev.screen, self.policy.page_fine),
                             daemon=True).start()

    def nudgeLater_(self, sender):
        if self.nudge_ev is not None:
            d, _ = self.nudge_ev
            self.policy.log_response(d.id, "not now", d.rule)
            self._hide_nudge()

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
        ctx_text = "\n".join(t for t in (self.context_text, self.evidence_text) if t)
        ctx_h = 0 if done else ui.fit(self.context, ctx_text, iw)
        rows = [(30, None), (header, "header")]
        if not done:
            rows += [(18, None), (18, "place"), (10, None), (body_h, "body")]
            if ctx_h:
                rows += [(8, None), (ctx_h, "context")]
            if self.mode in ("why", "checkin"):
                rows += [(16, None), (30, "why")]
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
            for b in (self.back, self.need):
                b.sizeToFit()
            bw, nw = max(self.back.frame().size.width, 130), max(self.need.frame().size.width, 120)
            self.back.setFrame_(NSMakeRect(W - PAD - bw, by, bw, 36))
            self.need.setFrame_(NSMakeRect(W - PAD - bw - 10 - nw, by, nw, 36))
            self.fine.setFrameOrigin_((PAD - 2, by + 9))
            nx = PAD - 2 + self.fine.frame().size.width + 12
            room = W - PAD - bw - 10 - nw - 12 - nx  # the links give way to the buttons
            self.never.setFrame_(NSMakeRect(nx, by + 9, min(self.never.frame().size.width, max(room, 60)), 18))
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
        self.place.setStringValue_(f"{where} — {host}" if host and host not in where.lower() else where)
        self.body.setStringValue_(reason(d, ev.reading, rule, lang))
        self.headline.setToolTip_("The wording changes now and then, so it doesn't turn into wallpaper.")
        self.context_text = context(shown, self.policy.last_snooze(d.rule))
        checking = ev.reading is not None and not self.demo and self.mode == "ask"
        self.evidence_text = "Checking which part of the screen triggered it…" if checking else ""
        self.why.setStringValue_("")
        self.snooze_minutes = FOCUS_SNOOZE_MINUTES if focus else SNOOZE_MINUTES
        self.back.setTitle_("Back to it" if focus else "Take me back")
        self.fine.setTitle_("It's part of the task" if focus else "Not this one")
        self.fine.sizeToFit()
        place = host or ev.screen.app.strip("‎")
        self.never.setTitle_((f"Never on {place}" if host else f"Never in {place}")[:32])
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
        NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeFirstResponder_(self.why if self.mode == "checkin" else None)
        ui.fade(self.panel, 1.0, 0.2)

    @objc.python_method
    def _default(self, button):
        """The button Return presses."""
        for b in (self.back, self.need):
            b.setKeyEquivalent_("\r" if b is button else "")
        self.panel.setDefaultButtonCell_(button.cell())

    @objc.python_method
    def _enter_checkin(self):
        """What for, and how long. Return starts 5 minutes once the wait is over."""
        from .explain import session_context

        d, _ = self.current
        self.mode = "checkin"
        rule = self.policy.rule(d.rule)
        ui.recolor(self.badge_box, "checkin")
        ui.set_symbol(self.badge_icon, "checkin", BADGE)
        self.eyebrow.setStringValue_(f"{rule.id.replace('_', ' ')} · check in".upper())
        self.headline.setStringValue_("What are you here for?")
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
    def _confirm(self, text: str):
        """A short "got it" in the panel, then it goes."""
        self.mode = "done"
        ui.recolor(self.badge_box, "done")
        ui.set_symbol(self.badge_icon, "done", BADGE)
        self.eyebrow.setStringValue_("")
        self.headline.setStringValue_(text)
        self._layout()
        which = self.current
        AppHelper.callLater(1.4, lambda: self._close() if self.current is which else None)

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
            threading.Thread(target=go_back, args=(ev.screen, self.policy.page_fine),
                             daemon=True).start()

    def need_(self, sender):
        if self.current is None or self.mode == "done":
            return
        d, ev = self.current
        if self.mode == "checkin":
            if self._left() > 0:
                return
            reason = str(self.why.stringValue()).strip()
            if len(reason) < 3:
                self.why.setPlaceholderString_("A few words first: what's it for?")
                self.panel.makeFirstResponder_(self.why)
                return
            c = self.policy.start_session(d.rule, self._picked(), reason, d.id)
            self._confirm(f"Until {datetime.fromtimestamp(c.until):%H:%M}, for “{reason[:60]}”.")
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
        if len(reason) < 3:
            self.why.setPlaceholderString_("A few words first: what's it for?")
            self.panel.makeFirstResponder_(self.why)
            return
        self.policy.snooze(d.rule, self.snooze_minutes, reason, d.id)
        until = datetime.now() + timedelta(minutes=self.snooze_minutes)
        self._confirm(f"Until {until:%H:%M}, for “{reason[:60]}”.")

    def never_(self, sender):
        if self.current is None or self.mode == "done":
            return
        d, ev = self.current
        place = self.policy.never_here(ev.screen.bundle_id, ev.screen.app.strip("‎"), ev.screen.url, d.id)
        self.set_status(f"never again: {place} (undo on the dashboard, Tune)")
        self._confirm(f"I won't look at {place} again. Undo it on the dashboard.")

    def fine_(self, sender):
        if self.current is None or self.mode == "done":
            return
        d, ev = self.current
        self.policy.mark_fine(d.rule, ev.screen.url, ev.screen.window_title, d.id)
        self._confirm("Got it. I'll let pages like this through.")

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
        hint = ui.label("Every rule steps in at once, and the pop-up reminds you of this.", 12, 0.0,
                        NSColor.tertiaryLabelColor(), width=w - 2 * PAD)
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
        if len(intent) < 2:
            self.focus_field.setPlaceholderString_("A few words: what are you here to do?")
            return
        start_focus(self.policy.data_dir, intent, FOCUS_LENGTHS[max(0, self.focus_length.selectedSegment())])
        self.policy.reload_session()
        self.focus_prompt.orderOut_(None)
        self._refresh()

    def focusCancel_(self, sender):
        self.focus_prompt.orderOut_(None)


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


def run_app(policy: Policy | None, rules_path: str, budget: int, demo: str | None = None, review: bool = True,
            data_dir: str | None = None) -> None:
    """The app. With no policy yet (a first run), the setup window comes
    first, and the rules it writes are loaded when it's done."""
    import atexit

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    keep = []  # the controller and the setup window live as long as the app

    def start(policy: Policy):
        ctrl = Controller.alloc().init().setup(policy, str(Path(rules_path).resolve()), demo)
        keep.append(ctrl)
        atexit.register(lambda: ctrl.server and ctrl.server.stop())

        def on_event(ev: Event):
            print(f"[{datetime.now():%H:%M:%S}] {describe(ev)}", flush=True)
            AppHelper.callAfter(ctrl.handle, ev)

        def on_status(text: str):
            print(f"  [{text}]", flush=True)
            AppHelper.callAfter(ctrl.set_status, text)

        from ApplicationServices import AXIsProcessTrusted

        if not AXIsProcessTrusted():
            print("! No Accessibility permission: Qualm can only see app names. Grant it in System Settings >"
                  " Privacy & Security > Accessibility, then restart.", flush=True)
            AppHelper.callAfter(ctrl.set_status, "needs Accessibility permission (see System Settings)")
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
            ctrl.watcher = watcher
            threading.Thread(target=watcher.run, daemon=True).start()
        if review:
            from .review import PORT, start_server

            try:
                server = start_server(policy.data_dir, Path(rules_path), PORT)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                print(f"dashboard: http://127.0.0.1:{PORT}/", flush=True)
            except OSError:
                print(f"dashboard: port {PORT} is taken; `qualm review --web` is probably running", flush=True)

    if policy is not None:
        start(policy)
    else:
        from .onboard import Onboarding
        from .rules import load_config

        keep.append(Onboarding.alloc().init().setup(
            lambda: start(Policy(*load_config(rules_path), data_dir or "data"))))
    AppHelper.runEventLoop(installInterrupt=True)
