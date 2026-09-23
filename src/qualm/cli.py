"""Command line entry points. See the package docstring for the commands."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

THRESHOLDS = (0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.7, 0.85)


def _config(args):
    """(settings, rules), with --lang overriding [settings] lang."""
    from .personalize import config_path

    settings, rules = config_path(args.rules).load()
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
    _print_reading(ask(make_client(settings), state, rules, settings.lang, settings.allow))


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
            recheck=args.recheck, rules_path=args.rules).run()


def cmd_app(args) -> None:
    import tempfile

    from . import paths
    from . import setup as s
    from .app import run_app
    from .policy import Policy

    if not args.demo and Path(args.rules) == paths.rules_file() and s.needed():
        run_app(None, args.rules, args.budget, data_dir=args.data)  # a first run: the setup window, then watching
        return
    settings, rules = _config(args)
    # The demo must not teach your real rules anything.
    data = tempfile.mkdtemp(prefix="qualm-demo-") if args.demo else args.data
    run_app(Policy(settings, rules, data), args.rules, args.budget, demo=args.demo, review=not args.demo)


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
    from .rules import DENY_OPTIONS, PAGE_KINDS, PURPOSES, CHECK_IN_OPTIONS
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
            r.id: _pick(f"rule {r.id} ({r.text(settings.lang)})", DENY_OPTIONS if r.kind == "deny" else CHECK_IN_OPTIONS)
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
        sys.exit(f"{path} not found: run `qualm app` first.")
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
    """Every judgement the app or `watch` made. --web opens the review page
    (the app already serves it); --fix ID rule=yes|no answers from the terminal."""
    from .review import load_judgements, load_reviews, save_review, serve

    settings, rules = _config(args)
    by_id = {r.id: r for r in rules}
    data = Path(args.data)
    if args.web:
        serve(data, Path(args.rules))
        return
    events = load_judgements(data)
    if not events:
        sys.exit(f"no judgements in {data}: run `qualm app` or `watch` first.")

    if args.fix:
        jid, *pairs = args.fix
        ev = next((e for e in events if e["id"] == jid), None)
        if ev is None or "p_hit" not in ev:
            sys.exit(f"#{jid}: no such judgement with page content (sensitive and unmonitored pages have none)")
        answers, extra = {}, {}
        for pair in pairs:
            key, _, val = pair.partition("=")
            if key in by_id and val in ("yes", "no", "unknown"):
                answers[key] = None if val == "unknown" else val
            elif key == "verdict" or key in ("page_kind", "purpose", "note"):
                extra[key] = val
            else:
                sys.exit(f"can't use {pair!r}: rule ids are {', '.join(by_id)} (=yes|no); "
                         "also verdict=right|should_block|should_not_block, page_kind=, purpose=, note=")
        save_review(data, jid, rules=answers, **extra)
        print(f"saved #{jid}. The review page and `eval --reviews` use it.")
        return

    reviews = load_reviews(data)
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
        rv = reviews.get(e["id"])
        mark = f"   [you: {rv.get('verdict') or ''} {rv.get('rules') or ''}]" if rv else ""
        print(f"    {seen}-> {acts}{mark}")
        if "p_hit" in e:
            cells = [f"{rid} {p:.2f}{'*' if p >= (by_id[rid].threshold if rid in by_id else 1) else ''}"
                     for rid, p in e["p_hit"].items()]
            print("    p_hit: " + "  ".join(cells) + "   (* = at or over the rule's threshold)")
    print(f"\n{len(shown)} judgements. Easier: qualm review --web")


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
    records = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()] if path.exists() else []
    if args.reviews:
        from .review import review_labels

        records += review_labels(Path(args.data), rules)
    if not records:
        sys.exit(f"no records in {path}" + (" or your reviews" if args.reviews else ""))
    client = make_client(settings)
    lat = []
    acc = {"page_kind": [0, 0], "purpose": [0, 0], "sensitive": [0, 0]}  # right, labelled
    points: dict[str, list[tuple[float, bool]]] = {r.id: [] for r in rules}
    dump = Path(args.dump).open("w", encoding="utf-8") if args.dump else None
    for rec in records:
        state = ScreenState(**rec["screen"]).to_state(args.budget)
        reading = ask(client, state, rules, settings.lang, settings.allow)
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
                "allow": reading.allow,
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


def cmd_install(args) -> None:
    from .autostart import install

    install()


def cmd_serve(args) -> None:
    from .autostart import serve

    serve(Path(args.kev_dir).expanduser().resolve() if args.kev_dir else None, args.model, args.port, args.bits)


def cmd_uninstall(args) -> None:
    from . import autostart

    if not args.all:
        autostart.uninstall()
        return
    from .localmodel import listening
    from .review import PORT

    items = autostart.everything()
    if not items:
        print("Nothing of Qualm's is left on this Mac.")
    else:
        print("This removes:")
        for what, where in items:
            print(f"  {what}" + (f": {where}" if where else ""))
    kept = autostart.left_behind()
    if kept:
        print("It leaves the model downloads, which other tools may share:")
        for repo, size in kept:
            print(f"  {repo} ({size / 2**30:.1f} GB)")
        print("  remove them yourself if nothing else uses them:")
        print(f"  {autostart.forget_downloads_command([r for r, _ in kept])}")
    if not items or args.dry_run:
        return
    if listening(PORT):
        sys.exit("Qualm is running: quit it first (menu bar > Quit Qualm), then run this again.")
    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("Not run from a terminal: add --yes to remove all of this.")
        if input('Type "yes" to remove all of this: ').strip().lower() != "yes":
            print("Nothing removed.")
            return
    for line in autostart.uninstall_all():
        print(f"✓ {line}")
    if kept:
        print(f"Left: {', '.join(r for r, _ in kept)}. To remove them: {autostart.forget_downloads_command([r for r, _ in kept])}")


def _ask(prompt: str, default: str) -> str:
    try:
        return input(f"{prompt} [{default}]: ").strip() or default
    except EOFError:
        return default


def cmd_setup(args) -> None:
    """Where the model runs, the permission, your rules, start at login: each
    asked in turn (or given as flags, for scripts and agents)."""
    import getpass

    from . import keychain, paths
    from . import setup as s
    from .rules import load_config

    if copied := s.migrate():
        print(f"Copied {', '.join(copied)} from this folder to {paths.home()}; the originals stay where they are.")
    print(f"Qualm keeps your rules and data in {paths.home()}\n")

    rec, why = s.recommend()
    print("Where should the model run?")
    print("  kev  on this Mac: private, nothing leaves it; about 1 s per reading")
    print("  jev  hosted by TypeSafe: ~0.2 s and little memory, but the text of each new screen is sent to TypeSafe")
    print(f"  {why}")
    backend = args.backend or (_ask("kev or jev", rec) if args.interactive else rec)
    key = None
    if backend == "jev":
        have = keychain.stored()
        key = args.key or (None if have else os.environ.get("TYPESAFE_API_KEY") or keychain.api_key())
        if not key and not have:
            if not args.interactive:
                sys.exit("the hosted model needs a key: --key, or run setup in a terminal")
            key = getpass.getpass("TypeSafe API key (from console.typesafe.ai; not shown): ").strip()
        if key and (err := s.check_key(key)):
            sys.exit(f"that key didn't work: {err}")
        print("  the key works" if key else "  using the key in your keychain")

    current = None
    if paths.rules_file().exists():
        _, rules = load_config(paths.rules_file())
        current = [r.id for r in rules if r.enabled]
    starters = s.starters()
    on = current if current is not None else [x["id"] for x in starters if x["on"]]
    if args.rules is not None:
        picked = [x for x in args.rules.split(",") if x]
    else:
        print("\nWhat should Qualm watch for?")
        for x in starters:
            print(f"  {x['id']:<11} {x['what'][:80]} ({x['kind']})")
        picked = [x.strip() for x in _ask("rules to turn on, comma-separated", ",".join(i for i in on if i in {x['id'] for x in starters})).split(",")] \
            if args.interactive else [i for i in on if i in {x["id"] for x in starters}]
    unknown = set(picked) - {x["id"] for x in starters}
    if unknown:
        sys.exit(f"not a starter rule: {', '.join(sorted(unknown))}")
    login = args.login if args.login is not None else (_ask("\nStart Qualm at login? y/n", "y").lower().startswith("y")
                                                          if args.interactive else True)
    for line in s.apply(s.Choices(backend, picked, login, key)):
        print(f"✓ {line}")
    if not s.accessibility():
        print("\n! Accessibility isn't granted yet: without it Qualm sees only app names.")
        print("  System Settings > Privacy & Security > Accessibility: turn on Qualm (or, from a terminal, "
              "your terminal app). `qualm setup --permission` opens it.")
    if backend == "kev":
        print("\nThe app starts the local model itself. The first start downloads about 9 GB and saves an 8-bit copy "
              "(a few minutes); `qualm serve` does it now, in this terminal.")
    print("\nDone. Start it: open Qualm.app, or `qualm app`." if not login else "\nDone. Qualm is starting.")


def cmd_permission(args) -> None:
    from . import setup as s

    if s.accessibility():
        print("Accessibility is granted.")
    else:
        s.ask_accessibility()
        print("Opened System Settings > Privacy & Security > Accessibility: turn Qualm on there.")


def main() -> None:
    from . import __doc__ as doc
    from . import paths
    from .state import DEFAULT_CHAR_BUDGET

    # Your rules and data live in ~/Library/Application Support/Qualm (paths.py).
    DEFAULT_RULES, DEFAULT_DATA = str(paths.rules_file()), str(paths.data_dir())
    DEFAULT_LABELS = str(Path(DEFAULT_DATA) / "labels.jsonl")
    p = argparse.ArgumentParser(prog="qualm", description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
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
    sp.add_argument("--recheck", type=float, default=30.0, help="seconds between re-asks when only a page's text changes")
    sp.add_argument("--data", default=DEFAULT_DATA)

    sp = add("app", cmd_app)
    sp.add_argument("--data", default=DEFAULT_DATA)
    sp.add_argument("--demo", nargs="?", const="deny", choices=("deny", "feed", "checkin", "timesup", "focus", "prompt"),
                    help="show the panel once with a made-up moment (default: deny); nothing is watched or learned")

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
    sp.add_argument("--web", action="store_true", help="open the review page")

    sp = add("eval", cmd_eval)
    sp.add_argument("--labels", default=DEFAULT_LABELS)
    sp.add_argument("--dump", help="write every reading to this JSONL, for error analysis")
    sp.add_argument("--suggest", action="store_true", help="print per-rule thresholds for rules.toml")
    sp.add_argument("--reviews", action="store_true", help="also score your answers from the review page")
    sp.add_argument("--data", default=DEFAULT_DATA)
    sp.add_argument("--precision", type=float, default=0.9, help="precision --suggest aims for")

    sp = add("serve", cmd_serve, model=False)
    sp.add_argument("--kev-dir", help="run from a Kev checkout instead of the installed runtime (development)")
    sp.add_argument("--model", default="jaredpalmer/kev-4b")
    sp.add_argument("--port", type=int, default=8009)
    sp.add_argument("--bits", type=int, choices=(8, 16), default=8,
                    help="8 (default): half the memory, the same answers on the trials; 16: bf16 as trained")
    add("install", cmd_install, model=False)
    sp = add("uninstall", cmd_uninstall, model=False)
    sp.add_argument("--all", action="store_true",
                    help="also remove your rules and data, the model runtime, the logs, the key and the `qualm` command")
    sp.add_argument("--yes", action="store_true", help="don't ask first")
    sp.add_argument("--dry-run", action="store_true", help="only list what --all would remove")

    sp = add("setup", cmd_setup, model=False)
    sp.add_argument("--backend", choices=("kev", "jev"), help="kev: on this Mac; jev: hosted by TypeSafe")
    sp.add_argument("--key", help="a TypeSafe API key (saved in your keychain)")
    sp.add_argument("--rules", help="starter rules to turn on, comma-separated (\"\" for none)")
    sp.add_argument("--login", action=argparse.BooleanOptionalAction, default=None, help="start at login")
    sp.add_argument("--permission", action="store_true", help="only open the Accessibility settings")
    sp.set_defaults(interactive=sys.stdin.isatty())

    sp = add("export", cmd_export)
    sp.add_argument("--labels", default=DEFAULT_LABELS)
    sp.add_argument("--out", default="data/train.jsonl")

    from .personalize import register

    register(sub, {"rules": DEFAULT_RULES, "data": DEFAULT_DATA})

    args = p.parse_args()
    if args.cmd == "setup" and args.permission:
        args.fn = cmd_permission
    try:
        args.fn(args)
    except KeyboardInterrupt:
        pass
    except ValueError as e:  # a rules.toml that doesn't load, with what to fix
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
