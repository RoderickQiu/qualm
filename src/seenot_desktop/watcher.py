"""The loop: screen -> precheck or model -> policy -> callbacks.

Shared by `seenot-desktop watch` (prints) and `seenot-desktop app` (menu bar
and intervention panel). Runs on its own thread in the app.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from AppKit import NSRunningApplication, NSWorkspace

from .decide import Reading, ask, make_client
from .policy import Decision, Policy
from .state import DEFAULT_CHAR_BUDGET, ScreenState, capture

BROWSERS = {
    "com.google.Chrome", "com.apple.Safari", "company.thebrowser.Browser", "com.microsoft.edgemac",
    "com.brave.Browser", "org.mozilla.firefox",
}


@dataclass
class Event:
    screen: ScreenState
    state: dict
    reading: Reading | None
    decisions: list[Decision]
    id: str = ""  # the line in data/judgements.jsonl; `review --fix <id>` refers to it


def describe(ev: Event) -> str:
    """One line per judgement, for logs."""
    r = ev.reading
    seen = f"page={r.page_kind} purpose={r.purpose} {r.latency_ms:.0f} ms" if r else "no model call"
    acts = "; ".join(f"{d.action} {d.rule} ({d.reason})".replace(" ()", "") for d in ev.decisions) or "nothing"
    hits = ""
    if r:
        top = sorted(r.rules, key=lambda v: -v.p_hit)[:3]
        hits = "  [" + " ".join(f"{v.rule_id}={v.p_hit:.2f}" for v in top) + "]"
    return f"#{ev.id} {ev.screen.app} | {ev.screen.window_title[:50]} | {ev.screen.url[:60]}\n    {seen} -> {acts}{hits}"


class Watcher:
    def __init__(
        self,
        policy: Policy,
        on_event: Callable[[Event], None],
        on_status: Callable[[str], None] = lambda s: None,
        budget: int = DEFAULT_CHAR_BUDGET,
        interval: float = 0.5,
        debounce: float = 0.5,
        heartbeat: float = 30.0,
    ):
        self.policy, self.on_event, self.on_status = policy, on_event, on_status
        self.budget, self.interval, self.debounce, self.heartbeat = budget, interval, debounce, heartbeat
        self.stop = threading.Event()

    def run(self) -> None:
        client = make_client()
        last_sig, changed_at, last_asked = None, 0.0, float("-inf")
        me = os.getpid()
        while not self.stop.is_set():
            now = time.monotonic()
            self.policy.tick()
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is None or front.processIdentifier() == me:
                # Our own panel is in front: keep judging the screen behind it.
                time.sleep(self.interval)
                continue
            s = capture(skip=self.policy.settings.no_monitor)
            if s.signature() != last_sig:
                last_sig, changed_at = s.signature(), now
            # Ask once the screen has been stable for the debounce window, or on
            # the heartbeat if nothing changed (a long video, a long read).
            settled = now - changed_at >= self.debounce and changed_at > last_asked
            if settled or now - last_asked >= self.heartbeat:
                last_asked = now
                self._judge(client, s)
            time.sleep(self.interval)

    def _judge(self, client, s: ScreenState) -> None:
        state = s.to_state(self.budget)
        pre = self.policy.precheck(s.bundle_id, s.url)
        if pre is not None:
            ev = Event(s, state, None, [pre])
            ev.id = self.policy.log_judgement(s.as_record(), None, [pre])
            self.on_event(ev)
            return
        try:
            reading = ask(client, state, self.policy.rules, self.policy.settings.lang)
        except Exception as e:  # server down, timeout: say so, keep watching
            self.on_status(f"model unreachable: {type(e).__name__}")
            time.sleep(5)
            return
        self.on_status("watching")
        decisions = self.policy.decide(state, reading, s.bundle_id)
        for d in decisions:
            if d.action == "intervene":
                self.policy.log_intervention(d, s.as_record(), reading)
        ev = Event(s, state, reading, decisions)
        ev.id = self.policy.log_judgement(s.as_record(), reading, decisions)
        self.on_event(ev)


def go_back(bundle_id: str) -> None:
    """Leave the page: Back in a browser, hide any other app."""
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
    if not apps:
        return
    app = apps[0]
    if bundle_id not in BROWSERS:
        app.hide()
        return
    app.activateWithOptions_(0)
    time.sleep(0.25)
    # Cmd-[ is Back in every major browser; needs Accessibility, which the
    # watcher already has.
    subprocess.run(
        ["osascript", "-e", 'tell application "System Events" to keystroke "[" using command down'],
        check=False, capture_output=True,
    )
