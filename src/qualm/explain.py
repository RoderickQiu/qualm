"""Why Qualm stepped in, in words.

Kev answers with probabilities only; it can't say why. So the explanation
is built from what Qualm does know: which signal fired (a URL pattern,
the score, an entertainment feed, a budget), how far past the threshold
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
"""

from __future__ import annotations

import re
from datetime import datetime

from .decide import Reading, ask
from .policy import Decision
from .rules import Rule

PAGE_WORDS = {"feed": "a feed of recommendations", "single_item": "a single page or item", "search": "search results",
              "work": "a work tool", "other": "a page"}
PURPOSE_WORDS = {"learn": "learning", "task": "getting something done", "entertain": "entertainment"}


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def label(rule: Rule, lang: str = "en") -> str:
    """The rule's description cut to a phrase: "short videos made for endless
    swiping, such as Douyin, ..." -> "short videos made for endless swiping"."""
    what = rule.text(lang)
    head = re.split(r",? such as |[,:;，：；(（]", what, maxsplit=1)[0].strip()
    return head or rule.id


def headline(d: Decision, rule: Rule, lang: str, focus: dict | None = None) -> tuple[str, str]:
    """(eyebrow, headline) for the pop-up."""
    name = rule.id.replace("_", " ")
    if focus is not None:
        left = max(1, round((focus["until"] - datetime.now().timestamp()) / 60))
        return f"Focus · {left} min left", f"You're here to: {focus['intent']}."
    if "min today" in d.reason or "visit" in d.reason:
        m = re.search(r"of (\d+(?:\.\d+)?) min today", d.reason)  # the budget's reason names only what ran out
        if m:
            return f"{name} · daily limit", f"That's today's {m.group(1)} minutes of {label(rule, lang)}."
        return f"{name} · daily limit", f"That's today's visits for {label(rule, lang)}."
    if d.reason == "an entertainment feed":
        return name, "This is a feed, picked for you."
    return name, f"This looks like {label(rule, lang)}."


def reason(d: Decision, reading: Reading | None, rule: Rule, lang: str) -> str:
    """One or two sentences on why, without another model call."""
    if d.reason == "matches URL pattern":
        head = f"This address is on your list for {rule.id}."
    elif d.reason == "an entertainment feed":
        head = "Nothing on it was your choice yet: it's a feed of recommendations, for entertainment."
        return head
    elif "min today" in d.reason or "visit" in d.reason:
        head = f"Your {rule.id} budget: {d.reason}."
    elif reading is not None and (v := reading.verdict(rule.id)) is not None:
        ratio = v.p_hit / rule.threshold if rule.threshold else 1.0
        sure = "A clear match" if ratio >= 3 else "A likely match" if ratio >= 1.5 else "A close call"
        head = f"{sure} for your {rule.id} rule."
    else:
        head = f"It matches your {rule.id} rule."
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
        parts.append(f"Your {snooze[1]:g} minutes for “{snooze[2]}” are up")
    n = len(shown_today) + 1
    if n > 1:
        parts.append(f"{ordinal(n)} time today · last at {shown_today[-1][11:16]}")
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
    title = (state.get("window_title") or state.get("url") or "")[:70]
    snippet = next(iter(state.get("headings") or state.get("visible_text") or []), "")[:70]
    if p_head >= t and p_body < t:
        return f'The title and address alone are enough: "{title}".'
    if p_body >= t and p_head < t:
        return f'It comes from the text on the page, e.g. "{snippet}".'
    if p_head >= t and p_body >= t:
        return "Both the title and the text on the page point this way."
    return "Neither the title nor the page text alone is enough; it's the two together."
