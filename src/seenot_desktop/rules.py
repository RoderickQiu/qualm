"""SeeNot rules, and the typed questions they become.

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
QUESTION_STYLE = os.environ.get("SEENOT_QUESTION_STYLE", "rule")


@dataclass
class Rule:
    id: str
    kind: Kind
    description: str  # as the user wrote it, usually Chinese
    description_en: str = ""  # optional English version; `lang = "en"` sends it
    exceptions: tuple[str, ...] = ()  # repair rules: typed, or added by "Not this one"
    # p_hit at or above this is a hit. Set it from `eval --suggest`: Kev-4B
    # ranks well but its p_hit runs low (0.06-0.20 in the trials).
    threshold: float = 0.5
    target: Target = "content"
    patterns: tuple[str, ...] = ()  # URL regexes that hit without the model's say
    allow_learning: bool = False  # lectures, tutorials, docs never hit this rule
    allow_intentional: bool = False  # one item opened from search or a work app is exempt
    # Any page the model reads as a feed for entertainment hits this rule too,
    # on sites the rule never names. In the trials it lifted feed recall from
    # 0.40 to 0.67 with no false positives.
    feed_hit: bool = False
    minutes_per_day: float = 0  # time_cap: daily budget
    visits_per_day: int = 0  # time_cap: 0 = no visit limit

    def text(self, lang: str) -> str:
        return self.description_en if lang == "en" and self.description_en else self.description

    def matches_url(self, url: str) -> bool:
        return bool(url) and any(re.search(p, url) for p in self.patterns)


@dataclass
class AllowClass:
    """A kind of page no rule may fire on, in your words ("an online store").
    Each is one yes/no question per reading."""

    id: str
    description: str
    description_en: str = ""
    threshold: float = 0.5  # P(yes) at or above this: the page is this class

    def text(self, lang: str) -> str:
        return self.description_en if lang == "en" and self.description_en else self.description


@dataclass
class Settings:
    lang: str = "en"  # which rule description the model reads
    no_monitor: tuple[str, ...] = ()  # bundle ids that are never read at all
    allow_urls: tuple[str, ...] = ()  # URL regexes that are never judged
    # False: a time_cap hit steps in at once, like a deny rule, instead of
    # counting minutes and visits. Clearer while testing.
    budgets: bool = True
    allow: tuple[AllowClass, ...] = ()  # [[allow]]: kinds of page never flagged


RULE_FIELDS = set(Rule.__dataclass_fields__)


def load_config(path: str | Path) -> tuple[Settings, list[Rule]]:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    s = data.get("settings", {})
    settings = Settings(
        lang=s.get("lang", "en"),
        no_monitor=tuple(s.get("no_monitor", ())),
        allow_urls=tuple(s.get("allow_urls", ())),
        budgets=bool(s.get("budgets", True)),
        allow=tuple(AllowClass(**{**a, "id": str(a["id"])}) for a in data.get("allow", ())),
    )
    rules = []
    for r in data["rules"]:
        unknown = set(r) - RULE_FIELDS
        if unknown:
            raise ValueError(f"rule {r.get('id')!r}: unknown keys {sorted(unknown)}")
        r = {**r, "id": str(r["id"])}
        for key in ("exceptions", "patterns"):
            r[key] = tuple(r.get(key, ()))
        rules.append(Rule(**r))
    return settings, rules


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
        questions[f"allow_{c.id}"] = allow_question(c, lang)
    for rule in rules:
        questions[f"rule_{rule.id}"] = rule_question(rule, lang)
    return questions
