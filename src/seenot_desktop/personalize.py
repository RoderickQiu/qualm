"""Every way to make SeeNot yours, as commands: rules, allowed kinds of page,
exceptions, never-here places and settings. The review page and the pop-up
change the same files, so any of the three can undo the others.

Every command that shows something takes --json, and every change is
checked before rules.toml is saved: a person or an agent can drive all of
it from a terminal. docs/PERSONALIZE.md walks through it.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import EXAMPLE, Config, apply_assignments, starter_blocks
from .rules import ID_RE, AllowClass, Rule, Settings


def config_path(path: str) -> Config:
    """rules.toml, created from the starter rules the first time."""
    p = Path(path)
    if not p.exists():
        shutil.copyfile(EXAMPLE, p)
        print(f"created {p} from the starter rules (rules.example.toml)", file=sys.stderr)
    return Config(p)


def _out(args, obj, text: str) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2) if getattr(args, "json", False) else text)


def _fail(msg: str) -> None:
    sys.exit(f"error: {msg}")


def _usage_today(data_dir: Path) -> dict:
    p = data_dir / "usage.json"
    if not p.exists():
        return {}
    saved = json.loads(p.read_text(encoding="utf-8"))
    return saved.get("counts", {}) if saved.get("day") == datetime.now().date().isoformat() else {}


def summary(r: Rule, settings: Settings) -> str:
    """The rule in one sentence."""
    what = r.text(settings.lang)
    if r.kind == "deny":
        s = f"Steps in on {what}"
    else:
        limits = [f"{r.minutes_per_day:g} min" if r.minutes_per_day else "", f"{r.visits_per_day} visits" if r.visits_per_day else ""]
        s = f"{' and '.join(x for x in limits if x) or 'No limit set'} a day of {what}"
        if not settings.budgets:
            s += " (testing mode: steps in at once)"
    s += "." if not s.endswith(".") else ""
    extra = []
    if r.when:
        extra.append("only " + "; ".join(r.when))
    if r.target == "page":
        extra.append("feeds and home pages count too")
    if r.feed_hit:
        extra.append("any entertainment feed hits it")
    fine = [x for x, on in (("for learning material", r.allow_learning), ("when opened on purpose", r.allow_intentional)) if on]
    if fine:
        extra.append("fine " + " or ".join(fine))
    if r.sites or r.patterns:
        extra.append("always on " + ", ".join([*r.sites, *(f"/{p}/" for p in r.patterns)]))
    tail = "; ".join(extra)
    return s + (" " + tail[0].upper() + tail[1:] + "." if extra else "")


def _rule_json(r: Rule, settings: Settings, usage: dict) -> dict:
    d = asdict(r)
    d |= {"reads": r.text(settings.lang), "active_now": r.active(), "summary": summary(r, settings)}
    if r.kind == "time_cap":
        u = usage.get(r.id, {})
        d["today"] = {"minutes": round(u.get("seconds", 0) / 60, 1), "visits": u.get("visits", 0)}
    return d


def _find(items, id: str, what: str):
    found = next((x for x in items if x.id == id), None)
    if found is None:
        _fail(f"no {what} {id!r}; there are: {', '.join(x.id for x in items) or 'none'}")
    return found


# -- rules ------------------------------------------------------------------


def rules_list(args) -> None:
    cfg = config_path(args.rules)
    settings, rules = cfg.load()
    usage = _usage_today(Path(args.data))
    rows = [_rule_json(r, settings, usage) for r in rules]
    lines = []
    for r, d in zip(rules, rows):
        state = "off" if not r.enabled else "on" if d["active_now"] else "on, not now"
        today = f"  today: {d['today']['minutes']:g} min, {d['today']['visits']} visits" if "today" in d else ""
        lines.append(f"{r.id:<12} [{state}] threshold {r.threshold:g}{today}\n    {d['summary']}")
    if not rules:
        lines.append("no rules. `seenot-desktop rules starters` lists ready-made ones; `rules add` makes your own.")
    _out(args, rows, "\n".join(lines))


def rules_show(args) -> None:
    from .review import exceptions

    cfg = config_path(args.rules)
    settings, rules = cfg.load()
    r = _find(rules, args.id, "rule")
    d = _rule_json(r, settings, _usage_today(Path(args.data)))
    learned = [e for e in exceptions(Path(args.data)) if e.get("rule") == r.id]
    d["learned_exceptions"] = learned
    text = [f"{r.id}: {d['summary']}", ""]
    for k, v in asdict(r).items():
        if k != "id" and v not in ((), "", None):
            text.append(f"  {k} = {json.dumps(v, ensure_ascii=False) if not isinstance(v, (int, float)) else v}")
    if learned:
        text.append("  from the pop-up and review page (`except list`): " + str(len(learned)))
    _out(args, d, "\n".join(text))


def rules_add(args) -> None:
    cfg = config_path(args.rules)
    if args.from_starter:
        starters = starter_blocks()
        if args.id not in starters or starters[args.id][0] != "rules":
            _fail(f"no starter rule {args.id!r}; `rules starters` lists them")
        cfg.add("rules", {"id": args.id}, text=starters[args.id][1])
    else:
        import re

        if not re.fullmatch(ID_RE, args.id):
            _fail(f"id {args.id!r}: lowercase letters, digits and _, starting with a letter")
        if not args.what:
            _fail("say what the rule is about: --what \"short videos made for endless swiping\"")
        entry = {"id": args.id, "kind": "time_cap" if (args.minutes or args.visits) else "deny", "description": args.what}
        if args.minutes:
            entry["minutes_per_day"] = args.minutes
        if args.visits:
            entry["visits_per_day"] = args.visits
        entry["threshold"] = args.threshold
        for key, val in (("sites", args.site), ("when", args.when)):
            if val:
                entry[key] = val
        for key, on in (("allow_learning", args.learning_ok), ("allow_intentional", args.on_purpose_ok), ("feed_hit", args.feeds)):
            if on:
                entry[key] = True
        if args.whole_page:
            entry["target"] = "page"
        if args.off:
            entry["enabled"] = False
        if args.note:
            entry["note"] = args.note
        more, unset = apply_assignments(Rule, entry, args.set or [])
        entry |= more
        try:
            cfg.add("rules", entry)
        except ValueError as e:
            _fail(str(e))
    settings, rules = cfg.load()
    r = _find(rules, args.id, "rule")
    _out(args, _rule_json(r, settings, {}), f"added {r.id}: {summary(r, settings)}\n"
         f"Untested: see what it would catch with `seenot-desktop rules test {r.id}`.")


def _edit(args, table: str, cls, set_: dict, unset: list[str]) -> None:
    cfg = config_path(args.rules)
    try:
        cfg.edit(table, args.id, set_, unset)
    except (KeyError, ValueError) as e:
        _fail(e.args[0])


def _assignments(args, table: str, cls, id: str, pairs: list[str]) -> tuple[dict, list[str]]:
    cfg = config_path(args.rules)
    settings, rules = cfg.load()
    obj = _find(rules if table == "rules" else settings.allow, id, "rule" if table == "rules" else "allow class")
    # `what` is whichever description the model reads.
    pairs = [obj.text_field(settings.lang) + p[4:] if p.startswith(("what=", "what+=")) else p for p in pairs]
    try:
        return apply_assignments(cls, asdict(obj), pairs)
    except ValueError as e:
        _fail(str(e))


def rules_set(args) -> None:
    set_, unset = _assignments(args, "rules", Rule, args.id, args.pairs)
    _edit(args, "rules", Rule, set_, unset)
    _show_one(args)


def _show_one(args, verb: str = "") -> None:
    settings, rules = Config(args.rules).load()
    r = _find(rules, args.id, "rule")
    _out(args, _rule_json(r, settings, {}), f"{verb or 'saved'} {r.id}: {summary(r, settings)}")


def rules_on(args) -> None:
    _edit(args, "rules", Rule, {}, ["enabled"])
    _show_one(args, "on")


def rules_off(args) -> None:
    _edit(args, "rules", Rule, {"enabled": False}, [])
    _show_one(args, "off")


def rules_remove(args) -> None:
    cfg = config_path(args.rules)
    try:
        cfg.remove("rules", args.id)
    except KeyError as e:
        _fail(e.args[0])
    _out(args, {"removed": args.id}, f"removed {args.id} (the previous file is {cfg.path.name}.bak)")


def rules_starters(args) -> None:
    import tomllib

    have = set()
    if Path(args.rules).exists():
        settings, rules = Config(args.rules).load()
        have = {r.id for r in rules} | {c.id for c in settings.allow}
    rows = []
    for id, (table, block) in starter_blocks().items():
        entry = tomllib.loads(block)[table][0]
        rows.append({"id": id, "table": table, "description": entry["description"], "note": entry.get("note", ""),
                     "have": id in have})
    text = "\n".join(f"{'✓' if r['have'] else ' '} {r['id']:<12} {'rule' if r['table'] == 'rules' else 'allow'}: {r['description']}"
                     for r in rows)
    _out(args, rows, text + "\n\nAdd one: seenot-desktop rules add ID --from-starter (allow classes: allow add ID --from-starter)")


def rules_test(args) -> None:
    from .decide import make_client
    from .trial import run

    settings, rules = config_path(args.rules).load()
    r = _find(rules, args.id, "rule")
    data = Path(args.data)

    def progress(n, total):
        if not args.json and sys.stderr.isatty():
            print(f"\r  asking the model: {n}/{total}", end="" if n < total else "\n", file=sys.stderr, flush=True)

    try:
        rows = run(make_client(), data, r, settings, last=args.last, on_progress=progress)
    except Exception as e:  # the server is down or slow
        _fail(f"couldn't ask the model ({type(e).__name__}: {e}). Is the Kev server running? See HANDOFF.md, Run it.")
    if not rows:
        _fail(f"no screens in {data}/judgements.jsonl yet: run `seenot-desktop app` for a while first")
    fires = [x for x in rows if x["does"] in ("pops up", "counts")]
    lines = [f"{r.id} on your last {len(rows)} distinct screens, at threshold {r.threshold:g}: "
             f"{len(fires)} would {'pop up' if r.kind == 'deny' else 'count'}.", ""]
    shown = rows if args.all else rows[:args.show]
    for x in shown:
        you = f"  you: {x['you_said']}" if x["you_said"] else ""
        why = f" ({x['why']})" if x["does"] not in ("nothing", "pops up", "counts") else ""
        lines.append(f"#{x['id']}  {x['p_hit']:.2f}  {x['does']}{why}{you}\n      {(x['title'] or x['app'])[:70]}  {x['url'][:70]}")
    if not args.all and len(rows) > args.show:
        lines.append(f"… {len(rows) - args.show} more, lower scores (--all to see them)")
    lines += ["", f"Tell it which are right:  seenot-desktop rules label {r.id} --yes ID ... --no ID ...",
              f"Then:                     seenot-desktop rules tune {r.id} --apply"]
    _out(args, {"rule": r.id, "threshold": r.threshold, "screens": rows}, "\n".join(lines))


def rules_label(args) -> None:
    from .trial import label

    _, rules = config_path(args.rules).load()
    _find(rules, args.id, "rule")
    try:
        n = label(Path(args.data), args.id, args.yes or [], args.no or [])
    except ValueError as e:
        _fail(str(e))
    _out(args, {"rule": args.id, "yes": args.yes or [], "no": args.no or []}, f"saved {n} answers for {args.id}")


def rules_tune(args) -> None:
    from .review import MIN_EACH
    from .trial import tune

    cfg = config_path(args.rules)
    settings, rules = cfg.load()
    r = _find(rules, args.id, "rule")
    t = tune(Path(args.data), r, settings.lang, args.precision)
    pct = lambda x: "–" if x is None else f"{round(x * 100)}%"
    lines = [f"{r.id}: {t['yes']} yes, {t['no']} no from you ({t['scored_with_current_wording']} scored with the current wording)",
             f"  now: threshold {t['threshold']:g}, right on {pct(t['precision'])} of hits, catches {pct(t['recall'])}"]
    if t["suggested"] is None:
        need = "" if t["yes"] >= MIN_EACH and t["no"] >= MIN_EACH else f"; needs at least {MIN_EACH} yes and {MIN_EACH} no"
        lines.append(f"  no threshold reaches {pct(args.precision)} right{need}. `rules test {r.id}`, then `rules label`.")
    else:
        lines.append(f"  suggested: {t['suggested']:g}, right on {pct(t['suggested_precision'])}, catches {pct(t['suggested_recall'])}")
        if args.apply and t["suggested"] != r.threshold:
            cfg.edit("rules", r.id, {"threshold": t["suggested"]})
            t["applied"] = True
            lines.append("  applied: rules.toml updated; the app picks it up without a restart")
        elif not args.apply:
            lines.append(f"  apply it: seenot-desktop rules tune {r.id} --apply")
    _out(args, t, "\n".join(lines))


# -- allow classes ----------------------------------------------------------


def allow_list(args) -> None:
    settings, _ = config_path(args.rules).load()
    rows = [asdict(c) | {"reads": c.text(settings.lang)} for c in settings.allow]
    text = "\n".join(f"{c.id:<12} [{'on' if c.enabled else 'off'}] threshold {c.threshold:g}: never flag {c.text(settings.lang)}"
                     + (f"\n    known: {', '.join([*c.sites, *c.apps, *c.patterns])}" if c.sites or c.apps or c.patterns else "")
                     for c in settings.allow)
    _out(args, rows, text or "no allow classes. `rules starters` lists ready-made ones.")


def allow_add(args) -> None:
    cfg = config_path(args.rules)
    try:
        if args.from_starter:
            starters = starter_blocks()
            if args.id not in starters or starters[args.id][0] != "allow":
                _fail(f"no starter allow class {args.id!r}; `rules starters` lists them")
            cfg.add("allow", {"id": args.id}, text=starters[args.id][1])
        else:
            if not args.what:
                _fail('say what kind of page: --what "an online store: a product page, listing, cart or checkout"')
            entry = {"id": args.id, "description": args.what, "threshold": args.threshold}
            if args.site:
                entry["sites"] = args.site
            if args.app:
                entry["apps"] = args.app
            cfg.add("allow", entry)
    except ValueError as e:
        _fail(str(e))
    print(f"added {args.id}: pages the model reads as this are never flagged, except by a rule's sites or patterns")


def allow_set(args) -> None:
    set_, unset = _assignments(args, "allow", AllowClass, args.id, args.pairs)
    _edit(args, "allow", AllowClass, set_, unset)
    print(f"saved {args.id}")


def allow_toggle(args) -> None:
    _edit(args, "allow", AllowClass, {"enabled": False} if args.cmd2 == "off" else {}, [] if args.cmd2 == "off" else ["enabled"])
    print(f"{args.id} {args.cmd2}")


def allow_remove(args) -> None:
    try:
        config_path(args.rules).remove("allow", args.id)
    except KeyError as e:
        _fail(e.args[0])
    print(f"removed {args.id}")


# -- exceptions and never-here ----------------------------------------------


def _append(data_dir: Path, e: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "exceptions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({**e, "at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False) + "\n")


def live_exceptions(data_dir: Path) -> list[dict]:
    """What the pop-up ("Not this one") and the review page taught, minus what was removed."""
    from .review import exceptions

    out: list[dict] = []
    for e in exceptions(data_dir):
        if e.get("never") or not e.get("rule"):
            continue
        if e.get("removed"):
            out = [x for x in out if not (x["rule"] == e["rule"] and _same(x, e))]
        else:
            out.append(e)
    return out


def _same(x: dict, e: dict) -> bool:
    return any(e.get(k) and x.get(k) == e.get(k) for k in ("text", "title", "url"))


def except_list(args) -> None:
    _, rules = config_path(args.rules).load()
    rows = []
    for r in rules:
        if args.rule and r.id != args.rule:
            continue
        rows += [{"rule": r.id, "text": x, "source": "rules.toml"} for x in r.exceptions]
    for e in live_exceptions(Path(args.data)):
        if not args.rule or e["rule"] == args.rule:
            rows.append({"rule": e["rule"], "text": e.get("text") or e.get("title") or "", "url": e.get("url", ""),
                         "source": "typed" if e.get("text") else "not this one", "at": e.get("at", "")})
    text = "\n".join(f"{x['rule']:<12} {x['text']}" + (f"  <{x['url']}>" if x.get("url") else "") + f"  ({x['source']})" for x in rows)
    _out(args, rows, text or "no exceptions")


def except_add(args) -> None:
    cfg = config_path(args.rules)
    _, rules = cfg.load()
    r = _find(rules, args.rule, "rule")
    if args.text in r.exceptions:
        _fail("already there")
    cfg.edit("rules", r.id, {"exceptions": [*r.exceptions, args.text]})
    print(f"{r.id}: the model now reads “{args.text}” as fine")


def except_remove(args) -> None:
    cfg = config_path(args.rules)
    _, rules = cfg.load()
    r = _find(rules, args.rule, "rule")
    if args.text in r.exceptions:
        cfg.edit("rules", r.id, {"exceptions": [x for x in r.exceptions if x != args.text]})
    else:
        match = next((e for e in live_exceptions(Path(args.data))
                      if e["rule"] == r.id and args.text in (e.get("text"), e.get("title"), e.get("url"))), None)
        if match is None:
            _fail(f"{r.id} has no exception {args.text!r}; `except list {r.id}` shows them")
        _append(Path(args.data), {k: match[k] for k in ("rule", "text", "title", "url") if match.get(k)} | {"removed": True})
    print(f"removed from {r.id}: {args.text}")


def _place(args) -> dict:
    if bool(args.site) == bool(args.app):
        _fail("give one of --site example.com or --app com.example.App")
    if args.site:
        from .policy import host_of

        return {"host": host_of(args.site if "://" in args.site else "https://" + args.site)}
    return {"app": args.app, "name": args.name or args.app}


def never_list(args) -> None:
    from .review import never_places

    rows = never_places(Path(args.data))
    _out(args, rows, "\n".join(f"never on {n['host']}" if n.get("host") else f"never in {n.get('name') or n['app']} ({n['app']})"
                               for n in rows) or "none")


def never_add(args) -> None:
    _append(Path(args.data), {"never": _place(args)})
    print("saved: no rule fires there")


def never_remove(args) -> None:
    from .review import undo_never

    place = _place(args)
    undo_never(Path(args.data), place)
    print("removed")


# -- settings -----------------------------------------------------------------


SETTING_HELP = {
    "budgets": "true: time caps count minutes and visits first; false (testing): every hit pops up",
    "lang": 'en: rules with a description_en send that; anything else sends description',
    "no_monitor": "apps (bundle ids) never read at all",
    "allow_sites": "sites never judged; links from them count as opened on purpose",
    "allow_urls": "the same, as URL regexes",
}


def settings_show(args) -> None:
    settings, _ = config_path(args.rules).load()
    d = {k: v for k, v in asdict(settings).items() if k != "allow"}
    _out(args, d, "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}\n    {SETTING_HELP.get(k, '')}" for k, v in d.items()))


def settings_set(args) -> None:
    cfg = config_path(args.rules)
    settings, _ = cfg.load()
    try:
        set_, unset = apply_assignments(Settings, asdict(settings), args.pairs)
        cfg.edit_settings(set_, unset)
    except ValueError as e:
        _fail(str(e))
    print("saved: " + ", ".join([*set_, *unset]))


# -- the parser ---------------------------------------------------------------


def register(sub, defaults: dict) -> None:
    """Adds rules / allow / except / never / settings to the main parser."""

    def group(name, help):
        g = sub.add_parser(name, help=help)
        return g.add_subparsers(dest="cmd2", required=True, metavar="ACTION")

    def cmd(g, name, fn, help, json_out=True):
        sp = g.add_parser(name, help=help, description=help)
        sp.add_argument("--rules", default=defaults["rules"], help="the rules file")
        sp.add_argument("--data", default=defaults["data"], help="the data folder")
        if json_out:
            sp.add_argument("--json", action="store_true", help="machine-readable output")
        sp.set_defaults(fn=fn)
        return sp

    g = group("rules", "list, add, change, test and tune your rules")
    cmd(g, "list", rules_list, "every rule in a sentence: on or off, active now, today's usage")
    cmd(g, "show", rules_show, "one rule, every field").add_argument("id")
    sp = cmd(g, "add", rules_add, "a new rule, in your words (or a starter: --from-starter)")
    sp.add_argument("id", help="short name: lowercase, digits, _")
    sp.add_argument("--what", help='what it is about, in your words: "short videos made for endless swiping"')
    sp.add_argument("--minutes", type=float, help="a daily time budget instead of stepping in at once")
    sp.add_argument("--visits", type=int, help="a daily visit limit instead of stepping in at once")
    sp.add_argument("--site", action="append", help="always counts here: douyin.com, youtube.com/shorts, youtube.com/ (home only); repeat")
    sp.add_argument("--when", action="append", help='only then: "mon-fri 09:00-18:00", "weekends", "22:00-02:00"; repeat')
    sp.add_argument("--learning-ok", action="store_true", help="lectures, tutorials and docs never hit it")
    sp.add_argument("--on-purpose-ok", action="store_true", help="one item opened from search, a work app or a chat link is fine")
    sp.add_argument("--whole-page", action="store_true", help="judge the page itself, so feeds and home pages can hit")
    sp.add_argument("--feeds", action="store_true", help="any page the model reads as an entertainment feed hits it")
    sp.add_argument("--threshold", type=float, default=0.2, help="score that counts as a hit (default 0.2; `rules tune` sets it)")
    sp.add_argument("--off", action="store_true", help="add it switched off, to test first")
    sp.add_argument("--note", help="why it's there, for whoever reads the file later")
    sp.add_argument("--from-starter", action="store_true", help="copy the starter rule with this id")
    sp.add_argument("set", nargs="*", metavar="KEY=VALUE", help="any other field")
    sp = cmd(g, "set", rules_set, "change fields: threshold=0.3 what=\"...\" sites+=tiktok.com when-=weekends note=")
    sp.add_argument("id")
    sp.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    cmd(g, "on", rules_on, "switch a rule on").add_argument("id")
    cmd(g, "off", rules_off, "switch a rule off (kept in the file)").add_argument("id")
    cmd(g, "remove", rules_remove, "delete a rule from the file").add_argument("id")
    cmd(g, "starters", rules_starters, "the ready-made rules and allow classes, and which you have")
    sp = cmd(g, "test", rules_test, "what the rule would do on your recent screens (asks the model)")
    sp.add_argument("id")
    sp.add_argument("--last", type=int, default=100, help="how many recent distinct screens")
    sp.add_argument("--show", type=int, default=20, help="how many to print, highest score first")
    sp.add_argument("--all", action="store_true", help="print all of them")
    sp = cmd(g, "label", rules_label, "say which screens really are this rule (ids from `rules test`)")
    sp.add_argument("id")
    sp.add_argument("--yes", nargs="+", metavar="JID")
    sp.add_argument("--no", nargs="+", metavar="JID")
    sp = cmd(g, "tune", rules_tune, "the threshold from your answers")
    sp.add_argument("id")
    sp.add_argument("--apply", action="store_true", help="write it to rules.toml")
    sp.add_argument("--precision", type=float, default=0.9, help="share of hits that must be right")

    g = group("allow", "kinds of page no rule fires on (shopping, music)")
    cmd(g, "list", allow_list, "every allow class")
    sp = cmd(g, "add", allow_add, "a kind of page never flagged, in your words", json_out=False)
    sp.add_argument("id")
    sp.add_argument("--what", help='"a music player or music streaming site"')
    sp.add_argument("--threshold", type=float, default=0.5)
    sp.add_argument("--site", action="append", help="known to be this: allowed without the model; repeat")
    sp.add_argument("--app", action="append", help="bundle id known to be this; repeat")
    sp.add_argument("--from-starter", action="store_true")
    sp = cmd(g, "set", allow_set, "change fields, like `rules set`", json_out=False)
    sp.add_argument("id")
    sp.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    cmd(g, "on", allow_toggle, "switch on", json_out=False).add_argument("id")
    cmd(g, "off", allow_toggle, "switch off", json_out=False).add_argument("id")
    cmd(g, "remove", allow_remove, "delete", json_out=False).add_argument("id")

    g = group("except", "things that look like a rule but are fine")
    cmd(g, "list", except_list, "per rule: typed, and learned from “Not this one”").add_argument("rule", nargs="?")
    sp = cmd(g, "add", except_add, "an exception in your words; the model reads it with the rule", json_out=False)
    sp.add_argument("rule")
    sp.add_argument("text", help='"a lecture or conference talk"')
    sp = cmd(g, "remove", except_remove, "remove one (the exact text, title or URL `except list` shows)", json_out=False)
    sp.add_argument("rule")
    sp.add_argument("text")

    g = group("never", "apps and sites where no rule ever fires")
    cmd(g, "list", never_list, "every never-here place")
    for name, fn, help in (("add", never_add, "no rule fires here again"), ("remove", never_remove, "undo")):
        sp = cmd(g, name, fn, help, json_out=False)
        sp.add_argument("--site", help="example.com")
        sp.add_argument("--app", help="bundle id, e.g. net.whatsapp.WhatsApp")
        sp.add_argument("--name", help="the app's name, for display")

    g = group("settings", "budgets on or off, apps never read, sites never judged")
    cmd(g, "show", settings_show, "every setting, with what it does")
    sp = cmd(g, "set", settings_set, "budgets=true, no_monitor+=com.example.App, allow_sites+=github.com", json_out=False)
    sp.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
