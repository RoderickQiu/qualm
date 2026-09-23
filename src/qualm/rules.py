"""Qualm rules, and the typed questions they become.

The Android ScreenAnalyzer prompt asks one model call for everything in
prose: page type, sensitivity, a decision per constraint, and a 0-100
confidence. Here each of those is its own typed question, and the logic that
combines them lives in `policy.py`, not in the prompt. docs/POLICY.md says
what the rules are for.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from typesafe_sdk import Choice, Noul

# Mirrors ConstraintType in seenot-variant: DENY and TIME_CAP. NO_MONITOR is
# [settings] no_monitor: those apps are never read.
Kind = Literal["deny", "time_cap"]
# "content": judge the opened item; a feed of candidates never hits.
# "page": judge the page itself, so feeds and hot lists can hit.
Target = Literal["content", "page"]

DENY_OPTIONS = ("violates", "safe", "unknown")
TIME_CAP_OPTIONS = ("in_scope", "out_of_scope", "unknown")
HIT_LABELS = ("violates", "in_scope")
PAGE_KINDS = ("feed", "single_item", "search", "work", "other")
PURPOSES = ("learn", "task", "entertain")
# "rule" asks whether the screen breaks the user's rule; "direct" asks what
# the screen is. See rule_question(). Trials: "rule" is better on Kev-4B.
QUESTION_STYLE = os.environ.get("QUALM_QUESTION_STYLE", "rule")


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_WORDS = {"daily": DAYS, "weekdays": DAYS[:5], "weekends": DAYS[5:]}


def site_pattern(site: str) -> str:
    """A site as people write it -> a URL regex. "douyin.com" is the site and
    its subdomains; "youtube.com/shorts" is that path and everything under
    it; "youtube.com/" (just a slash) is the home page only."""
    s = re.sub(r"^[a-z]+://", "", site.strip().lower()).removeprefix("www.")
    host, slash, path = s.partition("/")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
        raise ValueError(f"site {site!r}: write a domain, like douyin.com or youtube.com/shorts")
    h = re.escape(host)
    if slash and not path:
        return rf"^https?://(www\.)?{h}/?([?#]|$)"
    return rf"^https?://([^/?#]*\.)?{h}" + (re.escape("/" + path.rstrip("/")) if path else "") + r"([/?#:]|$)"


def _parse_when(spec: str) -> tuple[frozenset[int], int, int]:
    """ "mon-fri 09:00-18:00", "weekends", "22:00-02:00", "mon,wed 12:00-13:00"
    -> (days, start minute, end minute). No days: every day; no hours: all day."""
    days, start, end = set(), 0, 24 * 60
    for tok in spec.lower().replace(", ", ",").split():
        if m := re.fullmatch(r"(\d{1,2}):(\d\d)-(\d{1,2}):(\d\d)", tok):
            h1, m1, h2, m2 = map(int, m.groups())
            if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
                raise ValueError(f"when {spec!r}: bad time")
            start, end = h1 * 60 + m1, h2 * 60 + m2
            continue
        for part in tok.split(","):
            if part in DAY_WORDS:
                days |= {DAYS.index(d) for d in DAY_WORDS[part]}
            elif m := re.fullmatch(r"([a-z]{3})-([a-z]{3})", part):
                a, b = (DAYS.index(x) if x in DAYS else -1 for x in m.groups())
                if a < 0 or b < 0:
                    raise ValueError(f"when {spec!r}: days are {', '.join(DAYS)}")
                days |= {(a + i) % 7 for i in range((b - a) % 7 + 1)}
            elif part in DAYS:
                days.add(DAYS.index(part))
            else:
                raise ValueError(f"when {spec!r}: can't read {part!r}; e.g. \"mon-fri 09:00-18:00\" or \"weekends\"")
    return frozenset(days or range(7)), start, end


def in_window(specs: tuple[str, ...], now: datetime) -> bool:
    """True if `now` falls in any of the windows; no windows means always.
    A window past midnight ("fri 22:00-02:00") belongs to the day it starts."""
    if not specs:
        return True
    day, minute = now.weekday(), now.hour * 60 + now.minute
    for spec in specs:
        days, start, end = _parse_when(spec)
        if start < end and day in days and start <= minute < end:
            return True
        if start >= end and (day in days and minute >= start or (day - 1) % 7 in days and minute < end):
            return True
    return False


@dataclass
class Rule:
    id: str
    kind: Kind
    description: str  # what the rule is about, in your words; the model reads it
    description_en: str = ""  # optional English version; `lang = "en"` sends it instead
    exceptions: tuple[str, ...] = ()  # repair rules: typed, or added by "Not this one"
    # p_hit at or above this is a hit. Kev-4B ranks well but its p_hit runs
    # low: tuned thresholds are 0.15-0.5. `rules tune` sets it from your answers.
    threshold: float = 0.2
    target: Target = "content"
    sites: tuple[str, ...] = ()  # "douyin.com", "youtube.com/shorts": hit without the model's say
    patterns: tuple[str, ...] = ()  # the same as URL regexes, for what sites can't say
    allow_learning: bool = False  # lectures, tutorials, docs never hit this rule
    allow_intentional: bool = False  # one item opened from search or a work app is exempt
    # Any page the model reads as a feed for entertainment hits this rule too,
    # on sites the rule never names. In the trials it lifted feed recall from
    # 0.40 to 0.67 with no false positives.
    feed_hit: bool = False
    minutes_per_day: float = 0  # time_cap: daily budget
    visits_per_day: int = 0  # time_cap: 0 = no visit limit
    enabled: bool = True  # off: never asked, never fires
    when: tuple[str, ...] = ()  # "mon-fri 09:00-18:00"; empty = always
    note: str = ""  # for you (or an agent) reading the file: why it's set this way

    def text(self, lang: str) -> str:
        return self.description_en if lang == "en" and self.description_en else self.description

    def text_field(self, lang: str) -> str:
        """The field the model reads: what `rules set ID what=...` edits."""
        return "description_en" if lang == "en" and self.description_en else "description"

    def matches_url(self, url: str) -> bool:
        return bool(url) and any(re.search(p, url) for p in (*map(site_pattern, self.sites), *self.patterns))

    def active(self, now: datetime | None = None) -> bool:
        return self.enabled and in_window(self.when, now or datetime.now())


@dataclass
class AllowClass:
    """A kind of page no rule may fire on, in your words ("an online store").
    Each is one yes/no question per reading."""

    id: str
    description: str
    description_en: str = ""
    threshold: float = 0.5  # P(yes) at or above this: the page is this class
    sites: tuple[str, ...] = ()  # known to be this class: allowed without the model
    patterns: tuple[str, ...] = ()  # the same as URL regexes
    apps: tuple[str, ...] = ()  # bundle ids known to be this class
    enabled: bool = True
    note: str = ""

    def text(self, lang: str) -> str:
        return self.description_en if lang == "en" and self.description_en else self.description

    def text_field(self, lang: str) -> str:
        return "description_en" if lang == "en" and self.description_en else "description"

    def matches(self, bundle_id: str, url: str) -> bool:
        return bundle_id in self.apps or bool(url) and any(
            re.search(p, url) for p in (*map(site_pattern, self.sites), *self.patterns))


@dataclass
class Settings:
    lang: str = "en"  # "en": rules with a description_en send that instead of description
    no_monitor: tuple[str, ...] = ()  # bundle ids that are never read at all
    allow_sites: tuple[str, ...] = ()  # sites that are never judged ("github.com")
    allow_urls: tuple[str, ...] = ()  # the same as URL regexes
    # False: a time_cap hit steps in at once, like a deny rule, instead of
    # counting minutes and visits. Clearer while testing.
    budgets: bool = True
    # Questions one reading may ask: rules on at the same time + allow classes
    # + the 3 shared ones. Kev-4B on a 24 GB Mac: 13 questions 0.6 s, 28 1.8 s,
    # 53 4.5-18 s, 103 timed out and swapped the machine (HANDOFF, Measured).
    max_questions: int = 25
    allow: tuple[AllowClass, ...] = ()  # [[allow]]: kinds of page never flagged

    def allowed_url(self, url: str) -> bool:
        return bool(url) and any(re.search(p, url) for p in (*map(site_pattern, self.allow_sites), *self.allow_urls))


RULE_FIELDS = set(Rule.__dataclass_fields__)
ALLOW_FIELDS = set(AllowClass.__dataclass_fields__)
SETTINGS_FIELDS = set(Settings.__dataclass_fields__) - {"allow"}
ID_RE = r"[a-z][a-z0-9_]*"


def _make(cls, fields: set[str], table: str, raw: dict):
    """One [[rules]] or [[allow]] entry, checked, with a message that says what to fix."""
    rid = raw.get("id")
    if not isinstance(rid, str) or not re.fullmatch(ID_RE, rid):
        raise ValueError(f"[[{table}]] id {rid!r}: lowercase letters, digits and _, starting with a letter")
    unknown = set(raw) - fields
    if unknown:
        raise ValueError(f"{table} {rid!r}: unknown keys {sorted(unknown)}; known: {sorted(fields)}")
    if not raw.get("description"):
        raise ValueError(f"{table} {rid!r}: needs a description (what it's about, in your words)")
    obj = cls(**{k: tuple(v) if isinstance(v, list) else v for k, v in raw.items()})
    if not 0 < obj.threshold < 1:
        raise ValueError(f"{table} {rid!r}: threshold must be between 0 and 1")
    for p in obj.patterns:
        re.compile(p)
    for site in obj.sites:
        site_pattern(site)
    return obj


def parse_config(text: str) -> tuple[Settings, list[Rule]]:
    data = tomllib.loads(text)
    s = data.get("settings", {})
    unknown = set(s) - SETTINGS_FIELDS
    if unknown:
        raise ValueError(f"[settings]: unknown keys {sorted(unknown)}; known: {sorted(SETTINGS_FIELDS)}")
    settings = Settings(
        lang=s.get("lang", "en"),
        no_monitor=tuple(s.get("no_monitor", ())),
        allow_sites=tuple(s.get("allow_sites", ())),
        allow_urls=tuple(s.get("allow_urls", ())),
        budgets=bool(s.get("budgets", True)),
        max_questions=int(s.get("max_questions", Settings.max_questions)),
        allow=tuple(_make(AllowClass, ALLOW_FIELDS, "allow", a) for a in data.get("allow", ())),
    )
    for site in settings.allow_sites:
        site_pattern(site)
    rules = [_make(Rule, RULE_FIELDS, "rules", r) for r in data.get("rules", ())]
    for r in rules:
        if r.kind not in ("deny", "time_cap"):
            raise ValueError(f"rule {r.id!r}: kind is deny (step in) or time_cap (count, step in over budget)")
        if r.target not in ("content", "page"):
            raise ValueError(f"rule {r.id!r}: target is content or page")
        for w in r.when:
            _parse_when(w)
    ids = [r.id for r in rules] + [c.id for c in settings.allow]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate ids: {sorted({i for i in ids if ids.count(i) > 1})}")
    cap = capacity(settings, rules)
    if cap["peak"] > settings.max_questions:
        raise OverLimit(
            f"{cap['peak']} questions per reading at {cap['peak_at']}, over the limit of {settings.max_questions} "
            f"({cap['peak_rules']} rules on at once + {cap['allow']} allow classes + {cap['shared']} shared). "
            "Past the limit the model slows sharply. Switch rules off, give them hours (`when`) that don't "
            "overlap, or fold two rules into one description.")
    return settings, rules


class OverLimit(ValueError):
    """Too many questions per reading: see Settings.max_questions."""


SHARED = 3  # sensitive, page_kind, purpose: asked on every reading


def capacity(settings: Settings, rules: list[Rule], now: datetime | None = None) -> dict:
    """Questions per reading now and at the busiest moment of the week. Rules
    only switch on at a window's start, so those moments (and midnight) are
    the only ones to check."""
    allow = sum(c.enabled for c in settings.allow)
    on = [r for r in rules if r.enabled]
    starts = {0}
    for r in on:
        for w in r.when:
            starts.add(_parse_when(w)[1])
    monday = datetime(2024, 1, 1)  # any Monday
    peak, peak_at = -1, ""
    for day in range(7):
        for m in sorted(starts):
            t = monday.replace(day=1 + day, hour=m // 60 % 24, minute=m % 60)
            n = sum(in_window(r.when, t) for r in on)
            if n > peak:
                peak, peak_at = n, "any time" if all(not r.when for r in on) else f"{DAYS[day]} {m // 60:02d}:{m % 60:02d}"
    now_n = sum(r.active(now) for r in on)
    return {"limit": settings.max_questions, "now": SHARED + allow + now_n, "rules_now": now_n,
            "peak": SHARED + allow + peak, "peak_rules": peak, "peak_at": peak_at, "allow": allow, "shared": SHARED}


def load_config(path: str | Path) -> tuple[Settings, list[Rule]]:
    return parse_config(Path(path).read_text(encoding="utf-8"))


def load_rules(path: str | Path) -> list[Rule]:
    return load_config(path)[1]


SENSITIVE = Noul(
    instructions=(
        "The screen is asking the user to pay, log in, sign up, enter a verification code or password, "
        "confirm identity, or it shows bank cards, balances, transactions or saved passwords. "
        "A shopping cart, product page or checkout button alone does not count."
    )
)

PAGE_KIND = Choice(
    instructions="What kind of page is in front of the user right now?",
    criteria={
        "feed": "A home page, feed, recommendation list or grid of many candidate items the user has not opened",
        "single_item": "One opened item: an article, video, post, product, thread or chat",
        "search": "Search results or a search being typed",
        "work": "An editor, document, terminal, IDE, spreadsheet, design tool or other work tool",
        "other": "Anything else: settings, empty window, system dialog",
    },
)

PURPOSE = Choice(
    instructions="What is the content on screen for?",
    criteria={
        "learn": "Learning: a lecture, course, tutorial, documentation, paper, how-to or explainer",
        "task": "Getting something specific done: work, looking something up, shopping, booking, "
        "a message to or from a specific person, news the user went looking for",
        "entertain": "Entertainment or passing time: comedy, clips, memes, gossip, games, streams, trending lists, feeds",
    },
)


def rule_question(rule: Rule, lang: str = "zh") -> Choice:
    exceptions = ""
    if rule.exceptions:
        exceptions = " Exceptions the user has confirmed are fine: " + "; ".join(rule.exceptions) + "."
    judge = (
        "Judge the page as a whole, including feeds and lists."
        if rule.target == "page"
        else "Judge only the content currently open, not candidates listed in a feed."
    )
    what = rule.text(lang)
    if QUESTION_STYLE == "direct":
        # Ask what the screen is, not whether it breaks a prohibition.
        # Same answer keys, so the policy is unchanged.
        if rule.kind == "deny":
            return Choice(
                instructions=f"Is the content currently open {what}?{exceptions} {judge}",
                criteria={
                    "violates": f"Yes: the open content is {what}",
                    "safe": "No: the open content is something else",
                    "unknown": "The screen does not show enough to tell",
                },
            )
        return Choice(
            instructions=f"Is the user currently {what}?{exceptions}",
            criteria={
                "in_scope": f"Yes: the current screen is {what}",
                "out_of_scope": "No: the current screen is something else",
                "unknown": "The screen does not show enough to tell",
            },
        )
    if rule.kind == "deny":
        return Choice(
            instructions=f"The user set this rule: do not show me {what}.{exceptions} {judge}",
            criteria={
                "violates": "The open content is what the rule forbids",
                "safe": "The open content is not what the rule forbids",
                "unknown": "The screen does not show enough to tell",
            },
        )
    return Choice(
        instructions=f"The user set a time limit on: {what}.{exceptions} "
        "Does the current screen count toward that limit?",
        criteria={
            "in_scope": "The current screen is the limited activity",
            "out_of_scope": "The current screen is something else",
            "unknown": "The screen does not show enough to tell",
        },
    )


def allow_question(c: AllowClass, lang: str = "zh") -> Noul:
    return Noul(instructions=f"The screen shows {c.text(lang)}.")


def build_questions(rules: list[Rule], lang: str = "zh", allow: tuple[AllowClass, ...] = ()) -> dict:
    questions = {"sensitive": SENSITIVE, "page_kind": PAGE_KIND, "purpose": PURPOSE}
    for c in allow:
        if not c.enabled:
            continue
        questions[f"allow_{c.id}"] = allow_question(c, lang)
    for rule in rules:
        questions[f"rule_{rule.id}"] = rule_question(rule, lang)
    return questions
