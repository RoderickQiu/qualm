"""Your verdicts on what SeeNot did, and a local page to give them.

`seenot-desktop review --web` serves http://127.0.0.1:8765: one card per
screen with its screenshot, what SeeNot did and why, what the model read,
and each rule's score against its threshold. You answer "was SeeNot right?"
and, per rule, "is this X?". From those answers the page suggests
thresholds (from the logged scores, no model calls) and applies them to
rules.toml, which the running app reloads.

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
    """Rewrite one rule's `threshold = ...` line in rules.toml, keeping everything else."""
    text = rules_path.read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^(?=\[\[rules\]\])", text)
    for i, block in enumerate(blocks):
        if re.search(rf'(?m)^id\s*=\s*"{re.escape(rule_id)}"\s*$', block):
            stamp = f"threshold = {value:g}  # set from review, {datetime.now():%Y-%m-%d}"
            if re.search(r"(?m)^threshold\s*=", block):
                block = re.sub(r"(?m)^threshold\s*=.*$", stamp, block, count=1)
            else:
                block = re.sub(r"(?m)^(kind\s*=.*)$", rf"\1\n{stamp}", block, count=1)
            blocks[i] = block
            new = "".join(blocks)
            load_config_text(new)  # refuse to write a file that no longer parses
            rules_path.write_text(new, encoding="utf-8")
            return
    raise ValueError(f"no rule {rule_id!r} in {rules_path}")


def load_config_text(text: str) -> None:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as f:
        f.write(text)
    try:
        load_config(f.name)
    finally:
        Path(f.name).unlink()


def add_exception(data_dir: Path, rule_id: str, text: str) -> None:
    e = {"rule": rule_id, "text": text.strip(), "at": datetime.now().isoformat(timespec="seconds")}
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "exceptions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def week(data_dir: Path, days: int = 7) -> dict:
    """The last `days` days: pop-ups shown per rule, what you did with them,
    minutes and visits per time-capped rule."""
    from datetime import date, timedelta

    today = date.today()
    span = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    popups = {d: {} for d in span}
    for j in load_judgements(data_dir):
        day = j["at"][:10]
        if day in popups:
            for d in j["decisions"]:
                if d["action"] == "intervene":
                    popups[day][d["rule"]] = popups[day].get(d["rule"], 0) + 1
    answers = {"back": 0, "snooze": 0, "fine": 0, "never": 0}
    reasons = []
    path = data_dir / "decisions.jsonl"
    if path.exists():
        for line in path.open(encoding="utf-8"):
            e = json.loads(line)
            if e.get("type") == "response" and e["at"][:10] in popups and e.get("response") in answers:
                answers[e["response"]] += 1
                if e.get("reason"):
                    reasons.append({"at": e["at"], "rule": e.get("rule", ""), "reason": e["reason"]})
    hist_path = data_dir / "usage_history.json"
    history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}
    usage = {d: history.get(d, {}) for d in span}
    return {"days": span, "popups": popups, "answers": answers, "reasons": reasons[-20:], "usage": usage}


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
        settings, rules = load_config(rules_path)
        return {
            "judgements": load_judgements(data_dir),
            "reviews": load_reviews(data_dir),
            "rules": [{"id": r.id, "kind": r.kind, "text": r.text(settings.lang), "threshold": r.threshold,
                       "exceptions": list(r.exceptions)} for r in rules],
            "exceptions": [e for e in exceptions(data_dir) if not e.get("never")],
            "never": never_places(data_dir),
            "week": week(data_dir),
            "tuning": tuning(data_dir, rules),
            "budgets": settings.budgets,
        }

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
            if self.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/data":
                self._json(payload())
            elif self.path.startswith("/shots/"):
                name = Path(self.path).name
                f = data_dir / "shots" / name
                if re.fullmatch(r"\d+\.jpg", name) and f.exists():
                    self._send(200, f.read_bytes(), "image/jpeg")
                else:
                    self._send(404, b"", "text/plain")
            else:
                self._send(404, b"", "text/plain")

        def do_POST(self):
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
                elif self.path == "/api/exception":
                    if not body.get("text", "").strip():
                        raise ValueError("empty exception")
                    add_exception(data_dir, body["rule"], body["text"])
                else:
                    return self._send(404, b"", "text/plain")
                self._json(payload())
            except Exception as e:
                self._json({"error": str(e)}, 400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>SeeNot review</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { --bg:#f5f5f3; --card:#fff; --ink:#1d1d1f; --mute:#6e6e73; --line:#e2e2de;
  --red:#c8372d; --redbg:#fbeae8; --amber:#9a5700; --amberbg:#fcf0dc; --blue:#1f5fbf; --bluebg:#e7effb;
  --green:#1e7b3c; --greenbg:#e4f3e9; }
* { box-sizing:border-box; }
body { margin:0; font:15px/1.45 -apple-system, "PingFang SC", system-ui, sans-serif; background:var(--bg); color:var(--ink); }
nav { display:flex; align-items:center; gap:4px; padding:10px 20px; border-bottom:1px solid var(--line); background:#fff; position:sticky; top:0; z-index:5; }
nav b { margin-right:16px; }
nav a { padding:6px 12px; border-radius:8px; color:var(--mute); text-decoration:none; cursor:pointer; }
nav a.on { background:var(--ink); color:#fff; }
nav .right { margin-left:auto; font-size:12px; color:var(--mute); }
.wrap { max-width:1180px; margin:0 auto; padding:20px; }
.muted { color:var(--mute); } .small { font-size:13px; }
kbd { font:12px ui-monospace, monospace; border:1px solid var(--line); border-bottom-width:2px; border-radius:4px; padding:0 5px; background:#fff; color:var(--mute); }
button { font:inherit; border:1px solid var(--line); background:#fff; border-radius:9px; padding:8px 14px; cursor:pointer; }
button:hover { border-color:#b5b5b0; }

/* review: one screen at a time */
.progress { display:flex; align-items:center; gap:12px; margin-bottom:14px; }
.progress .track { flex:1; height:6px; background:#e8e8e4; border-radius:3px; overflow:hidden; }
.progress .fill { height:100%; background:var(--green); }
.one { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(0,1fr); gap:22px; background:var(--card); border:1px solid var(--line); border-radius:14px; padding:18px; }
.one img { width:100%; border-radius:10px; border:1px solid var(--line); display:block; cursor:zoom-in; }
.noshot { aspect-ratio:16/10; border-radius:10px; background:#efefec; display:flex; align-items:center; justify-content:center; color:var(--mute); }
.title { font-size:18px; font-weight:650; overflow-wrap:anywhere; }
.url { color:var(--mute); font-size:13px; overflow-wrap:anywhere; margin-top:2px; }
.said { margin:16px 0; padding:12px 14px; border-radius:10px; font-size:16px; }
.said.intervene { background:var(--redbg); color:var(--red); }
.said.allow { background:var(--amberbg); color:var(--amber); }
.said.count { background:var(--bluebg); color:var(--blue); }
.said.none { background:#efefec; color:#444; }
.said .why { display:block; font-size:13px; opacity:.85; margin-top:3px; }
.q { font-weight:650; margin:18px 0 8px; }
.choices { display:flex; gap:8px; flex-wrap:wrap; }
.choices button { font-size:15px; padding:10px 16px; }
.choices .right.sel { background:var(--greenbg); border-color:var(--green); color:var(--green); }
.choices .wrong.sel { background:var(--redbg); border-color:var(--red); color:var(--red); }
.chips { display:flex; flex-direction:column; gap:6px; margin-top:6px; }
.chip { display:flex; align-items:center; gap:10px; text-align:left; padding:8px 12px; border-radius:9px; }
.chip.sel { background:var(--redbg); border-color:var(--red); }
.chip.none.sel { background:var(--greenbg); border-color:var(--green); }
.chip .name { font-weight:600; } .chip .desc { font-size:12px; color:var(--mute); }
.chip kbd { flex:none; }
.save { margin-top:12px; display:flex; gap:8px; align-items:center; }
.save .go { background:var(--ink); color:#fff; border-color:var(--ink); }
details { margin-top:18px; } summary { cursor:pointer; color:var(--mute); font-size:13px; }
.scores { width:100%; border-collapse:collapse; font-size:13px; margin-top:8px; }
.scores td { padding:3px 4px; }
.meter { position:relative; height:7px; background:#eeeeea; border-radius:4px; width:130px; }
.meter .f { position:absolute; left:0; top:0; bottom:0; border-radius:4px; background:#9aa0a6; }
.meter .f.over { background:var(--red); }
.meter .t { position:absolute; top:-3px; bottom:-3px; width:2px; background:var(--ink); }
pre { background:#f5f5f3; border-radius:8px; padding:8px; white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px; max-height:220px; overflow:auto; }
.done { text-align:center; padding:60px 20px; background:#fff; border:1px solid var(--line); border-radius:14px; }
.done h2 { margin:0 0 6px; }
.keys { margin-top:14px; font-size:12px; color:var(--mute); }
.similar { margin:-6px 0 6px; } .similar summary { color:var(--ink); font-size:14px; }

/* all screens: one line each */
.opts { display:flex; gap:10px; align-items:center; margin-bottom:10px; flex-wrap:wrap; }
.opts input[type=search] { border:1px solid var(--line); border-radius:8px; padding:6px 10px; min-width:240px; font:inherit; font-size:14px; }
table.list { width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:12px; overflow:hidden; font-size:14px; }
table.list td { padding:6px 10px; border-top:1px solid #efefec; vertical-align:middle; }
table.list tr { cursor:pointer; } table.list tr:hover td { background:#fafaf8; }
table.list img { width:64px; height:40px; object-fit:cover; border-radius:5px; display:block; }
table.list .t { max-width:460px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.tag { display:inline-block; border-radius:6px; padding:1px 8px; font-size:12px; font-weight:600; white-space:nowrap; }
.tag.intervene { background:var(--redbg); color:var(--red); } .tag.allow { background:var(--amberbg); color:var(--amber); }
.tag.count { background:var(--bluebg); color:var(--blue); } .tag.none, .tag.skip { background:#efefec; color:var(--mute); }
.tag.right { background:var(--greenbg); color:var(--green); } .tag.wrong { background:var(--redbg); color:var(--red); }

/* tune */
.panel { background:#fff; border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:16px; }
.panel h2 { font-size:16px; margin:0 0 10px; }
table.tune { width:100%; border-collapse:collapse; font-size:14px; }
table.tune td, table.tune th { padding:8px; border-top:1px solid #efefec; text-align:left; vertical-align:top; }
table.tune th { font-size:12px; color:var(--mute); font-weight:500; border-top:0; }
textarea, select { font:inherit; font-size:14px; border:1px solid var(--line); border-radius:8px; padding:6px; }
textarea { width:100%; }
#zoom { position:fixed; inset:0; background:rgba(0,0,0,.82); display:none; align-items:center; justify-content:center; z-index:10; cursor:zoom-out; }
#zoom img { max-width:94vw; max-height:94vh; border-radius:8px; }
.toast { position:fixed; bottom:18px; left:50%; transform:translateX(-50%); background:var(--ink); color:#fff; padding:8px 14px; border-radius:8px; font-size:13px; display:none; z-index:20; }
</style></head><body>
<nav>
  <b>SeeNot</b>
  <a data-tab="review">Review <span id="nleft"></span></a>
  <a data-tab="all">All screens</a>
  <a data-tab="week">This week</a>
  <a data-tab="tune">Tune rules</a>
  <span class="right" id="mode"></span>
</nav>
<div class="wrap" id="view"></div>
<div id="zoom"><img alt=""></div>
<div class="toast" id="toast"></div>
<script>
"use strict";
let D = null;
let tab = "review", pos = 0, quiet = false, search = "", listFilter = "all";
let pick = null;      // while answering "wrong": the set of rule ids the page really is
let showDetails = false;
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function toast(t) { const el = $("#toast"); el.textContent = t; el.style.display = "block"; clearTimeout(el.tm); el.tm = setTimeout(() => { el.style.display = "none"; }, 1600); }

async function load() { D = await (await fetch("/api/data")).json(); render(); }
async function post(path, body) {
  const r = await (await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)})).json();
  if (r.error) { toast(r.error); return false; }
  D = r; return true;
}

// ---- model of the data -------------------------------------------------
function ruleName(r) {
  const t = r.text || "";
  const cut = t.split(/[,:，：]/)[0];
  return cut.length > 60 ? cut.slice(0, 57) + "…" : cut;
}
function outcome(j) {
  const a = j.decisions.map(d => d.action);
  for (const k of ["intervene", "allow", "count", "skip"]) if (a.includes(k)) return k;
  return "none";
}
function nearMiss(j) {
  if (!j.p_hit) return 0;
  let m = 0;
  for (const rid in j.p_hit) {
    const t = (j.thresholds || {})[rid] || 0.5;
    m = Math.max(m, j.p_hit[rid] / t);
  }
  return m;
}
// One entry per page (URL, or app + title): the latest detailed judgement of it.
function screens() {
  const by = new Map();
  for (const j of D.judgements) {
    if (j.screen.window_title === "SeeNot" && !j.screen.url) continue;  // SeeNot's own panel
    const key = j.screen.url || (j.screen.app + "|" + (j.screen.window_title || ""));
    const e = by.get(key) || {key, items:[]};
    e.items.push(j);
    by.set(key, e);
  }
  const out = [];
  for (const e of by.values()) {
    const detailed = e.items.filter(j => j.p_hit);
    e.j = detailed.length ? detailed[detailed.length - 1] : e.items[e.items.length - 1];
    e.review = null;
    for (const j of e.items) if (D.reviews[j.id]) e.review = D.reviews[j.id];
    e.first = e.items[0].at; e.last = e.items[e.items.length - 1].at;
    e.shot = e.j.shot || (e.items.slice().reverse().find(j => j.shot) || {}).shot;
    out.push(e);
  }
  return out;
}
// Similar screens: the same site and kind of page, with ids blanked out
// (youtube.com/shorts/*, reddit.com/r/*/comments/*), and the same outcome.
function looksLikeId(seg) { return /\d/.test(seg) || seg.length >= 12 || /^[A-Za-z0-9_-]{8,}$/.test(seg) && /[A-Z]/.test(seg) && /[a-z]/.test(seg); }
function simKey(e) {
  const j = e.j;
  let where = j.screen.app;
  if (j.screen.url && /^https?:/.test(j.screen.url)) {
    try {
      const u = new URL(j.screen.url);
      const segs = u.pathname.split("/").filter(Boolean).slice(0, 4).map(x => looksLikeId(x) ? "*" : x);
      // Subreddits, profiles and channels vary but are the same kind of page.
      if (segs[0] === "r" && segs.length > 1) segs[1] = "*";
      where = u.hostname.replace(/^www\./, "") + "/" + segs.join("/");
    } catch (err) { where = j.screen.url; }
  }
  const acts = j.decisions.filter(d => d.rule).map(d => d.action + ":" + d.rule).sort().join(",");
  return where + "|" + outcome(j) + "|" + acts;
}
function similarGroups(list) {
  const by = new Map();
  for (const e of list) { const k = simKey(e); if (!by.has(k)) by.set(k, []); by.get(k).push(e); }
  return [...by.values()].map(members => {
    members.sort((a, b) => (b.last < a.last ? -1 : 1));
    return {key: members[0].key, rep: members[0], members: members};
  });
}
const PRIORITY = {intervene:0, allow:1, count:2, none:3, skip:4};
function queue() {
  const open = screens()
    .filter(e => e.j.p_hit && !(e.review && e.review.verdict))
    .filter(e => quiet || outcome(e.j) !== "none" || nearMiss(e.j) >= 0.5);
  // Within each outcome, closest calls first: your answer there moves a
  // threshold the most. URL-pattern hits are certain, so they go last.
  return similarGroups(open)
    .sort((a, b) => (PRIORITY[outcome(a.rep.j)] - PRIORITY[outcome(b.rep.j)]) ||
                    (closeness(a.rep.j) - closeness(b.rep.j)) || (b.members.length - a.members.length) ||
                    (b.rep.last < a.rep.last ? -1 : 1));
}
// How far the nearest rule is from its threshold, on a log scale (0 = right on it).
function closeness(j) {
  if (!j.p_hit) return 99;
  const byPattern = new Set(j.decisions.filter(d => d.reason === "matches URL pattern").map(d => d.rule));
  let best = 99;
  for (const rid in j.p_hit) {
    if (byPattern.has(rid)) continue;
    const t = (j.thresholds || {})[rid] || 0.5;
    best = Math.min(best, Math.abs(Math.log(Math.max(j.p_hit[rid], 1e-3) / t)));
  }
  return best;
}

// ---- plain-language sentences -----------------------------------------
function said(j) {
  const o = outcome(j);
  const ds = j.decisions.filter(d => d.action === o);
  const rules = ds.map(d => "<b>" + esc(d.rule) + "</b>").join(", ");
  const why = ds.map(d => esc(d.reason)).filter(Boolean).join("; ");
  if (o === "intervene") return {cls:"intervene", text:"SeeNot popped up: this looks like " + rules, why:"because " + why};
  if (o === "allow") {
    let w = why;
    if (why.includes("opened on purpose") && j.came_from) w += " (you came from a " + esc(j.came_from.page_kind) + " page)";
    return {cls:"allow", text:"SeeNot let it through: it matched " + rules + " but an exemption applied", why:w};
  }
  if (o === "count") return {cls:"count", text:"SeeNot counted time toward " + rules, why:why};
  if (o === "skip") return {cls:"none", text:"Not judged", why:why};
  const top = j.p_hit ? Object.entries(j.p_hit).sort((a, b) => b[1] - a[1])[0] : null;
  return {cls:"none", text:"SeeNot did nothing", why: top ? "closest rule: " + esc(top[0]) + " at " + top[1].toFixed(2) + " (needs " + ((j.thresholds || {})[top[0]]) + ")" : ""};
}
function flagged(j) { return new Set(j.decisions.filter(d => d.action === "intervene" || d.action === "count" || d.action === "allow").map(d => d.rule)); }

// ---- review tab -------------------------------------------------------
function reviewView() {
  const q = queue();
  $("#nleft").textContent = q.length ? "(" + q.length + ")" : "";
  if (!q.length) {
    return '<div class="done"><h2>Nothing left to review</h2><div class="muted">New screens show up here as you browse.</div>' +
      '<div style="margin-top:14px"><label class="small"><input type="checkbox" id="quiet" ' + (quiet ? "checked" : "") +
      '> also review screens where SeeNot stayed quiet</label></div></div>';
  }
  if (pos >= q.length) pos = q.length - 1;
  if (pos < 0) pos = 0;
  const g = q[pos], e = g.rep, j = e.j, s = said(j);
  const total = screens().filter(x => x.j.p_hit).length;
  const reviewed = screens().filter(x => x.review && x.review.verdict).length;
  const img = e.shot ? '<img src="/shots/' + e.shot + '" alt="">' : '<div class="noshot">no screenshot for this one</div>';
  const times = e.items.length > 1 ? " · seen " + e.items.length + "× from " + e.first.slice(11, 16) + " to " + e.last.slice(11, 16) : " · " + e.first.slice(11, 16);
  let h = '<div class="progress"><span class="small muted">' + (pos + 1) + " of " + q.length + " to review</span>" +
    '<div class="track"><div class="fill" style="width:' + (total ? Math.round(100 * reviewed / total) : 0) + '%"></div></div>' +
    '<span class="small muted">' + reviewed + " reviewed</span>" +
    '<label class="small muted"><input type="checkbox" id="quiet" ' + (quiet ? "checked" : "") + "> include quiet screens</label></div>";
  h += '<div class="one"><div>' + img + '</div><div>';
  h += '<div class="title">' + esc(j.screen.window_title || j.screen.app) + '</div>';
  h += '<div class="url">' + esc(j.screen.app) + (j.screen.url ? " · " + esc(j.screen.url) : "") + esc(times) + "</div>";
  h += '<div class="said ' + s.cls + '">' + s.text + (s.why ? '<span class="why">' + s.why + "</span>" : "") + "</div>";
  if (g.members.length > 1) {
    h += '<details class="similar"><summary><b>+' + (g.members.length - 1) + " similar " + (g.members.length === 2 ? "page" : "pages") +
      "</b> with the same result. Your answer applies to all " + g.members.length + ".</summary><div class=\"small muted\" style=\"margin-top:6px\">" +
      g.members.slice(0, 25).map(m => "· " + esc(m.j.screen.window_title || m.j.screen.url)).join("<br>") +
      (g.members.length > 25 ? "<br>…and " + (g.members.length - 25) + " more" : "") + "</div></details>";
  }
  if (pick === null) {
    h += '<div class="q">Was that right?</div><div class="choices">' +
      '<button class="right" data-act="right">✓ Right <kbd>R</kbd></button>' +
      '<button class="wrong" data-act="wrong">✗ Wrong <kbd>W</kbd></button>' +
      '<button data-act="skip">Skip <kbd>S</kbd></button></div>';
  } else {
    h += '<div class="q">What is this page, really? <span class="small muted">(pick all that apply)</span></div><div class="chips">';
    D.rules.forEach((r, i) => {
      h += '<button class="chip ' + (pick.has(r.id) ? "sel" : "") + '" data-pick="' + r.id + '"><kbd>' + (i + 1) + '</kbd><span><span class="name">' +
        esc(r.id) + '</span> <span class="desc">' + esc(ruleName(r)) + "</span></span></button>";
    });
    h += '<button class="chip none ' + (pick.size === 0 ? "sel" : "") + '" data-pick=""><kbd>0</kbd><span><span class="name">None of these</span> ' +
      '<span class="desc">SeeNot should leave this page alone</span></span></button></div>' +
      '<div class="save"><button class="go" data-act="save">Save <kbd>Enter</kbd></button><button data-act="cancel">Cancel <kbd>Esc</kbd></button></div>';
  }
  h += '<details id="det" ' + (showDetails ? "open" : "") + '><summary>Details: scores and what the model read <kbd>D</kbd></summary>' + detailsHtml(j) + "</details>";
  h += '<div class="keys"><kbd>←</kbd> <kbd>→</kbd> previous / next · click the screenshot to enlarge</div>';
  h += "</div></div>";
  return h;
}
function detailsHtml(j) {
  if (!j.p_hit) return "";
  let h = '<table class="scores">';
  Object.entries(j.p_hit).sort((a, b) => b[1] - a[1]).forEach(([rid, p]) => {
    const t = (j.thresholds || {})[rid] || 0.5;
    h += "<tr><td>" + esc(rid) + '</td><td><div class="meter"><div class="f ' + (p >= t ? "over" : "") + '" style="width:' + Math.min(100, p * 100) +
      '%"></div><div class="t" style="left:' + (t * 100) + '%"></div></div></td><td class="small muted">' + p.toFixed(2) + " / needs " + t + "</td></tr>";
  });
  h += "</table>";
  const probs = o => o ? Object.entries(o).sort((a, b) => b[1] - a[1]).map(([k, v]) => k + " " + v.toFixed(2)).join(", ") : "";
  h += '<div class="small muted" style="margin-top:8px">page: ' + probs(j.page_probs) + "<br>purpose: " + probs(j.purpose_probs) +
    "<br>came from: " + (j.came_from ? esc(j.came_from.page_kind + " · " + j.came_from.key) : "—") + " · opened on purpose: " + (j.opened_on_purpose ? "yes" : "no") +
    " · " + j.latency_ms + " ms · #" + j.id + "</div>";
  h += "<pre>" + esc(JSON.stringify(j.state, null, 2)) + "</pre>";
  return h;
}
async function answer(kind) {
  const q = queue(); const g = q[pos]; if (!g) return;
  const e = g.rep, ids = g.members.map(m => m.j.id);
  if (kind === "skip") { pos++; pick = null; render(); return; }
  if (kind === "right") {
    if (await post("/api/review", {ids:ids, verdict:"right"})) { toast("✓ saved" + (ids.length > 1 ? " for " + ids.length + " pages" : "")); pick = null; render(); }
    return;
  }
  if (kind === "wrong") { pick = new Set(flagged(e.j)); render(); return; }
  if (kind === "cancel") { pick = null; render(); return; }
  if (kind === "save") {
    // You said what the page is: yes for those rules, no for every other.
    const rules = {};
    D.rules.forEach(r => { rules[r.id] = pick.has(r.id) ? "yes" : "no"; });
    const popped = outcome(e.j) === "intervene";
    const verdict = popped && pick.size === 0 ? "should_not_block" : !popped && pick.size ? "should_block" : "wrong";
    if (await post("/api/review", {ids:ids, verdict:verdict, rules:rules})) { toast("✗ saved" + (ids.length > 1 ? " for " + ids.length + " pages" : "")); pick = null; render(); }
  }
}

// ---- all screens tab ----------------------------------------------------
function allView() {
  let es = similarGroups(screens()).map(g => Object.assign({}, g.rep, {similar: g.members.length})).sort((a, b) => (b.last < a.last ? -1 : 1));
  if (search) es = es.filter(e => (e.j.screen.window_title + " " + e.j.screen.url + " " + e.j.screen.app).toLowerCase().includes(search));
  if (listFilter !== "all") es = es.filter(e => listFilter === "wrong" ? (e.review && e.review.verdict && e.review.verdict !== "right")
                                                  : listFilter === "right" ? (e.review && e.review.verdict === "right") : outcome(e.j) === listFilter);
  const F = [["all","All"],["intervene","Popped up"],["allow","Allowed"],["count","Counted"],["none","Nothing"],["wrong","You said wrong"],["right","You said right"]];
  let h = '<div class="opts"><input type="search" id="search" placeholder="Search title, URL, app…" value="' + esc(search) + '"> ' +
    F.map(([k, l]) => '<button data-lf="' + k + '" style="' + (listFilter === k ? "background:#1d1d1f;color:#fff;border-color:#1d1d1f" : "") + '">' + l + "</button>").join(" ") +
    ' <span class="small muted">' + es.length + " kinds of screen</span></div>";
  h += '<table class="list">' + es.slice(0, 400).map(e => {
    const j = e.j, o = outcome(j), rv = e.review;
    const rules = j.decisions.filter(d => d.rule).map(d => d.rule).join(", ");
    const you = rv && rv.verdict ? '<span class="tag ' + (rv.verdict === "right" ? "right" : "wrong") + '">you: ' + (rv.verdict === "right" ? "right" : "wrong") + "</span>" : "";
    return '<tr data-key="' + esc(e.key) + '"><td>' + (e.shot ? '<img src="/shots/' + e.shot + '" loading="lazy" alt="">' : "") + "</td>" +
      '<td class="small muted">' + e.last.slice(5, 16).replace("T", " ") + "</td>" +
      '<td class="t"><b>' + esc(j.screen.window_title || j.screen.app) + '</b>' + (e.similar > 1 ? ' <span class="small muted">+' + (e.similar - 1) + " similar</span>" : "") + '<br><span class="small muted">' + esc(j.screen.url || j.screen.app) + "</span></td>" +
      '<td><span class="tag ' + o + '">' + ({intervene:"popped up", allow:"allowed", count:"counted", none:"nothing", skip:"not judged"})[o] + (rules ? ": " + esc(rules) : "") + "</span></td>" +
      "<td>" + you + "</td></tr>";
  }).join("") + "</table>";
  return h;
}

// ---- this week ----------------------------------------------------------
function weekView() {
  const W = D.week, rules = D.rules.map(r => r.id);
  const total = W.days.reduce((n, d) => n + Object.values(W.popups[d]).reduce((a, b) => a + b, 0), 0);
  const a = W.answers, answered = a.back + a.snooze + a.fine + a.never;
  const pct = n => answered ? Math.round(100 * n / answered) + "%" : "–";
  const byRule = {};
  W.days.forEach(d => { for (const r in W.popups[d]) byRule[r] = (byRule[r] || 0) + W.popups[d][r]; });
  const top = Object.entries(byRule).sort((x, y) => y[1] - x[1])[0];
  let h = '<div class="panel"><h2>The last 7 days</h2><div style="font-size:16px">' + total + " pop-ups" +
    (top ? ", most for <b>" + esc(top[0]) + "</b> (" + top[1] + ")" : "") + ".</div>" +
    '<div class="small muted" style="margin-top:6px">You went back ' + a.back + "× (" + pct(a.back) + "), said you needed it " + a.snooze + "× (" + pct(a.snooze) +
    "), said it was wrong " + (a.fine + a.never) + "× (" + pct(a.fine + a.never) + ")." +
    (a.snooze > a.back ? " You snoozed more than you went back: maybe a rule is too strict, or a budget too small." : "") +
    ((a.fine + a.never) > answered / 3 && answered >= 6 ? " A third or more were wrong: review them and let the thresholds re-tune." : "") + "</div></div>";
  h += '<div class="panel"><h2>Pop-ups per day</h2><table class="tune"><tr><th>Day</th>' + rules.map(r => "<th>" + esc(r) + "</th>").join("") + "</tr>" +
    W.days.map(d => "<tr><td>" + d.slice(5) + "</td>" + rules.map(r => "<td>" + (W.popups[d][r] || "") + "</td>").join("") + "</tr>").join("") + "</table></div>";
  const capped = D.rules.filter(r => r.kind === "time_cap").map(r => r.id);
  if (capped.length) {
    h += '<div class="panel"><h2>Time on time-capped rules (minutes)</h2><div class="small muted" style="margin-bottom:6px">' +
      (D.budgets ? "" : "Testing mode is on, so these don't count yet: every hit pops up instead. ") + "Time away from the screen isn't counted.</div>" +
      '<table class="tune"><tr><th>Day</th>' + capped.map(r => "<th>" + esc(r) + "</th>").join("") + "</tr>" +
      W.days.map(d => "<tr><td>" + d.slice(5) + "</td>" + capped.map(r => { const u = W.usage[d][r]; return "<td>" + (u && u.seconds ? Math.round(u.seconds / 60) : "") + "</td>"; }).join("") + "</tr>").join("") + "</table></div>";
  }
  if (W.reasons.length) h += '<div class="panel"><h2>What you needed it for</h2>' + W.reasons.slice().reverse().map(x =>
    '<div class="small"><span class="muted">' + x.at.slice(5, 16).replace("T", " ") + " · " + esc(x.rule) + "</span> " + esc(x.reason) + "</div>").join("") + "</div>";
  return h;
}

// ---- tune tab -----------------------------------------------------------
function tuneView() {
  let h = '<div class="panel"><h2>Thresholds, from your answers</h2><div class="small muted" style="margin-bottom:8px">A rule fires when its score reaches the threshold. ' +
    "Suggestions need at least 3 “yes” and 3 “no” answers for that rule, and aim for 9 in 10 pop-ups being right.</div>";
  h += '<table class="tune"><tr><th>Rule</th><th>Threshold</th><th>Your answers</th><th>Now</th><th>Suggested</th></tr>';
  D.tuning.forEach(t => {
    const r = D.rules.find(x => x.id === t.rule) || {};
    const now = t.precision == null && t.recall == null ? "—" : "right " + (t.precision == null ? "–" : Math.round(t.precision * 100) + "%") +
      " of pop-ups, catches " + (t.recall == null ? "–" : Math.round(t.recall * 100) + "%");
    const sug = t.suggested == null ? '<span class="small muted">not enough answers yet</span>'
      : "<b>" + t.suggested + '</b> <span class="small muted">→ right ' + Math.round(t.suggested_precision * 100) + "%, catches " + Math.round(t.suggested_recall * 100) + "%</span> " +
        (t.suggested !== t.threshold ? '<button data-apply="' + t.rule + '" data-value="' + t.suggested + '">Apply</button>' : '<span class="small muted">(current)</span>');
    h += "<tr><td><b>" + esc(t.rule) + '</b><div class="small muted">' + esc(ruleName(r)) + "</div></td><td>" + t.threshold + "</td><td>" + t.yes + " yes · " + t.no + " no</td><td class=\"small\">" + now + "</td><td>" + sug + "</td></tr>";
  });
  h += "</table></div>";
  h += '<div class="panel"><h2>Exceptions</h2><div class="small muted" style="margin-bottom:8px">In your own words; the model reads them with the rule. ' +
    "E.g. for videos: “a lecture or conference talk”.</div>" +
    '<div style="display:flex;gap:8px;align-items:flex-start"><select id="excRule">' + D.rules.map(r => '<option value="' + r.id + '">' + esc(r.id) + "</option>").join("") +
    '</select><textarea id="excText" rows="1" placeholder="…is fine"></textarea><button data-act="exc">Add</button></div>';
  D.rules.forEach(r => {
    const xs = r.exceptions.concat(D.exceptions.filter(e => e.rule === r.id && e.text).map(e => e.text));
    if (xs.length) h += '<div class="small" style="margin-top:10px"><b>' + esc(r.id) + "</b>" + xs.map(x => '<div class="muted">· ' + esc(x) + "</div>").join("") + "</div>";
  });
  h += "</div>";
  h += '<div class="panel"><h2>Never here</h2><div class="small muted" style="margin-bottom:8px">Apps and sites where no rule fires, from the pop-up’s “Never here” button.</div>' +
    (D.never.length ? D.never.map((n, i) => '<div style="display:flex;gap:10px;align-items:center;margin:4px 0"><span>' + (n.host ? "on <b>" + esc(n.host) + "</b>" : "in <b>" + esc(n.name || n.app) + "</b>") +
      '</span><button data-undo="' + i + '">Undo</button></div>').join("") : '<div class="small muted">None yet.</div>') + "</div>";
  h += '<div class="panel small muted">Rules themselves (wording, budgets, URL patterns) are in rules.toml, via “Open rules…” in the SeeNot menu. The app picks up changes without a restart.</div>';
  return h;
}

// ---- render and input ---------------------------------------------------
function render() {
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("on", a.dataset.tab === tab));
  $("#mode").textContent = D.budgets ? "budgets on" : "testing mode: every hit pops up";
  const n = queue().length;
  $("#nleft").textContent = n ? "(" + n + ")" : "";
  $("#view").innerHTML = tab === "review" ? reviewView() : tab === "all" ? allView() : tab === "week" ? weekView() : tuneView();
  const s = $("#search"); if (s && document.activeElement !== s && search) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); }
}
document.addEventListener("click", ev => {
  const t = ev.target;
  const a = t.closest("nav a"); if (a) { tab = a.dataset.tab; pick = null; render(); return; }
  if (t.matches(".one img, table.list img")) { $("#zoom img").src = t.src; $("#zoom").style.display = "flex"; ev.stopPropagation(); return; }
  const b = t.closest("button");
  if (b && b.dataset.act === "exc") {
    post("/api/exception", {rule:$("#excRule").value, text:$("#excText").value}).then(ok => { if (ok) { toast("exception added"); render(); } });
    return;
  }
  if (b && b.dataset.act) { answer(b.dataset.act); return; }
  if (b && b.dataset.pick !== undefined) {
    if (b.dataset.pick === "") pick = new Set(); else if (pick.has(b.dataset.pick)) pick.delete(b.dataset.pick); else pick.add(b.dataset.pick);
    render(); return;
  }
  if (b && b.dataset.apply) { post("/api/threshold", {rule:b.dataset.apply, value:b.dataset.value}).then(ok => { if (ok) { toast("rules.toml updated"); render(); } }); return; }
  if (b && b.dataset.lf) { listFilter = b.dataset.lf; render(); return; }
  if (b && b.dataset.undo !== undefined) { post("/api/never/undo", {place:D.never[+b.dataset.undo]}).then(ok => { if (ok) { toast("undone"); render(); } }); return; }
  const row = t.closest("tr[data-key]");
  if (row) {
    // Open that screen in the review view, even if it was already reviewed.
    const e = screens().find(x => x.key === row.dataset.key);
    if (e && e.j.p_hit) { reviewOne(e); }
  }
});
function reviewOne(e) {
  // Put the screen back in the queue by clearing its verdict view-side: show it directly.
  tab = "review"; pick = null; quiet = true;
  const find = qq => qq.findIndex(g => g.members.some(m => m.key === e.key));
  const i = find(queue());
  if (i >= 0) { pos = i; render(); return; }
  // Already reviewed: clear the verdict so it can be answered again.
  post("/api/review", {ids:[e.j.id], verdict:null}).then(() => { pos = Math.max(0, find(queue())); render(); });
}
document.addEventListener("change", ev => { if (ev.target.id === "quiet") { quiet = ev.target.checked; pos = 0; render(); } });
document.addEventListener("input", ev => { if (ev.target.id === "search") { search = ev.target.value.toLowerCase(); render(); } });
document.addEventListener("toggle", ev => { if (ev.target.id === "det") showDetails = ev.target.open; }, true);
$("#zoom").onclick = () => { $("#zoom").style.display = "none"; };
document.addEventListener("keydown", ev => {
  if (tab !== "review" || ev.target.matches("input, textarea, select") || ev.metaKey || ev.ctrlKey) return;
  const k = ev.key.toLowerCase();
  if (pick === null) {
    if (k === "r") answer("right");
    else if (k === "w") answer("wrong");
    else if (k === "s" || k === "arrowright") answer("skip");
    else if (k === "arrowleft") { pos = Math.max(0, pos - 1); render(); }
    else if (k === "d") { showDetails = !showDetails; render(); }
    else return;
  } else {
    if (k === "enter") answer("save");
    else if (k === "escape") answer("cancel");
    else if (k === "0") { pick = new Set(); render(); }
    else if (/^[1-9]$/.test(k) && D.rules[+k - 1]) { const id = D.rules[+k - 1].id; if (pick.has(id)) pick.delete(id); else pick.add(id); render(); }
    else return;
  }
  ev.preventDefault();
});
load();
setInterval(() => { if (pick === null && !document.querySelector("textarea:focus, input:focus, select:focus")) load(); }, 15000);
</script></body></html>
"""
