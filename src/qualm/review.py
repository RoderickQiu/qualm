"""Your verdicts on what Qualm did, and a local page to give them.

`qualm review --web` serves http://127.0.0.1:8765 (the app serves it too;
[settings] dashboard_port changes the port, and when another program holds
it the app takes a free one and says which in data/dashboard.json):
the dashboard in dashboard.html. Today: focus and pause, the day's pop-ups
and check-in sessions. Review: one card per screen with its screenshot, what Qualm did and why, what the model read,
and each rule's score against its threshold. You answer "was Qualm right?"
and, per rule, "is this X?". From those answers the page suggests
thresholds (the same as `rules tune`, from scores already logged or tested:
no model calls) and applies them to rules.toml, which the running app reloads. Insights: how pop-ups ended,
when they happen, what you unlocked time for, check-ins said vs kept, focus sessions. Rules: on and
off, thresholds, exceptions, never-here places.

Verdicts go to data/reviews.jsonl (append-only; the last entry per
judgement wins).

The page never gets the whole log: the server groups the screens, sends
the first cards of the review queue and the first rows of All screens (more
as you go), from the last WINDOW_DAYS unless you ask for older ones, and
answers the 15 s refresh with {"same": true} while nothing changed. A tab
opened before Qualm was updated reloads, and changes nothing until it has.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import jsonl
from .rules import Rule, Settings, load_config

PORT = 8765  # asked for first; [settings] dashboard_port or QUALM_DASHBOARD_PORT change it
RUNTIME = "dashboard.json"  # in data/: the port the running app got, for the menu, status, doctor, uninstall, review --web
VERDICTS = ("right", "wrong", "should_block", "should_not_block")
MIN_EACH = 3  # positives and negatives a rule needs before a threshold is suggested
WINDOW_DAYS = 14  # what Review and All screens look through, unless you ask for older screens
QUEUE_PAGE, ROWS_PAGE = 20, 100  # review cards and All screens rows sent at a time; the page asks for more
EXCEPTION_MAX = 200  # characters: the model reads every exception with its rule, on every screen
STATUS_FILE = "status.json"  # the running watcher's pid and whether the model answers, for the page's chip
UPDATED = "Qualm was updated: reload this page."  # to a tab opened before that


def load_judgements(data_dir: Path) -> list[dict]:
    return jsonl.read(data_dir / "judgements.jsonl")


def load_reviews(data_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in jsonl.lines(data_dir / "reviews.jsonl"):
        out[r["id"]] = r
    return out


def save_review(data_dir: Path, jid: str, **changes) -> dict:
    """Merge changes (verdict, rules={id: yes|no|None}, page_kind, purpose, note) into the review of one judgement.
    A new verdict replaces the per-rule answers given with the old one: changing
    "none of these" to "right" mustn't leave a "no" behind that contradicts it."""
    review = load_reviews(data_dir).get(jid, {"id": jid, "rules": {}})
    if "verdict" in changes:
        review["rules"] = {}
    rules = changes.pop("rules", None) or {}
    for rid, answer in rules.items():
        if answer in (None, ""):
            review["rules"].pop(rid, None)
        elif answer in ("yes", "no"):
            review["rules"][rid] = answer
        else:
            raise ValueError(f"{rid}: answer yes or no")
    if "verdict" in changes and changes["verdict"] not in (*VERDICTS, None):
        raise ValueError(f"verdict must be one of {VERDICTS}")
    review |= {k: v for k, v in changes.items() if k in ("verdict", "page_kind", "purpose", "note")}
    review["at"] = datetime.now().isoformat(timespec="seconds")
    data_dir.mkdir(parents=True, exist_ok=True)
    with jsonl.appending(data_dir / "reviews.jsonl") as f:
        f.write(json.dumps(review, ensure_ascii=False) + "\n")
    return review


def rule_answers(j: dict, review: dict) -> dict[str, bool]:
    """Is-this-X answers for one judgement: what you said per rule, plus what
    "right" implies. "Right" confirms a rule that popped up (yes) and every
    rule that never came close to firing (no); exempted rules stay open,
    since an exemption says nothing about what the page is."""
    out = {rid: a == "yes" for rid, a in review.get("rules", {}).items()}
    if review.get("verdict") == "right" and "p_hit" in j:
        acted = {d["rule"]: d["action"] for d in j["decisions"] if d["rule"]}
        for rid in j["p_hit"]:
            if rid in out:
                continue
            if acted.get(rid) in ("intervene", "count"):
                out[rid] = True
            elif rid not in acted:
                out[rid] = False
    return out


def _score(pts: list[tuple[float, bool]], t: float, target: float = 0.9) -> tuple[bool, int, int]:
    """How a threshold does on your answers, best first when sorted high:
    (at least `target` of what it catches is right, how many it catches, minus how many it gets wrong)."""
    tp = sum(p >= t and y for p, y in pts)
    pp = sum(p >= t for p, _ in pts)
    return bool(pp) and tp / pp >= target, tp, tp - pp


def _pick(pts: list[tuple[float, bool]], target: float = 0.9):
    """The threshold that catches the most of your yes answers with at least
    `target` of what it catches right, and the fewest wrong among those; set
    halfway into the gap below, so it isn't fitted to one page's exact
    score. None if no threshold reaches `target`. A score of inf or -inf is
    a page the policy flags, or lets through, whatever the threshold."""
    best = None
    for t in sorted({p for p, _ in pts if math.isfinite(p)}):
        s = _score(pts, t, target)
        if s[0] and (best is None or s[1:] > best[1][1:]):
            best = (t, s)
    if best is None:
        return None
    t = best[0]
    below = [p for p, _ in pts if math.isfinite(p) and p < t]
    lo = max(below, default=0.0)
    mid = (t + lo) / 2 if below else t
    # Rounded, but never past a score it has to keep in or leave out.
    x = next((x for x in (round(mid, 3), round(mid, 4), math.floor(t * 1000) / 1000) if lo < x <= t), t)
    return x if 0 < x < 1 else None


def _pr(pts, t):
    tp = sum(p >= t and y for p, y in pts)
    pp = sum(p >= t for p, _ in pts)
    ap = sum(y for _, y in pts)
    return (round(tp / pp, 2) if pp else None), (round(tp / ap, 2) if ap else None)


def tuning(data_dir: Path, rules: list[Rule], settings: Settings | None = None,
           judgements: list[dict] | None = None, rules_path: Path | None = None,
           log: list[dict] | None = None) -> list[dict]:
    """Per rule, from your answers: how the current threshold does, and a
    threshold that does better, if one does. The same numbers as `rules tune`.
    `judgements`: the answered ones with what the model read, if already read;
    `log`: then the others too, for the wordings and model they were scored with."""
    from .trial import tune_all

    return tune_all(data_dir, rules, settings or Settings(), judgements=judgements, rules_path=rules_path, log=log)


def review_labels(data_dir: Path, rules: list[Rule]) -> list[dict]:
    """Reviewed judgements as `label` records, for `eval --reviews` and `export`."""
    kinds = {r.id: r.kind for r in rules}
    reviews = load_reviews(data_dir)
    out = []
    for j in load_judgements(data_dir):
        rv = reviews.get(j["id"])
        if not rv or "p_hit" not in j:
            continue
        labels = {"rules": {}}
        for rid, y in rule_answers(j, rv).items():
            if rid in kinds:
                deny = kinds[rid] == "deny"
                labels["rules"][rid] = ("violates" if deny else "in_scope") if y else ("safe" if deny else "out_of_scope")
        for k in ("page_kind", "purpose"):
            if rv.get(k):
                labels[k] = rv[k]
        out.append({"captured_at": j["at"], "screen": j["screen"], "labels": labels, "note": f"review #{j['id']}"})
    return out


def set_threshold(rules_path: Path, rule_id: str, value: float) -> None:
    """Set one rule's threshold in rules.toml, keeping everything else."""
    from .config import Config

    Config(rules_path).edit("rules", rule_id, {"threshold": value})


def _typed(data_dir: Path, rule_id: str) -> list[str]:
    """Exceptions in words kept in exceptions.jsonl (older dashboards wrote them there), minus the removed ones."""
    from .personalize import live_exceptions

    return [e["text"] for e in live_exceptions(data_dir) if e["rule"] == rule_id and e.get("text")]


def add_exception(rules_path: Path, data_dir: Path, rule_id: str, text: str) -> None:
    """An exception in words, saved with the rule in rules.toml, like `qualm except add`:
    checked, in `config export`, and removable here or from the CLI."""
    from .config import Config

    text = " ".join(text.split())
    if not text:
        raise ValueError("write the exception first, e.g. “a lecture or conference talk”")
    if len(text) > EXCEPTION_MAX:
        raise ValueError(f"keep it under {EXCEPTION_MAX} characters: the model reads it with the rule on every screen")
    cfg = Config(rules_path)
    with cfg.locked():  # read and save as one step: a change from elsewhere can't land in between
        rule = next((r for r in cfg.load()[1] if r.id == rule_id), None)
        if rule is None:
            raise ValueError(f"there's no rule {rule_id!r} any more: reload the page")
        if text in rule.exceptions or text in _typed(data_dir, rule_id):
            raise ValueError(f"{rule_id} already has that exception")
        cfg.edit("rules", rule_id, {"exceptions": [*rule.exceptions, text]})


def remove_exception(rules_path: Path, data_dir: Path, rule_id: str, text: str) -> None:
    from .config import Config

    cfg = Config(rules_path)
    with cfg.locked():  # read and save as one step: a change from elsewhere can't land in between
        rule = next((r for r in cfg.load()[1] if r.id == rule_id), None)
        if rule is not None and text in rule.exceptions:
            cfg.edit("rules", rule_id, {"exceptions": [x for x in rule.exceptions if x != text]})
            return
    if text in _typed(data_dir, rule_id):
        e = {"rule": rule_id, "text": text, "removed": True, "at": datetime.now().isoformat(timespec="seconds")}
        with jsonl.appending(data_dir / "exceptions.jsonl") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    else:
        raise ValueError(f"{rule_id} has no exception “{text}” any more: reload the page")


def _lines(path: Path) -> list[dict]:
    return jsonl.read(path)


# What you answered a pop-up with; "open": no answer. "session": you checked
# in; "snooze": "I need it", or "5 more" when a session's time was up. "Not
# now" on a focus session's corner nudge counts as going back: had you stayed,
# the panel would have come (app.NUDGE_S later), and its answer would count
# instead (popups()); with no panel after it, you had left.
OUTCOMES = ("back", "session", "snooze", "fine", "never")
SAME_AS = {"not now": "back"}
NUDGED_S = 60  # a focus nudge, then the panel on the same page this soon after (app.NUDGE_S * 3): one pop-up


def popups(events: list[dict]) -> list[dict]:
    """The pop-ups that were shown. The app shows one per screen, and none
    while one is open: a hit logged before the open one was answered, with
    no answer of its own, never showed. (Older logs also have a line for
    each further rule a screen hit, at the same second.) In a focus session
    a hit first gets a corner nudge, and the full panel if you're still
    there: the nudge, let go or answered "Not now", and that panel count
    once, with the panel's answer."""
    answered_at, answer = {}, {}
    for e in events:
        if e.get("type") == "response":
            answered_at.setdefault(e["id"], e["at"])
            answer.setdefault(e["id"], e.get("response"))
    out, seen, open_until, last = [], set(), "", {}
    for e in events:
        if e.get("type") != "intervention" or e.get("id") == "demo":
            continue
        s = e.get("screen") or {}
        key = (e["at"], s.get("url", ""), s.get("window_title", ""))
        if key in seen or (e["id"] not in answered_at and e["at"] < open_until):
            continue
        seen.add(key)
        page = (e.get("rule"), s.get("url") or s.get("window_title", ""))
        before = last.get(page)
        if before is not None and answer.get(before["id"]) in (None, "not now") and (
                datetime.fromisoformat(e["at"]) - datetime.fromisoformat(before["at"])).total_seconds() <= NUDGED_S:
            out.remove(before)  # the nudge: this panel is the same pop-up
        last[page] = e
        out.append(e)
        open_until = max(open_until, answered_at.get(e["id"], ""))
    return out


def insights(data_dir: Path, rules: list[Rule], days: int = 14, weeks: int = 8,
             judgements: list[dict] | None = None) -> dict:
    """Everything the Today and Insights tabs draw, from the logs: how each
    pop-up ended, per day; pop-ups per week; when in the week they happen;
    the time you asked for and what for; focus sessions; how often you said
    Qualm was right; check-in sessions, what you said and what you did. No
    streaks, no score."""
    today = date.today()
    span = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    monday = today - timedelta(days=today.weekday())
    week_starts = [(monday - timedelta(weeks=i)).isoformat() for i in range(weeks - 1, -1, -1)]
    events = _lines(data_dir / "decisions.jsonl")
    answer = {e["id"]: SAME_AS.get(e["response"], e["response"]) for e in events  # how each pop-up ended
              if e.get("type") == "response" and SAME_AS.get(e.get("response"), e.get("response")) in OUTCOMES}
    shown = popups(events)
    if judgements is None:
        judgements = load_judgements(data_dir)
    per_day = {d: {k: 0 for k in (*OUTCOMES, "open")} for d in span}
    per_rule_day = {d: {} for d in span}
    per_week = {w: 0 for w in week_starts}
    heat = [[0] * 24 for _ in range(7)]  # [weekday][hour]
    oldest_week = week_starts[0]
    for e in shown:
        day = e["at"][:10]
        ended = answer.get(e["id"], "open")
        if day in per_day:
            per_day[day][ended] += 1
            per_rule_day[day][e["rule"]] = per_rule_day[day].get(e["rule"], 0) + 1
        d = date.fromisoformat(day)
        wk = (d - timedelta(days=d.weekday())).isoformat()
        if wk in per_week:
            per_week[wk] += 1
        if wk >= oldest_week:
            heat[d.weekday()][int(e["at"][11:13])] += 1
    unlocks = [{"at": e["at"], "rule": e.get("rule", ""), "reason": e.get("reason", ""), "minutes": e.get("minutes")}
               for e in events if e.get("type") == "response" and e.get("response") in ("snooze", "session")][-30:]
    # Focus sessions: started, ended early or ran out; pop-ups while they ran.
    sessions = []
    for e in events:
        if e.get("type") == "focus":
            start = datetime.fromisoformat(e["at"])
            sessions.append({"intent": e["intent"], "at": e["at"], "planned": e.get("minutes", 0),
                             "end": (start + timedelta(minutes=e.get("minutes", 0))).isoformat(timespec="seconds")})
        elif e.get("type") == "focus_end" and sessions and sessions[-1]["intent"] == e.get("intent"):
            sessions[-1]["end"] = min(sessions[-1]["end"], e["at"])
    now = datetime.now().isoformat(timespec="seconds")
    for f in sessions:
        f["running"] = f["end"] > now
        until = now if f["running"] else f["end"]  # so far, for the one running now
        f["minutes"] = round((datetime.fromisoformat(until) - datetime.fromisoformat(f["at"])).total_seconds() / 60)
        during = [e for e in shown if f["at"] <= e["at"] <= f["end"]]
        f["popups"] = len(during)
        f["back"] = sum(answer.get(e["id"]) == "back" for e in during)
    # How often Qualm was right, by your own reviews of pop-ups.
    reviews = load_reviews(data_dir)
    right = wrong = 0
    for j in judgements:
        rv = reviews.get(j["id"])
        if rv and rv.get("verdict") and any(d["action"] == "intervene" for d in j["decisions"]):
            right += rv["verdict"] == "right"
            wrong += rv["verdict"] != "right"
    hist_path = data_dir / "usage_history.json"
    history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}
    live_path = data_dir / "usage.json"
    if live_path.exists():  # today's counts, saved every 30 s
        live = json.loads(live_path.read_text(encoding="utf-8"))
        history[live.get("day", "")] = live.get("counts", {})
    checkins = [{"rule": r.id, "minutes": [round(history.get(d, {}).get(r.id, {}).get("seconds", 0) / 60, 1) for d in span]}
                for r in rules if r.kind == "check_in" and r.enabled]
    # Check-in sessions: what you said (what for, how long) and what you did.
    started: dict[str, dict] = {}
    for e in events:
        if e.get("type") != "session":
            continue
        if e["event"] == "start":
            started[e["id"]] = {"at": e["at"], "rule": e["rule"], "for": e.get("for", ""), "minutes": e["minutes"],
                                "until": e.get("until", 0), "extended": 0, "stayed": None, "how": ""}
        elif (c := started.get(e["id"])) is not None:
            if e["event"] == "extend":
                c["extended"] += 1
                c["until"] = e.get("until", c["until"])
            elif e["event"] == "end":
                c.update(stayed=e.get("stayed"), how=e.get("how", ""))  # minutes: what you said at the start
    sessions_list = [c for c in started.values() if c["at"][:10] >= span[0]]
    now_ts = datetime.now().timestamp()
    for c in sessions_list:
        c["running"] = not c["how"] and c["until"] > now_ts
    reflections = {}
    for e in _lines(data_dir / "reflections.jsonl"):
        reflections[e["week"]] = e
    today_popups = [{"at": e["at"], "rule": e["rule"], "title": (e.get("screen") or {}).get("window_title", ""),
                     "ended": answer.get(e["id"], "open")} for e in shown if e["at"][:10] == span[-1]]
    judged_today = sum(1 for j in judgements if j["at"][:10] == span[-1] and "p_hit" in j)
    return {"days": span, "outcomes": per_day, "by_rule": per_rule_day, "weeks": per_week, "heat": heat,
            "unlocks": unlocks, "focus": sessions[-20:], "trust": {"right": right, "wrong": wrong},
            "checkins": checkins, "sessions": sessions_list[-60:], "reflections": reflections, "this_week": week_starts[-1], "judged_today": judged_today,
            "today": today_popups}


REFLECT_ANSWERS = ("mostly", "mixed", "not_really")


def reflect(data_dir: Path, week: str, answer: str, note: str = "") -> None:
    """The weekly question: was the time you checked in for worth it? Time you
    value and time you don't look the same in a total; only you can tell."""
    if answer not in REFLECT_ANSWERS:
        raise ValueError(f"answer one of {REFLECT_ANSWERS}")
    datetime.fromisoformat(week)
    e = {"week": week, "answer": answer, "note": note.strip()[:300], "at": datetime.now().isoformat(timespec="seconds")}
    data_dir.mkdir(parents=True, exist_ok=True)
    with jsonl.appending(data_dir / "reflections.jsonl") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def week(data_dir: Path, rules: list[Rule]) -> dict:
    """How this week went, in the Insights tab's own numbers (`qualm week`)."""
    from .setup import rule_name

    ins = insights(data_dir, rules)
    monday = ins["this_week"]
    words = {r.id: r.description for r in rules}
    name = lambda rid: rule_name(rid, words.get(rid, ""))  # the menu's names, yours in your words
    days = [d for d in ins["days"] if d >= monday]

    def ended(ds):
        out = {k: sum(ins["outcomes"][d][k] for d in ds) for k in (*OUTCOMES, "open")}
        answered = sum(out.values()) - out["open"]
        return {"popups": sum(out.values()), "ended": out,
                "went_back_pct": round(100 * out["back"] / answered) if answered else None}

    by_rule: dict[str, int] = {}
    for d in days:
        for rid, n in ins["by_rule"][d].items():
            by_rule[rid] = by_rule.get(rid, 0) + n
    sessions = [c for c in ins["sessions"] if c["at"][:10] >= monday]
    done = [c for c in ins["sessions"] if not c["running"] and c["how"]]
    return {
        "since": monday,
        "this_week": ended(days) | {"by_rule": {name(k): v for k, v in sorted(by_rule.items(), key=lambda x: -x[1])},
                                    "last_week": list(ins["weeks"].values())[-2]},
        "last_14_days": ended(ins["days"]) | {"not_this_one": sum(ins["outcomes"][d]["fine"] for d in ins["days"]),
                                            "never_here": sum(ins["outcomes"][d]["never"] for d in ins["days"])},
        "per_week": ins["weeks"],
        "check_ins": [{"rule": name(b["rule"]), "minutes": round(sum(m for d, m in zip(ins["days"], b["minutes"]) if d >= monday)),
                       "sessions": sum(c["rule"] == b["rule"] for c in sessions)} for b in ins["checkins"]],
        "check_ins_14_days": {"finished": len(done), "ended_when_you_said": sum(not c["extended"] and c["how"] != "new" for c in done)},
        "time_asked_for": [u | {"rule": name(u["rule"])} for u in ins["unlocks"] if u["at"][:10] >= monday],
        "focus": [{k: f[k] for k in ("intent", "at", "minutes", "planned", "popups", "back", "running")}
                  for f in ins["focus"] if f["at"][:10] >= monday],
        "reviews": ins["trust"],
        "question": (ins["reflections"].get(monday) or {}).get("answer"),
        "screens_read_today": ins["judged_today"],
    }


def week_text(w: dict) -> str:
    """`qualm week` in words."""
    t = w["this_week"]
    words = {"back": "went back", "session": "checked in", "snooze": "unlocked", "fine": "not this one",
             "never": "never here", "open": "no answer"}
    since = datetime.fromisoformat(w["since"]).strftime("%A %b %-d")
    lines = [f"This week (since {since}): {t['popups']} pop-up{'s' * (t['popups'] != 1)}; last week {t['last_week']}."]
    if t["popups"]:
        lines.append("  How they ended: " + ", ".join(f"{words[k]} {n}" for k, n in t["ended"].items() if n) + ".")
        if t["went_back_pct"] is not None:
            lines.append(f"  You went back on {t['went_back_pct']}% of the ones you answered.")
        lines.append("  By rule: " + ", ".join(f"{k} {n}" for k, n in t["by_rule"].items()) + ".")
    for c in (c for c in w["check_ins"] if c["sessions"] or c["minutes"]):
        lines.append(f"  Checked in on {c['rule']}: {c['sessions']} session{'s' * (c['sessions'] != 1)}, {c['minutes']} min.")
    for f in w["focus"]:
        popped = f"{f['popups']} pop-up{'s' * (f['popups'] != 1)}, went back {f['back']}×" if f["popups"] else "no pop-ups"
        lines.append(f"  Focus: {f['intent']} ({f['minutes']} of {f['planned']:g} min, {popped}).")
    for u in w["time_asked_for"][-5:]:
        lines.append(f"  Time you asked for: “{u['reason'] or 'no reason given'}” ({u['rule']}"
                     + (f", {u['minutes']} min" if u.get("minutes") else "") + ").")
    last = w["last_14_days"]
    lines.append(f"Last 14 days: {last['popups']} pop-ups" + (f"; you went back on {last['went_back_pct']}% of the ones you answered"
                                                             if last["went_back_pct"] is not None else "")
                 + f". “Not this one” {last['not_this_one']}, “Never here” {last['never_here']}.")
    lines.append("Pop-ups per week: " + " · ".join(f"{k[5:]} {n}" for k, n in w["per_week"].items()))
    r = w["reviews"]
    lines.append(f"Your reviews: {r['right']} of {r['right'] + r['wrong']} pop-ups you reviewed were right." if r["right"] + r["wrong"]
                 else "Your reviews: none yet (the dashboard's Review tab asks, closest calls first).")
    lines.append(f"Was the time you checked in for worth it? {w['question'].replace('_', ' ') if w['question'] else 'not answered yet (the dashboard asks, on Insights)'}.")
    if not t["popups"] and not last["popups"] and not w["screens_read_today"]:
        lines.append("Nothing yet: Qualm writes here as you use your Mac. Keep it running (it lives in the menu bar).")
    return "\n".join(lines)


def session_state(data_dir: Path) -> dict:
    from .policy import read_session

    s = read_session(data_dir)
    return {"paused_until": s["paused_until"], "pause_later": s["pause_later"], "focus": s["focus"],
            "now": datetime.now().timestamp()}


_written: dict[Path, dict] = {}  # what write_status last wrote, per folder: it's called on every reading


def write_status(data_dir: Path, **changes) -> None:
    """The watcher's side of the page's status chip, in data/status.json:
    pid (so a Qualm that quit shows as not running) and model ("starting",
    "ok", or why it didn't answer). Written only when something changed, or
    when the file names another watcher that has quit since (`qualm watch`
    on the same folder, then Ctrl-C): this one is still watching."""
    from .policy import _atomic_write

    data_dir = Path(data_dir)
    cur = _written.get(data_dir, {})
    same = all(cur.get(k) == v for k, v in changes.items())
    if same and "pid" in cur:
        try:
            theirs = json.loads((data_dir / STATUS_FILE).read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError, AttributeError):
            theirs = None
        same = theirs == cur["pid"] or _alive(theirs)
    if same:
        return
    _written[data_dir] = cur = cur | changes
    data_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(data_dir / STATUS_FILE, json.dumps(cur | {"at": datetime.now().isoformat(timespec="seconds")}))


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def app_status(data_dir: Path, judgements: list[dict], here: bool = False, access: bool | None = None) -> dict:
    """Is a Qualm watching (its pid alive, or `here`: this is the app's own
    page), is the model answering, may it read windows (`access`: the app's
    own Accessibility, asked now; else as the last screen it read says)."""
    try:
        s = json.loads((data_dir / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    if access is None:
        access = next((j["screen"]["ax_trusted"] for j in reversed(judgements) if "ax_trusted" in j["screen"]), None)
    if not isinstance(s, dict):
        s = {}
    # While rules.toml doesn't load, what the app judges with (app.Controller.rules_broken): "" from an
    # app that doesn't say; and whether it sends nothing to the hosted model meanwhile.
    return {"running": here or _alive(s.get("pid")), "model": s.get("model", ""), "accessibility": access,
            "rules": s.get("rules", ""), "hosted_off": bool(s.get("hosted_off"))}


def broken_file(rules_path: Path) -> dict:
    """While rules.toml doesn't load, for the page: where `config undo` goes
    back to, in words ("": nothing to go back to), and what the app starts
    on meanwhile (app.fallback_config), as the menu and the CLI say them."""
    from .app import fallback_config
    from .config import undo_to

    return {"undo": undo_to(rules_path) or "", "starts_on": fallback_config(rules_path)[2]}


def hosted_trouble(model: str) -> str:
    """The hosted model's trouble in status.json's model, of the kinds the
    menu bar tells in words (app.model_trouble): "nokey", "key", "quota", "down", or ""."""
    if model in ("", "ok", "starting"):
        return ""
    from .app import model_trouble

    return model_trouble(model if model.startswith("no TypeSafe API key") else f"model unreachable: {model}")


def never_places(data_dir: Path) -> list[dict]:
    """The apps and sites you said "never here" to, minus the ones you undid."""
    places: dict[str, dict] = {}
    for e in exceptions(data_dir):
        n = e.get("never")
        if n:
            key = n.get("host") or n.get("app")
            if e.get("removed"):
                places.pop(key, None)
            else:
                places[key] = n
    return list(places.values())


def undo_never(data_dir: Path, place: dict) -> None:
    e = {"never": place, "removed": True, "at": datetime.now().isoformat(timespec="seconds")}
    with jsonl.appending(data_dir / "exceptions.jsonl") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def exceptions(data_dir: Path) -> list[dict]:
    return jsonl.read(data_dir / "exceptions.jsonl")


# -- what the page shows: screens, grouped, in pages ---------------------------

_HEAD = re.compile(rb'"(id|at)": "([^"]*)"')
_LIGHT_SCREEN = ("app", "bundle_id", "window_title", "url", "site", "ax_trusted")


def scan(data_dir: Path, since: str = "", keep: set[str] = frozenset()) -> list[dict]:
    """The judgements from the day `since` on ("" for all) and those in `keep`,
    without what's heavy (what the model read, the whole capture): older lines
    are skipped without being parsed. Each keeps its line's offset in `_off`,
    for with_state()."""
    path = data_dir / "judgements.jsonl"
    out: list[dict] = []
    if not path.exists():
        return out
    with path.open("rb") as f:
        off = 0
        for line in f:
            start, off = off, off + len(line)
            if since:
                head = dict(_HEAD.findall(line[:160]))
                if head.get(b"at", b"9")[:10].decode() < since and head.get(b"id", b"").decode() not in keep:
                    continue
            try:
                j = json.loads(line)
            except ValueError:  # blank, or a line still being written
                continue
            j.pop("state", None), j.pop("answers", None), j.pop("allow", None)
            j["screen"] = {k: v for k, v in (j.get("screen") or {}).items() if k in _LIGHT_SCREEN}
            j["_off"] = start
            out.append(j)
    return out


def with_state(data_dir: Path, js: list[dict]) -> list[dict]:
    """Scanned judgements with what the model read again, from their lines."""
    out = [{k: v for k, v in j.items() if not k.startswith("_")} for j in js]
    path = data_dir / "judgements.jsonl"
    if not js or not path.exists():
        return out
    with path.open("rb") as f:
        for j, o in zip(js, out):
            try:
                f.seek(j["_off"])
                whole = json.loads(f.readline())
            except (KeyError, ValueError):
                continue
            if whole.get("id") == j["id"]:  # the file wasn't rewritten meanwhile (retention)
                o["state"], o["allow"] = whole.get("state"), whole.get("allow", {})
    return out


def outcome(j: dict, rule_ids: set[str] | None) -> str:
    """What Qualm did. A rule you removed doesn't count; one you only switched
    off does: its past pop-ups stay what they were."""
    acts = {d["action"] for d in j["decisions"] if not d["rule"] or rule_ids is None or d["rule"] in rule_ids}
    return next((k for k in ("intervene", "allow", "count", "skip") if k in acts), "none")


def _near_miss(j: dict) -> float:
    t = j.get("thresholds") or {}
    return max((p / (t.get(rid) or 0.5) for rid, p in (j.get("p_hit") or {}).items()), default=0.0)


def _closeness(j: dict) -> float:
    """How close the closest rule came to its threshold (log ratio): your
    answer there moves a threshold the most. URL-pattern hits are certain."""
    if "p_hit" not in j:
        return 99.0
    by_pattern = {d["rule"] for d in j["decisions"] if d["reason"] == "matches URL pattern"}
    t = j.get("thresholds") or {}
    return min((abs(math.log(max(p, 1e-3) / (t.get(rid) or 0.5))) for rid, p in j["p_hit"].items() if rid not in by_pattern),
               default=99.0)


def screens(judgements: list[dict], reviews: dict[str, dict], rule_ids: set[str] | None) -> list[dict]:
    """One entry per page (URL, or app + title): the latest detailed judgement of it."""
    by: dict[str, list[dict]] = {}
    for j in judgements:
        s = j["screen"]
        if s.get("window_title") in ("Qualm", "SeeNot") and not s.get("url"):
            continue  # our own panel
        by.setdefault(s.get("url") or f"{s.get('app', '')}|{s.get('window_title') or ''}", []).append(j)
    out = []
    for key, items in by.items():
        detailed = [j for j in items if "p_hit" in j]
        j = dict(detailed[-1] if detailed else items[-1])
        j["outcome"] = outcome(j, rule_ids)
        review = next((reviews[x["id"]] for x in reversed(items) if x["id"] in reviews), None)
        shot = j.get("shot") or next((x["shot"] for x in reversed(items) if x.get("shot")), "")
        out.append({"key": key, "j": j, "seen": len(items), "first": items[0]["at"], "last": items[-1]["at"],
                    "review": review, "shot": shot})
    return out


def _looks_like_id(seg: str) -> bool:
    return (any(c.isdigit() for c in seg) or len(seg) >= 12
            or bool(re.fullmatch(r"[A-Za-z0-9_-]{8,}", seg)) and seg.lower() != seg and seg.upper() != seg)


def _sim_key(e: dict) -> str:
    """Similar screens: the same site and kind of page, with ids blanked out
    (youtube.com/shorts/*, reddit.com/r/*/comments/*), and the same outcome."""
    j = e["j"]
    where = j["screen"].get("app", "")
    url = j["screen"].get("url", "")
    if re.match(r"https?:", url):
        u = urlsplit(url)
        segs = ["*" if _looks_like_id(x) else x for x in [s for s in u.path.split("/") if s][:4]]
        if segs[:1] == ["r"] and len(segs) > 1:
            segs[1] = "*"
        where = (u.hostname or "").removeprefix("www.") + "/" + "/".join(segs)
    acts = ",".join(sorted(f"{d['action']}:{d['rule']}" for d in j["decisions"] if d["rule"]))
    return f"{where}|{j['outcome']}|{acts}"


def groups(entries: list[dict]) -> list[list[dict]]:
    """Similar screens together, the latest first in each group."""
    by: dict[str, list[dict]] = {}
    for e in entries:
        by.setdefault(_sim_key(e), []).append(e)
    return [sorted(g, key=lambda e: e["last"], reverse=True) for g in by.values()]


PRIORITY = {"intervene": 0, "allow": 1, "count": 2, "none": 3, "skip": 4}


def review_queue(entries: list[dict], quiet: bool = False) -> list[list[dict]]:
    """Screens you haven't answered for, in groups of similar ones: pop-ups
    first, then exemptions, then (with `quiet`, or when a rule came within
    half its threshold) the ones Qualm left alone; closest calls first."""
    open_ = [e for e in entries if "p_hit" in e["j"] and not (e["review"] or {}).get("verdict")
             and (quiet or e["j"]["outcome"] != "none" or _near_miss(e["j"]) >= 0.5)]
    out = sorted(groups(open_), key=lambda g: g[0]["last"], reverse=True)
    return sorted(out, key=lambda g: (PRIORITY[g[0]["j"]["outcome"]], _closeness(g[0]["j"]), -len(g)))


def all_screens(entries: list[dict], search: str = "", show: str = "all") -> list[list[dict]]:
    """The All screens tab: every kind of screen, the latest first, searched and filtered."""
    out = sorted(groups(entries), key=lambda g: g[0]["last"], reverse=True)
    if search:
        s = search.lower()
        out = [g for g in out if s in " ".join(g[0]["j"]["screen"].get(k) or "" for k in ("window_title", "url", "app")).lower()]
    if show in ("wrong", "right"):
        out = [g for g in out if (v := (g[0]["review"] or {}).get("verdict")) and (v == "right") == (show == "right")]
    elif show != "all":
        out = [g for g in out if g[0]["j"]["outcome"] == show]
    return out


# -- the page ---------------------------------------------------------------


def dashboard_port(settings=None) -> int:
    """The port the dashboard asks for: QUALM_DASHBOARD_PORT, else [settings] dashboard_port, else 8765."""
    env = os.environ.get("QUALM_DASHBOARD_PORT", "")
    return int(env) if env.isdigit() else getattr(settings, "dashboard_port", PORT)


def dashboard_url(data_dir: Path, settings=None) -> str:
    """Where the dashboard is: the port the running app wrote down, else the one it asks for."""
    try:
        port = int(json.loads((Path(data_dir) / RUNTIME).read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        port = dashboard_port(settings)
    return f"http://127.0.0.1:{port}/"


def hello(url: str, timeout: float = 1.0, older: bool = True) -> dict | None:
    """What Qualm's dashboard at `url` says about itself ({"app": "qualm",
    "watching", "data", ...}); None if nothing answers there or it isn't
    Qualm: another program may hold the port (AnkiConnect's default is 8765 too).
    `older`: also recognise an app from before /api/hello, by its page."""
    try:
        with urllib.request.urlopen(url + "api/hello", timeout=timeout) as r:
            d = json.loads(r.read())
        return d if isinstance(d, dict) and d.get("app") == "qualm" else None
    except urllib.error.HTTPError as e:
        if e.code != 404 or not older:
            return None
    except Exception:
        return None
    # An app from before /api/hello: its page is titled "Qualm review".
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return {"app": "qualm", "watching": True, "older": True} if b"<title>Qualm review</title>" in r.read(4096) else None
    except Exception:
        return None


def app_running(data_dir: Path, settings=None) -> dict | None:
    """The app watching for this data folder, as its dashboard describes itself; None if there's none.
    An app from before /api/hello can't say which folder it watches: it's taken
    for the main install's (all it knew), never for a QUALM_HOME elsewhere."""
    from .paths import custom_home

    h = hello(dashboard_url(data_dir, settings), older=not custom_home())
    if not h or not h.get("watching"):
        return None
    return h if h.get("older") or h.get("data") == str(Path(data_dir).resolve()) else None


def start_dashboard(data_dir: Path, rules_path: Path, port: int = PORT) -> ThreadingHTTPServer:
    """The app's dashboard: on `port`, or on a free one when something else
    holds it. The port it got goes into data/dashboard.json until it quits."""
    import atexit

    try:
        server = start_server(data_dir, rules_path, port)
    except OSError:
        server = start_server(data_dir, rules_path, 0)
    runtime, pid = Path(data_dir) / RUNTIME, os.getpid()
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(json.dumps({"port": server.server_address[1], "pid": pid}), encoding="utf-8")

    def forget():
        try:
            if json.loads(runtime.read_text(encoding="utf-8")).get("pid") == pid:
                runtime.unlink()
        except (OSError, ValueError):
            pass

    atexit.register(forget)
    return server


def serve(data_dir: Path, rules_path: Path, port: int = PORT, open_browser: bool = True) -> None:
    """Serve the page until Ctrl-C. If the app already serves it, just open it."""
    if app_running(data_dir):
        url = dashboard_url(data_dir)
        print(f"already running (the app serves it): {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    try:
        server = start_server(data_dir, rules_path, port, watching=False)
    except OSError:  # something else holds the port
        server = start_server(data_dir, rules_path, 0, watching=False)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"review page: {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def start_server(data_dir: Path, rules_path: Path, port: int = PORT, watching: bool = True) -> ThreadingHTTPServer:
    """The review page's server, not yet serving (port 0: any free one). Raises
    OSError if the port is taken. `watching`: this is the app's, not `review --web`'s."""
    data_dir, rules_path = Path(data_dir), Path(rules_path)
    watched = [rules_path, data_dir / "shots", *(data_dir / n for n in (
        "judgements.jsonl", "reviews.jsonl", "decisions.jsonl", "exceptions.jsonl", "session.json",
        "reflections.jsonl", "trials.jsonl", STATUS_FILE))]
    # The page as this server knows it, read once: a tab opened before Qualm
    # was updated asks with another version (or, from before versions, with
    # no query at all) and is told to reload instead of breaking.
    page = PAGE_FILE.read_bytes()
    page_version = hashlib.sha1(page).hexdigest()[:12]
    page = page.replace(b"__PAGE_VERSION__", page_version.encode())

    def access() -> bool | None:
        """The app's own Accessibility, asked now: granted in System Settings,
        the page says so at its next refresh. None from `review --web`, whose
        process isn't the one that reads windows."""
        if not watching:
            return None
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())

    def about() -> dict:
        """Who answers here, for `status`, `doctor` and `uninstall`: Qualm, whether
        it's the app watching this data folder, and the permissions it has."""
        from ApplicationServices import AXIsProcessTrusted
        from Quartz import CGPreflightScreenCaptureAccess

        from .paths import bundle

        return {"app": "qualm", "watching": watching, "data": str(data_dir.resolve()), "pid": os.getpid(),
                "bundle": bool(bundle()), "exe": str(Path(sys.executable).resolve()),
                "accessibility": bool(AXIsProcessTrusted()), "screen_recording": bool(CGPreflightScreenCaptureAccess())}

    def version(q: dict) -> str:
        """Changes when a file the page shows changes, when the page asks for
        something else, when the app gets or loses Accessibility, and each
        minute (times; today's check-in minutes, as usage.json is saved every
        30 s even when nothing happens)."""
        parts = [f"{k}={q[k]}" for k in sorted(q) if k != "v"] + [datetime.now().strftime("%Y-%m-%d %H:%M"), f"ax={access()}"]
        for p in watched:
            try:
                st = p.stat()
                parts.append(f"{st.st_size}.{st.st_mtime_ns}")
            except OSError:
                parts.append("-")
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]

    def config():
        """Settings, rules and "" or, when rules.toml doesn't load, no rules and
        why: the history tabs keep working, and the page says what to fix."""
        try:
            return (*load_config(rules_path), "")
        except FileNotFoundError:
            return Settings(), None, f"there's no rules file at {rules_path} yet: open Qualm to set it up, or run `qualm setup`"
        except Exception as e:  # a hand edit that doesn't parse or check
            return Settings(), None, str(e)

    def view(q: dict):
        settings, rules, problem = config()
        rule_ids = None if rules is None else {r.id for r in rules}
        reviews = load_reviews(data_dir)
        since = "" if q.get("older") == "1" else (date.today() - timedelta(days=WINDOW_DAYS - 1)).isoformat()
        js = scan(data_dir, since, set(reviews))  # the window, and every reviewed one (trust, tuning)
        recent = [j for j in js if j["at"][:10] >= since]
        return settings, rules or [], problem, js, recent, screens(recent, reviews, rule_ids)

    def cards(gs: list[list[dict]], have: set[str]) -> list[dict]:
        full = with_state(data_dir, [g[0]["j"] for g in gs])
        return [{"key": g[0]["key"], "j": j, "shot": g[0]["shot"] if g[0]["shot"] in have else "", "first": g[0]["first"],
                 "last": g[0]["last"], "seen": g[0]["seen"], "review": g[0]["review"], "ids": [m["j"]["id"] for m in g],
                 "titles": [m["j"]["screen"].get("window_title") or m["j"]["screen"].get("url", "") for m in g[:25]]}
                for g, j in zip(gs, full)]

    def shots() -> set[str]:
        """Screenshots are kept for fewer days than judgements (retention.py):
        an old one's file is gone, so the page shows "no screenshot" instead of a broken image."""
        return {p.name for p in (data_dir / "shots").glob("*.jpg")}

    def number(q: dict, key: str, default: int) -> int:
        return min(max(int(q[key]), 1), 10_000) if q.get(key, "").isdigit() else default

    def status(recent: list[dict], hosted: bool) -> dict:
        """The chip's facts, and the hosted model's trouble by kind, told in
        words as the menu bar tells it, with its fix; `local`: the menu also
        offers the model on this Mac (app.Controller._set_trouble)."""
        from .localmodel import unsupported

        s = app_status(data_dir, recent, here=watching, access=access())
        trouble = hosted_trouble(s["model"]) if hosted else ""
        return s | {"trouble": trouble, "local": bool(trouble) and unsupported() is None
                    and not os.environ.get("QUALM_BACKEND")}

    def payload(q: dict) -> dict:
        from .agent import command
        from .decide import backend
        from .personalize import live_exceptions, summary
        from .rules import capacity
        from .setup import rule_name

        settings, rules, problem, js, recent, entries = view(q)
        reviewed = load_reviews(data_dir)
        queue = review_queue(entries, q.get("quiet") == "1")
        rows = all_screens(entries, q.get("q", ""), q.get("f", "all"))
        have = shots()
        typed: dict[str, list[str]] = {}
        for e in live_exceptions(data_dir):
            if e.get("text"):
                typed.setdefault(e["rule"], []).append(e["text"])
        return {
            "queue": {"cards": cards(queue[:number(q, "qn", QUEUE_PAGE)], have), "total": len(queue),
                      "screens": sum("p_hit" in e["j"] for e in entries),
                      "reviewed": sum(bool((e["review"] or {}).get("verdict")) for e in entries)},
            "rows": {"total": len(rows), "items": [
                {"key": g[0]["key"], "shot": g[0]["shot"] if g[0]["shot"] in have else "", "last": g[0]["last"],
                 "title": g[0]["j"]["screen"].get("window_title") or g[0]["j"]["screen"].get("app", ""),
                 "where": g[0]["j"]["screen"].get("url") or g[0]["j"]["screen"].get("app", ""),
                 "outcome": g[0]["j"]["outcome"], "rules": [d["rule"] for d in g[0]["j"]["decisions"] if d["rule"]],
                 "similar": len(g), "verdict": (g[0]["review"] or {}).get("verdict"), "open": "p_hit" in g[0]["j"]}
                for g in rows[:number(q, "rn", ROWS_PAGE)]]},
            "window": WINDOW_DAYS, "older": q.get("older") == "1",
            # URL patterns by the site each names; the regexes themselves only on request.
            "rules": [{"id": r.id, "name": rule_name(r.id, r.description), "kind": r.kind, "text": r.text(settings.lang), "enabled": r.enabled,
                       "active": r.active(), "summary": summary(r, settings, patterns=False), "patterns": list(r.patterns),
                       "threshold": r.threshold, "note": r.note, "exceptions": [*r.exceptions, *typed.get(r.id, [])]}
                      for r in rules],
            "capacity": capacity(settings, rules),
            "never": never_places(data_dir),
            "insights": insights(data_dir, rules, judgements=js),
            "session": session_state(data_dir),
            "tuning": tuning(data_dir, rules, settings, with_state(data_dir, [j for j in js if j["id"] in reviewed]),
                             rules_path, log=js),
            "status": status(recent, not problem and backend(settings) == "jev"),
            # Where screens are read, for what the page says about it; unknown while rules.toml doesn't load.
            "backend": "" if problem else backend(settings),
            "problem": problem,
            **(broken_file(rules_path) if problem else {"undo": "", "starts_on": ""}),
            "rules_file": str(rules_path),
            "command": command(),  # how this Mac's terminal runs qualm, for the commands the page names
        }

    def screen(q: dict) -> dict | None:
        """The card of one screen from All screens, with its group, answered or not."""
        *_, entries = view(q)
        g = next((g for g in all_screens(entries) if any(e["key"] == q.get("key") for e in g)), None)
        return cards([g], shots())[0] if g else None

    ok_hosts: set[str] = set()  # filled in once the port is known

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if not self._our_host():
                return self._send(403, b"", "text/plain")
            url = urlsplit(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif url.path == "/api/hello":
                self._json(about())
            elif url.path in ("/api/data", "/api/screen"):
                # A tab from before Qualm was updated: it reloads (or, a page from before
                # versions, which asked with no query, shows this in place of its tabs).
                if not url.query or q.get("page", page_version) != page_version:
                    return self._json({"error": UPDATED, "reload": True}, 409)
                try:
                    if url.path == "/api/screen":
                        return self._json({"card": screen(q)})
                    v = version(q)
                    self._json({"same": True} if q.get("v") == v else payload(q) | {"v": v})
                except Exception as e:  # say so on the page
                    self._json({"error": str(e)}, 500)
            elif self.path.startswith("/shots/"):
                name = Path(self.path).name
                f = data_dir / "shots" / name
                if re.fullmatch(r"\d+\.jpg", name) and f.exists():
                    self._send(200, f.read_bytes(), "image/jpeg")
                else:
                    self._send(404, b"", "text/plain")
            else:
                self._send(404, b"", "text/plain")

        def _our_host(self) -> bool:
            """A page on another domain that resolves to 127.0.0.1 (DNS rebinding)
            sends its own name as Host: it may not read, let alone change, anything."""
            return self.headers.get("Host") in ok_hosts

        def _trusted(self) -> bool:
            """Only this page may change things. A site open in your browser can
            send a request to 127.0.0.1 too; it can't send JSON without asking
            first (CORS), and can't pass for this host."""
            origin = self.headers.get("Origin")
            return (self._our_host() and (origin is None or origin.removeprefix("http://") in ok_hosts)
                    and self.headers.get("Content-Type", "").startswith("application/json"))

        def do_POST(self):
            if not self._trusted():
                return self._json({"error": "not from the dashboard"}, 403)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.headers.get("X-Qualm-Page") != page_version:  # a tab from before Qualm was updated: change nothing
                    return self._json({"error": UPDATED, "reload": True}, 409)
                if self.path == "/api/review":
                    # One judgement, or a group of similar ones answered together.
                    ids = body.pop("ids", None) or [body.pop("id")]
                    for jid in ids:
                        save_review(data_dir, jid, **{k: (dict(v) if isinstance(v, dict) else v) for k, v in body.items()})
                elif self.path == "/api/threshold":
                    set_threshold(rules_path, body["rule"], float(body["value"]))
                elif self.path == "/api/never/undo":
                    undo_never(data_dir, body["place"])
                elif self.path == "/api/focus":
                    from .policy import end_focus, start_focus

                    if body.get("stop"):
                        end_focus(data_dir)
                    else:
                        start_focus(data_dir, str(body.get("intent", "")), float(body.get("minutes", 50)))
                elif self.path == "/api/pause":
                    from .policy import pause_for

                    minutes = float(body.get("minutes", 0))
                    if not 0 <= minutes <= 24 * 60:
                        raise ValueError("pause for up to 24 hours")
                    pause_for(data_dir, minutes)
                elif self.path == "/api/rule/enabled":
                    from .config import Config

                    Config(rules_path).edit("rules", str(body["rule"]), {} if body.get("on") else {"enabled": False},
                                            ["enabled"] if body.get("on") else [])
                elif self.path == "/api/reflect":
                    reflect(data_dir, str(body["week"]), str(body["answer"]), str(body.get("note", "")))
                elif self.path == "/api/exception":
                    add_exception(rules_path, data_dir, str(body["rule"]), str(body.get("text", "")))
                elif self.path == "/api/exception/remove":
                    remove_exception(rules_path, data_dir, str(body["rule"]), str(body["text"]))
                else:
                    return self._send(404, b"", "text/plain")
                self._json({"ok": True})  # the page asks for what it shows again
            except Exception as e:
                # str(KeyError) comes in quotes: its message is the first argument.
                self._json({"error": str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)}, 400)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    real = server.server_address[1]
    ok_hosts.update({f"127.0.0.1:{real}", f"localhost:{real}"})
    server.page_version = page_version  # what the page sends back with each request (X-Qualm-Page on changes)
    return server


PAGE_FILE = Path(__file__).with_name("dashboard.html")  # the page: Today, Review, All screens, Insights, Rules
