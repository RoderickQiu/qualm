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
from pathlib import Path
from typing import Callable

from AppKit import NSRunningApplication, NSWorkspace

from .decide import Reading, ask, make_client
from .policy import Decision, Policy
from .rules import load_config
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
        recheck: float = 30.0,
        rules_path: str | None = None,
        shots: bool = True,
    ):
        self.policy, self.on_event, self.on_status = policy, on_event, on_status
        self.budget, self.interval, self.debounce, self.recheck = budget, interval, debounce, recheck
        self.stop = threading.Event()
        self.rules_path = Path(rules_path) if rules_path else None
        self._rules_mtime = self.rules_path.stat().st_mtime if self.rules_path else 0.0
        self._exc_path = policy.data_dir / "exceptions.jsonl"
        self._exc_mtime = self._exc_path.stat().st_mtime if self._exc_path.exists() else 0.0
        self.shots = shots
        self._shot_sig, self._shot = None, ""

    def _reload_rules(self) -> None:
        """Edits to rules.toml and exceptions (by hand or from the review page) apply without a restart."""
        if self._exc_path.exists() and self._exc_path.stat().st_mtime != self._exc_mtime:
            self._exc_mtime = self._exc_path.stat().st_mtime
            self.policy.reload_exceptions()
        if not self.rules_path:
            return
        try:
            mtime = self.rules_path.stat().st_mtime
            if mtime == self._rules_mtime:
                return
            self._rules_mtime = mtime
            self.policy.reload(*load_config(self.rules_path))
            self.on_status("rules reloaded")
        except Exception as e:  # a half-saved or broken file: keep the old rules
            self.on_status(f"rules.toml not reloaded: {e}")

    def _screenshot(self, s: ScreenState) -> str:
        """A small picture of the window, once per new screen, for the review page."""
        if not self.shots or len(s.frame) != 4:
            return ""
        if s.signature() == self._shot_sig:
            return self._shot
        folder = self.policy.data_dir / "shots"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns() // 1_000_000}.jpg"
        x, y, w, h = (int(v) for v in s.frame)
        r = subprocess.run(["screencapture", "-x", "-t", "jpg", "-R", f"{x},{y},{w},{h}", str(folder / name)],
                           capture_output=True)
        if r.returncode != 0:
            return ""
        # Shrink off the loop: a retina window is ~1 MB, 900 px wide is ~60 KB.
        threading.Thread(target=subprocess.run, daemon=True, args=(
            ["sips", "-Z", "900", "-s", "formatOptions", "60", str(folder / name)],), kwargs={"capture_output": True}).start()
        self._shot_sig, self._shot = s.signature(), name
        return name

    def run(self) -> None:
        client = make_client()
        last_sig, changed_at, last_asked = None, 0.0, float("-inf")
        judged_state = None  # what the model read last time
        me = os.getpid()
        while not self.stop.is_set():
            now = time.monotonic()
            self._reload_rules()
            self.policy.tick()
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is None or front.processIdentifier() == me:
                # Our own panel is in front: keep judging the screen behind it.
                time.sleep(self.interval)
                continue
            s = capture(skip=self.policy.settings.no_monitor)
            if s.signature() != last_sig:
                last_sig, changed_at = s.signature(), now
            # A new screen (app, title or URL): ask once it has been stable for
            # the debounce window. The same screen whose text changed (a feed
            # scrolled, the next video loaded in place): ask again, at most once
            # per recheck. Nothing changed: don't ask; the answer would be the
            # same. Time caps keep counting from the last answer meanwhile.
            new_screen = now - changed_at >= self.debounce and changed_at > last_asked
            state = s.to_state(self.budget)
            new_text = changed_at <= last_asked and state != judged_state and now - last_asked >= self.recheck
            if new_screen or new_text:
                last_asked, judged_state = now, state
                if not self._judge(client, s):
                    judged_state = None  # the model didn't answer: ask again next time
            time.sleep(self.interval)

    def _judge(self, client, s: ScreenState) -> bool:
        """Judge one screen. False if the model couldn't be asked."""
        state = s.to_state(self.budget)
        pre = self.policy.precheck(s.bundle_id, s.url)
        if pre is not None:
            ev = Event(s, state, None, [pre])
            shot = self._screenshot(s) if pre.action != "skip" else ""
            ev.id = self.policy.log_judgement(s.as_record(), None, [pre], shot=shot)
            self.on_event(ev)
            return True
        shot = self._screenshot(s)
        try:
            reading = ask(client, state, self.policy.rules, self.policy.settings.lang, self.policy.settings.allow)
        except Exception as e:  # server down, timeout: say so, keep watching
            self.on_status(f"model unreachable: {type(e).__name__}")
            time.sleep(5)
            return False
        self.on_status("watching")
        decisions = self.policy.decide(state, reading, s.bundle_id)
        for d in decisions:
            if d.action == "intervene":
                self.policy.log_intervention(d, s.as_record(), reading)
        if reading.sensitive >= 0.5 and shot:
            # Taken before the model said it was sensitive: don't keep it.
            (self.policy.data_dir / "shots" / shot).unlink(missing_ok=True)
            self._shot_sig, shot = None, ""
        ev = Event(s, state, reading, decisions)
        ev.id = self.policy.log_judgement(s.as_record(), reading, decisions, state=state, shot=shot)
        self.on_event(ev)
        return True


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
