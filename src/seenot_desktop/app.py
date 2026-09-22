"""Menu bar app: the watcher on a thread, an intervention panel on top.

The panel is friction, not a lock: it floats over everything (full-screen
video included) and offers three ways out. Each answer is logged, and
"Not this one" teaches the rule an exception.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSButton,
    NSFont,
    NSImage,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSPanel,
    NSStatusBar,
    NSStatusWindowLevel,
    NSTextField,
    NSVariableStatusItemLength,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from .policy import Decision, Policy
from .watcher import Event, Watcher, go_back

SNOOZE_MINUTES = 10


class Controller(NSObject):
    @objc.python_method
    def setup(self, policy: Policy, rules_path: str, demo: bool = False):
        self.policy, self.rules_path, self.demo = policy, rules_path, demo
        self.current: tuple[Decision, Event] | None = None
        self._build_menu()
        self._build_panel()
        return self

    # -- menu bar ------------------------------------------------------------

    @objc.python_method
    def _item(self, title, action=None, key=""):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        if action:
            item.setTarget_(self)
        else:
            item.setEnabled_(False)
        return item

    @objc.python_method
    def _build_menu(self):
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        # A stable identity across launches, so macOS and menu bar managers
        # (Thaw, Bartender) remember where you put it instead of treating each
        # launch as a new item.
        self.status_item.setAutosaveName_("SeeNot")
        self.status_item.setVisible_(True)
        self._set_icon(paused=False)
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.status_line = self._item("starting…")
        self.usage_line = self._item(self.policy.usage_summary() or "no time limits")
        self.pause_item = self._item("Pause 30 min", "togglePause:")
        for item in (self.status_line, self.usage_line, NSMenuItem.separatorItem(), self.pause_item,
                     self._item("Open rules…", "openRules:"), NSMenuItem.separatorItem(),
                     self._item("Quit SeeNot", "quit:", "q")):
            menu.addItem_(item)
        self.status_item.setMenu_(menu)

    @objc.python_method
    def _set_icon(self, paused: bool):
        # An SF Symbol, so menu bar managers (Thaw, Bartender) can show it;
        # they list the item as "python3" because it isn't an app bundle.
        name = "pause.circle" if paused else "eye"
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "SeeNot")
        image.setTemplate_(True)
        button = self.status_item.button()
        button.setImage_(image)
        button.setToolTip_("SeeNot" + (" (paused)" if paused else ""))

    @objc.python_method
    def set_status(self, text):
        self.status_line.setTitle_(text[:80])

    def togglePause_(self, sender):
        paused = self.pause_item.title().startswith("Resume")
        self.policy.pause(0 if paused else 30)
        self.pause_item.setTitle_("Pause 30 min" if paused else "Resume")
        self._set_icon(paused=not paused)

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
        self.set_status(f"{ev.screen.app}: {summary}")
        self.usage_line.setTitle_(self.policy.usage_summary() or "no time limits")
        hit = next((d for d in ev.decisions if d.action == "intervene"), None)
        if hit and not self.panel.isVisible():
            self._show(hit, ev)

    # -- the panel -----------------------------------------------------------

    @objc.python_method
    def _build_panel(self):
        w, h = 460, 230
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False
        )
        panel.setTitle_("SeeNot")
        panel.setLevel_(NSStatusWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        panel.setHidesOnDeactivate_(False)
        view = panel.contentView()
        self.headline = NSTextField.labelWithString_("")
        self.headline.setFont_(NSFont.boldSystemFontOfSize_(16))
        self.headline.setFrame_(NSMakeRect(20, h - 44, w - 40, 24))
        self.body = NSTextField.wrappingLabelWithString_("")
        self.body.setFrame_(NSMakeRect(20, 96, w - 40, h - 146))
        self.why = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 60, w - 40, 24))
        self.why.setPlaceholderString_("If you need it: what for? (optional)")
        back = NSButton.buttonWithTitle_target_action_("Take me back", self, "back:")
        back.setKeyEquivalent_("\r")
        need = NSButton.buttonWithTitle_target_action_(f"I need it: {SNOOZE_MINUTES} min", self, "need:")
        fine = NSButton.buttonWithTitle_target_action_("Not this one", self, "fine:")
        back.setFrame_(NSMakeRect(w - 150, 16, 130, 32))
        need.setFrame_(NSMakeRect(w - 300, 16, 145, 32))
        fine.setFrame_(NSMakeRect(20, 16, 120, 32))
        for v in (self.headline, self.body, self.why, back, need, fine):
            view.addSubview_(v)
        self.panel = panel

    @objc.python_method
    def _show(self, d: Decision, ev: Event):
        self.current = (d, ev)
        rule = self.policy.rule(d.rule)
        where = ev.screen.window_title or ev.screen.app
        self.headline.setStringValue_(f"This looks like: {rule.id}")
        self.body.setStringValue_(
            f"{where[:90]}\n\nYour rule: {rule.text(self.policy.settings.lang)}\n({d.reason})"
        )
        self.why.setStringValue_("")
        self.panel.center()
        NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)

    @objc.python_method
    def _close(self):
        self.panel.orderOut_(None)
        cur, self.current = self.current, None
        return cur

    def back_(self, sender):
        d, ev = self._close()
        self.policy.log_response(d.id, "back", d.rule)
        if not self.demo:
            threading.Thread(target=go_back, args=(ev.screen.bundle_id,), daemon=True).start()

    def need_(self, sender):
        reason = str(self.why.stringValue())
        d, ev = self._close()
        self.policy.snooze(d.rule, SNOOZE_MINUTES, reason, d.id)

    def fine_(self, sender):
        d, ev = self._close()
        self.policy.mark_fine(d.rule, ev.screen.url, ev.screen.window_title, d.id)


def run_app(policy: Policy, rules_path: str, budget: int, demo: bool = False) -> None:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    ctrl = Controller.alloc().init().setup(policy, str(Path(rules_path).resolve()), demo)

    def on_event(ev: Event):
        AppHelper.callAfter(ctrl.handle, ev)

    def on_status(text: str):
        AppHelper.callAfter(ctrl.set_status, text)

    if demo:
        from .state import ScreenState
        from .watcher import Event as Ev

        rule = policy.rules[0]
        screen = ScreenState(app="Safari", bundle_id="com.apple.Safari",
                             window_title="Try Not To Smile #shorts - YouTube",
                             url="https://www.youtube.com/shorts/demo")
        d = Decision("intervene", rule.id, "demo: matches URL pattern", "demo")
        AppHelper.callLater(0.5, ctrl.handle, Ev(screen, {}, None, [d]))
    else:
        watcher = Watcher(policy, on_event, on_status, budget=budget)
        threading.Thread(target=watcher.run, daemon=True).start()
    AppHelper.runEventLoop(installInterrupt=True)

