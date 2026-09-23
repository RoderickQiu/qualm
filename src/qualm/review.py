"""Your verdicts on what Qualm did, and a local page to give them.

`qualm review --web` serves http://127.0.0.1:8765 (the app serves it too):
the dashboard in dashboard.html. Today: focus and pause, the day's pop-ups
and check-in sessions. Review: one card per screen with its screenshot, what Qualm did and why, what the model read,
and each rule's score against its threshold. You answer "was Qualm right?"
and, per rule, "is this X?". From those answers the page suggests
thresholds (from the logged scores, no model calls) and applies them to
rules.toml, which the running app reloads. Insights: how pop-ups ended,
when they happen, what you unlocked time for, check-ins said vs kept, focus sessions. Rules: on and
off, thresholds, exceptions, never-here places.

Verdicts go to data/reviews.jsonl (append-only; the last entry per
judgement wins).
"""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .rules import Rule, load_config

PORT = 8765
VERDICTS = ("right", "wrong", "should_block", "should_not_block")
MIN_EACH = 3  # positives and negatives a rule needs before a threshold is suggested


def load_judgements(data_dir: Path) -> list[dict]:
    path = data_dir / "judgements.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def load_reviews(data_dir: Path) -> dict[str, dict]:
    path = data_dir / "reviews.jsonl"
    out: dict[str, dict] = {}
    if path.exists():
        for line in path.open(encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                out[r["id"]] = r
    return out


def save_review(data_dir: Path, jid: str, **changes) -> dict:
    """Merge changes (verdict, rules={id: yes|no|None}, page_kind, purpose, note) into the review of one judgement."""
    review = load_reviews(data_dir).get(jid, {"id": jid, "rules": {}})
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
    with (data_dir / "reviews.jsonl").open("a", encoding="utf-8") as f:
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


def _pick(pts: list[tuple[float, bool]], target: float = 0.9):
    best = None
    for t in sorted({p for p, _ in pts}):
        tp = sum(p >= t and y for p, y in pts)
        pp = sum(p >= t for p, _ in pts)
        if pp and tp / pp >= target and (best is None or tp > best[1]):
            best = (t, tp)
    if best is None:
        return None
    below = [p for p, y in pts if p < best[0] and not y]
    return round((best[0] + max(below)) / 2, 3) if below else round(best[0], 3)


def _pr(pts, t):
    tp = sum(p >= t and y for p, y in pts)
    pp = sum(p >= t for p, _ in pts)
    ap = sum(y for _, y in pts)
    return (round(tp / pp, 2) if pp else None), (round(tp / ap, 2) if ap else None)


def tuning(data_dir: Path, rules: list[Rule]) -> list[dict]:
    """Per rule, from your reviews and the logged scores: how the current
    threshold does, and the threshold with the best recall at precision 0.9."""
    reviews = load_reviews(data_dir)
    pts: dict[str, list[tuple[float, bool]]] = {r.id: [] for r in rules}
    for j in load_judgements(data_dir):
        if j["id"] not in reviews or "p_hit" not in j:
            continue
        for rid, y in rule_answers(j, reviews[j["id"]]).items():
            if rid in pts and rid in j["p_hit"]:
                pts[rid].append((j["p_hit"][rid], y))
    out = []
    for r in rules:
        p = pts[r.id]
        pos, neg = sum(y for _, y in p), sum(not y for _, y in p)
        cur_p, cur_r = _pr(p, r.threshold)
        row = {"rule": r.id, "threshold": r.threshold, "yes": pos, "no": neg, "precision": cur_p, "recall": cur_r,
               "suggested": None, "suggested_precision": None, "suggested_recall": None}
        if pos >= MIN_EACH and neg >= MIN_EACH:
            t = _pick(p)
            if t is not None:
                sp, sr = _pr(p, t)
                row |= {"suggested": t, "suggested_precision": sp, "suggested_recall": sr}
        out.append(row)
    return out


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


def add_exception(data_dir: Path, rule_id: str, text: str) -> None:
    e = {"rule": rule_id, "text": text.strip(), "at": datetime.now().isoformat(timespec="seconds")}
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "exceptions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


# What you answered a pop-up with; "open": no answer. "session": you checked
# in; "snooze": "I need it", or "5 more" when a session's time was up.
OUTCOMES = ("back", "session", "snooze", "fine", "never")


def insights(data_dir: Path, rules: list[Rule], days: int = 14, weeks: int = 8) -> dict:
    """Everything the Today and Insights tabs draw, from the logs: how each
    pop-up ended, per day; pop-ups per week; when in the week they happen;
    the time you asked for and what for; focus sessions; how often you said
    Qualm was right; check-in sessions, what you said and what you did. No
    streaks, no score."""
    from datetime import date, timedelta

    today = date.today()
    span = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    monday = today - timedelta(days=today.weekday())
    week_starts = [(monday - timedelta(weeks=i)).isoformat() for i in range(weeks - 1, -1, -1)]
    events = _lines(data_dir / "decisions.jsonl")
    answer = {e["id"]: e for e in events if e.get("type") == "response" and e.get("response") in OUTCOMES}
    shown = [e for e in events if e.get("type") == "intervention" and e.get("id") != "demo"]
    per_day = {d: {k: 0 for k in (*OUTCOMES, "open")} for d in span}
    per_rule_day = {d: {} for d in span}
    per_week = {w: 0 for w in week_starts}
    heat = [[0] * 24 for _ in range(7)]  # [weekday][hour]
    oldest_week = week_starts[0]
    for e in shown:
        day = e["at"][:10]
        ended = answer.get(e["id"], {}).get("response", "open")
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
        f["back"] = sum(answer.get(e["id"], {}).get("response") == "back" for e in during)
    # How often Qualm was right, by your own reviews of pop-ups.
    reviews = load_reviews(data_dir)
    right = wrong = 0
    for j in load_judgements(data_dir):
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
                     "ended": answer.get(e["id"], {}).get("response", "open")} for e in shown if e["at"][:10] == span[-1]]
    judged_today = sum(1 for j in load_judgements(data_dir) if j["at"][:10] == span[-1] and "p_hit" in j)
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
    with (data_dir / "reflections.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def session_state(data_dir: Path) -> dict:
    from .policy import read_session

    s = read_session(data_dir)
    return {"paused_until": s["paused_until"], "focus": s["focus"], "now": datetime.now().timestamp()}


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
    with (data_dir / "exceptions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def exceptions(data_dir: Path) -> list[dict]:
    path = data_dir / "exceptions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


# -- the page ---------------------------------------------------------------


def serve(data_dir: Path, rules_path: Path, port: int = PORT, open_browser: bool = True) -> None:
    """Serve the page until Ctrl-C. If the app already serves it, just open it."""
    url = f"http://127.0.0.1:{port}/"
    try:
        server = start_server(data_dir, rules_path, port)
    except OSError:
        print(f"already running (the app serves it): {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    print(f"review page: {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def start_server(data_dir: Path, rules_path: Path, port: int = PORT) -> ThreadingHTTPServer:
    """The review page's server, not yet serving. Raises OSError if the port is taken."""
    data_dir, rules_path = Path(data_dir), Path(rules_path)

    def payload() -> dict:
        from .personalize import live_exceptions, summary
        from .rules import capacity

        settings, rules = load_config(rules_path)
        return {
            "judgements": load_judgements(data_dir),
            "reviews": load_reviews(data_dir),
            "rules": [{"id": r.id, "kind": r.kind, "text": r.text(settings.lang), "threshold": r.threshold,
                       "exceptions": list(r.exceptions)} for r in rules if r.enabled],
            "allrules": [{"id": r.id, "kind": r.kind, "enabled": r.enabled, "active": r.active(), "summary": summary(r, settings),
                          "threshold": r.threshold, "note": r.note} for r in rules],
            "capacity": capacity(settings, rules),
            "exceptions": live_exceptions(data_dir),
            "never": never_places(data_dir),
            "insights": insights(data_dir, rules),
            "session": session_state(data_dir),
            "tuning": tuning(data_dir, rules),
        }

    ok_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

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
            if self.path == "/":
                self._send(200, PAGE_FILE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/data":
                try:
                    self._json(payload())
                except Exception as e:  # a rules.toml that doesn't load: say so on the page
                    self._json({"error": f"{type(e).__name__}: {e}"}, 500)
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
                    if body.get("rule") not in {r.id for r in load_config(rules_path)[1]}:
                        raise ValueError(f"no rule {body.get('rule')!r}")
                    if not body.get("text", "").strip():
                        raise ValueError("empty exception")
                    add_exception(data_dir, body["rule"], body["text"])
                else:
                    return self._send(404, b"", "text/plain")
                self._json(payload())
            except Exception as e:
                self._json({"error": str(e)}, 400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


PAGE_FILE = Path(__file__).with_name("dashboard.html")  # the page: Today, Review, All screens, Insights, Rules
