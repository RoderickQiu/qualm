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
VERDICTS = ("right", "should_block", "should_not_block")
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
            "exceptions": exceptions(data_dir),
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
                    jid = body.pop("id")
                    save_review(data_dir, jid, **body)
                elif self.path == "/api/threshold":
                    set_threshold(rules_path, body["rule"], float(body["value"]))
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
:root { --bg:#f6f6f4; --card:#fff; --ink:#1d1d1f; --mute:#6e6e73; --line:#e3e3e0;
  --red:#c8372d; --redbg:#fbeae8; --amber:#a15c00; --amberbg:#fdf1de; --blue:#1f5fbf; --bluebg:#e7effb;
  --green:#1e7b3c; --greenbg:#e5f4ea; }
* { box-sizing:border-box; }
body { margin:0; font:14px/1.45 -apple-system, "PingFang SC", system-ui, sans-serif; background:var(--bg); color:var(--ink); }
header { position:sticky; top:0; z-index:5; background:rgba(246,246,244,.95); backdrop-filter:blur(8px); border-bottom:1px solid var(--line); padding:12px 24px; }
header h1 { font-size:17px; margin:0 12px 0 0; display:inline; }
.bar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.chip { border:1px solid var(--line); background:#fff; border-radius:999px; padding:4px 11px; cursor:pointer; font-size:13px; }
.chip.on { background:var(--ink); color:#fff; border-color:var(--ink); }
.chip .n { color:var(--mute); margin-left:4px; } .chip.on .n { color:#ccc; }
input[type=search] { border:1px solid var(--line); border-radius:8px; padding:5px 10px; min-width:220px; font:inherit; }
main { display:grid; grid-template-columns:minmax(0,1fr) 340px; gap:20px; padding:20px 24px; align-items:start; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; margin-bottom:14px; display:grid; grid-template-columns:260px minmax(0,1fr); gap:16px; }
.card.wrong { border-color:var(--red); box-shadow:0 0 0 1px var(--red) inset; }
.card.ok { border-color:#bfe0c9; }
.shot { width:260px; border-radius:8px; border:1px solid var(--line); cursor:zoom-in; display:block; background:#eee; }
.noshot { width:260px; height:150px; border-radius:8px; background:#efefec; color:var(--mute); display:flex; align-items:center; justify-content:center; font-size:12px; text-align:center; padding:10px; }
.title { font-weight:600; font-size:15px; overflow-wrap:anywhere; }
.url { color:var(--mute); font-size:12px; overflow-wrap:anywhere; }
.meta { color:var(--mute); font-size:12px; margin:2px 0 8px; }
.pill { display:inline-block; border-radius:6px; padding:3px 9px; font-weight:600; font-size:13px; margin:2px 6px 2px 0; }
.pill.intervene { background:var(--redbg); color:var(--red); }
.pill.allow { background:var(--amberbg); color:var(--amber); }
.pill.count { background:var(--bluebg); color:var(--blue); }
.pill.none, .pill.skip { background:#efefec; color:var(--mute); }
.why { font-weight:400; }
.ask { margin:10px 0 6px; display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.ask b { margin-right:4px; }
button { font:inherit; font-size:13px; border:1px solid var(--line); background:#fff; border-radius:7px; padding:4px 10px; cursor:pointer; }
button:hover { border-color:#b9b9b5; }
button.sel.right { background:var(--greenbg); border-color:var(--green); color:var(--green); }
button.sel.bad { background:var(--redbg); border-color:var(--red); color:var(--red); }
button.sel.yes { background:var(--redbg); border-color:var(--red); color:var(--red); }
button.sel.no { background:var(--greenbg); border-color:var(--green); color:var(--green); }
table.rules { border-collapse:collapse; width:100%; margin-top:6px; font-size:13px; }
table.rules td { padding:4px 6px; border-top:1px solid #f0f0ee; vertical-align:middle; }
table.rules td.q { width:48%; }
.q .rid { font-weight:600; } .q .desc { color:var(--mute); font-size:12px; }
.meter { position:relative; height:8px; background:#eeeeeb; border-radius:4px; width:120px; }
.meter .fill { position:absolute; left:0; top:0; bottom:0; border-radius:4px; background:#9aa0a6; }
.meter .fill.over { background:var(--red); }
.meter .tick { position:absolute; top:-3px; bottom:-3px; width:2px; background:var(--ink); }
.num { font-variant-numeric:tabular-nums; color:var(--mute); font-size:12px; white-space:nowrap; }
.implied { color:var(--mute); font-size:11px; }
details { margin-top:8px; } summary { cursor:pointer; color:var(--mute); font-size:12px; }
pre { background:#f6f6f4; border-radius:8px; padding:8px; white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px; max-height:260px; overflow:auto; }
aside { position:sticky; top:76px; }
.panel { background:#fff; border:1px solid var(--line); border-radius:12px; padding:14px; margin-bottom:14px; }
.panel h2 { font-size:14px; margin:0 0 8px; }
.tune { border-top:1px solid #f0f0ee; padding:8px 0; font-size:13px; }
.tune:first-of-type { border-top:0; }
.tune .rid { font-weight:600; }
.small { font-size:12px; color:var(--mute); }
.exc { font-size:12px; margin:2px 0 0 10px; color:var(--mute); }
textarea { width:100%; font:inherit; font-size:13px; border:1px solid var(--line); border-radius:8px; padding:6px; }
select { font:inherit; font-size:13px; }
#zoom { position:fixed; inset:0; background:rgba(0,0,0,.8); display:none; align-items:center; justify-content:center; z-index:10; cursor:zoom-out; }
#zoom img { max-width:92vw; max-height:92vh; border-radius:8px; }
.empty { color:var(--mute); padding:40px; text-align:center; }
.toast { position:fixed; bottom:18px; left:50%; transform:translateX(-50%); background:var(--ink); color:#fff; padding:8px 14px; border-radius:8px; font-size:13px; display:none; z-index:20; }
</style></head><body>
<header>
  <div class="bar">
    <h1>SeeNot review</h1>
    <span id="filters"></span>
    <input type="search" id="q" placeholder="Search title, URL, app…">
    <span class="small" id="mode"></span>
  </div>
</header>
<main>
  <section id="cards"></section>
  <aside>
    <div class="panel"><h2>How to review</h2>
      <div class="small">For each screen: was SeeNot right? If not, say which rule is what the page really is
      (<b>Yes</b> = this page <i>is</i> that thing). Answers tune the thresholds on the right.
      "Right" also counts as <i>no</i> for every rule that stayed quiet.</div></div>
    <div class="panel"><h2>Thresholds, from your answers</h2><div id="tuning"></div></div>
    <div class="panel"><h2>Add an exception</h2>
      <div class="small" style="margin-bottom:6px">In your own words; the model reads it with the rule. E.g. "a lecture or conference talk on YouTube".</div>
      <select id="excRule"></select>
      <textarea id="excText" rows="2" placeholder="…is fine"></textarea>
      <button id="excAdd">Add</button>
      <div id="excList"></div></div>
  </aside>
</main>
<div id="zoom"><img></div>
<div class="toast" id="toast"></div>
<script>
let D = null, filter = "all", q = "";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function toast(t) { const el = $("#toast"); el.textContent = t; el.style.display = "block"; clearTimeout(el._t); el._t = setTimeout(() => el.style.display = "none", 1800); }

async function load() { D = await (await fetch("/api/data")).json(); render(); }
async function post(path, body) {
  const r = await (await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)})).json();
  if (r.error) { toast(r.error); return; }
  D = r; render();
}

// Consecutive judgements of the same screen with the same outcome are one card.
function groups() {
  const out = [];
  for (const j of D.judgements) {
    const key = [j.screen.app, j.screen.window_title, j.screen.url, JSON.stringify(j.decisions)].join("|");
    const last = out[out.length - 1];
    if (last && last.key === key) { last.items.push(j); continue; }
    out.push({key, items:[j]});
  }
  return out.reverse();
}
function outcome(j) {
  const acts = j.decisions.map(d => d.action);
  if (acts.includes("intervene")) return "intervene";
  if (acts.includes("allow")) return "allow";
  if (acts.includes("count")) return "count";
  if (acts.includes("skip")) return "skip";
  return "none";
}
function reviewOf(g) { for (const j of [...g.items].reverse()) if (D.reviews[j.id]) return [j, D.reviews[j.id]]; return [g.items[g.items.length - 1], null]; }

const LABEL = {intervene:"Popped up", allow:"Allowed (exempt)", count:"Counted", skip:"Not judged", none:"Nothing"};
function verdictLine(j) {
  const ds = j.decisions.filter(d => d.action !== "skip" || d.reason);
  if (!ds.length) return `<span class="pill none">Nothing: no rule reached its threshold</span>`;
  return ds.map(d => {
    let why = d.reason;
    if (d.reason === "opened on purpose" && j.came_from) why += ` (from ${esc(j.came_from.page_kind)}${j.came_from.app && j.came_from.app !== j.screen.bundle_id ? " in another app" : ""})`;
    return `<span class="pill ${d.action}">${LABEL[d.action]}${d.rule ? ": " + esc(d.rule) : ""} <span class="why">· ${esc(why)}</span></span>`;
  }).join("");
}

function ruleRows(j, rv) {
  if (!j.p_hit) return "";
  const answers = (rv && rv.rules) || {};
  const implied = {};
  if (rv && rv.verdict === "right") {
    const acted = Object.fromEntries(j.decisions.filter(d => d.rule).map(d => [d.rule, d.action]));
    for (const rid in j.p_hit) if (!(rid in answers)) {
      if (acted[rid] === "intervene" || acted[rid] === "count") implied[rid] = "yes";
      else if (!(rid in acted)) implied[rid] = "no";
    }
  }
  const rules = Object.fromEntries(D.rules.map(r => [r.id, r]));
  const ids = Object.keys(j.p_hit).sort((a, b) => j.p_hit[b] - j.p_hit[a]);
  return `<table class="rules">` + ids.map(rid => {
    const r = rules[rid] || {text:"", threshold: (j.thresholds || {})[rid] ?? 0.5};
    const p = j.p_hit[rid], t = (j.thresholds || {})[rid] ?? r.threshold;
    const a = answers[rid], imp = implied[rid];
    const ans = j.answers && j.answers[rid] ? Object.entries(j.answers[rid]).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(" · ") : "";
    return `<tr><td class="q"><span class="rid">Is this ${esc(rid)}?</span><div class="desc">${esc(r.text)}</div></td>
      <td><div class="meter" title="${esc(ans)}"><div class="fill ${p >= t ? "over" : ""}" style="width:${Math.min(100, p * 100)}%"></div><div class="tick" style="left:${t * 100}%"></div></div>
      <div class="num">score ${p.toFixed(2)} · threshold ${t}</div></td>
      <td style="white-space:nowrap"><button class="${a === "yes" ? "sel yes" : ""}" data-j="${j.id}" data-rule="${rid}" data-a="yes">Yes</button>
      <button class="${a === "no" ? "sel no" : ""}" data-j="${j.id}" data-rule="${rid}" data-a="no">No</button>
      ${imp && !a ? `<div class="implied">${imp} (implied by “right”)</div>` : ""}</td></tr>`;
  }).join("") + `</table>`;
}

function card(g) {
  const [j, rv] = reviewOf(g);
  const first = g.items[0], last = g.items[g.items.length - 1];
  const t0 = first.at.slice(11, 16), t1 = last.at.slice(11, 16);
  const shot = g.items.map(x => x.shot).find(Boolean);
  const verdict = rv && rv.verdict;
  const cls = verdict === "right" ? "ok" : verdict ? "wrong" : "";
  const img = shot ? `<img class="shot" src="/shots/${shot}" loading="lazy">`
    : `<div class="noshot">${j.p_hit ? "no screenshot" : "not judged: " + esc((j.decisions[0] || {}).reason || "")}</div>`;
  const reviewable = !!j.p_hit;
  const ask = reviewable ? `<div class="ask"><b>Was SeeNot right?</b>
      <button class="${verdict === "right" ? "sel right" : ""}" data-j="${j.id}" data-v="right">✓ Right</button>
      <button class="${verdict === "should_block" ? "sel bad" : ""}" data-j="${j.id}" data-v="should_block">Should have popped up</button>
      <button class="${verdict === "should_not_block" ? "sel bad" : ""}" data-j="${j.id}" data-v="should_not_block">Shouldn't have popped up</button>
      ${verdict ? `<button data-j="${j.id}" data-v="">clear</button>` : ""}</div>` : "";
  const kinds = ["feed","single_item","search","work","other"], purposes = ["learn","task","entertain"];
  const sel = (name, opts, cur, model) => `<select data-j="${j.id}" data-field="${name}"><option value="">${name}: model said ${esc(model)}</option>` +
      opts.map(o => `<option value="${o}" ${cur === o ? "selected" : ""}>${name} is really: ${o}</option>`).join("") + `</select>`;
  const probs = o => o ? Object.entries(o).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(", ") : "";
  return `<div class="card ${cls}">
    <div>${img}</div>
    <div>
      <div class="title">${esc(j.screen.window_title || j.screen.app)}</div>
      <div class="url">${esc(j.screen.app)}${j.screen.url ? " · " + esc(j.screen.url) : ""}</div>
      <div class="meta">${t0}${t1 !== t0 ? "–" + t1 : ""}${g.items.length > 1 ? ` · judged ${g.items.length}×` : ""}${j.page_kind ? ` · model: ${esc(j.page_kind)}, ${esc(j.purpose)}` : ""}${j.latency_ms ? ` · ${j.latency_ms} ms` : ""} · #${j.id}</div>
      ${verdictLine(j)}
      ${ask}
      ${reviewable ? ruleRows(j, rv) : ""}
      ${reviewable ? `<details><summary>What the model read, and the rest of its answers</summary>
        <pre>${esc(JSON.stringify(j.state, null, 2))}</pre>
        <div class="small">page kind: ${probs(j.page_probs)}<br>purpose: ${probs(j.purpose_probs)}<br>sensitive: ${j.sensitive}
        <br>came from: ${j.came_from ? esc(j.came_from.page_kind + " · " + j.came_from.key) : "—"} · opened on purpose: ${j.opened_on_purpose ? "yes" : "no"}</div>
        <div style="margin-top:6px">${sel("page_kind", kinds, rv && rv.page_kind, j.page_kind)} ${sel("purpose", purposes, rv && rv.purpose, j.purpose)}</div>
        <textarea data-j="${j.id}" data-field="note" rows="1" placeholder="note (optional)">${esc(rv && rv.note || "")}</textarea>
      </details>` : ""}
    </div></div>`;
}

function render() {
  const gs = groups();
  const counts = {all: gs.length, intervene:0, allow:0, count:0, none:0, unreviewed:0, wrong:0};
  for (const g of gs) {
    const [j, rv] = reviewOf(g); const o = outcome(j);
    if (o in counts) counts[o]++;
    if (j.p_hit && !(rv && rv.verdict)) counts.unreviewed++;
    if (rv && rv.verdict && rv.verdict !== "right") counts.wrong++;
  }
  const F = [["all","All"],["intervene","Popped up"],["allow","Allowed"],["count","Counted"],["none","Nothing"],["unreviewed","Not reviewed"],["wrong","Marked wrong"]];
  $("#filters").innerHTML = F.map(([k, l]) => `<span class="chip ${filter === k ? "on" : ""}" data-f="${k}">${l}<span class="n">${counts[k]}</span></span>`).join(" ");
  $("#mode").textContent = D.budgets ? "budgets on" : "testing mode: every hit pops up";
  const shown = gs.filter(g => {
    const [j, rv] = reviewOf(g);
    if (filter === "unreviewed" && !(j.p_hit && !(rv && rv.verdict))) return false;
    if (filter === "wrong" && !(rv && rv.verdict && rv.verdict !== "right")) return false;
    if (["intervene","allow","count","none"].includes(filter) && outcome(j) !== filter) return false;
    if (q && !(j.screen.window_title + " " + j.screen.url + " " + j.screen.app).toLowerCase().includes(q)) return false;
    return true;
  });
  $("#cards").innerHTML = shown.length ? shown.slice(0, 200).map(card).join("") : `<div class="empty">Nothing here yet. Browse with SeeNot running, then refresh.</div>`;
  $("#tuning").innerHTML = D.tuning.map(t => `<div class="tune"><span class="rid">${esc(t.rule)}</span> · threshold ${t.threshold}
      <div class="small">your answers: ${t.yes} yes, ${t.no} no${t.precision != null || t.recall != null ? ` · now precision ${t.precision ?? "–"}, recall ${t.recall ?? "–"}` : ""}</div>
      ${t.suggested != null ? `<div class="small">suggested <b>${t.suggested}</b> → precision ${t.suggested_precision}, recall ${t.suggested_recall}
        ${t.suggested !== t.threshold ? `<button data-apply="${t.rule}" data-value="${t.suggested}">Apply</button>` : ""}</div>`
        : `<div class="small">needs ${3} yes and ${3} no to suggest</div>`}</div>`).join("");
  $("#excRule").innerHTML = D.rules.map(r => `<option value="${r.id}">${esc(r.id)}</option>`).join("");
  $("#excList").innerHTML = D.rules.filter(r => r.exceptions.length || D.exceptions.some(e => e.rule === r.id && e.text)).map(r =>
    `<div class="small" style="margin-top:6px"><b>${esc(r.id)}</b>` +
    [...r.exceptions, ...D.exceptions.filter(e => e.rule === r.id && e.text).map(e => e.text)].map(x => `<div class="exc">· ${esc(x)}</div>`).join("") + `</div>`).join("");
}

document.addEventListener("click", e => {
  const b = e.target.closest("button, .chip, img.shot");
  if (!b) return;
  if (b.matches("img.shot")) { $("#zoom img").src = b.src; $("#zoom").style.display = "flex"; return; }
  if (b.dataset.f) { filter = b.dataset.f; render(); return; }
  if (b.dataset.v !== undefined) {
    const v = b.dataset.v || null;
    post("/api/review", {id:b.dataset.j, verdict:v}).then(() => toast(v ? "saved" : "cleared"));
    // A wrong verdict with no rule answers yet: open the rule questions.
    return;
  }
  if (b.dataset.rule) {
    const rv = D.reviews[b.dataset.j] || {rules:{}};
    const cur = (rv.rules || {})[b.dataset.rule];
    post("/api/review", {id:b.dataset.j, rules:{[b.dataset.rule]: cur === b.dataset.a ? null : b.dataset.a}});
    return;
  }
  if (b.dataset.apply) { post("/api/threshold", {rule:b.dataset.apply, value:b.dataset.value}).then(() => toast("rules.toml updated; the app reloads it")); return; }
  if (b.id === "excAdd") { post("/api/exception", {rule:$("#excRule").value, text:$("#excText").value}).then(() => { $("#excText").value = ""; toast("exception added"); }); }
});
document.addEventListener("change", e => {
  const el = e.target;
  if (el.dataset.field) post("/api/review", {id:el.dataset.j, [el.dataset.field]: el.value || null});
});
$("#zoom").onclick = () => $("#zoom").style.display = "none";
$("#q").oninput = e => { q = e.target.value.toLowerCase(); render(); };
load();
setInterval(() => { if (!document.querySelector("textarea:focus, select:focus, input:focus")) load(); }, 10000);
</script></body></html>
"""
