"""SeeNot rules, and the typed questions they become.

The Android ScreenAnalyzer prompt asks one model call for everything in
prose: page type, sensitivity, a decision per constraint, and a 0-100
confidence. Here each of those is its own typed question, and the logic that
combines them lives in `decide.py`, not in the prompt.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from typesafe_sdk import Choice, Noul

# Mirrors ConstraintType in seenot-variant: DENY and TIME_CAP. NO_MONITOR
# needs no question -- it just skips the app.
Kind = Literal["deny", "time_cap"]

DENY_OPTIONS = ("violates", "safe", "unknown")
TIME_CAP_OPTIONS = ("in_scope", "out_of_scope", "unknown")
PAGE_KINDS = ("feed", "single_item", "search", "work", "other")


@dataclass
class Rule:
    id: str
    kind: Kind
    description: str  # as the user wrote it, usually Chinese
    description_en: str = ""  # optional English version, for the language test
    exceptions: tuple[str, ...] = ()  # repair rules from false positives

    def text(self, lang: str) -> str:
        return self.description_en if lang == "en" and self.description_en else self.description


def load_rules(path: str | Path) -> list[Rule]:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Rule(
            id=str(r["id"]),
            kind=r["kind"],
            description=r["description"],
            description_en=r.get("description_en", ""),
            exceptions=tuple(r.get("exceptions", ())),
        )
        for r in data["rules"]
    ]


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


def rule_question(rule: Rule, lang: str = "zh") -> Choice:
    exceptions = ""
    if rule.exceptions:
        exceptions = " Exceptions the user has confirmed are fine: " + "; ".join(rule.exceptions) + "."
    if rule.kind == "deny":
        return Choice(
            instructions=f"The user set this rule: do not show me {rule.text(lang)}.{exceptions} "
            "Judge only the content currently open, not candidates listed in a feed.",
            criteria={
                "violates": "The open content is what the rule forbids",
                "safe": "The open content is not what the rule forbids",
                "unknown": "The screen does not show enough to tell",
            },
        )
    return Choice(
        instructions=f"The user set a time limit on: {rule.text(lang)}.{exceptions} "
        "Does the current screen count toward that limit?",
        criteria={
            "in_scope": "The current screen is the limited activity",
            "out_of_scope": "The current screen is something else",
            "unknown": "The screen does not show enough to tell",
        },
    )


def build_questions(rules: list[Rule], lang: str = "zh") -> dict:
    questions = {"sensitive": SENSITIVE, "page_kind": PAGE_KIND}
    for rule in rules:
        questions[f"rule_{rule.id}"] = rule_question(rule, lang)
    return questions
