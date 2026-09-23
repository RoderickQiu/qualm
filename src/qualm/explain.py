"""Why Qualm stepped in, in words.

Kev answers with probabilities only; it can't say why. So the explanation
is built from what Qualm does know: which signal fired (a URL pattern,
the score, an entertainment feed, a budget), how far past the threshold
the score was, what the model took the page to be, and, asked once per
pop-up, which part of the screen carried the signal: the model is asked
again with only the title and address, and with only the page text. In
the spirit of Time2Stop's feature attributions (CHI 2024), where
explanations raised acceptance of interventions.
"""

from __future__ import annotations

from .decide import Reading, ask
from .policy import Decision
from .rules import Rule

PAGE_WORDS = {"feed": "a feed of recommendations", "single_item": "a single page or item", "search": "search results",
              "work": "a work tool", "other": "a page"}
PURPOSE_WORDS = {"learn": "learning", "task": "getting something done", "entertain": "entertainment"}


def reason(d: Decision, reading: Reading | None, rule: Rule, lang: str) -> str:
    """One or two sentences, without another model call."""
    what = rule.text(lang)
    if d.reason == "matches URL pattern":
        head = f"This address is on your list for {rule.id}."
    elif d.reason == "an entertainment feed":
        head = "This is a feed of recommendations for entertainment."
    elif "min today" in d.reason or "visit" in d.reason:
        head = f"Over today's limit for {rule.id}: {d.reason}."
    elif reading is not None and (v := reading.verdict(rule.id)) is not None:
        ratio = v.p_hit / rule.threshold if rule.threshold else 1.0
        sure = "a clear match" if ratio >= 3 else "a likely match" if ratio >= 1.5 else "a close call"
        head = f"Your {rule.id} rule, {sure}: {what}."
    else:
        head = f"This looks like {what}."
    if reading is None:
        return head
    seen = f"Qualm read the screen as {PAGE_WORDS.get(reading.page_kind, 'a page')}, for {PURPOSE_WORDS.get(reading.purpose, 'something')}."
    return f"{head} {seen}"


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
