"""From a reading to an action.

The model says what the screen is. This module decides what to do about it,
using what a single reading can't know: how the user got here, what they
already allowed, and the check-in session they're in. docs/POLICY.md has
the reasoning.

Actions: "skip" (not judged), "allow" (a rule hit, but an exemption or your
session covers it), "intervene". An intervention's `panel` says which pop-up:
"" (step in), "check_in" (what for, how long) or "times_up".
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from .decide import Reading
from .rules import Rule, Settings

LEARN_MIN = 0.6  # P(purpose = learn) needed for the learning exemption
# P(purpose = entertain) a feed needs before feed_hit fires. Every trial feed
# scored 0.82 or more; the pages it wrongly fired on in real use (an Apple Ads
# "recommendations" page, a job board) won the argmax at 0.41-0.55.
ENTERTAIN_MIN = 0.6
RETURN_S = 1800  # coming back to a rule's pages within this long after going back: a return
MAX_TICK_S = 5.0  # longer gaps (sleep, a stalled model call) don't count as usage
MAX_EXCEPTIONS = 10  # per rule; the newest "Not this one" titles the model reads
# "Not this one" in an app with no address (WeChat's window is always
# "Weixin"): that rule lets this window through below the score it had, plus
# this. The title as words for the model didn't hold: four presses on WeChat
# chats and it still fired, and naming the apps in an exception made the
# model flag them more.
FINE_MARGIN = 0.1
OWN_URLS = ("http://127.0.0.1:8765",)  # the review page: never judge Qualm itself
OWN_TITLES = ("Qualm review", "SeeNot review")  # the same page shown elsewhere (Cursor's browser: a vscode-file:// URL)
SESSION_FILE = "session.json"  # pause and focus: set from the menu, the dashboard or the CLI

# Check-ins (docs/POLICY.md). The wait before a session can start doubles with
# each session today, the first free: 0, 5, 10, 20, 40, 60 s (max_wait_s), and
# doubles again when you come back within SOON_S of a session ending. The
# research this follows: one sec (PNAS 2023), a short wait and a question at
# each opening cut use where a message alone didn't.
CHECK_IN_MINUTES = (5, 15, 30)  # offered on arrival; 5 is preselected
LONGER_WAIT_S = {15: 5, 30: 10}  # a longer session costs a few more seconds
FIRST_WAIT_S = 5
SOON_S = 20 * 60
EXTEND_MINUTES, EXTEND_WAIT_S = 5, 10  # "5 more" when the time is up
RECHECK_S = 30  # still on the page this long after "Take me back" or "Done": step in again
ANSWER_S = 120  # a session whose time is up waits this long for your answer to "time's up"


def host_of(url: str) -> str:
    """example.com for https://www.example.com/a; "" for non-web URLs."""
    m = re.match(r"https?://([^/:?#]+)", url or "")
    return m.group(1).lower().removeprefix("www.") if m else ""


def gate(rule: Rule, p: float, state: dict, reading: Reading, settings: Settings,
         intentional: bool = False) -> tuple[str, str] | None:
    """One rule on one reading, before anything you said at runtime (snoozes,
    "Not this one", check-ins): None (no hit), ("hit", why), or ("allow" |
    "skip", why) for a hit an exemption covers. Shared by the live policy and
    `rules test`, so a test shows what the rule would really do."""
    url = state.get("url", "")
    pattern = rule.matches_url(url)
    feed = rule.feed_hit and reading.page_kind == "feed" and reading.purpose_probs.get("entertain", 0) >= ENTERTAIN_MIN
    if not (pattern or feed or p >= rule.threshold):
        return None
    # A feed of candidates doesn't break "don't show me X"; scrolling it
    # still needs a check-in.
    if rule.kind == "deny" and rule.target == "content" and reading.page_kind == "feed" and not pattern:
        return None
    # An app that shows nothing but its name (WhatsApp, games, players)
    # leaves the model guessing; only a URL pattern may fire then.
    if not pattern and not url and not state.get("headings") and not state.get("visible_text"):
        return "skip", f"too little on screen to judge (p_hit {p:.2f})"
    # A kind of page you said is never flagged ([[allow]]: shopping, ...).
    allowed_as = next((c.id for c in settings.allow if c.enabled and reading.allow.get(c.id, 0) >= c.threshold), None)
    if allowed_as and not pattern:
        return "allow", f"{allowed_as} is never flagged"
    # The model's best guess is a work tool (editor, terminal, docs): leave
    # it alone. Code and notes are full of words any rule can match.
    if reading.page_kind == "work" and not pattern:
        return "allow", "work tool"
    if rule.allow_learning and reading.page_kind != "feed" and reading.purpose_probs.get("learn", 0) >= LEARN_MIN:
        return "allow", "learning material"
    if rule.allow_intentional and intentional:
        return "allow", "opened on purpose"
    return "hit", ("matches URL pattern" if pattern else f"p_hit {p:.2f} >= {rule.threshold:.2f}"
                   if p >= rule.threshold else "an entertainment feed")


def read_session(data_dir: Path) -> dict:
    """{"paused_until": wall time, "focus": {"intent", "started", "until"} or None}, expired entries dropped."""
    path = Path(data_dir) / SESSION_FILE
    try:
        s = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):  # unreadable: treat as nothing set
        s = {}
    if not isinstance(s, dict):
        s = {}
    now = time.time()
    paused = s.get("paused_until")
    focus = s.get("focus")
    # Written by hand or by an agent too: anything malformed counts as not set.
    ok_focus = (isinstance(focus, dict) and isinstance(focus.get("intent"), str)
                and all(isinstance(focus.get(k), (int, float)) for k in ("started", "until")))
    return {"paused_until": float(paused) if isinstance(paused, (int, float)) and paused > now else 0.0,
            "focus": focus if ok_focus and focus["until"] > now else None}


_SESSION_LOCK = threading.Lock()  # the menu, the dashboard and the watcher share the file


def write_session(data_dir: Path, **changes) -> dict:
    path = Path(data_dir) / SESSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SESSION_LOCK:
        s = read_session(data_dir) | changes
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".session-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
        os.replace(tmp, path)  # the watcher reloads on change; never let it read half a file
    return s


def start_focus(data_dir: Path, intent: str, minutes: float) -> dict:
    """A focus session: what you're here to do, until when. While it runs,
    every rule hit steps in at once (check-ins don't apply) and the pop-up
    reminds you what you said: SeeNot's session intents, on the desktop."""
    intent = " ".join(intent.split())
    if not intent:
        raise ValueError("say what you're focusing on, in a few words")
    if not 1 <= minutes <= 12 * 60:
        raise ValueError("a focus session lasts between 1 minute and 12 hours")
    end_focus(data_dir)  # a new session replaces the running one: close it in the log
    now = time.time()
    focus = {"intent": intent[:120], "started": now, "until": now + minutes * 60}
    _log_line(data_dir, {"type": "focus", "intent": focus["intent"], "minutes": minutes})
    write_session(data_dir, focus=focus)
    return focus


def end_focus(data_dir: Path) -> dict | None:
    focus = read_session(data_dir)["focus"]
    if focus:
        _log_line(data_dir, {"type": "focus_end", "intent": focus["intent"],
                             "minutes": round((time.time() - focus["started"]) / 60, 1)})
        write_session(data_dir, focus=None)
    return focus


def pause_for(data_dir: Path, minutes: float) -> float:
    """Pause every rule for `minutes` (0: resume). Returns when it ends (0.0 if not paused)."""
    until = time.time() + minutes * 60 if minutes > 0 else 0.0
    write_session(data_dir, paused_until=until)
    return until


def _log_line(data_dir: Path, event: dict) -> None:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    event = {"at": datetime.now().isoformat(timespec="seconds"), **event}
    with (data_dir / "decisions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


@dataclass
class Decision:
    action: str
    rule: str = ""
    reason: str = ""
    id: str = ""  # set on interventions, to join the user's response in the log
    panel: str = ""  # interventions: "" (step in), "check_in" or "times_up"


@dataclass
class CheckIn:
    """A session you checked in for: this rule's pages are fine until `until`."""
    id: str  # the check-in pop-up's decision id
    rule: str
    purpose: str  # what you said it's for
    minutes: float
    started: float
    until: float
    extended: int = 0
    seconds: float = 0.0  # time actually on the rule's pages
    asked_at: float = 0.0  # when "time's up" popped up: kept for ANSWER_S, for the answer
    showing: bool = False  # the "time's up" pop-up is on screen: kept until it's answered


def _atomic_write(path: Path, text: str) -> None:
    """The dashboard reads these files while the app writes them: never half a file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class Usage:
    """Seconds and sessions per check-in rule, for today, kept across restarts."""

    def __init__(self, path: Path):
        self.path = path
        self.day, self.counts = date.today().isoformat(), {}
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("day") == self.day:
                self.counts = saved["counts"]

    def _rule(self, rule_id: str) -> dict:
        today = date.today().isoformat()
        if today != self.day:
            self.day, self.counts = today, {}
        c = self.counts.setdefault(rule_id, {})
        c.setdefault("seconds", 0.0)
        c.setdefault("sessions", 0)
        return c

    def add(self, rule_id: str, seconds: float = 0.0, sessions: int = 0) -> None:
        c = self._rule(rule_id)
        c["seconds"] += seconds
        c["sessions"] += sessions

    def get(self, rule_id: str) -> tuple[float, int]:
        c = self._rule(rule_id)
        return c["seconds"], c["sessions"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, json.dumps({"day": self.day, "counts": self.counts}))
        # Every day's totals, for the dashboard's charts.
        hist_path = self.path.with_name("usage_history.json")
        history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}
        history[self.day] = self.counts
        _atomic_write(hist_path, json.dumps(history))


class Policy:
    def __init__(self, settings: Settings, rules: list[Rule], data_dir: str | Path = "data"):
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.usage = Usage(self.data_dir / "usage.json")
        self.lock = threading.RLock()
        self.snoozed: dict[str, float] = {}  # rule id -> wall time the snooze ends
        self._snooze_times: list[float] = []  # when "I need it" was used, for the growing wait
        self._back_times: dict[str, list[float]] = {}  # rule -> when "Take me back" / "Done" was answered
        self._snoozes: dict[str, tuple[float, float, str]] = {}  # rule id -> (ends at, minutes, what for)
        self.paused_until = 0.0
        self.focus: dict | None = None  # {"intent", "started", "until"}: see start_focus()
        self.reload_session()
        self.counting: set[str] = set()  # check-in rules whose session the current screen is in
        self.sessions: dict[str, CheckIn] = {}  # rule id -> its session (running, or up and not yet ended)
        self._started_today: list[float] = []  # when each check-in session today began, for the wait
        self.last_end = 0.0  # when the latest session ended
        self._rejudge_at: list[float] = []  # when the watcher should judge the screen again
        self._seen: OrderedDict[str, bool] = OrderedDict()  # recent URLs -> judged fine
        self._restore_sessions()
        self._base_rules = rules
        self._allowed: dict[str, set[str]] = {}  # rule id -> URLs marked "Not this one"
        self._titles: dict[str, list[str]] = {}
        self._bars: dict[tuple[str, str, str], float] = {}  # (rule, bundle id, title) -> "Not this one" below this score
        self._never_apps: dict[str, str] = {}  # bundle id -> app name: "Never in WhatsApp"
        self._never_hosts: set[str] = set()  # "Never on example.com"
        self._load_exceptions()
        self._last_tick: float | None = None
        self._last_save = 0.0
        # The screen before this one, for "opened on purpose".
        self._cur = {"key": None, "page_kind": "other", "purpose": "", "app": "", "intentional": False}

    # -- rules, with what the user taught ------------------------------------

    @property
    def rules(self) -> list[Rule]:
        """The rules on right now (enabled, inside their `when`), with what you taught them."""
        return [
            replace(r, exceptions=r.exceptions + tuple(self._titles.get(r.id, [])[-MAX_EXCEPTIONS:]))
            for r in self.active_rules()
        ]

    def active_rules(self) -> list[Rule]:
        now = datetime.now()
        return [r for r in self._base_rules if r.active(now)]

    def rule(self, rule_id: str) -> Rule:
        return next(r for r in self._base_rules if r.id == rule_id)

    def _load_exceptions(self) -> None:
        path = self.data_dir / "exceptions.jsonl"
        if not path.exists():
            return
        for line in path.open(encoding="utf-8"):
            if line.strip():
                self._add_exception(json.loads(line))

    def _add_exception(self, e: dict) -> None:
        if e.get("never"):
            n = e["never"]
            if n.get("app"):
                if e.get("removed"):
                    self._never_apps.pop(n["app"], None)
                else:
                    self._never_apps[n["app"]] = n.get("name", n["app"])
            if n.get("host"):
                (self._never_hosts.discard if e.get("removed") else self._never_hosts.add)(n["host"])
            return
        titles = self._titles.setdefault(e["rule"], [])
        bar = e.get("app") is not None and e.get("p_hit") is not None and not e.get("url")
        said = [f'the page "{e["title"]}"'] if e.get("title") and not bar else []
        said += [e["text"]] if e.get("text") else []  # typed on the review page or `except add`
        if e.get("removed"):  # `except remove`
            self._allowed.get(e["rule"], set()).discard(e.get("url"))
            titles[:] = [t for t in titles if t not in said and t != f'the page "{e.get("title")}"']
            for k in [k for k in self._bars if k[0] == e["rule"] and k[2] == e.get("title")]:
                del self._bars[k]
            return
        if bar:
            k = (e["rule"], e["app"], e.get("title", ""))
            self._bars[k] = max(self._bars.get(k, 0.0), float(e["p_hit"]) + FINE_MARGIN)
            return
        if e.get("url"):
            self._allowed.setdefault(e["rule"], set()).add(e["url"])
        titles += said

    # -- decisions -----------------------------------------------------------

    def precheck(self, bundle_id: str, url: str, title: str = "") -> Decision | None:
        """Decisions that need no model call. None means: ask the model."""
        with self.lock:
            if time.time() < self.paused_until:
                d = Decision("skip", reason="paused")
            elif bundle_id in self.settings.no_monitor:
                d = Decision("skip", reason="app not monitored")
            elif url.startswith(OWN_URLS) or title.startswith(OWN_TITLES):
                d = Decision("skip", reason="Qualm's own page")
            elif self.settings.allowed_url(url):
                d = Decision("allow", reason="allowed URL")
            elif bundle_id in self._never_apps or host_of(url) in self._never_hosts:
                d = Decision("allow", reason="you said never here")
            elif (c := next((c for c in self.settings.allow if c.enabled and c.matches(bundle_id, url)), None)) is not None:
                d = Decision("allow", reason=f"{c.id} is never flagged")
            else:
                return None
            self.counting = set()
            if url and d.action == "allow":
                self._remember(url, True)
            # Leaving an allowed page (docs, a repo) for a link counts as on purpose.
            kind = "work" if d.action == "allow" else "other"
            self._arrive(url or bundle_id, kind, "task", bundle_id)
            return d

    def _arrive(self, key: str, page_kind: str, purpose: str, app: str) -> None:
        cur = self._cur
        if key != cur["key"]:
            cur["from"] = {k: cur[k] for k in ("key", "page_kind", "purpose", "app")} if cur["key"] else None
            # One item opened straight from search, a work tool, or a link in
            # another app (chat, mail) is on purpose. From a feed or from
            # another item it is drift.
            from_intent = cur["page_kind"] in ("search", "work") or (cur["purpose"] == "task" and cur["app"] != app)
            cur["intentional"] = page_kind == "single_item" and from_intent
        cur.update(key=key, page_kind=page_kind, purpose=purpose, app=app)

    def decide(self, state: dict, reading: Reading, bundle_id: str = "") -> list[Decision]:
        with self.lock:
            url = state.get("url", "")
            key = url or f"{state.get('app', '')}|{state.get('window_title', '')}"
            if reading.sensitive >= 0.5:
                self.counting = set()
                self._arrive(key, "other", "task", bundle_id)
                return [Decision("skip", reason="sensitive page")]
            self._arrive(key, reading.page_kind, reading.purpose, bundle_id)
            out, counted, now, check_ins = [], set(), time.time(), []
            for rule in self.active_rules():
                v = reading.verdict(rule.id)
                g = gate(rule, v.p_hit if v else 0.0, state, reading, self.settings, self._cur["intentional"])
                if g is None:
                    continue
                action, why = g
                if action != "hit":
                    out.append(Decision(action, rule.id, why))
                elif url and url in self._allowed.get(rule.id, ()):
                    out.append(Decision("allow", rule.id, "you marked this page fine"))
                elif (not url and v is not None
                      and v.p_hit < self._bars.get((rule.id, bundle_id, state.get("window_title", "")), 0.0)):
                    out.append(Decision("allow", rule.id, "you marked this window fine"))
                elif self.snoozed.get(rule.id, 0) > now:
                    until = datetime.fromtimestamp(self.snoozed[rule.id]).strftime("%H:%M")
                    out.append(Decision("allow", rule.id, f"snoozed until {until}"))
                elif rule.kind == "deny" or self.focusing():
                    out.append(Decision("intervene", rule.id, why, uuid.uuid4().hex[:12]))
                else:
                    check_ins.append((rule, why))
            out += self._check_in(check_ins, counted, now)
            self.counting = counted
            if url:
                self._remember(url, not any(d.action == "intervene" or d.reason.startswith(("your ", "snoozed")) for d in out))
            return out

    def _remember(self, url: str, fine: bool) -> None:
        self._seen[url] = fine
        self._seen.move_to_end(url)
        while len(self._seen) > 500:
            self._seen.popitem(last=False)

    def page_fine(self, url: str) -> bool:
        """Qualm judged this page and nothing on it needed you to step in
        (a lecture, a docs page): where "Take me back" may stop."""
        return self._seen.get(url, False)

    def _check_in(self, hits: list[tuple[Rule, str]], counted: set[str], now: float) -> list[Decision]:
        """The check-in rules this page hits: one pop-up at most. A running
        session covers the page's other check-in rules too (a video on a
        social site is one session, not two)."""
        if not hits:
            return []
        live = next((self.sessions[r.id] for r, _ in hits if r.id in self.sessions and self.sessions[r.id].until > now), None)
        if live is not None:
            counted.add(live.rule)
            said = f"until {datetime.fromtimestamp(live.until):%H:%M}, for “{live.purpose}”"
            return [Decision("allow", r.id, f"your session {said}" if r.id == live.rule else f"your {live.rule} session {said}")
                    for r, _ in hits]
        up = next((self.sessions[r.id] for r, _ in hits if r.id in self.sessions), None)
        if up is not None:  # still here when the time you chose is up
            up.asked_at = up.asked_at or now
            spent = f"{EXTEND_MINUTES:g} more minutes" if up.extended else f"{up.minutes:g} minutes"
            return [Decision("intervene", up.rule, f"your {spent} for “{up.purpose}” are up", uuid.uuid4().hex[:12], "times_up")]
        rule, why = hits[0]
        return [Decision("intervene", rule.id, why, uuid.uuid4().hex[:12], "check_in")]

    def tick(self, now: float | None = None, away: bool = False) -> None:
        """Call on every loop: adds the time since the last tick to the
        sessions the current screen is in, unless you're away (presence.py),
        and ends the sessions whose time ran out while you were elsewhere."""
        now = time.time() if now is None else now
        with self.lock:
            if self._last_tick is not None and not away:
                elapsed = min(now - self._last_tick, MAX_TICK_S)
                for rule_id in self.counting:
                    self.usage.add(rule_id, seconds=elapsed)
                    if rule_id in self.sessions:
                        self.sessions[rule_id].seconds += elapsed
            self._last_tick = now
            for rule_id, c in list(self.sessions.items()):
                if c.until > now:
                    continue
                if rule_id in self.counting:
                    # Time's up while you're on it: judge the screen again,
                    # which pops up "time's up".
                    self.counting.discard(rule_id)
                    c.asked_at = now  # kept until the pop-up is answered (ANSWER_S)
                    self._rejudge_at.append(now)
                elif c.showing or (c.asked_at and now - c.asked_at < ANSWER_S):
                    continue  # "time's up" is on screen, or about to be: its answer ends it
                elif c.asked_at:
                    # Asked, but the pop-up never showed (another was open) or you
                    # left: judge again, so a page you're still on gets a check-in.
                    self.end_session(rule_id, "time", now=now)
                    self._rejudge_at.append(now)
                else:
                    # Ran out while you were elsewhere (or the Mac slept, or Qualm
                    # was off): it ended when the time was up, not now.
                    self.end_session(rule_id, "time", now=min(now, c.until))
            if now - self._last_save > 30:
                self.usage.save()
                self._last_save = now

    def rejudge_due(self, now: float | None = None) -> bool:
        """True once when the screen should be judged again though it didn't
        change: a session's time is up, an unlock ran out, or you're still
        on a page RECHECK_S after "Take me back"."""
        now = time.time() if now is None else now
        with self.lock:
            due = [t for t in self._rejudge_at if t <= now]
            if not due:
                return False
            self._rejudge_at = [t for t in self._rejudge_at if t > now]
            return True

    def rejudge_in(self, seconds: float) -> None:
        with self.lock:
            self._rejudge_at.append(time.time() + seconds)

    # -- check-in sessions ---------------------------------------------------

    def check_in_wait(self, minutes: float = CHECK_IN_MINUTES[0], now: float | None = None) -> int:
        """Seconds before a session of `minutes` can start: see CHECK_IN_MINUTES."""
        now = time.time() if now is None else now
        with self.lock:
            n = sum(1 for t in self._started_today if datetime.fromtimestamp(t).date() == date.today())
            wait = FIRST_WAIT_S * 2 ** (n - 1) if n else 0
            if self.last_end and now - self.last_end < SOON_S:
                wait = max(wait * 2, FIRST_WAIT_S)
            return int(min(wait + LONGER_WAIT_S.get(int(minutes), 0), self.settings.max_wait_s))

    def hold(self, rule_id: str, showing: bool) -> None:
        """The app shows (or closed) "time's up" for this rule's session: it
        isn't ended under the open pop-up, however long the answer takes."""
        with self.lock:
            if (c := self.sessions.get(rule_id)) is not None:
                c.showing = showing

    def start_session(self, rule_id: str, minutes: float, purpose: str, decision_id: str = "") -> CheckIn:
        now = time.time()
        with self.lock:
            if rule_id in self.sessions:
                self.end_session(rule_id, "new", now=now)
            c = CheckIn(decision_id or uuid.uuid4().hex[:12], rule_id, purpose, minutes, now, now + minutes * 60)
            self.sessions[rule_id] = c
            self._started_today.append(now)
            self.usage.add(rule_id, sessions=1)
            self._rejudge_at.append(now)  # the page you're on is in the session now: start counting
        self._log({"type": "session", "event": "start", "id": c.id, "rule": rule_id, "minutes": minutes,
                   "for": purpose, "until": round(c.until)})
        self.log_response(decision_id, "session", rule_id, reason=purpose, minutes=minutes)
        return c

    def extend_session(self, rule_id: str, decision_id: str = "", minutes: float = EXTEND_MINUTES) -> CheckIn | None:
        """ "5 more" when the time is up; None when the session is gone or out of extensions."""
        now = time.time()
        with self.lock:
            c = self.sessions.get(rule_id)
            if c is None or c.extended >= self.settings.extensions:
                return None
            c.until = max(c.until, now) + minutes * 60
            c.extended += 1
            c.asked_at = 0.0
            self._rejudge_at.append(now)
        self._log({"type": "session", "event": "extend", "id": c.id, "rule": rule_id, "minutes": minutes,
                   "until": round(c.until)})
        self.log_response(decision_id, "snooze", rule_id, reason=c.purpose, minutes=minutes)
        return c

    def end_session(self, rule_id: str, how: str, decision_id: str = "", now: float | None = None) -> CheckIn | None:
        """how: "done" (you ended it when the time was up), "time" (it ran out
        while you were elsewhere), "new" (a new session replaced it)."""
        now = time.time() if now is None else now
        with self.lock:
            c = self.sessions.pop(rule_id, None)
            if c is None:
                return None
            self.last_end = now
            self.counting.discard(rule_id)
        self._log({"type": "session", "event": "end", "id": c.id, "rule": rule_id, "how": how, "for": c.purpose,
                   "minutes": c.minutes + c.extended * EXTEND_MINUTES, "stayed": round(c.seconds / 60, 1),
                   "extended": c.extended})
        if how == "done":
            self.log_response(decision_id, "back", rule_id)
        return c

    def _restore_sessions(self) -> None:
        """Today's sessions from the log, so a restart keeps a running one and the wait."""
        path = self.data_dir / "decisions.jsonl"
        if not path.exists():
            return
        today = date.today().isoformat()
        for line in path.open(encoding="utf-8"):
            if not line.startswith('{"at": "' + today) or '"session"' not in line:
                continue
            e = json.loads(line)
            if e.get("type") != "session":
                continue
            at = datetime.fromisoformat(e["at"]).timestamp()
            if e["event"] == "start":
                self._started_today.append(at)
                self.sessions[e["rule"]] = CheckIn(e["id"], e["rule"], e.get("for", ""), e["minutes"], at, e["until"])
            elif e["event"] == "extend" and (c := self.sessions.get(e["rule"])) is not None:
                c.until, c.extended = e["until"], c.extended + 1
            elif e["event"] == "end":
                self.sessions.pop(e["rule"], None)
                self.last_end = at

    # -- what the user says back ---------------------------------------------

    def snooze(self, rule_id: str, minutes: float, reason: str = "", decision_id: str = "") -> None:
        with self.lock:
            self.snoozed[rule_id] = time.time() + minutes * 60
            self._rejudge_at.append(self.snoozed[rule_id] + 1)  # still on it when the unlock runs out: step in
            self._snooze_times.append(time.time())
            self._snoozes[rule_id] = (self.snoozed[rule_id], minutes, reason)
        self.log_response(decision_id, "snooze", rule_id, reason=reason, minutes=minutes)

    def mark_fine(self, rule_id: str, url: str, title: str, decision_id: str = "",
                  bundle_id: str = "", p_hit: float | None = None) -> int:
        """"Not this one". A page with an address: that address is let through,
        and the model reads its title as fine. An app window with none: this
        window is let through below the score it had (FINE_MARGIN). Returns how
        many times you've said it here, this one included."""
        e = {"rule": rule_id, "url": url, "title": title, "at": datetime.now().isoformat(timespec="seconds")}
        if not url and bundle_id and p_hit is not None:
            e |= {"app": bundle_id, "p_hit": round(p_hit, 4)}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "exceptions.jsonl"
        said = sum(1 for line in path.open(encoding="utf-8")
                   if line.strip() and (x := json.loads(line)).get("rule") == rule_id and not x.get("removed")
                   and (x.get("url") == url if url else x.get("title") == title and x.get("app") in (None, bundle_id))
                   ) if path.exists() else 0
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        with self.lock:
            self._add_exception(e)
        self.log_response(decision_id, "fine", rule_id)
        return said + 1

    def last_snooze(self, rule_id: str) -> tuple[float, float, str] | None:
        """(ends at, minutes, what for) of this rule's latest "I need it"."""
        return self._snoozes.get(rule_id)

    def popups_today(self, rule_id: str, but: str = "") -> list[str]:
        """When this rule popped up today (ISO times, oldest first), leaving out decision `but`."""
        path = self.data_dir / "decisions.jsonl"
        if not path.exists():
            return []
        today = date.today().isoformat()
        out = []
        for line in path.open(encoding="utf-8"):
            if line.startswith('{"at": "' + today) and '"intervention"' in line:
                e = json.loads(line)
                if e.get("type") == "intervention" and e.get("rule") == rule_id and e.get("id") != but:
                    out.append(e["at"])
        return out

    def snoozes_in_last_hour(self) -> int:
        with self.lock:
            cutoff = time.time() - 3600
            self._snooze_times = [t for t in self._snooze_times if t > cutoff]
            return len(self._snooze_times)

    def never_here(self, bundle_id: str, app_name: str, url: str, decision_id: str = "") -> str:
        """"Never in this app" (or, in a browser, "never on this site"): no rule fires there again."""
        host = host_of(url)
        never = {"host": host} if host else {"app": bundle_id, "name": app_name}
        e = {"never": never, "at": datetime.now().isoformat(timespec="seconds")}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with (self.data_dir / "exceptions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        with self.lock:
            self._add_exception(e)
        self.log_response(decision_id, "never", "", place=never)
        return host or app_name

    def add_exception_text(self, rule_id: str, text: str) -> None:
        """An exception in your own words ("a lecture on YouTube is fine"); the model reads it."""
        e = {"rule": rule_id, "text": text, "at": datetime.now().isoformat(timespec="seconds")}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with (self.data_dir / "exceptions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        with self.lock:
            self._add_exception(e)

    def reload_exceptions(self) -> None:
        """exceptions.jsonl changed (the review page adds to it)."""
        with self.lock:
            self._allowed, self._titles, self._bars = {}, {}, {}
            self._load_exceptions()

    def reload(self, settings: Settings, rules: list[Rule]) -> None:
        """rules.toml changed: new rules and settings, same history and usage."""
        with self.lock:
            self.settings, self._base_rules = settings, rules

    def pause(self, minutes: float) -> None:
        pause_for(self.data_dir, minutes)
        self.reload_session()

    def reload_session(self) -> None:
        """data/session.json changed (the menu, the dashboard or `qualm pause|focus`)."""
        s = read_session(self.data_dir)
        with self.lock:
            self.paused_until, self.focus = s["paused_until"], s["focus"]

    def focusing(self) -> dict | None:
        """The focus session running now, if any."""
        f = self.focus
        return f if f and f["until"] > time.time() else None

    def paused(self) -> bool:
        return time.time() < self.paused_until

    def usage_summary(self) -> str:
        """For the menu: a running session, or today's minutes."""
        with self.lock:
            parts, now = [], time.time()
            for r in self.active_rules():
                if r.kind != "check_in":
                    continue
                c = self.sessions.get(r.id)
                seconds, _ = self.usage.get(r.id)
                if c is not None and c.until > now:
                    parts.append(f"{r.id} until {datetime.fromtimestamp(c.until):%H:%M}")
                elif seconds >= 60:
                    parts.append(f"{r.id} {seconds / 60:.0f} min today")
            return " · ".join(parts)

    # -- the log that becomes labels -----------------------------------------

    def log_judgement(self, screen: dict, reading: Reading | None, decisions: list[Decision],
                      state: dict | None = None, shot: str = "") -> str:
        """Every judgement, to data/judgements.jsonl, for `qualm review`:
        what was on screen, exactly what the model read, every answer's
        probabilities, the screen before, and what the policy did and why.
        Sensitive pages and unmonitored apps are logged without their content.
        A sensitive page keeps its site (the host, nothing after it) and its
        score, so a page wrongly taken for private can be found later: the
        first day's 69 Chrome ones could only be guessed from their neighbours."""
        sensitive = any(d.reason == "sensitive page" for d in decisions)
        private = sensitive or any(d.reason == "app not monitored" for d in decisions)
        with self.lock:
            event = {
                "id": uuid.uuid4().hex[:8],
                "at": datetime.now().isoformat(timespec="seconds"),
                "screen": {"app": screen.get("app", ""), "bundle_id": screen.get("bundle_id", "")} if private else screen,
                "decisions": [{"action": d.action, "rule": d.rule, "reason": d.reason} for d in decisions],
                "thresholds": {r.id: r.threshold for r in self.active_rules()},
                "came_from": None if private else self._cur.get("from"),
                "opened_on_purpose": self._cur["intentional"],
            }
            if (focus := self.focusing()) is not None:
                event["focus"] = focus["intent"]
            if sensitive:
                if site := host_of(screen.get("url", "")):
                    event["screen"]["site"] = site
                if reading is not None:
                    event["sensitive"] = round(reading.sensitive, 3)
        if shot and not private:
            event["shot"] = shot
        if reading is not None and not private:
            event |= {
                "state": state,  # exactly what the model read
                "page_kind": reading.page_kind, "purpose": reading.purpose,
                "page_probs": {k: round(v, 3) for k, v in reading.page_probs.items()},
                "purpose_probs": {k: round(v, 3) for k, v in reading.purpose_probs.items()},
                "sensitive": round(reading.sensitive, 3),
                "p_hit": {v.rule_id: round(v.p_hit, 3) for v in reading.rules},
                "answers": {v.rule_id: {k: round(x, 3) for k, x in v.probabilities.items()} for v in reading.rules},
                "latency_ms": round(reading.latency_ms),
                "cached": reading.cached,
                "allow": {k: round(v, 3) for k, v in reading.allow.items()},
            }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with (self.data_dir / "judgements.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event["id"]

    def log_intervention(self, d: Decision, screen: dict, reading: Reading | None) -> None:
        """Only interventions are logged with screen content: they are the
        moments the user's answer turns into a label (`harvest`). No reading:
        the model was down and a rule's own site fired."""
        self._log({
            "type": "intervention", "id": d.id, "rule": d.rule, "reason": d.reason, "screen": screen,
            **({"panel": d.panel} if d.panel else {}),
            **({"page_kind": reading.page_kind, "purpose": reading.purpose,
                "p_hit": {v.rule_id: round(v.p_hit, 4) for v in reading.rules}} if reading is not None else {}),
        })

    def log_response(self, decision_id: str, response: str, rule_id: str, **extra) -> None:
        if response in ("back", "done"):
            with self.lock:
                self._back_times.setdefault(rule_id, []).append(time.time())
        self._log({"type": "response", "id": decision_id, "rule": rule_id, "response": response, **extra})

    def returns(self, rule_id: str, within: float = RETURN_S) -> int:
        """How often you went back from this rule's pages lately, and are here again.
        Graded friction (InteractOut, CHI 2024): each return makes "I need it"
        wait longer, instead of blocking harder."""
        with self.lock:
            cutoff = time.time() - within
            kept = self._back_times[rule_id] = [t for t in self._back_times.get(rule_id, []) if t > cutoff]
            return len(kept)

    def _log(self, event: dict) -> None:
        _log_line(self.data_dir, event)
