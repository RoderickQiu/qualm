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
WORK_MIN = 0.6  # P(page_kind = work) needed to treat the screen as a work tool
MAX_TICK_S = 5.0  # longer gaps (sleep, a stalled model call) don't count as usage
MAX_EXCEPTIONS = 10  # per rule; the newest "Not this one" titles the model reads
OWN_URLS = ("http://127.0.0.1:8765",)  # the review page: never judge SeeNot itself


def host_of(url: str) -> str:
    """example.com for https://www.example.com/a; "" for non-web URLs."""
    m = re.match(r"https?://([^/:?#]+)", url or "")
    return m.group(1).lower().removeprefix("www.") if m else ""


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


class Policy:
    def __init__(self, settings: Settings, rules: list[Rule], data_dir: str | Path = "data"):
        self.settings = settings
        self.data_dir = Path(data_dir)
        self.usage = Usage(self.data_dir / "usage.json")
        self.lock = threading.RLock()
        self.snoozed: dict[str, float] = {}  # rule id -> wall time the snooze ends
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
        return [
            replace(r, exceptions=r.exceptions + tuple(self._titles.get(r.id, [])[-MAX_EXCEPTIONS:]))
            for r in self._base_rules
        ]

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
        if e.get("url"):
            self._allowed.setdefault(e["rule"], set()).add(e["url"])
        if e.get("title"):
            self._titles.setdefault(e["rule"], []).append(f'the page "{e["title"]}"')
        if e.get("text"):  # typed on the review page
            self._titles.setdefault(e["rule"], []).append(e["text"])

    # -- decisions -----------------------------------------------------------

    def precheck(self, bundle_id: str, url: str) -> Decision | None:
        """Decisions that need no model call. None means: ask the model."""
        with self.lock:
            if time.time() < self.paused_until:
                d = Decision("skip", reason="paused")
            elif bundle_id in self.settings.no_monitor:
                d = Decision("skip", reason="app not monitored")
            elif url.startswith(OWN_URLS):
                d = Decision("skip", reason="SeeNot's own page")
            elif url and any(re.search(p, url) for p in self.settings.allow_urls):
                d = Decision("allow", reason="allowed URL")
            elif bundle_id in self._never_apps or host_of(url) in self._never_hosts:
                d = Decision("allow", reason="you said never here")
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
            # An app that shows nothing but its name (WhatsApp, games, players)
            # leaves the model guessing; only a URL pattern may fire then.
            thin = not url and not state.get("headings") and not state.get("visible_text")
            # A kind of page you said is never flagged ([[allow]]: shopping, ...).
            allowed_as = next((c.id for c in self.settings.allow if reading.allow.get(c.id, 0) >= c.threshold), None)
            work = reading.page_kind == "work" and reading.page_probs.get("work", 0) >= WORK_MIN
            learning = reading.page_kind != "feed" and reading.purpose_probs.get("learn", 0) >= LEARN_MIN
            out, counted, now = [], set(), time.time()
            for rule in self._base_rules:
                v = reading.verdict(rule.id)
                pattern = rule.matches_url(url)
                p = v.p_hit if v else 0.0
                feed = rule.feed_hit and reading.page_kind == "feed" and reading.purpose == "entertain"
                if not (pattern or feed or p >= rule.threshold):
                    continue
                # A feed of candidates doesn't break "don't show me X"; scrolling
                # it still counts toward a time budget.
                if rule.kind == "deny" and rule.target == "content" and reading.page_kind == "feed" and not pattern:
                    continue
                if thin and not pattern:
                    out.append(Decision("skip", rule.id, f"too little on screen to judge (p_hit {p:.2f})"))
                    continue
                why = ("matches URL pattern" if pattern else f"p_hit {p:.2f} >= {rule.threshold:.2f}" if p >= rule.threshold
                       else "an entertainment feed")
                if allowed_as and not pattern:
                    out.append(Decision("allow", rule.id, f"{allowed_as} is never flagged"))
                elif url and url in self._allowed.get(rule.id, ()):
                    out.append(Decision("allow", rule.id, "you marked this page fine"))
                elif self.snoozed.get(rule.id, 0) > now:
                    until = datetime.fromtimestamp(self.snoozed[rule.id]).strftime("%H:%M")
                    out.append(Decision("allow", rule.id, f"snoozed until {until}"))
                elif work and not pattern:
                    out.append(Decision("allow", rule.id, "work tool"))
                elif rule.allow_learning and learning:
                    out.append(Decision("allow", rule.id, "learning material"))
                elif rule.allow_intentional and self._cur["intentional"]:
                    out.append(Decision("allow", rule.id, "opened on purpose"))
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

    def tick(self, now: float | None = None) -> None:
        """Call on every loop: adds the time since the last tick to the rules
        the current screen counts toward."""
        now = time.time() if now is None else now
        with self.lock:
            if self._last_tick is not None:
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
        self.log_response(decision_id, "snooze", rule_id, reason=reason, minutes=minutes)

    def mark_fine(self, rule_id: str, url: str, title: str, decision_id: str = "") -> None:
        e = {"rule": rule_id, "url": url, "title": title, "at": datetime.now().isoformat(timespec="seconds")}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with (self.data_dir / "exceptions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        with self.lock:
            self._add_exception(e)
        self.log_response(decision_id, "fine", rule_id)

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
            for r in self._base_rules:
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
        """Every judgement, to data/judgements.jsonl, for `seenot-desktop review`:
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
                "thresholds": {r.id: r.threshold for r in self._base_rules},
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
