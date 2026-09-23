"""Menu bar app: the watcher on a thread, an intervention panel on top.

The panel is friction, not a lock: it floats over everything (full-screen
video included), dims the screens behind it, and offers a way out. "Take
me back" is the default; "I need it" unlocks after a short wait that grows
with each use and asks what for; "Not this one" teaches the rule an
exception; "Never here" silences an app or site. Each answer is logged.

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
    NSSegmentedControl,
    NSStatusBar,
    NSTextField,
    NSTimer,
    NSVariableStatusItemLength,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from . import ui
from .policy import Decision, Policy, end_focus, host_of, start_focus
from .watcher import PANEL_TITLE, Event, Watcher, describe, go_back

SNOOZE_MINUTES = 10
FOCUS_SNOOZE_MINUTES = 5  # in a focus session, "I need it" is a short break
# "I need it" unlocks after a wait that doubles with each snooze in the last
# hour: 5, 10, 20, 40, 60 s. Research on one sec (PNAS 2023) found the
# option to back out and a short wait both cut use; the message alone didn't.
FIRST_WAIT_S, MAX_WAIT_S = 5, 60
FOCUS_LENGTHS = (25, 50, 90)  # minutes offered by the focus prompt
W, PAD = 560.0, 28.0  # panel width and margin
BADGE = 46.0


class Controller(NSObject):
    @objc.python_method
    def setup(self, policy: Policy, rules_path: str, demo: str | None = None):
        self.policy, self.rules_path, self.demo = policy, rules_path, demo
        self.current: tuple[Decision, Event] | None = None
        self.last: Event | None = None  # the latest judged screen, for "This should have been blocked"
        self.mode = "ask"  # the panel: "ask" -> "why" (what do you need it for) -> "done" (a short "got it")
        self.evidence_text = self.context_text = ""
        self.dimmer = ui.Dimmer()
        self._build_menu()
        self._build_panel()
        self._build_focus_prompt()
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
        for item in (self.status_line, self.usage_line, NSMenuItem.separatorItem(),
                     self.focus_line, self.focus_item, self.end_focus_item, self.pause_item, self.resume_item,
                     NSMenuItem.separatorItem(),
                     self._item("This should have been blocked", "flagMiss:"),
                     self._item("Open dashboard", "openReview:", "d"),
                     self._item("Edit rules…", "openRules:"), NSMenuItem.separatorItem(),
                     self._item("Quit Qualm", "quit:", "q")):
            menu.addItem_(item)
        self.status_item.setMenu_(menu)

    @objc.python_method
    def _refresh(self):
        """Icon and menu from the policy's state: focus, pause, today's budgets."""
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
        usage = self.policy.usage_summary()
        self.usage_line.setTitle_(usage)
        self.usage_line.setHidden_(not usage)

    def tick_(self, timer):
        self._refresh()

    @objc.python_method
    def set_status(self, text):
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
        NSApp.terminate_(self)

    # -- events from the watcher (main thread) --------------------------------

    @objc.python_method
    def handle(self, ev: Event):
        shown = [d for d in ev.decisions if d.action != "skip" or d.reason != "paused"]
        summary = ", ".join(f"{d.action} {d.rule}".strip() for d in shown) or "nothing"
        if ev.reading is not None:
            self.last = ev
        self.set_status(f"{ev.screen.app.strip(chr(0x200e))}: {summary}")
        hit = next((d for d in ev.decisions if d.action == "intervene"), None)
        if hit and not self.panel.isVisible():
            self._show(hit, ev)

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
        self.back = NSButton.buttonWithTitle_target_action_("Take me back", self, "back:")
        self.back.setKeyEquivalent_("\r")
        self.back.setControlSize_(3)  # large
        self.need = NSButton.buttonWithTitle_target_action_("I need it", self, "need:")
        self.need.setControlSize_(3)
        self.fine = ui.link("Not this one", self, "fine:")
        self.never = ui.link("Never here", self, "never:")
        for v in (self.badge_box, self.badge_icon, self.eyebrow, self.headline, self.place_icon, self.place,
                  self.body, self.context, self.why, self.back, self.need, self.fine, self.never):
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
            if self.mode == "why":
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
        for v in (self.place_icon, self.place, self.body, self.context, self.why, self.back, self.need, self.fine, self.never):
            v.setHidden_(done)
        if not done:
            self.place_icon.setFrame_(NSMakeRect(PAD, y["place"] + 1, 16, 16))
            has_icon = self.place_icon.image() is not None
            self.place.setFrame_(NSMakeRect(PAD + (22 if has_icon else 0), y["place"], iw - (22 if has_icon else 0), 18))
            self.body.setFrame_(NSMakeRect(PAD, y["body"], iw, body_h))
            self.context.setHidden_(not ctx_h)
            if ctx_h:
                self.context.setFrame_(NSMakeRect(PAD, y["context"], iw, ctx_h))
            self.why.setHidden_(self.mode != "why")
            if self.mode == "why":
                self.why.setFrame_(NSMakeRect(PAD, y["why"], iw, 30))
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
        self.mode = "ask"
        rule = self.policy.rule(d.rule)
        lang = self.policy.settings.lang
        focus = self.policy.focusing()
        eyebrow, head = headline(d, rule, lang, focus)
        kind = "focus" if focus else "budget" if eyebrow.endswith("daily limit") else "deny"
        ui.recolor(self.badge_box, kind)
        ui.set_symbol(self.badge_icon, kind, BADGE)
        self.eyebrow.setStringValue_(eyebrow.upper())
        self.headline.setStringValue_(head)
        self.place_icon.setImage_(ui.app_icon(ev.screen.bundle_id))
        where = ev.screen.window_title or ev.screen.app.strip("‎")
        host = host_of(ev.screen.url)
        self.place.setStringValue_(f"{where} — {host}" if host and host not in where.lower() else where)
        self.body.setStringValue_(reason(d, ev.reading, rule, lang))
        self.context_text = context(self.policy.popups_today(d.rule, but=d.id), self.policy.last_snooze(d.rule))
        checking = ev.reading is not None and not self.demo
        self.evidence_text = "Checking which part of the screen triggered it…" if checking else ""
        self.why.setStringValue_("")
        self.back.setKeyEquivalent_("\r")
        self.need.setKeyEquivalent_("")
        self.snooze_minutes = FOCUS_SNOOZE_MINUTES if focus else SNOOZE_MINUTES
        self.back.setTitle_("Back to it" if focus else "Take me back")
        self.fine.setTitle_("It's part of the task" if focus else "Not this one")
        self.fine.sizeToFit()
        place = host or ev.screen.app.strip("‎")
        self.never.setTitle_((f"Never on {place}" if host else f"Never in {place}")[:32])
        self.never.sizeToFit()
        self._start_countdown()
        self._layout()
        if checking:
            threading.Thread(target=self._find_evidence, args=(d, ev), daemon=True).start()
        self.panel.center()
        self.panel.setAlphaValue_(0.0)
        self.dimmer.show()
        NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeFirstResponder_(None)
        ui.fade(self.panel, 1.0, 0.2)

    @objc.python_method
    def _find_evidence(self, d: Decision, ev: Event):
        from .decide import make_client
        from .explain import evidence

        rule = next((r for r in self.policy.rules if r.id == d.rule), None)  # with the exceptions the model saw
        try:
            text = evidence(make_client(), ev.state, rule, self.policy.settings.lang) if rule else ""
        except Exception:
            text = ""
        AppHelper.callAfter(self._set_evidence, d, text)

    @objc.python_method
    def _set_evidence(self, d: Decision, text: str):
        if self.current and self.current[0] is d and self.mode != "done":
            self.evidence_text = text
            self._layout()

    @objc.python_method
    def _start_countdown(self):
        n = self.policy.snoozes_in_last_hour()
        self._wait = min(MAX_WAIT_S, FIRST_WAIT_S * 2 ** n)
        self._tick_countdown(self.current)

    @objc.python_method
    def _tick_countdown(self, which):
        if self.current is not which or self.mode != "ask":
            return  # the panel closed or moved on
        if self._wait <= 0:
            self.need.setEnabled_(True)
            self.need.setTitle_("I need it" if self.snooze_minutes == SNOOZE_MINUTES else f"{self.snooze_minutes}-min break")
            return
        self.need.setEnabled_(False)
        self.need.setTitle_(f"Wait {self._wait} s")
        self._wait -= 1
        AppHelper.callLater(1.0, self._tick_countdown, which)

    @objc.python_method
    def _close(self):
        cur, self.current = self.current, None
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
        d, ev = self._close()
        self.policy.log_response(d.id, "back", d.rule)
        if not self.demo:
            threading.Thread(target=go_back, args=(ev.screen.bundle_id,), daemon=True).start()

    def need_(self, sender):
        if self.current is None or self.mode == "done":
            return
        if self.mode == "ask":
            # Say what for first: a reason turns an impulse into a decision.
            self.mode = "why"
            self.need.setTitle_(f"Unlock {self.snooze_minutes} min")
            self.need.setEnabled_(True)
            self.back.setKeyEquivalent_("")  # Return now means "unlock", from the field or the button
            self.need.setKeyEquivalent_("\r")
            self._layout()
            self.panel.makeFirstResponder_(self.why)
            return
        reason = str(self.why.stringValue()).strip()
        if len(reason) < 3:
            self.why.setPlaceholderString_("A few words first: what's it for?")
            self.panel.makeFirstResponder_(self.why)
            return
        d, ev = self.current
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


DEMOS = ("deny", "feed", "budget", "focus", "prompt")


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
    cap = pick(lambda r: r.kind == "time_cap", deny)
    social = pick(lambda r: r.id == "social", cap)
    screens = {
        "deny": (deny, "p_hit 0.91 >= 0.15", ScreenState(
            app="Google Chrome", bundle_id="com.google.Chrome", window_title="Try Not To Smile Challenge #shorts - YouTube",
            url="https://www.youtube.com/shorts/demo")),
        "feed": (feed, "an entertainment feed", ScreenState(
            app="Google Chrome", bundle_id="com.google.Chrome", window_title="小红书 - 你的生活兴趣社区",
            url="https://www.xiaohongshu.com/explore")),
        "budget": (cap, f"46 of {cap.minutes_per_day or 45:g} min today", ScreenState(
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
    # An earlier pop-up today, so the context line shows.
    earlier = max(datetime.now() - timedelta(minutes=50), datetime.now().replace(hour=0, minute=1))
    with (policy.data_dir / "decisions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": earlier.isoformat(timespec="seconds"), "type": "intervention", "id": "earlier",
                            "rule": rule.id, "reason": why, "screen": {}}) + "\n")
    reading = Reading(0.02, "single_item", {"single_item": 0.9}, "entertain", {"entertain": 0.9},
                      [RuleVerdict(rule.id, "violates", 0.91 if kind == "deny" else 0.73, {})], 850.0)
    d = Decision("intervene", rule.id, why, "demo")
    AppHelper.callLater(0.5, ctrl.handle, Event(screen, {}, reading if kind != "feed" else None, [d]))


def run_app(policy: Policy, rules_path: str, budget: int, demo: str | None = None, review: bool = True) -> None:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    ctrl = Controller.alloc().init().setup(policy, str(Path(rules_path).resolve()), demo)

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
    mode = "budgets on: time caps count first" if policy.settings.budgets else "budgets off: every hit pops up"
    print(f"Qualm watching ({mode}). Ctrl-C to stop.", flush=True)

    if demo:
        _demo(ctrl, policy, demo)
    else:
        watcher = Watcher(policy, on_event, on_status, budget=budget, rules_path=rules_path)
        threading.Thread(target=watcher.run, daemon=True).start()
    if review:
        from .review import PORT, start_server

        try:
            server = start_server(policy.data_dir, Path(rules_path), PORT)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print(f"dashboard: http://127.0.0.1:{PORT}/", flush=True)
        except OSError:
            print(f"dashboard: port {PORT} is taken; `qualm review --web` is probably running", flush=True)
    AppHelper.runEventLoop(installInterrupt=True)
