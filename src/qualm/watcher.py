"""The loop: screen -> precheck or model -> policy -> callbacks.

Shared by `qualm watch` (prints) and `qualm app` (menu bar
and intervention panel). Runs on its own thread in the app.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from AppKit import NSRunningApplication, NSWorkspace

from .decide import Reading, ask, backend, make_client
from .policy import Decision, Policy, host_of
from .presence import Presence
from .rules import build_questions, load_config
from .state import DEFAULT_CHAR_BUDGET, ScreenState, capture

PANEL_TITLE = "Qualm"  # the intervention panel's window title
SETUP_TITLE = "Set up Qualm"  # the first-run window (onboard.py)
OLD_PANEL_TITLES = ("SeeNot",)  # before the rename
BUNDLE_ID = "com.qualm.app"  # Qualm.app
# What the policy gets when the model can't answer: nothing but the URL, so
# only a rule's own sites and patterns can hit.
NO_READING = Reading(sensitive=0.0, page_kind="other", page_probs={}, purpose="", purpose_probs={}, rules=[], latency_ms=0.0)
CACHE_SIZE = 1000  # answers kept, keyed by exactly what the model reads and is asked
DWELL_S = 4.0  # a pop-up waits until you've been on the page this long

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
    seen = f"page={r.page_kind} purpose={r.purpose} " + ("cached" if r.cached else f"{r.latency_ms:.0f} ms") if r else "no model call"
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
        presence: bool = True,
        wake: threading.Event | None = None,
        idle: float = 3.0,
    ):
        self.policy, self.on_event, self.on_status = policy, on_event, on_status
        self.budget, self.interval, self.debounce, self.recheck = budget, interval, debounce, recheck
        self.stop = threading.Event()
        self.rules_path = Path(rules_path) if rules_path else None
        self._rules_mtime = self.rules_path.stat().st_mtime if self.rules_path else 0.0
        self._exc_path = policy.data_dir / "exceptions.jsonl"
        self._exc_mtime = self._exc_path.stat().st_mtime if self._exc_path.exists() else 0.0
        self._session_path = policy.data_dir / "session.json"
        self._session_mtime = self._mtime(self._session_path)
        self.shots = shots
        self._shot_sig, self._shot = None, ""
        self._cache: OrderedDict[str, Reading] = OrderedDict()
        self._pending: dict | None = None  # a judgement with a pop-up, waiting out DWELL_S
        self._changed_at = 0.0  # when the current screen appeared (monotonic)
        self._rejudge = False  # set when focus or pause changed, or the policy asks
        self.dwell = DWELL_S
        self.presence = Presence() if presence else None
        # With `wake` (events.py sets it when the front app, window or title
        # changes), an idle loop looks every `idle` s instead of every
        # `interval`: while a screen settles or a pop-up waits, it stays fast.
        self.wake, self.idle = wake, idle

    def _away(self, front) -> bool:
        if self.presence is None:
            return False
        try:
            return self.presence.away(str(front.localizedName() or "") if front is not None else "")
        except Exception:  # never let presence detection stop the loop
            return False

    @staticmethod
    def _mtime(path: Path) -> float:
        return path.stat().st_mtime if path.exists() else 0.0

    def _reload_rules(self) -> None:
        """Edits to rules.toml and exceptions (by hand or from the review page),
        and a pause or focus session set from anywhere, apply without a restart."""
        if (m := self._mtime(self._session_path)) != self._session_mtime:
            self._session_mtime = m
            self.policy.reload_session()
            # Focus or pause changes what the screen you're on means: judge it
            # again now (from the cache), not when you next move.
            self._rejudge = True
            f = self.policy.focusing()
            self.on_status(f"paused until {time.strftime('%H:%M', time.localtime(self.policy.paused_until))}"
                           if self.policy.paused() else f"focus: {f['intent']}" if f else "watching")
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

    def _client(self, client):
        """The model client, made again when [settings] backend changes; None
        while it can't be made (hosted, with no key yet)."""
        if client is not None and client.backend == backend(self.policy.settings):
            return client
        try:
            return make_client(self.policy.settings)
        except RuntimeError as e:
            self.on_status(str(e))
            return None

    def run(self) -> None:
        client = self._client(None)
        last_sig, changed_at, last_asked = None, 0.0, float("-inf")
        judged_state = None  # what the model read last time
        me = os.getpid()
        while not self.stop.is_set():
            now = time.monotonic()
            self._reload_rules()
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            self.policy.tick(away=self._away(front))
            if self.policy.rejudge_due():
                # A session's time is up, or you're still here after "Take me
                # back": judge the same screen again (from the cache).
                self._rejudge = True
            if front is None or front.processIdentifier() == me:
                # Our own panel is in front: keep judging the screen behind it.
                time.sleep(self.interval)
                continue
            s = capture(skip=self.policy.settings.no_monitor)
            if s.window_title in (PANEL_TITLE, SETUP_TITLE, *OLD_PANEL_TITLES) or s.bundle_id == BUNDLE_ID:
                # Another Qualm's window (a demo, the setup window, a second
                # copy): never judge ourselves. Its text names the very sites
                # the rules are about (the setup lists Douyin, TikTok, Shorts).
                time.sleep(self.interval)
                continue
            if s.signature() != last_sig:
                last_sig, changed_at = s.signature(), now
                self._changed_at = now
            elif self._rejudge:
                changed_at = now  # as if the screen had just appeared: judged after the debounce
            self._rejudge = False
            self._settle_pending(s, now)
            # A new screen (app, title or URL): ask once it has been stable for
            # the debounce window. The same screen whose text changed (a feed
            # scrolled, the next video loaded in place): ask again, at most once
            # per recheck. Nothing changed: don't ask; the answer would be the
            # same. A check-in session keeps counting from the last answer meanwhile.
            new_screen = now - changed_at >= self.debounce and changed_at > last_asked
            state = s.to_state(self.budget)
            new_text = changed_at <= last_asked and state != judged_state and now - last_asked >= self.recheck
            if new_screen or new_text:
                last_asked, judged_state = now, state
                client = self._client(client)
                if not self._judge(client, s):
                    judged_state = None  # the model didn't answer: ask again next time
            settling = changed_at > last_asked or self._pending is not None or self._rejudge or judged_state is None
            self._nap(busy=settling)

    def _nap(self, busy: bool) -> None:
        if self.wake is None or busy:
            time.sleep(self.interval)
            return
        self.wake.wait(self.idle)
        self.wake.clear()

    def _ask(self, client, state: dict) -> Reading:
        """The model's answer, or the cached one if it already read exactly
        this text with exactly these questions (tab switches, going back)."""
        settings = self.policy.settings
        rules = self.policy.rules
        questions = build_questions(rules, settings.lang, settings.allow)
        key = hashlib.sha1(json.dumps(
            [getattr(client, "backend", ""), state,  # a different model is a different answer
             {k: q.model_dump(mode="json", exclude_none=True) for k, q in questions.items()}],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if key in self._cache:
            self._cache.move_to_end(key)
            return replace(self._cache[key], latency_ms=0.0, cached=True)
        reading = ask(client, state, rules, settings.lang, settings.allow)
        self._cache[key] = reading
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return reading

    def _judge(self, client, s: ScreenState) -> bool:
        """Judge one screen. False if the model couldn't be asked."""
        self._drop_pending()  # a new judgement replaces one still waiting
        state = s.to_state(self.budget)
        pre = self.policy.precheck(s.bundle_id, s.url, s.window_title)
        if pre is not None:
            ev = Event(s, state, None, [pre])
            shot = self._screenshot(s) if pre.action != "skip" else ""
            ev.id = self.policy.log_judgement(s.as_record(), None, [pre], shot=shot)
            self.on_event(ev)
            return True
        shot = self._screenshot(s)
        try:
            if client is None:
                raise LookupError("no model to ask")
            reading = self._ask(client, state)
        except Exception as e:  # server down, timeout, no key: say so, keep watching
            if client is not None:
                self.on_status(f"model unreachable: {type(e).__name__}")
            if not any(r.matches_url(s.url) for r in self.policy.active_rules()):
                time.sleep(5)
                return False
            # A rule's own sites are a hit without the model's say, so they
            # still step in while it's down or swapped out (a 30 s timeout).
            decisions = self.policy.decide(state, NO_READING, s.bundle_id)
            reading = None
        else:
            self.on_status("watching")
            decisions = self.policy.decide(state, reading, s.bundle_id)
        if reading is not None and reading.sensitive >= 0.5 and shot:
            # Taken before the model said it was sensitive: don't keep it.
            (self.policy.data_dir / "shots" / shot).unlink(missing_ok=True)
            self._shot_sig, shot = None, ""
        ev = Event(s, state, reading, decisions)
        pending = {"ev": ev, "shot": shot, "sig": s.signature(), "due": self._changed_at + self.dwell}
        if any(d.action == "intervene" for d in decisions) and time.monotonic() < pending["due"]:
            # Don't pop up on a page you're passing through, or one still
            # loading: wait until you've stayed DWELL_S, then show it.
            self._pending = pending
        else:
            self._finish(pending, shown=True)
        return reading is not None  # no answer: ask again next time

    def _settle_pending(self, s: ScreenState, now: float) -> None:
        p = self._pending
        if p is None:
            return
        if s.signature() != p["sig"]:
            self._pending = None
            self._finish(p, shown=False)
        elif now >= p["due"]:
            self._pending = None
            self._finish(p, shown=True)

    def _drop_pending(self) -> None:
        if self._pending is not None:
            p, self._pending = self._pending, None
            self._finish(p, shown=False)

    def _finish(self, p: dict, shown: bool) -> None:
        """Log the judgement and hand it on; a pop-up you left before it was
        due is logged as not shown."""
        ev = p["ev"]
        if not shown:
            ev.decisions = [Decision("skip", d.rule, f"left within {self.dwell:g} s") if d.action == "intervene" else d
                            for d in ev.decisions]
        for d in ev.decisions:
            if d.action == "intervene":
                self.policy.log_intervention(d, ev.screen.as_record(), ev.reading)
        ev.id = self.policy.log_judgement(ev.screen.as_record(), ev.reading, ev.decisions, state=ev.state, shot=p["shot"])
        self.on_event(ev)


CHROMIUM = {"com.google.Chrome", "com.brave.Browser", "com.microsoft.edgemac"}
MAX_BACK = 12  # history steps "Take me back" tries before giving up on a tab


def _osa(script: str) -> str | None:
    """Run AppleScript; its output, or None if it failed (no Automation
    permission for that browser, no window)."""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


class Tab:
    """The front tab of a browser, driven through its AppleScript dictionary:
    reads the address after each step instead of pressing keys blind."""

    def __init__(self, bundle_id: str):
        self.app = f'application id "{bundle_id}"'
        self.chromium = bundle_id in CHROMIUM
        self.ref = "active tab of front window" if self.chromium else "current tab of front window"

    def url(self) -> str | None:
        return _osa(f"tell {self.app} to get URL of {self.ref}")

    def back(self) -> None:
        if self.chromium:
            _osa(f"tell {self.app} to tell {self.ref} to go back")
        else:  # Safari has no "go back": its menu shortcut, with Safari in front
            _osa(f'tell {self.app} to activate\ndelay 0.2\n'
                 'tell application "System Events" to keystroke "[" using command down')

    def blank(self) -> None:
        _osa(f'tell {self.app} to set URL of {self.ref} to "{"chrome://newtab/" if self.chromium else "favorites://"}"')


def leave(tab, url: str, fine: Callable[[str], bool] = lambda u: False, poll: float = 0.15, patience: float = 2.0) -> str:
    """Go back until the tab is off the pop-up's site (the host of `url`) or
    on a page of it Qualm found fine (the lecture before the Shorts). One
    step back often lands on the same site's feed, so one step isn't enough.
    Nothing to go back to: a new-tab page. Returns what it did."""
    host = host_of(url)
    cur = tab.url()
    if cur is None:
        return "no tab"
    for _ in range(MAX_BACK):
        if not host or host_of(cur) != host or fine(cur):
            return "left"
        tab.back()
        waited = 0.0
        while True:  # single-page sites change the address late
            time.sleep(poll)
            waited += poll
            new = tab.url() or cur
            if new != cur or waited >= patience:
                break
        if new == cur:
            break  # no history left
        cur = new
    if host_of(cur) == host and not fine(cur):
        tab.blank()
        return "new tab"
    return "left"


def go_back(bundle_id: str, url: str = "", fine: Callable[[str], bool] = lambda u: False) -> None:
    """Leave the page: in a browser, back off the site (see leave()); any other app is hidden."""
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
    if not apps:
        return
    app = apps[0]
    if bundle_id not in BROWSERS:
        app.hide()
        return
    if bundle_id in CHROMIUM or bundle_id == "com.apple.Safari":
        if leave(Tab(bundle_id), url, fine) != "no tab":
            return
    # Other browsers, or no Automation permission: Cmd-[ once, as before.
    app.activateWithOptions_(0)
    time.sleep(0.25)
    # Cmd-[ is Back in every major browser; needs Accessibility, which the
    # watcher already has.
    subprocess.run(
        ["osascript", "-e", 'tell application "System Events" to keystroke "[" using command down'],
        check=False, capture_output=True,
    )
