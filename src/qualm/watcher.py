"""The loop: screen -> precheck or model -> policy -> callbacks.

Shared by `qualm watch` (prints) and `qualm app` (menu bar
and intervention panel). Runs on its own thread in the app.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from AppKit import NSRunningApplication, NSWorkspace
from ApplicationServices import AXUIElementCreateApplication, AXUIElementPerformAction

from . import paths
from .decide import Reading, ask, backend, make_client
from .policy import Decision, Policy, clock, host_of
from .presence import Presence, screen_locked
from .review import write_status
from .rules import build_questions, listed_anyway, load_config
from .state import (BROWSERS, CHROMIUM, DEFAULT_CHAR_BUDGET, SAFARI, ScreenState, _ax, _clean, _frame, _title,
                    browser_window, capture, page_address)

PANEL_TITLE = "Qualm"  # the intervention panel's window title
SETUP_TITLE = "Set up Qualm"  # the first-run window (onboard.py)
OLD_PANEL_TITLES = ("SeeNot",)  # before the rename
BUNDLE_ID = "com.qualm.app"  # Qualm.app
# What the policy gets when the model can't answer: nothing but the URL and
# the app, so only a rule's own sites, patterns and apps can hit.
NO_READING = Reading(sensitive=0.0, page_kind="other", page_probs={}, purpose="", purpose_probs={}, rules=[], latency_ms=0.0)
CACHE_SIZE = 1000  # answers kept, keyed by exactly what the model reads and is asked
DWELL_S = 4.0  # a pop-up waits until you've been on the page this long
TRACEBACK_EVERY_S = 60  # an error in the loop is printed with its traceback at most this often
LOG_MAX = 2 << 20  # past this, the login item's log is set aside as .log.1 (as kev.log is)
LOG_CHECK_S = 60  # how often the watcher looks at that log's size


def bound_log(limit: int = LOG_MAX) -> None:
    """The log the login item sends the app's output to (in ~/Library/Logs/Qualm,
    which launchd opens for appending): past `limit`, its text is set aside
    as .log.1 and it starts again. Output anywhere else (a terminal, a file
    of your own) is left alone."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        err = os.fstat(2)
        if not stat.S_ISREG(err.st_mode) or err.st_size <= limit:
            return
        for fd in (1, 2):  # emptied under a writer that doesn't append, it would write past the end
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) == (err.st_dev, err.st_ino) and not fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_APPEND:
                return
        path = Path(os.fsdecode(fcntl.fcntl(2, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0]))
        if path.parent != paths.LOGS.resolve():
            return
        shutil.copyfile(path, path.with_name(path.name + ".1"))
        os.ftruncate(2, 0)
    except (OSError, ValueError):
        pass


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
        self._paused = policy.paused()  # as last said in the status line
        self.shots = shots
        self._shot_sig, self._shot = None, ""
        self._shrink: threading.Thread | None = None  # the latest screenshot being made smaller
        self._cache: OrderedDict[str, Reading] = OrderedDict()
        self._pending: dict | None = None  # a judgement with a pop-up, waiting out DWELL_S
        self.latest: Event | None = None  # the latest judgement, logged or still waiting out DWELL_S
        self._front_sig = None  # the screen in front, as last read
        self._changed_at = 0.0  # when the current screen appeared (monotonic)
        self._rejudge = False  # set when focus or pause changed, or the policy asks
        self.renew = False  # set by the app when the TypeSafe key changes: make the client again
        # rules.toml doesn't load (the app started on another version, or keeps the rules it had): it may
        # list apps not to read that can't be told apart, so nothing goes to a hosted model until it loads.
        self.rules_broken = False
        self.dwell = DWELL_S
        self.presence = Presence() if presence else None
        # With `wake` (events.py sets it when the front app, window or title
        # changes), an idle loop looks every `idle` s instead of every
        # `interval`: while a screen settles or a pop-up waits, it stays fast.
        self.wake, self.idle = wake, idle
        self._log_checked = float("-inf")

    def _bound_log(self) -> None:
        """The app prints every judgement and status, and the login item's log
        keeps it all: its size is looked at now and then (bound_log)."""
        if (now := time.monotonic()) - self._log_checked >= LOG_CHECK_S:
            self._log_checked = now
            bound_log()

    def _away(self, front) -> bool:
        if self.presence is None:
            return False
        try:
            if front is None:
                return self.presence.away()
            exe = front.executableURL()
            return self.presence.away(str(front.localizedName() or ""), str(exe.lastPathComponent()) if exe else "")
        except Exception:  # never let presence detection stop the loop
            return False

    @staticmethod
    def _mtime(path: Path) -> float:
        return path.stat().st_mtime if path.exists() else 0.0

    def _reload_rules(self) -> None:
        """Edits to rules.toml and exceptions (by hand or from the review page),
        and a pause or focus session set from anywhere, apply without a restart;
        so does a planned pause beginning, or a pause ending, which write nothing."""
        if (m := self._mtime(self._session_path)) != self._session_mtime or self.policy.paused() != self._paused:
            self._session_mtime = m
            self.policy.reload_session()
            # Focus or pause changes what the screen you're on means: judge it
            # again now (from the cache), not when you next move.
            self._rejudge = True
            f, self._paused = self.policy.focusing(), self.policy.paused()
            self.on_status(f"paused until {clock(self.policy.paused_until)}"
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
            self.rules_broken = False
            self.on_status("rules reloaded")
        except Exception as e:  # a half-saved or broken file: keep the old rules
            # But skip what it lists in no_monitor, as far as that can be read (the mistake may be in
            # the edit that added an app there), and send nothing to a hosted model until it loads.
            self.rules_broken = True
            try:
                more = listed_anyway(self.rules_path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                more = ()
            settings = self.policy.settings
            settings.no_monitor = tuple(dict.fromkeys((*settings.no_monitor, *more)))
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
        self._shrink = threading.Thread(target=subprocess.run, daemon=True, args=(
            ["sips", "-Z", "900", "-s", "formatOptions", "60", str(folder / name)],), kwargs={"capture_output": True})
        self._shrink.start()
        self._shot_sig, self._shot = s.signature(), name
        return name

    def _drop_shot(self, shot: str) -> None:
        """Delete a screenshot no judgement will keep."""
        if not shot:
            return
        if self._shrink is not None:
            self._shrink.join(timeout=5)  # sips writes the file back when done: let it finish first
        (self.policy.data_dir / "shots" / shot).unlink(missing_ok=True)
        if shot == self._shot:
            self._shot_sig, self._shot = None, ""

    def _client(self, client):
        """The model client, made again when [settings] backend changes or a new
        key is saved (`renew`); None while it can't be made (hosted, with no key
        yet), and while it would be hosted and rules.toml doesn't load."""
        if self.rules_broken and backend(self.policy.settings) == "jev":
            return None
        if client is not None and client.backend == backend(self.policy.settings) and not self.renew:
            return client
        self.renew = False
        try:
            return make_client(self.policy.settings)
        except RuntimeError as e:
            write_status(self.policy.data_dir, model=str(e))
            self.on_status(str(e))
            return None

    def run(self) -> None:
        """The loop. An error in one pass (a pattern in rules.toml that isn't a
        valid regex, an app's odd Accessibility tree) is logged and shown, and
        the loop goes on: a watcher thread that died silently left Qualm blind
        with the menu bar looking fine."""
        write_status(self.policy.data_dir, pid=os.getpid(), model="starting")  # for the dashboard's chip
        last, printed, met = None, float("-inf"), set()
        while not self.stop.is_set():
            try:
                self._run()
            except Exception as e:
                # The same error every pass: once in the log is enough. Its text
                # can carry an id or a value that changes each time, so it's
                # told apart by its type and where it was raised. One not met
                # before is printed at once; one back after another, at most
                # once a minute (two taking turns): the login item keeps its log.
                tb = traceback.extract_tb(e.__traceback__)
                where = (type(e).__name__, *((tb[-1].filename, tb[-1].lineno) if tb else ()))
                self._bound_log()
                if where not in met or where != last and time.monotonic() - printed >= TRACEBACK_EVERY_S:
                    traceback.print_exc()
                    met.add(where)
                    last, printed = where, time.monotonic()
                self.on_status(f"couldn't judge the screen ({type(e).__name__}: {e}); still watching")
                time.sleep(5)

    def _run(self) -> None:
        client = self._client(None)
        last_sig, changed_at, last_asked = None, 0.0, float("-inf")
        judged_state = None  # what the model read last time
        me = os.getpid()
        while not self.stop.is_set():
            now = time.monotonic()
            self._bound_log()
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
            if self.presence is not None and screen_locked():
                self._nap(busy=False)  # nobody is looking: nothing to judge, nothing sent to a hosted model
                continue
            s = capture(skip=self.policy.settings.no_monitor)
            self._front_sig = s.signature()
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

    def _ask(self, client, state: dict, rules: list | None = None) -> Reading:
        """The model's answer, or the cached one if it already read exactly
        this text with exactly these questions (tab switches, going back).
        `rules`: the ones to ask about (all that are on, by default)."""
        settings = self.policy.settings
        rules = self.policy.rules if rules is None else rules
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
        """Judge one screen. False if the model couldn't be asked. A screenshot
        is kept only with a reading that didn't find the page private: none
        for a screen let through or skipped before the model is asked, none
        while it's down or starting, none for a login or bank page."""
        self._drop_pending()  # a new judgement replaces one still waiting
        state = s.to_state(self.budget)
        pre = self.policy.precheck(s.bundle_id, s.url, s.window_title, s.app)
        if pre is not None:
            ev = self.latest = Event(s, state, None, [pre], uuid.uuid4().hex[:8])
            self.policy.log_judgement(s.as_record(), None, [pre], id=ev.id)
            self.on_event(ev)
            return True
        shot, reading = "", None
        # A rule that names this app (a game, the TV app) is a hit without the
        # model, which could only guess from a window showing its name: it
        # isn't asked about. The other rules are, as in any app.
        on = self.policy.rules
        rules = [r for r in on if not r.matches_app(s.bundle_id, s.app)]
        by_app = len(rules) < len(on)
        if by_app and not rules:
            decisions = self.policy.decide(state, NO_READING, s.bundle_id)
        else:
            reused = self._shot_sig == s.signature()
            shot = self._screenshot(s) if client is not None else ""
            try:
                if client is None:
                    raise LookupError("no model to ask")
                reading = self._ask(client, state, rules)
            except Exception as e:  # server down, timeout, no key: say so, keep watching
                if client is not None:
                    write_status(self.policy.data_dir, model=type(e).__name__)
                    self.on_status(f"model unreachable: {type(e).__name__}")
                if not reused:
                    self._drop_shot(shot)  # never read: nothing says whether the page was private
                shot = ""
                if not by_app and not any(r.matches_url(s.url) for r in self.policy.active_rules()):
                    time.sleep(5)
                    return False
                # A rule's own sites and apps are a hit without the model's say, so
                # they still step in while it's down or swapped out (a 30 s timeout).
                decisions = self.policy.decide(state, NO_READING, s.bundle_id)
            else:
                write_status(self.policy.data_dir, model="ok")
                self.on_status("watching")
                decisions = self.policy.decide(state, reading, s.bundle_id)
                if reading.sensitive >= 0.5:
                    # Taken before the model said it was sensitive: don't keep it.
                    self._drop_shot(shot)
                    shot = ""
        ev = self.latest = Event(s, state, reading, decisions, uuid.uuid4().hex[:8])
        pending = {"ev": ev, "shot": shot, "sig": s.signature(), "due": self._changed_at + self.dwell}
        if any(d.action == "intervene" for d in decisions) and time.monotonic() < pending["due"]:
            # Don't pop up on a page you're passing through, or one still
            # loading: wait until you've stayed DWELL_S, then show it.
            self._pending = pending
        else:
            self._finish(pending, shown=True)
        return reading is not None or by_app and not rules  # no answer: ask again next time

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
        # The app shows one pop-up per screen, the first hit's: the others never show, so they aren't logged as pop-ups.
        hit = next((d for d in ev.decisions if d.action == "intervene"), None)
        if hit is not None:
            self.policy.log_intervention(hit, ev.screen.as_record(), ev.reading)
        self.policy.log_judgement(ev.screen.as_record(), ev.reading, ev.decisions, state=ev.state, shot=p["shot"], id=ev.id)
        self.on_event(ev)

    def current(self) -> Event | None:
        """The judgement of the screen in front now, logged or still waiting
        out the dwell: what "This should have been blocked" flags. None if
        this screen hasn't been judged (just arrived, or the model is down)."""
        ev = self.latest
        return ev if ev is not None and ev.screen.signature() == self._front_sig else None


MAX_BACK = 12  # history steps "Take me back" tries before giving up on a tab
FRONT_WAIT_S = 1.0  # how long a browser brought forward for its keys may take to come to the front


def _osa(script: str) -> str | None:
    """Run AppleScript; its output, or None if it failed (no Automation
    permission for that browser, no window)."""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


class Away(Exception):
    """The browser isn't in front (you went elsewhere, or it never came
    forward): its keys would reach whatever app is, so Take me back stops."""


class Tab:
    """The front tab of a browser, driven through its AppleScript dictionary:
    reads the address after each step instead of pressing keys blind. Safari
    has no "go back": its Back is Cmd-[, pressed by `keys`."""

    def __init__(self, bundle_id: str, keys: Keys | None = None):
        self.app = f'application id "{bundle_id}"'
        self.chromium = bundle_id in CHROMIUM
        self.ref = "active tab of front window" if self.chromium else "current tab of front window"
        self.keys = keys

    def url(self) -> str | None:
        return _osa(f"tell {self.app} to get URL of {self.ref}")

    def back(self) -> bool:
        if self.chromium:
            return _osa(f"tell {self.app} to tell {self.ref} to go back") is not None
        return self.keys is not None and self.keys.back()

    def blank(self) -> bool:
        new = "chrome://newtab/" if self.chromium else "favorites://"
        return _osa(f'tell {self.app} to set URL of {self.ref} to "{new}"') is not None


class Keys:
    """A browser with no AppleScript for this (Firefox, Orion, ...), and
    Safari's Back: Cmd-[ for Back and Cmd-T for a new tab, the address read
    back through Accessibility, or with `by_title` the window's title. The
    browser is brought forward once; before every key it must still be the
    app in front, or the walk stops (Away): it never pulls you back. Cmd-T
    only with `new_tab`: in a browser Qualm doesn't list, it could be
    anything (Show Fonts)."""

    def __init__(self, app, by_title: bool = False, new_tab: bool = True):
        self.app, self.by_title, self.new_tab, self.raised = app, by_title, new_tab, False

    def url(self) -> str | None:
        return page_address(self.app.processIdentifier(), self.by_title)

    def _front(self) -> bool:
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        return front is not None and front.processIdentifier() == self.app.processIdentifier()

    def _press(self, key: str) -> bool:
        if not self.raised:
            self.raised = True
            self.app.activateWithOptions_(0)
            time.sleep(0.25)
            waited = 0.25
            while not self._front() and waited < FRONT_WAIT_S:
                time.sleep(0.05)
                waited += 0.05
        if not self._front():
            raise Away
        # Needs Accessibility, which the watcher already has.
        return _osa(f'tell application "System Events" to keystroke "{key}" using command down') is not None

    def back(self) -> bool:
        return self._press("[")

    def blank(self) -> bool:
        return self.new_tab and self._press("t")


def _step(tab, cur: str, poll: float, patience: float) -> str | None:
    """One step back: the address after it, or None if the tab refused."""
    if tab.back() is False:
        return None
    waited = 0.0
    while True:  # single-page sites change the address late
        time.sleep(poll)
        waited += poll
        new = tab.url() or cur
        if new != cur or waited >= patience:
            return new


def leave(tab, url: str, fine: Callable[[str], bool] = lambda u: False, poll: float = 0.15,
          patience: float = 2.0) -> str:
    """Go back until the tab is off the pop-up's site (the host of `url`) or
    on a page of it Qualm found fine (the lecture before the Shorts). One
    step back often lands on the same site's feed, so one step isn't enough.
    With no site to go by (no address), one step that changes the address is
    enough. Nothing to go back to: a new-tab page, where the tab can open
    one, else "stayed". Returns what it did ("no tab": nothing to drive)."""
    host = host_of(url)
    cur = tab.url()
    if cur is None:
        return "no tab"
    if host and (host_of(cur) != host or fine(cur)):
        return "left"  # already gone
    if not host:
        new = _step(tab, cur, poll, patience)
        if new is None:
            return "no tab"
        if new != cur:
            return "left"
    else:
        for n in range(MAX_BACK):
            new = _step(tab, cur, poll, patience)
            if new is None and n == 0:
                return "no tab"
            if new is None or new == cur:
                break  # no history left
            cur = new
            if host_of(cur) != host or fine(cur):
                return "left"
    return "new tab" if tab.blank() else "stayed"


def judged_window(windows: list[tuple[str, list[float]]], title: str, frame: list[float]) -> int | None:
    """Which of an app's open windows (title, frame) the pop-up was about, if
    it's one of several: WeChat's photo viewer over the chats. None when it's
    the only one, or can't be told apart; then the app is hidden instead."""
    if len(windows) < 2:
        return None

    def near(f):
        return bool(frame) and len(f) == 4 and all(abs(a - b) <= 2 for a, b in zip(f, frame))

    for match in (lambda t, f: t == title and near(f), lambda t, f: near(f), lambda t, f: t == title):
        hits = [i for i, (t, f) in enumerate(windows) if match(t, f)]
        if len(hits) == 1:
            return hits[0]
    return None


def close_window(pid: int, app_name: str, title: str, frame: list[float]) -> bool:
    """Close the window the pop-up was about, if the app has others open
    (its close button, through Accessibility). True if it's gone."""
    root = AXUIElementCreateApplication(pid)
    windows = [w for w in (_ax(root, "AXWindows") or []) if not _ax(w, "AXMinimized")]
    i = judged_window([(_title(_clean(_ax(w, "AXTitle")), app_name), _frame(w)) for w in windows], title, frame)
    button = _ax(windows[i], "AXCloseButton") if i is not None else None
    if button is None or AXUIElementPerformAction(button, "AXPress") != 0:
        return False
    time.sleep(0.3)
    return len([w for w in (_ax(root, "AXWindows") or []) if not _ax(w, "AXMinimized")]) < len(windows)


def go_back(screen: ScreenState, fine: Callable[[str], bool] = lambda u: False,
            done: Callable[[str], None] = lambda how: None) -> str:
    """Leave the page. In a browser Qualm knows (state.BROWSERS), back off the
    site (see leave()): through its AppleScript where it speaks Chrome's or
    Safari's, else by keys; a browser window, with its tabs, is never closed.
    A browser it doesn't list (its window has a page with an address, and a
    tab strip or an address field: state.browser_window) gets one Back, by
    keys. In any other app, even one showing a web page (a Chrome or Edge web
    app, Slack), close the window if it's one of several (a photo opened from
    a chat), else hide the app: a Back there stays in the app. Returns what
    it did ("stayed" if you went elsewhere before it was done), and tells
    `done`."""
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(screen.bundle_id)
    how = _go_back(apps[0], screen, fine) if apps else "gone"
    done(how)
    return how


def _go_back(app, screen: ScreenState, fine: Callable[[str], bool]) -> str:
    bundle_id, url = screen.bundle_id, screen.url
    if bundle_id not in BROWSERS:
        if ".app." not in bundle_id and browser_window(app.processIdentifier()):
            # A browser Qualm doesn't list (a new one, a Chinese one): closing
            # its window would lose every tab in it. One Back, the address read
            # back, and no Cmd-T. Chrome's and Edge's web apps
            # (com.google.Chrome.app.…) are apps, not browsers.
            try:
                how = leave(Keys(app, new_tab=False), "", fine)
            except Away:
                return "stayed"
            return "stayed" if how == "no tab" else how
        if close_window(app.processIdentifier(), screen.app, screen.window_title, screen.frame):
            return "closed"
        app.hide()
        return "hidden"
    keys = Keys(app)
    try:
        if bundle_id in CHROMIUM or bundle_id == SAFARI:
            how = leave(Tab(bundle_id, keys), url, fine)
            if how not in ("no tab", "stayed"):  # "stayed": it wouldn't open a new-tab page; Cmd-T will
                return how
        # No AppleScript for it (or no Automation permission): keys.
        how = leave(keys, url, fine)
        if how == "no tab":  # no address to read back: go by the window's title
            keys.by_title = True
            how = leave(keys, "", fine)
        if how == "no tab":  # not even a title: one Back, blind, as before
            how = "back" if keys.back() else "stayed"
        return how
    except Away:
        return "stayed"
