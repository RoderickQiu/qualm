"""Why Qualm stepped in, in words.

Kev answers with probabilities only; it can't say why. So the explanation
is built from what Qualm does know: which signal fired (a URL pattern,
the score, an entertainment feed), how far past the threshold
the score was, what the model took the page to be, and, asked once per
pop-up, which part of the screen carried the signal: the model is asked
again with only the title and address, and with only the page text. In
the spirit of Time2Stop's feature attributions (CHI 2024), where
explanations raised acceptance of interventions.

The words follow what the research on these pop-ups found: say what the
page looks like and why, plainly; the option to back out does the work,
not a lecture. Nothing here counts against you ("again?", "wasted"): the
context line is neutral, and the time you asked for is echoed back when
it's up.

The headline rotates between a few wordings, one per pop-up, and says so
(a tooltip; README): a fixed message wears off, rotating ones hold up if
people know they rotate (Kovacs et al., CSCW 2018). Your own words in a
focus session never rotate.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from .decide import Reading, ask
from .policy import Decision
from .rules import Rule

PAGE_WORDS = {"feed": "a feed of recommendations", "single_item": "a single page or item", "search": "search results",
              "work": "a work tool", "other": "a page"}
PURPOSE_WORDS = {"learn": "learning", "task": "getting something done", "entertain": "entertainment"}


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def cut(text: str, n: int) -> str:
    """At most `n` characters wide (a CJK character is two): cut at a word
    where one ends in the second half of that width, else mid-word (CJK has
    no spaces), with an ellipsis."""
    text = " ".join(text.split())
    widths = [2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text]
    if sum(widths) <= n:
        return text
    end, room = 0, n - 1
    while widths[end] <= room:
        room -= widths[end]
        end += 1
    head = text[:end]
    space = head.rfind(" ")
    # Mid-word: back to the last space, unless that drops more than half the width
    # ("You're here to: 准备明天…" would keep only "You're here to").
    if text[end] != " " and space > 0 and 2 * sum(widths[:space]) >= sum(widths[:end]):
        head = head[:space]
    return head.rstrip(" ,.;:!?，。；：、") + "…"


def sentence(text: str) -> str:
    """Text ending in one full stop, whatever it ended with."""
    text = text.rstrip(" ,;:，；：、")
    return text if text.endswith(("…", ".", "?", "!", "。", "？", "！")) else text + "."


def name(rule: Rule) -> str:
    """The rule as the menu names it: "Short videos", "Online shopping"; never the raw id."""
    from .setup import rule_name

    return rule_name(rule.id, rule.description)


def label(rule: Rule, lang: str = "en") -> str:
    """The rule's description cut to a phrase: "short videos made for endless
    swiping, such as Douyin, ..." -> "short videos made for endless swiping"."""
    what = rule.text(lang)
    head = re.split(r",? such as |[,:;，：；(（]", what, maxsplit=1)[0].strip().rstrip(".。!！")
    return cut(head, 90) or name(rule)


def starter(rule: Rule) -> bool:
    """One of the starter rules, whose descriptions are noun phrases written
    for "This looks like …"; a rule you wrote may be any sentence."""
    from .setup import RULE_LOOK

    return rule.id in RULE_LOOK


DENY_WORDS = ("This looks like {}", "Qualm reads this as {}", "A second look: this seems to be {}")
# Your own rules are quoted, not fitted into the sentence: "news sites and
# headlines" or "YouTube after 10pm" don't read as what a page "looks like".
YOURS_WORDS = ("This looks like it's under your rule: {}", "Qualm reads this as a case of your rule: {}",
               "A second look: this seems to fall under your rule: {}")
FEED_WORDS = ("This is a feed, picked for you.", "A feed of recommendations, chosen by the site.",
              "Nothing here is your pick yet: it's a feed.")


def looks_like(rule: Rule, lang: str, n: int = 0) -> str:
    """"This looks like …" for the rule, in one of the rotating wordings."""
    words = DENY_WORDS if starter(rule) else YOURS_WORDS
    return sentence(words[n % len(words)].format(label(rule, lang)))


def headline(d: Decision, rule: Rule, lang: str, focus: dict | None = None, n: int = 0) -> tuple[str, str]:
    """(eyebrow, headline) for the pop-up; `n`: this rule's pop-ups earlier today, to rotate the wording."""
    called = name(rule)
    if focus is not None:
        left = max(1, round((focus["until"] - datetime.now().timestamp()) / 60))
        return f"Focus · {left} min left", sentence(f"You're here to: {cut(focus['intent'], 110)}")
    if d.panel == "check_in":
        return f"{called} · check in", "What are you here for?"
    if d.panel == "times_up":
        return f"{called} · time's up", d.reason[0].upper() + d.reason[1:] + "."
    if d.reason == "an entertainment feed":
        return called, FEED_WORDS[n % len(FEED_WORDS)]
    return called, looks_like(rule, lang, n)


def reason(d: Decision, reading: Reading | None, rule: Rule, lang: str) -> str:
    """One or two sentences on why, without another model call."""
    if d.panel == "times_up":
        return "Done takes you back."
    if d.panel == "check_in":
        return f"{looks_like(rule, lang)} Say what for and how long, and Qualm stays out of the way until then."
    if d.reason == "matches URL pattern":
        head = f"This address is on your “{name(rule)}” list."
    elif d.reason == "the app is on this rule's list":
        head = f"This app is on your “{name(rule)}” list."
    elif d.reason == "an entertainment feed":
        head = "Nothing on it was your choice yet: it's a feed of recommendations, for entertainment."
        return head
    elif reading is not None and (v := reading.verdict(rule.id)) is not None:
        ratio = v.p_hit / rule.threshold if rule.threshold else 1.0
        sure = "A clear match" if ratio >= 3 else "A likely match" if ratio >= 1.5 else "A close call"
        head = f"{sure} for your “{name(rule)}” rule."
    else:
        head = f"It matches your “{name(rule)}” rule."
    if reading is None:
        return head
    seen = f"Qualm read the screen as {PAGE_WORDS.get(reading.page_kind, 'a page')}, for {PURPOSE_WORDS.get(reading.purpose, 'something')}."
    return f"{head} {seen}"


def context(shown_today: list[str], snooze: tuple[float, float, str] | None = None) -> str:
    """A neutral line: how often today, and the time you asked for, echoed
    back once it's up. `shown_today` are earlier pop-ups' ISO times;
    `snooze` is (ended at, minutes, what for) for this rule, if any."""
    parts = []
    now = datetime.now().timestamp()
    if snooze and snooze[2] and 0 <= now - snooze[0] < 30 * 60:
        parts.append(f"Your {snooze[1]:g} minutes for “{cut(snooze[2], 60)}” are up")
    n = len(shown_today) + 1
    if n > 1:
        parts.append(f"{ordinal(n)} time today · last at {shown_today[-1][11:16]}")
    return " · ".join(parts)


def session_context(n_today: int, minutes_today: float, last_end: float = 0.0) -> str:
    """The check-in's neutral line: "3rd time today · 52 min so far · last ended 14:20"."""
    if not n_today:
        return ""
    parts = [f"{ordinal(n_today + 1)} time today", f"{minutes_today:.0f} min so far"]
    if last_end and datetime.fromtimestamp(last_end).date() == datetime.now().date():
        parts.append(f"last ended {datetime.fromtimestamp(last_end):%H:%M}")
    return " · ".join(parts)


def evidence(client, state: dict, rule: Rule, lang: str) -> str:
    """Which part of the screen carried the signal. Two model calls, one
    question each; returns "" when it can't tell."""
    head = {k: state[k] for k in ("app", "window_title", "url") if k in state}
    body = {k: state[k] for k in ("app", "headings", "visible_text") if k in state}
    if not (state.get("headings") or state.get("visible_text")):
        return ""
    p_head = ask(client, head, [rule], lang).verdict(rule.id).p_hit
    p_body = ask(client, body, [rule], lang).verdict(rule.id).p_hit
    t = rule.threshold
    title = cut(state.get("window_title") or state.get("url") or "", 70)
    snippet = cut(next(iter(state.get("headings") or state.get("visible_text") or []), ""), 70)
    if p_head >= t and p_body < t:
        return f'The title and address alone are enough: "{title}".'
    if p_body >= t and p_head < t:
        return f'It comes from the text on the page, e.g. "{snippet}".'
    if p_head >= t and p_body >= t:
        return "Both the title and the text on the page point this way."
    return "Neither the title nor the page text alone is enough; it's the two together."
