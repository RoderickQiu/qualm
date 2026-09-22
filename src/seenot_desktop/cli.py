"""Command line entry points. See the package docstring for the commands."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RULES = "rules.toml"
DEFAULT_LABELS = "data/labels.jsonl"
DEFAULT_DATA = "data"
THRESHOLDS = (0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.7, 0.85)


def _config(args):
    """(settings, rules), with --lang overriding [settings] lang."""
    from .rules import load_config

    p = Path(args.rules)
    if not p.exists():
        sys.exit(f"{args.rules} not found. Copy rules.example.toml to rules.toml and edit it.")
    settings, rules = load_config(p)
    if getattr(args, "lang", None):
        settings.lang = args.lang
    return settings, rules


def _print_reading(reading) -> None:
    print(
        f"  {reading.latency_ms:6.0f} ms  tokens={reading.input_tokens}  "
        f"sensitive={reading.sensitive:.2f}  page={reading.page_kind} "
        f"({reading.page_probs.get(reading.page_kind, 0):.2f})  purpose={reading.purpose} "
        f"({reading.purpose_probs.get(reading.purpose, 0):.2f})",
        flush=True,
    )
    for v in reading.rules:
        print(f"    rule {v.rule_id:<12} {v.choice:<12} p_hit={v.p_hit:.2f}", flush=True)


def cmd_probe(args) -> None:
    from .state import capture

    last = None
    while True:
        s = capture()
        if not s.ax_trusted and last is None:
            print("! Accessibility not granted: only app name and browser URL are readable.", file=sys.stderr)
        if s.signature() != last:
            last = s.signature()
            print(json.dumps(s.to_state(args.budget), ensure_ascii=False), flush=True)
        if args.once:
            return
        time.sleep(args.interval)


def cmd_ask(args) -> None:
    from .decide import ask, make_client
    from .state import capture

    settings, rules = _config(args)
    time.sleep(args.delay)
    state = capture(skip=settings.no_monitor).to_state(args.budget)
    print(json.dumps(state, ensure_ascii=False))
    _print_reading(ask(make_client(), state, rules, settings.lang))


def cmd_watch(args) -> None:
    """The live loop in the terminal: every judgement, no panel."""
    from .policy import Policy
    from .watcher import Watcher

    settings, rules = _config(args)
    policy = Policy(settings, rules, args.data)

    def on_event(ev):
        print(f"[{datetime.now():%H:%M:%S}] {ev.screen.app} | {ev.screen.window_title[:60]}", flush=True)
        if ev.reading:
            _print_reading(ev.reading)
        for d in ev.decisions:
            print(f"    -> {d.action} {d.rule} ({d.reason})", flush=True)

    Watcher(policy, on_event, lambda s: print(f"  [{s}]", flush=True), budget=args.budget,
            heartbeat=args.heartbeat).run()


def cmd_app(args) -> None:
    import tempfile

    from .app import run_app
    from .policy import Policy

    settings, rules = _config(args)
    # The demo must not teach your real rules anything.
    data = tempfile.mkdtemp(prefix="seenot-demo-") if args.demo else args.data
    run_app(Policy(settings, rules, data), args.rules, args.budget, demo=args.demo)


def _pick(prompt: str, options: tuple[str, ...]) -> str:
    menu = " / ".join(f"{i}={o}" for i, o in enumerate(options))
    while True:
        ans = input(f"{prompt} [{menu}]: ").strip()
        if ans.isdigit() and int(ans) < len(options):
            return options[int(ans)]
        if ans in options:
            return ans


def _save_label(path: str, record: dict) -> int:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return sum(1 for _ in out.open(encoding="utf-8"))


def cmd_label(args) -> None:
    from .rules import DENY_OPTIONS, PAGE_KINDS, PURPOSES, TIME_CAP_OPTIONS
    from .state import capture

    settings, rules = _config(args)
    print(f"Switch to the window to label. Capturing in {args.delay:.0f}s...")
    time.sleep(args.delay)
    s = capture(skip=settings.no_monitor)
    print(json.dumps(s.to_state(args.budget), ensure_ascii=False, indent=2))
    labels = {
        "sensitive": _pick("sensitive page?", ("no", "yes")) == "yes",
        "page_kind": _pick("page kind", PAGE_KINDS),
        "purpose": _pick("what is it for", PURPOSES),
        "rules": {
            r.id: _pick(f"rule {r.id} ({r.text(settings.lang)})", DENY_OPTIONS if r.kind == "deny" else TIME_CAP_OPTIONS)
            for r in rules
        },
    }
    record = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        # The full capture, not the budgeted state, so eval can re-cut it at
        # a different --budget later.
        "screen": s.as_record(),
        "labels": labels,
        "note": input("note (optional): ").strip(),
    }
    print(f"saved -> {args.labels} ({_save_label(args.labels, record)} records)")


def cmd_harvest(args) -> None:
    """Interventions you answered -> label records. "Take me back" and "I need
    it" confirm the hit; "Not this one" says it was wrong. Other questions
    stay unlabelled, and eval skips them."""
    from .rules import load_config

    _, rules = load_config(args.rules)
    kinds = {r.id: r.kind for r in rules}
    path = Path(args.data) / "decisions.jsonl"
    if not path.exists():
        sys.exit(f"{path} not found: run `seenot-desktop app` first.")
    events = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    shown = {e["id"]: e for e in events if e["type"] == "intervention"}
    have = set()
    if Path(args.labels).exists():
        have = {json.loads(line).get("decision_id") for line in Path(args.labels).open(encoding="utf-8")}
    n = 0
    for e in events:
        if e["type"] != "response" or e["id"] not in shown or e["id"] in have:
            continue
        iv = shown[e["id"]]
        hit = e["response"] in ("back", "snooze")
        if kinds.get(iv["rule"]) == "deny":
            label = "violates" if hit else "safe"
        else:
            label = "in_scope" if hit else "out_of_scope"
        _save_label(args.labels, {
            "captured_at": iv["at"], "screen": iv["screen"], "labels": {"rules": {iv["rule"]: label}},
            "note": f"harvest: {e['response']}", "decision_id": e["id"],
        })
        n += 1
    print(f"{n} new labels -> {args.labels}")


def cmd_review(args) -> None:
    """Every judgement the app or `watch` made, newest last. `--fix ID rule=yes|no`
    records the right answer as a label; `eval --suggest` then re-tunes from it."""
    from .rules import DENY_OPTIONS, PAGE_KINDS, PURPOSES, TIME_CAP_OPTIONS

    settings, rules = _config(args)
    by_id = {r.id: r for r in rules}
    path = Path(args.data) / "judgements.jsonl"
    if not path.exists():
        sys.exit(f"{path} not found: run `seenot-desktop app` or `watch` first.")
    events = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]

    if args.fix:
        jid, *pairs = args.fix
        ev = next((e for e in events if e["id"] == jid), None)
        if ev is None or "p_hit" not in ev:
            sys.exit(f"#{jid}: no such judgement with page content (sensitive and unmonitored pages have none)")
        labels: dict = {"rules": {}}
        for pair in pairs:
            key, _, val = pair.partition("=")
            if key in by_id:
                deny = by_id[key].kind == "deny"
                val = {"yes": "violates" if deny else "in_scope", "no": "safe" if deny else "out_of_scope"}.get(val, val)
                if val not in (DENY_OPTIONS if deny else TIME_CAP_OPTIONS):
                    sys.exit(f"{key}: use yes / no / unknown")
                labels["rules"][key] = val
            elif key == "page_kind" and val in PAGE_KINDS or key == "purpose" and val in PURPOSES:
                labels[key] = val
            elif key == "sensitive" and val in ("yes", "no"):
                labels[key] = val == "yes"
            else:
                sys.exit(f"can't use {pair!r}: rule ids are {', '.join(by_id)}; also page_kind=, purpose=, sensitive=")
        n = _save_label(args.labels, {"captured_at": ev["at"], "screen": ev["screen"], "labels": labels,
                                      "note": f"review #{jid}"})
        print(f"saved -> {args.labels} ({n} records). Re-tune with: seenot-desktop eval --suggest")
        return

    shown = events
    if args.acted:
        shown = [e for e in shown if any(d["action"] in ("intervene", "allow", "count") for d in e["decisions"])]
    if args.rule:
        shown = [e for e in shown if any(d["rule"] == args.rule for d in e["decisions"])
                 or e.get("p_hit", {}).get(args.rule, 0) >= by_id[args.rule].threshold / 2]
    for e in shown[-args.last:]:
        s = e["screen"]
        print(f"#{e['id']} {e['at'][11:]}  {s.get('app', '')} | {s.get('window_title', '')[:60]} | {s.get('url', '')[:70]}")
        acts = "; ".join(f"{d['action']} {d['rule']} ({d['reason']})".replace(" ()", "") for d in e["decisions"]) or "nothing"
        seen = f"page={e['page_kind']} purpose={e['purpose']}  " if "page_kind" in e else ""
        print(f"    {seen}-> {acts}")
        if "p_hit" in e:
            cells = []
            for rid, p in e["p_hit"].items():
                t = by_id[rid].threshold if rid in by_id else 1.0
                cells.append(f"{rid} {p:.2f}{'*' if p >= t else ''}")
            print("    p_hit: " + "  ".join(cells) + "   (* = at or over the rule's threshold)")
    print(f"\n{len(shown)} judgements shown. Wrong one? seenot-desktop review --fix <id> <rule>=yes|no")


def _suggest(rule_id: str, pts: list[tuple[float, bool]], target: float) -> str:
    """The threshold with the best recall at precision >= target."""
    pos = sum(y for _, y in pts)
    best = None
    for t in sorted({p for p, _ in pts}):
        tp = sum(p >= t and y for p, y in pts)
        pp = sum(p >= t for p, _ in pts)
        if pp and tp / pp >= target and (best is None or tp > best[1]):
            best = (t, tp, pp)
    if best is None or not pos:
        return f"  {rule_id:<12} no threshold reaches precision {target} (positives: {pos})"
    t, tp, pp = best
    # Halfway to the highest negative below it, so the number isn't fitted to
    # the exact p_hit of one example.
    below = [p for p, y in pts if p < t and not y]
    t = (t + max(below)) / 2 if below else t
    return f"  {rule_id:<12} threshold = {t:.3f}   # recall {tp / pos:.2f}, precision {tp / pp:.2f}"


def cmd_eval(args) -> None:
    from .decide import ask, make_client
    from .rules import HIT_LABELS
    from .state import ScreenState

    settings, rules = _config(args)
    path = Path(args.labels)
    records = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    if not records:
        sys.exit(f"no records in {path}")
    client = make_client()
    lat = []
    acc = {"page_kind": [0, 0], "purpose": [0, 0], "sensitive": [0, 0]}  # right, labelled
    points: dict[str, list[tuple[float, bool]]] = {r.id: [] for r in rules}
    dump = Path(args.dump).open("w", encoding="utf-8") if args.dump else None
    for rec in records:
        state = ScreenState(**rec["screen"]).to_state(args.budget)
        reading = ask(client, state, rules, settings.lang)
        lat.append(reading.latency_ms)
        gold = rec["labels"]
        for key, got in (("page_kind", reading.page_kind), ("purpose", reading.purpose),
                         ("sensitive", reading.sensitive >= 0.5)):
            if gold.get(key) is not None:
                acc[key][0] += got == gold[key]
                acc[key][1] += 1
        if dump:
            dump.write(json.dumps({
                "note": rec.get("note", ""), "labels": gold, "sensitive": reading.sensitive,
                "page_kind": reading.page_kind, "purpose": reading.purpose,
                "page_probs": reading.page_probs, "purpose_probs": reading.purpose_probs, "url": state.get("url", ""),
                "p_hit": {v.rule_id: v.p_hit for v in reading.rules}, "input_tokens": reading.input_tokens,
            }, ensure_ascii=False) + "\n")
        for v in reading.rules:
            label = gold.get("rules", {}).get(v.rule_id)
            if label not in (None, "unknown"):
                points[v.rule_id].append((v.p_hit, label in HIT_LABELS))
    n = len(records)
    lat.sort()
    print(f"records={n}  lang={settings.lang}  budget={args.budget} chars")
    print(f"latency p50={statistics.median(lat):.0f} ms  p95={lat[math.ceil(0.95 * n) - 1]:.0f} ms")
    print("  ".join(f"{k} accuracy={r / t:.2f} (n={t})" for k, (r, t) in acc.items() if t))
    print("rule hits (unknown labels excluded):")
    for rule in rules:
        pts = points[rule.id]
        print(f"  {rule.id} (threshold in rules: {rule.threshold})")
        for t in sorted({*THRESHOLDS, rule.threshold}):
            tp = sum(p >= t and y for p, y in pts)
            pp = sum(p >= t for p, _ in pts)
            ap = sum(y for _, y in pts)
            prec = f"{tp / pp:.2f}" if pp else "-"
            rec_ = f"{tp / ap:.2f}" if ap else "-"
            mark = " <- current" if t == rule.threshold else ""
            print(f"    p_hit>={t:.2f}  precision={prec} ({tp}/{pp})  recall={rec_} ({tp}/{ap}){mark}")
    if args.suggest:
        print(f"suggested thresholds (best recall at precision >= {args.precision}), for rules.toml:")
        for rule in rules:
            print(_suggest(rule.id, points[rule.id], args.precision))


def cmd_export(args) -> None:
    """Labels -> Kev's training JSONL ({"state", "questions": {name: {type, instructions, criteria, label}}}),
    asking exactly the questions `ask` sends, so a fine-tune learns the live prompt."""
    from .rules import build_questions
    from .state import ScreenState

    settings, rules = _config(args)
    questions = {k: q.model_dump(mode="json", exclude_none=True) for k, q in build_questions(rules, settings.lang).items()}
    n = 0
    with Path(args.labels).open(encoding="utf-8") as src, Path(args.out).open("w", encoding="utf-8") as out:
        for line in src:
            if not line.strip():
                continue
            rec = json.loads(line)
            gold = {k: rec["labels"].get(k) for k in ("sensitive", "page_kind", "purpose")}
            gold |= {f"rule_{k}": v for k, v in rec["labels"].get("rules", {}).items()}
            qs = {k: {**q, "label": gold[k]} for k, q in questions.items() if gold.get(k) not in (None, "unknown")}
            if not qs:
                continue
            state = ScreenState(**rec["screen"]).to_state(args.budget)
            out.write(json.dumps({"state": state, "questions": qs}, ensure_ascii=False) + "\n")
            n += 1
    print(f"{n} records -> {args.out}")


def main() -> None:
    from . import __doc__ as doc
    from .state import DEFAULT_CHAR_BUDGET

    p = argparse.ArgumentParser(prog="seenot-desktop", description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, model=True):
        sp = sub.add_parser(name)
        sp.add_argument("--budget", type=int, default=DEFAULT_CHAR_BUDGET, help="state size in characters")
        if model:
            sp.add_argument("--rules", default=DEFAULT_RULES)
            sp.add_argument("--lang", choices=("zh", "en"), help="rule text to send; default: [settings] lang")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("probe", cmd_probe, model=False)
    sp.add_argument("--interval", type=float, default=1.0)
    sp.add_argument("--once", action="store_true")

    sp = add("ask", cmd_ask)
    sp.add_argument("--delay", type=float, default=0.0, help="seconds to switch windows first")

    sp = add("watch", cmd_watch)
    sp.add_argument("--heartbeat", type=float, default=30.0)
    sp.add_argument("--data", default=DEFAULT_DATA)

    sp = add("app", cmd_app)
    sp.add_argument("--data", default=DEFAULT_DATA)
    sp.add_argument("--demo", action="store_true", help="show the panel once with a made-up hit; no watching")

    sp = add("label", cmd_label)
    sp.add_argument("--delay", type=float, default=5.0)
    sp.add_argument("--labels", default=DEFAULT_LABELS)

    sp = add("harvest", cmd_harvest)
    sp.add_argument("--data", default=DEFAULT_DATA)
    sp.add_argument("--labels", default=DEFAULT_LABELS)

    sp = add("review", cmd_review)
    sp.add_argument("--data", default=DEFAULT_DATA)
    sp.add_argument("--labels", default=DEFAULT_LABELS)
    sp.add_argument("--last", type=int, default=30, help="how many to show")
    sp.add_argument("--rule", help="only judgements near or over this rule's threshold")
    sp.add_argument("--acted", action="store_true", help="only those that intervened, allowed or counted")
    sp.add_argument("--fix", nargs="+", metavar="ID RULE=yes|no", help="record the right answer for a judgement")

    sp = add("eval", cmd_eval)
    sp.add_argument("--labels", default=DEFAULT_LABELS)
    sp.add_argument("--dump", help="write every reading to this JSONL, for error analysis")
    sp.add_argument("--suggest", action="store_true", help="print per-rule thresholds for rules.toml")
    sp.add_argument("--precision", type=float, default=0.9, help="precision --suggest aims for")

    sp = add("export", cmd_export)
    sp.add_argument("--labels", default=DEFAULT_LABELS)
    sp.add_argument("--out", default="data/train.jsonl")

    args = p.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        pass
