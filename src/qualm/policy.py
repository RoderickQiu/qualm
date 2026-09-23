"""From a reading to an action.

The model says what the screen is. This module decides what to do about it,
using what a single reading can't know: how the user got here, what they
already allowed, and how much of today's budget is left. docs/POLICY.md has
the reasoning.

Actions: "skip" (not judged), "allow" (a rule hit, but an exemption applies),
"count" (time_cap in scope, within budget), "intervene".
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from .decide import Reading
from .rules import Rule, Settings

LEARN_MIN = 0.6  # P(purpose = learn) needed for the learning exemption
MAX_TICK_S = 5.0  # longer gaps (sleep, a stalled model call) don't count as usage
MAX_EXCEPTIONS = 10  # per rule; the newest "Not this one" titles the model reads
OWN_URLS = ("http://127.0.0.1:8765",)  # the review page: never judge Qualm itself
OWN_TITLES = ("Qualm review", "SeeNot review")  # the same page shown elsewhere (Cursor's browser: a vscode-file:// URL)


def host_of(url: str) -> str:
    """example.com for https://www.example.com/a; "" for non-web URLs."""
    m = re.match(r"https?://([^/:?#]+)", url or "")
    return m.group(1).lower().removeprefix("www.") if m else ""


def gate(rule: Rule, p: float, state: dict, reading: Reading, settings: Settings,
         intentional: bool = False) -> tuple[str, str] | None:
    """One rule on one reading, before anything you said at runtime (snoozes,
    "Not this one", budgets): None (no hit), ("hit", why), or ("allow" |
    "skip", why) for a hit an exemption covers. Shared by the live policy and
    `rules test`, so a test shows what the rule would really do."""
    url = state.get("url", "")
    pattern = rule.matches_url(url)
    feed = rule.feed_hit and reading.page_kind == "feed" and reading.purpose == "entertain"
    if not (pattern or feed or p >= rule.threshold):
        return None
    # A feed of candidates doesn't break "don't show me X"; scrolling it
    # still counts toward a time budget.
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


@dataclass
class Decision:
    action: str
    rule: str = ""
    reason: str = ""
    id: str = ""  # set on interventions, to join the user's response in the log


class Usage:
    """Seconds and visits per time_cap rule, for today, kept across restarts."""

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
        return self.counts.setdefault(rule_id, {"seconds": 0.0, "visits": 0})

    def add(self, rule_id: str, seconds: float = 0.0, visits: int = 0) -> None:
        c = self._rule(rule_id)
        c["seconds"] += seconds
        c["visits"] += visits

    def get(self, rule_id: str) -> tuple[float, int]:
        c = self._rule(rule_id)
        return c["seconds"], c["visits"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"day": self.day, "counts": self.counts}), encoding="utf-8")
        # Every day's totals, for the review page's weekly summary.
        hist_path = self.path.with_name("usage_history.json")
        history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}
        history[self.day] = self.counts
        hist_path.write_text(json.dumps(history), encoding="utf-8")


class Policy:
    def __init__(self, settings: Settings, rules: list[Rule], data_dir: str | Path = "data"):
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.usage = Usage(self.data_dir / "usage.json")
        self.lock = threading.RLock()
        self.snoozed: dict[str, float] = {}  # rule id -> wall time the snooze ends
        self._snooze_times: list[float] = []  # when "I need it" was used, for the growing wait
        self.paused_until = 0.0
        self.counting: set[str] = set()  # time_cap rules the current screen counts toward
        self._base_rules = rules
        self._allowed: dict[str, set[str]] = {}  # rule id -> URLs marked "Not this one"
        self._titles: dict[str, list[str]] = {}
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
        said = [f'the page "{e["title"]}"'] if e.get("title") else []
        said += [e["text"]] if e.get("text") else []  # typed on the review page or `except add`
        if e.get("removed"):  # `except remove`
            self._allowed.get(e["rule"], set()).discard(e.get("url"))
            titles[:] = [t for t in titles if t not in said]
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
            out, counted, now = [], set(), time.time()
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
                elif self.snoozed.get(rule.id, 0) > now:
                    until = datetime.fromtimestamp(self.snoozed[rule.id]).strftime("%H:%M")
                    out.append(Decision("allow", rule.id, f"snoozed until {until}"))
                elif rule.kind == "deny" or not self.settings.budgets:
                    out.append(Decision("intervene", rule.id, why, uuid.uuid4().hex[:12]))
                else:
                    counted.add(rule.id)
                    if rule.id not in self.counting:
                        self.usage.add(rule.id, visits=1)
                    out.append(self._budget(rule, why))
            self.counting = counted
            return out

    def _budget(self, rule: Rule, why: str) -> Decision:
        seconds, visits = self.usage.get(rule.id)
        parts, over = [], False
        if rule.minutes_per_day:
            parts.append(f"{seconds / 60:.0f} of {rule.minutes_per_day:g} min today")
            over |= seconds >= rule.minutes_per_day * 60
        if rule.visits_per_day:
            parts.append(f"visit {visits} of {rule.visits_per_day} today")
            over |= visits > rule.visits_per_day
        reason = ", ".join(parts) or why
        if over:
            return Decision("intervene", rule.id, reason, uuid.uuid4().hex[:12])
        return Decision("count", rule.id, reason)

    def tick(self, now: float | None = None, away: bool = False) -> None:
        """Call on every loop: adds the time since the last tick to the rules
        the current screen counts toward, unless you're away (presence.py)."""
        now = time.time() if now is None else now
        with self.lock:
            if self._last_tick is not None and not away:
                elapsed = min(now - self._last_tick, MAX_TICK_S)
                for rule_id in self.counting:
                    self.usage.add(rule_id, seconds=elapsed)
            self._last_tick = now
            if now - self._last_save > 30:
                self.usage.save()
                self._last_save = now

    # -- what the user says back ---------------------------------------------

    def snooze(self, rule_id: str, minutes: float, reason: str = "", decision_id: str = "") -> None:
        with self.lock:
            self.snoozed[rule_id] = time.time() + minutes * 60
            self._snooze_times.append(time.time())
        self.log_response(decision_id, "snooze", rule_id, reason=reason, minutes=minutes)

    def mark_fine(self, rule_id: str, url: str, title: str, decision_id: str = "") -> None:
        e = {"rule": rule_id, "url": url, "title": title, "at": datetime.now().isoformat(timespec="seconds")}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with (self.data_dir / "exceptions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        with self.lock:
            self._add_exception(e)
        self.log_response(decision_id, "fine", rule_id)

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
            self._allowed, self._titles = {}, {}
            self._load_exceptions()

    def reload(self, settings: Settings, rules: list[Rule]) -> None:
        """rules.toml changed: new rules and settings, same history and usage."""
        with self.lock:
            self.settings, self._base_rules = settings, rules

    def pause(self, minutes: float) -> None:
        with self.lock:
            self.paused_until = time.time() + minutes * 60 if minutes else 0.0

    def usage_summary(self) -> str:
        with self.lock:
            parts = []
            for r in self.active_rules():
                if r.kind != "time_cap":
                    continue
                seconds, visits = self.usage.get(r.id)
                s = f"{r.id} {seconds / 60:.0f}"
                s += f"/{r.minutes_per_day:g} min" if r.minutes_per_day else " min"
                if r.visits_per_day:
                    s += f", {visits}/{r.visits_per_day} visits"
                parts.append(s)
            return " · ".join(parts)

    # -- the log that becomes labels -----------------------------------------

    def log_judgement(self, screen: dict, reading: Reading | None, decisions: list[Decision],
                      state: dict | None = None, shot: str = "") -> str:
        """Every judgement, to data/judgements.jsonl, for `qualm review`:
        what was on screen, exactly what the model read, every answer's
        probabilities, the screen before, and what the policy did and why.
        Sensitive pages and unmonitored apps are logged without their content."""
        private = any(d.reason in ("sensitive page", "app not monitored") for d in decisions)
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

    def log_intervention(self, d: Decision, screen: dict, reading: Reading) -> None:
        """Only interventions are logged with screen content: they are the
        moments the user's answer turns into a label (`harvest`)."""
        self._log({
            "type": "intervention", "id": d.id, "rule": d.rule, "reason": d.reason, "screen": screen,
            "page_kind": reading.page_kind, "purpose": reading.purpose,
            "p_hit": {v.rule_id: round(v.p_hit, 4) for v in reading.rules},
        })

    def log_response(self, decision_id: str, response: str, rule_id: str, **extra) -> None:
        self._log({"type": "response", "id": decision_id, "rule": rule_id, "response": response, **extra})

    def _log(self, event: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        event = {"at": datetime.now().isoformat(timespec="seconds"), **event}
        with (self.data_dir / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
