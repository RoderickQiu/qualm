"""Command line entry points. See the package docstring for the commands."""

from __future__ import annotations

import argparse
import json
import math
import re
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
    import shlex
    import tempfile

    from . import paths
    from . import setup as s
    from .app import run_app
    from .policy import Policy

    if not args.demo and Path(args.rules) == paths.rules_file() and s.needed():
        run_app(None, args.rules, args.budget, data_dir=args.data)  # a first run: the setup window, then watching
        return
    broken = None
    try:
        settings, rules = _config(args)
    except (ValueError, OSError) as e:  # rules.toml doesn't load: the app starts anyway, and its menu bar says so
        from .app import fallback_config

        if isinstance(e, OSError):  # it can't be read at all (its permissions)
            e = f"{args.rules} can't be read ({e.strerror or e}): give yourself read and write access to it " \
                f"again, e.g. `chmod u+rw {shlex.quote(str(args.rules))}`"
        settings, rules, using = fallback_config(args.rules)
        if getattr(args, "lang", None):
            settings.lang = args.lang
        broken = (str(e), using)
        print(f"! {e}\n  Qualm starts on {using} until it's fixed.", flush=True)
    # The demo must not teach your real rules anything.
    data = tempfile.mkdtemp(prefix="qualm-demo-") if args.demo else args.data
    run_app(Policy(settings, rules, data), args.rules, args.budget, demo=args.demo, review=not args.demo, broken=broken)


def _pick(prompt: str, options: tuple[str, ...]) -> str:
    menu = " / ".join(f"{i}={o}" for i, o in enumerate(options))
    while True:
        ans = input(f"{prompt} [{menu}]: ").strip()
        if ans.isdigit() and int(ans) < len(options):
            return options[int(ans)]
        if ans in options:
            return ans


def _save_label(path: str, record: dict) -> int:
    from . import jsonl

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.appending(out) as f:
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
    from . import jsonl
    from .agent import start_hint
    from .rules import load_config

    _, rules = load_config(args.rules)
    kinds = {r.id: r.kind for r in rules}
    path = Path(args.data) / "decisions.jsonl"
    if not path.exists():
        sys.exit(f"{path} not found: {start_hint()} first.")
    events = jsonl.read(path)
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


# Why a rule let a page through that you chose at the time: your check-in
# session, "I need it", one item opened on purpose, "Not this one", "It's part
# of the task". Letting it through was the design, not a miss.
BY_DESIGN = ("your ", "snoozed until", "opened on purpose", "you marked this", "you said it's part of the task")


def _missed(e: dict, rv: dict | None) -> list[str] | None:
    """The rules you said should have stepped in here and didn't; [] for a
    miss flagged without naming a rule (the menu's "This should have been
    blocked"); None if you didn't flag it. A rule that let it through by
    design (BY_DESIGN) didn't miss it."""
    if not rv:
        return None
    acted = {d["rule"] for d in e["decisions"] if d["action"] in ("intervene", "count")
             or d["action"] == "allow" and d.get("reason", "").startswith(BY_DESIGN)}
    said = [rid for rid, a in (rv.get("rules") or {}).items() if a == "yes" and rid not in acted]
    return said if said or rv.get("verdict") == "should_block" else None


def _since(text: str) -> str:
    """--since as the log writes times: today, yesterday, 2026-09-22 (or
    2026-9-22), 2026-09-22T18:00 or "2026-09-22 18:00"."""
    from datetime import date, timedelta

    from .personalize import CliError

    t = text.strip().lower()
    if t in ("today", "yesterday"):
        return (date.today() - timedelta(days=t == "yesterday")).isoformat()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[t ](\d{1,2}):(\d{2})(?::(\d{2}))?)?", t)
    try:
        y, mo, d, h, mi, sec = (int(x) if x else None for x in m.groups())
        return (datetime(y, mo, d, h, mi, sec or 0).isoformat(timespec="seconds") if h is not None
                else date(y, mo, d).isoformat())
    except (AttributeError, ValueError):
        raise CliError(f"--since {text!r}: a date like 2026-09-22, a time like 2026-09-22T18:00, today or "
                       "yesterday") from None


# What `review` shows without --last: the newest 30, or with --since the newest
# 100 from then. A real first day logged 1,242 judgements, ~700 bytes each in
# JSON: all of them is more than an agent reads in one go.
REVIEW_LAST, REVIEW_SINCE = 30, 100

# How a pop-up ended, in the words of its buttons.
ENDED = {"open": "no answer", "back": "you went back", "session": "you checked in", "snooze": "you said I need it",
         "fine": "you said Not this one", "never": "you said Never here"}


def _popups(data: Path, events: list[dict]) -> dict[str, dict]:
    """Judgement id -> the pop-up it brought up: its decision id (what the
    menu's prompt for an agent names), rule, and how it ended: your latest
    answer ("back", "session", "snooze", "fine", "never", ...), or "open"."""
    from . import jsonl

    log = jsonl.read(data / "decisions.jsonl")
    ended = {x["id"]: x.get("response", "") for x in log if x.get("type") == "response" and x.get("id")}
    by_id = {d["id"]: e["id"] for e in events for d in e["decisions"] if d.get("id")}
    # Judgements logged before they kept the decision id: the one logged with the pop-up, same rule and screen.
    near: dict[tuple, list[tuple[datetime, str]]] = {}
    for e in events:
        for d in e["decisions"]:
            if d["action"] == "intervene" and not d.get("id"):
                s = e["screen"]
                near.setdefault((d["rule"], s.get("url", ""), s.get("window_title", "")), []).append(
                    (datetime.fromisoformat(e["at"]), e["id"]))
    out = {}
    for x in log:
        if x.get("type") != "intervention" or not x.get("id") or not x.get("at"):
            continue
        jid = by_id.get(x["id"])
        if jid is None:
            s, at = x.get("screen") or {}, datetime.fromisoformat(x["at"])
            key = (x.get("rule", ""), s.get("url", ""), s.get("window_title", ""))
            gap, jid = min(((abs((t - at).total_seconds()), j) for t, j in near.get(key, [])), default=(0, None))
            jid = jid if gap <= 5 else None
        if jid is not None:
            out[jid] = {"id": x["id"], "rule": x.get("rule", ""), "ended": ended.get(x["id"]) or "open"}
    return out


def cmd_review(args) -> None:
    """Every judgement the app or `watch` made: when, what Qualm did and why,
    each rule's score, your answers, and how each pop-up ended. --web opens
    the review page (the app already serves it); --fix ID rule=yes|no
    answers from the terminal; --id finds one judgement, or a pop-up by its
    decision id."""
    from .agent import command, start_hint
    from .personalize import CliError, _out
    from .review import dashboard_port, load_judgements, load_reviews, save_review, serve

    settings, rules = _config(args)
    by_id = {r.id: r for r in rules}
    data = Path(args.data)
    if args.web:
        if args.json:
            raise CliError("--web opens the page and has no JSON output: run it without --json")
        serve(data, Path(args.rules), dashboard_port(settings))
        return
    events = load_judgements(data)
    if not events:
        raise CliError(f"no screens yet in {data}. To start Qualm, {start_hint()}; use your Mac for a while with it "
                       "running, then look again", "no_screens")
    if args.rule and args.rule not in by_id:
        raise CliError(f"no rule {args.rule!r}; there are: {', '.join(by_id) or 'none'}", "not_found")

    if args.fix:
        jid, *pairs = args.fix
        ev = next((e for e in events if e["id"] == jid), None)
        if ev is None or "p_hit" not in ev:
            raise CliError(f"#{jid}: no such judgement with page content (sensitive and unmonitored pages have none)", "not_found")
        answers, extra = {}, {}
        for pair in pairs:
            key, _, val = pair.partition("=")
            if key in by_id and val in ("yes", "no", "unknown"):
                answers[key] = None if val == "unknown" else val
            elif key == "verdict" or key in ("page_kind", "purpose", "note"):
                extra[key] = val
            else:
                raise CliError(f"can't use {pair!r}: rule ids are {', '.join(by_id)} (=yes|no); "
                               "also verdict=right|should_block|should_not_block, page_kind=, purpose=, note=")
        saved = save_review(data, jid, rules=answers, **extra)
        _out(args, {"saved": saved}, f"saved #{jid}. The review page and `eval --reviews` use it.")
        return

    if args.last is not None and args.last < 1:
        raise CliError("--last: how many to show, 1 or more")
    since = _since(args.since) if args.since else ""
    reviews = load_reviews(data)
    missed = {e["id"]: _missed(e, reviews.get(e["id"])) for e in events}
    popup = _popups(data, events)
    shown = events
    if args.id:
        shown = [e for e in shown if args.id in (e["id"], popup.get(e["id"], {}).get("id"))]
        if not shown:
            raise CliError(f"no judgement or pop-up with id {args.id!r} in {data}; ids come from `review`, `rules test` "
                           "or the pop-ups listed in the menu's prompt for your AI agent", "not_found")
    if since:
        shown = [e for e in shown if e["at"] >= since]
    if args.acted:
        shown = [e for e in shown if any(d["action"] in ("intervene", "allow", "count") for d in e["decisions"])]
    if args.misses:
        shown = [e for e in shown if missed[e["id"]] is not None]
    if args.rule:  # a miss you flagged for this rule shows however low it scored
        shown = [e for e in shown if any(d["rule"] == args.rule for d in e["decisions"])
                 or e.get("p_hit", {}).get(args.rule, 0) >= by_id[args.rule].threshold / 2
                 or args.rule in (missed[e["id"]] or ())]
    last = args.last or (None if args.id else REVIEW_SINCE if since else REVIEW_LAST)  # one id: all its rows
    picked = shown[-last:] if last else shown
    cut = ""
    if len(picked) < len(shown):  # how to get more, or fewer: never a flag that's already there
        narrow = [f for f, on in (("--rule R", not args.rule), ("--acted", not args.acted)) if on]
        cut = (f"the newest {len(picked)} of {len(shown)} matching" + (f" since {args.since}" if since else "")
               + "; --last N shows more" + ("" if since else ", --since DATE only those from then")
               + (f", {' or '.join(narrow)} narrows it" if narrow else ""))
    if args.json:
        rows = [{"id": e["id"], "at": e["at"], "app": e["screen"].get("app", ""), "title": e["screen"].get("window_title", ""),
                 "url": e["screen"].get("url", ""), "page_kind": e.get("page_kind"), "purpose": e.get("purpose"),
                 "opened_on_purpose": e.get("opened_on_purpose", False), "decisions": e["decisions"],
                 "p_hit": e.get("p_hit", {}), "thresholds": e.get("thresholds", {}), "allow": e.get("allow", {}),
                 "you": {k: v for k, v in reviews[e["id"]].items() if k != "id"} if e["id"] in reviews else None,
                 "missed": missed[e["id"]], "popup": popup.get(e["id"])} for e in picked]
        _out(args, {"judgements": rows, "matching": len(shown), "shown": len(rows), "truncated": bool(cut),
                    **({"note": cut} if cut else {})}, "")
        return
    for e in picked:
        s = e["screen"]
        print(f"#{e['id']} {e['at'][:10]} {e['at'][11:]}  {s.get('app', '')} | {s.get('window_title', '')[:60]} | {s.get('url', '')[:70]}")
        acts = "; ".join(f"{d['action']} {d['rule']} ({d['reason']})".replace(" ()", "") for d in e["decisions"]) or "nothing"
        seen = f"page={e['page_kind']} purpose={e['purpose']}  " if "page_kind" in e else ""
        rv = reviews.get(e["id"])
        mark = f"   [you: {rv.get('verdict') or ''} {rv.get('rules') or ''}]" if rv else ""
        if missed[e["id"]] is not None:
            mark += f"   [you said it missed{': ' + ', '.join(missed[e['id']]) if missed[e['id']] else ' this'}]"
        if pop := popup.get(e["id"]):
            mark += f"   [pop-up {pop['id']}: {ENDED.get(pop['ended'], 'you said ' + pop['ended'])}]"
        print(f"    {seen}-> {acts}{mark}")
        if "p_hit" in e:
            cells = [f"{rid} {p:.2f}{'*' if p >= (by_id[rid].threshold if rid in by_id else 1) else ''}"
                     for rid, p in e["p_hit"].items()]
            print("    p_hit: " + "  ".join(cells) + "   (* = at or over the rule's threshold)")
    print(f"\n{cut[0].upper() + cut[1:] if cut else f'{len(shown)} judgements'}. Easier: `{command()} review --web`")


def cmd_week(args) -> None:
    """How this week went: the dashboard's Insights numbers (--json for agents)."""
    from .review import week, week_text

    w = week(Path(args.data), _config(args)[1])
    print(json.dumps(w, ensure_ascii=False, indent=2) if args.json else week_text(w))


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
    from .agent import command
    from .personalize import CliError, _out
    from .rules import build_questions
    from .state import ScreenState

    settings, rules = _config(args)
    labels = Path(args.labels)
    if not labels.exists():
        raise CliError(f"no labels in {labels} yet: `{command()} label` records some, and `harvest` makes "
                       "them from your answers to pop-ups", "not_found")
    questions = {k: q.model_dump(mode="json", exclude_none=True) for k, q in build_questions(rules, settings.lang).items()}
    n = skipped = 0
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with labels.open(encoding="utf-8") as src, Path(args.out).open("w", encoding="utf-8") as out:
        for line in src:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                gold = {k: rec["labels"].get(k) for k in ("sensitive", "page_kind", "purpose")}
                gold |= {f"rule_{k}": v for k, v in rec["labels"].get("rules", {}).items()}
                qs = {k: {**q, "label": gold[k]} for k, q in questions.items() if gold.get(k) not in (None, "unknown")}
                state = ScreenState(**rec["screen"]).to_state(args.budget) if qs else None
            except (ValueError, KeyError, TypeError, AttributeError):  # not a label record
                skipped += 1
                continue
            if not qs:
                continue
            out.write(json.dumps({"state": state, "questions": qs}, ensure_ascii=False) + "\n")
            n += 1
    skip = "" if not skipped else f" (skipped {skipped} line{'s' * (skipped != 1)} that " + (
        "isn't a label record)" if skipped == 1 else "aren't label records)")
    _out(args, {"records": n, "skipped": skipped, "out": args.out}, f"{n} record{'s' * (n != 1)} -> {args.out}{skip}")


def cmd_install(args) -> None:
    from . import autostart, paths
    from .personalize import CliError

    if paths.custom_home() and autostart.installed() and not autostart.login_is_ours():
        print(f"Replacing the login item that starts Qualm on {autostart.login_home()} (a Qualm it started quits "
              "now); `install` again without QUALM_HOME puts that one back.")
    try:
        autostart.install()
    except RuntimeError as e:
        raise CliError(str(e)) from None


def cmd_serve(args) -> None:
    from .autostart import serve

    serve(Path(args.kev_dir).expanduser().resolve() if args.kev_dir else None, args.model, args.port, args.bits)


def _quiet_settings():
    """[settings] if rules.toml loads, else None: uninstall works on a broken file too."""
    from . import paths
    from .rules import load_config

    try:
        return load_config(paths.rules_file())[0]
    except (OSError, ValueError):
        return None


def cmd_uninstall(args) -> None:
    from . import autostart, paths
    from .review import app_running

    custom = paths.custom_home()  # QUALM_HOME: the login item, key, `qualm` command and logs are the main install's
    if not args.all:
        agents = [autostart.AGENTS / f"{label}.plist" for label in (autostart.APP_LABEL, *autostart.OLD_LABELS)]
        present = [p for p in agents if p.exists()]
        if custom and present and not autostart.login_is_ours():
            print(f"QUALM_HOME is set, and the login item starts Qualm on {autostart.login_home()}: left alone. "
                  "Unset QUALM_HOME to remove it.")
        elif args.dry_run:
            print("\n".join(f"would remove: {p}" for p in present) + "\n(dry run: nothing removed. Removing the login "
                  "item also quits a Qualm it started.)" if present else f"not installed: {autostart.APP_LABEL}")
        else:
            autostart.uninstall()
        return

    items = autostart.everything()
    if custom:
        print(f"QUALM_HOME is set: only {paths.home()} goes (and a login item asked for there). The login item, "
              "the `qualm` command, the keychain key and the logs of your main install stay; unset QUALM_HOME "
              "to remove those.")
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
    if app_running(paths.data_dir(), _quiet_settings()):
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
    """One answer; Enter takes the default, Ctrl-D stops setup."""
    from .personalize import CliError

    try:
        return input(f"{prompt} [{default}]: ").strip() or default
    except EOFError:
        print()
        raise CliError("setup stopped: nothing was changed") from None


def _until_valid(prompt: str, default: str, parse):
    """Asks until `parse` takes the answer; a ValueError says what's wrong with it."""
    while True:
        try:
            return parse(_ask(prompt, default))
        except ValueError as e:
            print(f"  {e}")


def _backend(answer: str) -> str:
    from .localmodel import unsupported

    b = {"local": "kev", "hosted": "jev"}.get(answer.strip().lower(), answer.strip().lower())
    if b not in ("kev", "jev"):
        raise ValueError("answer kev (on this Mac) or jev (hosted by TypeSafe)")
    if b == "kev" and (why := unsupported()):  # Apple silicon and macOS 14
        raise ValueError(f"{why} This Mac can use jev, the hosted model.")
    return b


def _rule_ids(answer: str, starters: list[dict]) -> list[str]:
    """ "shortvideo, feeds" -> the ids; "none" or nothing -> []; anything that isn't a starter rule is refused."""
    from .agent import command
    from .config import starter_blocks

    ids = [x["id"] for x in starters]
    picked = [x.strip() for x in answer.split(",") if x.strip()]
    if picked == ["none"]:
        return []
    if unknown := [x for x in picked if x not in ids]:
        allow = [x for x in unknown if starter_blocks().get(x, ("",))[0] == "allow"]
        raise ValueError(f"not a starter rule: {', '.join(unknown)}. The starter rules are: {', '.join(ids)} "
                         "(comma-separated, or none)" + (f". {', '.join(allow)}: a kind of page never flagged, "
                                                          f"not a rule (`{command()} allow list`)" if allow else ""))
    return picked


def _yes_no(answer: str) -> bool:
    if answer.strip().lower() in ("y", "yes"):
        return True
    if answer.strip().lower() in ("n", "no"):
        return False
    raise ValueError("answer y or n")


def cmd_setup(args) -> None:
    """Where the model runs, the permission, your rules, start at login: each
    asked in turn, or given as flags (for scripts and agents, which never
    turn on start at login unless --login says so)."""
    import getpass

    from . import autostart, keychain, localmodel, paths, review
    from . import setup as s
    from .agent import command, start_hint
    from .personalize import CliError, _out
    from .rules import load_config

    q = command()
    interactive = args.interactive and not args.json
    notes: list[str] = []
    say = (lambda line: notes.append(line.strip())) if args.json else print  # under --json, the words go into "notes"

    if args.dry_run and paths.legacy():
        say(f"Would copy this folder's rules.toml and data/ to {paths.home()} first.")
    elif not args.dry_run and (copied := s.migrate()):
        say(f"Copied {', '.join(copied)} from this folder to {paths.home()}; the originals stay where they are.")
    if not args.json:
        print(f"Qualm keeps your rules and data in {paths.home()}\n")

    fresh = s.needed()
    saved = None if fresh or not paths.rules_file().exists() else load_config(paths.rules_file())[0].backend
    rec, why = s.recommend()
    if not args.json:
        print("Where should the model run?")
        print("  kev  on this Mac: private, nothing leaves it; about 1 s per reading")
        print("  jev  hosted by TypeSafe: ~0.2 s and little memory, but the text of each new screen is sent to TypeSafe")
        print(f"  {why}")
        if saved and saved != rec:
            print(f"  So {rec} is the one for this Mac right now; you chose {saved} before, and that stays unless you pick {rec}.")
    if args.backend:
        backend = _backend(args.backend)
    elif interactive:
        backend = _until_valid("kev or jev", saved or rec, _backend)
    else:
        backend = _backend(saved or rec)
        say(f"  using {backend}, " + ("as you chose before" if saved else "the one for this Mac") + " (--backend changes it)")

    key = None
    if backend == "jev":
        have = keychain.stored() is not None
        if args.key == "-":  # on stdin, so it's in no process list or shell history
            key = sys.stdin.readline().strip()
            if not key:
                raise CliError("--key -: no key came on stdin")
        else:
            key = args.key or (None if have else keychain.api_key())  # TYPESAFE_API_KEY counts
        checked = False
        while backend == "jev" and not key and not have:
            if not interactive:
                raise CliError(f"the hosted model needs a TypeSafe API key (from console.typesafe.ai). Give it on stdin "
                               f"(`{q} setup --backend jev --key -`) or in TYPESAFE_API_KEY, or run `{q} setup` in a "
                               f"terminal. `--backend kev` runs the model on this Mac instead.")
            try:
                key = getpass.getpass("TypeSafe API key (from console.typesafe.ai; not shown): ").strip()
            except EOFError:
                print()
                raise CliError("setup stopped: nothing was changed") from None
            if not key:
                print("  no key given. Get one at console.typesafe.ai, or pick kev to run the model on this Mac.")
                if not args.backend:
                    backend = _until_valid("kev or jev", "jev" if localmodel.unsupported() else "kev", _backend)
            elif err := s.check_key(key):
                print(f"  that key didn't work: {err}")
                key = None
            else:
                checked = True
        if backend == "jev":
            if key and not checked and (err := s.check_key(key)):
                raise CliError(f"that key didn't work: {err}")
            say("  the key works" if key else "  using the key in your keychain")

    starters = s.starters()
    ids = [x["id"] for x in starters]
    current = [r.id for r in load_config(paths.rules_file())[1] if r.enabled] if paths.rules_file().exists() else None
    on = [i for i in (current if current is not None else [x["id"] for x in starters if x["on"]]) if i in ids]
    if args.rules is not None:
        picked = _rule_ids(args.rules, starters)
    elif interactive:
        print("\nWhat should Qualm watch for?")
        for x in starters:
            print(f"  {x['id']:<11} {x['what'][:80]} ({x['kind']})")
        picked = _until_valid("rules to turn on, comma-separated", ",".join(on) or "none", lambda a: _rule_ids(a, starters))
    else:
        picked = on

    if args.login is not None:
        login = args.login
    elif interactive and not paths.custom_home():
        # A checkout's login item runs its Python, which needs its own Accessibility permission: off unless asked.
        default = autostart.installed() if not fresh else bool(paths.bundle())
        login = _until_valid("\nStart Qualm at login? y/n", "y" if default else "n", _yes_no)
    else:
        login = None  # as it is

    if args.dry_run:
        plan = {"dry_run": True, "backend": backend, "rules": picked, "login": login,
                "key": "new" if key else "keychain" if backend == "jev" else None, "notes": notes}
        _out(args, plan, f"Would set up: the model {'hosted by TypeSafe (Jev)' if backend == 'jev' else 'on this Mac'}; "
                         f"rules on: {', '.join(picked) or 'none'}; start at login: "
                         f"{'on' if login else 'off' if login is False else 'as it is'}.\n(dry run: nothing changed)")
        return
    choices = s.Choices(backend, picked, login, key)
    done = s.apply(choices, say=say)
    notes += choices.notes
    app = review.app_running(paths.data_dir(), _quiet_settings())
    if paths.bundle() and not (app and "accessibility" in app):
        notes.append("Qualm.app asks for Accessibility when it starts: turn Qualm on in System Settings > Privacy & "
                     f"Security > Accessibility (`{q} setup --permission` opens it). Without it Qualm sees only app names.")
    elif (app.get("accessibility") if app and "accessibility" in app else s.accessibility()) is False:
        who = "Qualm" if app and app.get("bundle") else s.terminal_name()
        notes.append(f"! Accessibility isn't granted to {who} yet: without it Qualm sees only app names. System "
                     f"Settings > Privacy & Security > Accessibility: turn on {who}. `{q} setup --permission` opens it.")
    if choices.started and not paths.bundle():
        notes.append(f"Started at login, Qualm runs as {Path(sys.executable).resolve()}: macOS asks for Accessibility "
                     "(and Screen Recording) for that too.")
    if backend == "kev" and not (localmodel.saved_copy() / "model.safetensors").exists():
        size = localmodel.DOWNLOAD_GB + (0 if localmodel.runtime_ready() else localmodel.RUNTIME_GB)
        notes.append(f"The app starts the local model itself. The first start downloads about {size:.0f} GB, once"
                     + (": the app is at it now, and its menu bar item shows how far." if choices.started or app
                        else f"; `{q} serve` does it now, in a terminal."))
    if choices.started:
        final = (f"Done. Qualm is starting now, from its login item: look for its eye in the menu bar. "
                 f"Don't also run `{q} app`: that would be a second copy.")
    elif app:
        final = "Done. The running Qualm picks up the changes."
    else:
        final = f"Done. Start it: {start_hint()}."
    if args.json:
        _out(args, {"backend": backend, "rules": picked, "login": login, "started": choices.started,
                    "done": done, "notes": notes, "next": final}, "")
        return
    for line in done:
        print(f"✓ {line}")
    for note in notes:
        print(f"\n{note}")
    print(f"\n{final}")


def cmd_permission(args) -> None:
    import subprocess

    from . import paths
    from . import setup as s
    from .agent import command

    if paths.bundle():
        # From a terminal, macOS answers for the terminal: Qualm.app's own switch is only in the list.
        subprocess.run(["open", s.ACCESSIBILITY_PANE], check=False)
        print("Opened System Settings > Privacy & Security > Accessibility: turn Qualm on there. If it isn't listed, "
              "open Qualm.app once: it adds itself.")
    elif s.accessibility():
        print(f"Accessibility is granted to {s.terminal_name()}, so `{command()} app` run from it can read windows.")
    else:
        s.ask_accessibility()
        print(f"Opened System Settings > Privacy & Security > Accessibility: turn on {s.terminal_name()} there "
              "(Qualm runs inside it), then start it again.")


class Parser(argparse.ArgumentParser):
    """argparse, except that with --json on the command line a usage error
    comes back as JSON too: {"error": {"code": "invalid", ...}}, exit 2."""

    json_errors = False

    def error(self, message):
        if Parser.json_errors:
            print(json.dumps({"error": {"code": "invalid", "message": f"{self.prog}: {message}"}}, ensure_ascii=False, indent=2))
            raise SystemExit(2)
        super().error(message)


# Commands whose output is for a person (or a panel, or a server): --json gets an error saying so.
NO_JSON = ("probe", "ask", "watch", "app", "label", "harvest", "eval", "serve", "install", "uninstall")


def main(argv: list[str] | None = None) -> None:
    from . import __doc__ as doc
    from . import paths
    from .personalize import register, run
    from .state import DEFAULT_CHAR_BUDGET

    argv = sys.argv[1:] if argv is None else argv
    Parser.json_errors = "--json" in argv
    # Your rules and data live in ~/Library/Application Support/Qualm (paths.py).
    DEFAULT_RULES, DEFAULT_DATA = str(paths.rules_file()), str(paths.data_dir())
    DEFAULT_LABELS = str(Path(DEFAULT_DATA) / "labels.jsonl")
    p = Parser(prog="qualm", description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    def add(name, fn, help, model=True, budget=False):
        sp = sub.add_parser(name, help=help, description=help)
        if budget:
            sp.add_argument("--budget", type=int, default=DEFAULT_CHAR_BUDGET, help="state size in characters")
        if model:
            sp.add_argument("--rules", default=DEFAULT_RULES)
            sp.add_argument("--lang", choices=("zh", "en"), help="rule text to send; default: [settings] lang")
        sp.add_argument("--json", action="store_true", help="no JSON output here: says so as a JSON error"
                        if name in NO_JSON else "machine-readable output, errors included")
        sp.set_defaults(fn=run, cmd_fn=fn)  # errors and exit codes as for every other command
        return sp

    sp = add("setup", cmd_setup, "start here: where the model runs, the permission, your rules, start at login", model=False)
    sp.add_argument("--backend", choices=("kev", "jev"), help="kev: on this Mac; jev: hosted by TypeSafe "
                    "(default: the one chosen before; the first time, the one this Mac has room for)")
    sp.add_argument("--key", help="a TypeSafe API key, saved in your keychain; - reads it from stdin "
                    "(TYPESAFE_API_KEY works too)")
    sp.add_argument("--rules", help="starter rules to turn on, comma-separated (\"\" for none)")
    sp.add_argument("--login", action=argparse.BooleanOptionalAction, default=None,
                    help="start at login, or not (default: asked in a terminal; left as it is otherwise)")
    sp.add_argument("--permission", action="store_true", help="only open the Accessibility settings")
    sp.add_argument("--dry-run", action="store_true", help="say what it would set up; change nothing")
    sp.set_defaults(interactive=sys.stdin.isatty())

    sp = add("probe", cmd_probe, "print the screen state on every change, no model", model=False, budget=True)
    sp.add_argument("--interval", type=float, default=1.0)
    sp.add_argument("--once", action="store_true")

    sp = add("ask", cmd_ask, "one reading of the current screen", budget=True)
    sp.add_argument("--delay", type=float, default=0.0, help="seconds to switch windows first")

    sp = add("watch", cmd_watch, "the app's loop in the terminal, printing every judgement", budget=True)
    sp.add_argument("--recheck", type=float, default=30.0, help="seconds between re-asks when only a page's text changes")
    sp.add_argument("--data", default=DEFAULT_DATA)

    sp = add("app", cmd_app, "the menu bar app: watch, and step in when a rule is hit", budget=True)
    sp.add_argument("--data", default=DEFAULT_DATA)
    sp.add_argument("--demo", nargs="?", const="deny", choices=("deny", "feed", "checkin", "timesup", "focus", "prompt"),
                    help="show the panel once with a made-up moment (default: deny); nothing is watched or learned")

    sp = add("label", cmd_label, "capture the screen and record your ground-truth labels", budget=True)
    sp.add_argument("--delay", type=float, default=5.0)
    sp.add_argument("--labels", default=DEFAULT_LABELS)

    sp = add("harvest", cmd_harvest, "your answers to pop-ups -> label records")
    sp.add_argument("--data", default=DEFAULT_DATA)
    sp.add_argument("--labels", default=DEFAULT_LABELS)

    sp = add("review", cmd_review, "every judgement Qualm made; --fix the wrong ones; --web opens the dashboard")
    sp.add_argument("--data", default=DEFAULT_DATA)
    sp.add_argument("--labels", default=DEFAULT_LABELS)
    sp.add_argument("--last", type=int, help="how many to show, newest last (default: 30; with --since, 100)")
    sp.add_argument("--id", help="one judgement, or the one a pop-up was on, by its decision id (as the menu's "
                    "prompt for an agent names it)")
    sp.add_argument("--rule", help="only judgements near or over this rule's threshold, and misses you flagged for it")
    sp.add_argument("--acted", action="store_true", help="only those that intervened, allowed or counted")
    sp.add_argument("--misses", action="store_true", help="only the ones you said it should have caught")
    sp.add_argument("--since", metavar="DATE", help="only from then on: 2026-09-22, 2026-09-22T18:00, today or yesterday")
    sp.add_argument("--fix", nargs="+", metavar="ID RULE=yes|no", help="record the right answer for a judgement")
    sp.add_argument("--web", action="store_true", help="open the review page")

    sp = add("week", cmd_week, "how this week went: the dashboard's Insights numbers (--json for agents)", model=False)
    sp.add_argument("--rules", default=DEFAULT_RULES)
    sp.add_argument("--data", default=DEFAULT_DATA)

    sp = add("eval", cmd_eval, "replay labels through the model: precision, recall, latency", budget=True)
    sp.add_argument("--labels", default=DEFAULT_LABELS)
    sp.add_argument("--dump", help="write every reading to this JSONL, for error analysis")
    sp.add_argument("--suggest", action="store_true", help="print per-rule thresholds for rules.toml")
    sp.add_argument("--reviews", action="store_true", help="also score your answers from the review page")
    sp.add_argument("--data", default=DEFAULT_DATA)
    sp.add_argument("--precision", type=float, default=0.9, help="precision --suggest aims for")

    sp = add("serve", cmd_serve, "the local model server (Kev-4B, 8-bit), in this terminal", model=False)
    sp.add_argument("--kev-dir", help="run from a Kev checkout instead of the installed runtime (development)")
    sp.add_argument("--model", default="jaredpalmer/kev-4b")
    sp.add_argument("--port", type=int, default=8009)
    sp.add_argument("--bits", type=int, choices=(8, 16), default=8,
                    help="8 (default): half the memory, the same answers on the trials; 16: bf16 as trained")
    add("install", cmd_install, "start the app at every login", model=False)
    sp = add("uninstall", cmd_uninstall, "stop starting at login; --all removes everything of Qualm's", model=False)
    sp.add_argument("--all", action="store_true",
                    help="also remove your rules and data, the model runtime, the logs, the key and the `qualm` command")
    sp.add_argument("--yes", action="store_true", help="don't ask first")
    sp.add_argument("--dry-run", action="store_true", help="only list what would be removed")

    sp = add("export", cmd_export, "labels -> Kev training JSONL, for a fine-tune", budget=True)
    sp.add_argument("--labels", default=DEFAULT_LABELS)
    sp.add_argument("--out", default=str(Path(DEFAULT_DATA) / "train.jsonl"))

    register(sub, {"rules": DEFAULT_RULES, "data": DEFAULT_DATA})

    args = p.parse_args(argv)
    if args.cmd is None:
        p.print_help()
        return
    if args.cmd == "setup" and args.permission:
        args.cmd_fn = cmd_permission
    if getattr(args, "json", False) and args.cmd in NO_JSON:
        print(json.dumps({"error": {"code": "invalid", "message": f"`qualm {args.cmd}` has no JSON output: run it "
                                    "without --json"}}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
