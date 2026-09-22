"""Command line entry points. See the package docstring for the commands."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RULES = "rules.toml"
DEFAULT_LABELS = "data/labels.jsonl"


def _rules(path: str):
    from .rules import load_rules

    p = Path(path)
    if not p.exists():
        sys.exit(f"{path} not found. Copy rules.example.toml to rules.toml and edit it.")
    return load_rules(p)


def _print_reading(reading) -> None:
    print(
        f"  {reading.latency_ms:6.0f} ms  tokens={reading.input_tokens}  "
        f"sensitive={reading.sensitive:.2f}  page={reading.page_kind} "
        f"({reading.page_probs.get(reading.page_kind, 0):.2f})"
    )
    for v in reading.rules:
        print(f"    rule {v.rule_id:<12} {v.choice:<12} p_hit={v.p_hit:.2f}")


def _notify(title: str, body: str) -> None:
    body = body.replace('"', "'")
    subprocess.run(["osascript", "-e", f'display notification "{body}" with title "{title}"'], check=False)


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

    rules = _rules(args.rules)
    time.sleep(args.delay)
    state = capture().to_state(args.budget)
    print(json.dumps(state, ensure_ascii=False))
    _print_reading(ask(make_client(), state, rules, args.lang))


def cmd_watch(args) -> None:
    from .decide import Gate, ask, make_client
    from .state import capture

    rules = _rules(args.rules)
    client, gate = make_client(), Gate(args.high, args.low)
    last_sig, changed_at, last_asked = None, 0.0, float("-inf")
    while True:
        s = capture()
        now = time.monotonic()
        if s.signature() != last_sig:
            last_sig, changed_at = s.signature(), now
        # Ask once the screen has been stable for the debounce window, or on
        # the heartbeat if nothing changed (a long video, a long read).
        settled = now - changed_at >= args.debounce and changed_at > last_asked
        if settled or now - last_asked >= args.heartbeat:
            last_asked = now
            state = s.to_state(args.budget)
            reading = ask(client, state, rules, args.lang)
            print(f"[{datetime.now():%H:%M:%S}] {state.get('app')} | {state.get('window_title', '')[:60]}", flush=True)
            _print_reading(reading)
            for action, what in gate.actions(reading, rules):
                print(f"    -> {action} {what}", flush=True)
                if action in ("intervene", "nudge"):
                    _notify("SeeNot", f"{action}: rule {what}")
        time.sleep(args.interval)


def _pick(prompt: str, options: tuple[str, ...]) -> str:
    menu = " / ".join(f"{i}={o}" for i, o in enumerate(options))
    while True:
        ans = input(f"{prompt} [{menu}]: ").strip()
        if ans.isdigit() and int(ans) < len(options):
            return options[int(ans)]
        if ans in options:
            return ans


def cmd_label(args) -> None:
    from .rules import DENY_OPTIONS, PAGE_KINDS, TIME_CAP_OPTIONS
    from .state import capture

    rules = _rules(args.rules)
    print(f"Switch to the window to label. Capturing in {args.delay:.0f}s...")
    time.sleep(args.delay)
    s = capture()
    print(json.dumps(s.to_state(args.budget), ensure_ascii=False, indent=2))
    labels = {
        "sensitive": _pick("sensitive page?", ("no", "yes")) == "yes",
        "page_kind": _pick("page kind", PAGE_KINDS),
        "rules": {
            r.id: _pick(f"rule {r.id} ({r.description})", DENY_OPTIONS if r.kind == "deny" else TIME_CAP_OPTIONS)
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
    out = Path(args.labels)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    n = sum(1 for _ in out.open(encoding="utf-8"))
    print(f"saved -> {out} ({n} records)")


def cmd_eval(args) -> None:
    from .decide import ask, make_client
    from .state import ScreenState

    rules = _rules(args.rules)
    by_id = {r.id: r for r in rules}
    path = Path(args.labels)
    records = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    if not records:
        sys.exit(f"no records in {path}")
    client = make_client()
    lat, page_hits, sens_hits = [], 0, 0
    thresholds = (0.5, 0.7, 0.85, 0.95)
    tally = {t: [0, 0, 0] for t in thresholds}  # true pos, predicted pos, actual pos
    for rec in records:
        state = ScreenState(**rec["screen"]).to_state(args.budget)
        reading = ask(client, state, rules, args.lang)
        lat.append(reading.latency_ms)
        page_hits += reading.page_kind == rec["labels"]["page_kind"]
        sens_hits += (reading.sensitive >= 0.5) == rec["labels"]["sensitive"]
        for v in reading.rules:
            gold = rec["labels"]["rules"].get(v.rule_id)
            if gold is None or gold == "unknown":
                continue
            positive = gold == ("violates" if by_id[v.rule_id].kind == "deny" else "in_scope")
            for t in thresholds:
                pred = v.p_hit >= t
                tally[t][0] += pred and positive
                tally[t][1] += pred
                tally[t][2] += positive
    n = len(records)
    lat.sort()
    print(f"records={n}  lang={args.lang}  budget={args.budget} chars")
    print(f"latency p50={statistics.median(lat):.0f} ms  p95={lat[math.ceil(0.95 * n) - 1]:.0f} ms")
    print(f"page_kind accuracy={page_hits / n:.2f}  sensitive accuracy={sens_hits / n:.2f}")
    print("rule hits (unknown labels excluded):")
    for t, (tp, pp, ap) in tally.items():
        prec = f"{tp / pp:.2f}" if pp else "-"
        rec_ = f"{tp / ap:.2f}" if ap else "-"
        print(f"  p_hit>={t:.2f}  precision={prec} ({tp}/{pp})  recall={rec_} ({tp}/{ap})")


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
            sp.add_argument("--lang", choices=("zh", "en"), default="zh", help="which rule description to send")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("probe", cmd_probe, model=False)
    sp.add_argument("--interval", type=float, default=1.0)
    sp.add_argument("--once", action="store_true")

    sp = add("ask", cmd_ask)
    sp.add_argument("--delay", type=float, default=0.0, help="seconds to switch windows first")

    sp = add("watch", cmd_watch)
    sp.add_argument("--interval", type=float, default=0.5)
    sp.add_argument("--debounce", type=float, default=0.5)
    sp.add_argument("--heartbeat", type=float, default=30.0)
    sp.add_argument("--high", type=float, default=0.85)
    sp.add_argument("--low", type=float, default=0.5)

    sp = add("label", cmd_label)
    sp.add_argument("--delay", type=float, default=5.0)
    sp.add_argument("--labels", default=DEFAULT_LABELS)

    sp = add("eval", cmd_eval)
    sp.add_argument("--labels", default=DEFAULT_LABELS)

    args = p.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        pass
