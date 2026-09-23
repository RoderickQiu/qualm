"""Every way to make Qualm yours, as commands: rules, allowed kinds of page,
exceptions, never-here places and settings, plus `config` (the whole thing
as data), `schema` (what every field means) and `status` (is it running).
The review page and the pop-up change the same files, so any of them can
undo the others.

Built to be driven by an agent (Claude Code: .claude/skills/qualm) as well
as by hand:
- every command takes --json, errors included: {"error": {"code", "message"}};
- exit codes: 0 ok, 2 invalid, 3 not found, 4 over the question limit,
  5 model or app unreachable;
- every change takes --dry-run, which prints the diff and saves nothing;
- `config apply` changes many things at once, checked together;
- nothing that fails a check is ever saved, and `config undo` reverts the
  last change.
docs/PERSONALIZE.md walks through it.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sys
from dataclasses import MISSING, asdict, fields
from datetime import datetime
from pathlib import Path

from .config import EXAMPLE, Config, apply_assignments, field_type, starter_blocks
from .rules import ID_RE, AllowClass, OverLimit, Rule, Settings, capacity

EXIT = {"invalid": 2, "not_found": 3, "over_limit": 4, "unreachable": 5}


class CliError(Exception):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.message, self.code = message, code


def _fail(msg: str, code: str = "invalid"):
    raise CliError(msg, code)


# One command's run: whether it's a dry run, and what it would have changed.
SESSION: dict = {"dry_run": False, "configs": [], "appends": []}


def config_path(path: str) -> Config:
    """rules.toml, created from the starter rules the first time."""
    p = Path(path)
    if not p.exists():
        if SESSION["dry_run"]:
            _fail(f"{p} doesn't exist yet; run once without --dry-run", "not_found")
        shutil.copyfile(EXAMPLE, p)
        print(f"created {p} from the starter rules (rules.example.toml)", file=sys.stderr)
    for c in SESSION["configs"]:
        if c.path == p:
            return c
    cfg = Config(p, dry_run=SESSION["dry_run"])
    SESSION["configs"].append(cfg)
    return cfg


def _out(args, obj, text: str) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2) if getattr(args, "json", False) else text)


def _append(data_dir: Path, e: dict) -> None:
    e = {**e, "at": datetime.now().isoformat(timespec="seconds")}
    if SESSION["dry_run"]:
        SESSION["appends"].append({"file": str(data_dir / "exceptions.jsonl"), "line": e})
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "exceptions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _usage_today(data_dir: Path) -> dict:
    p = data_dir / "usage.json"
    if not p.exists():
        return {}
    saved = json.loads(p.read_text(encoding="utf-8"))
    return saved.get("counts", {}) if saved.get("day") == datetime.now().date().isoformat() else {}


def _capacity_line(cap: dict) -> str:
    peak = "" if cap["peak"] == cap["now"] else f", {cap['peak']} at the busiest ({cap['peak_at']})"
    return f"Questions per reading: {cap['now']} of {cap['limit']} now{peak}."


def summary(r: Rule, settings: Settings) -> str:
    """The rule in one sentence."""
    what = r.text(settings.lang)
    if r.kind == "deny":
        s = f"Steps in on {what}"
    else:
        s = f"Checks in on {what}: asks what for and for how long, and steps in when that time is up"
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
    if r.kind == "check_in":
        u = usage.get(r.id, {})
        d["today"] = {"minutes": round(u.get("seconds", 0) / 60, 1), "sessions": u.get("sessions", 0)}
    return d


def _find(items, id: str, what: str):
    found = next((x for x in items if x.id == id), None)
    if found is None:
        _fail(f"no {what} {id!r}; there are: {', '.join(x.id for x in items) or 'none'}", "not_found")
    return found


def _changed(args, what: dict, text: str) -> None:
    """After a change: what it was, and the question budget it leaves."""
    settings, rules = config_path(args.rules).load()
    cap = capacity(settings, rules)
    _out(args, {**what, "capacity": cap}, f"{text}\n{_capacity_line(cap)}")


# -- rules ------------------------------------------------------------------


def rules_list(args) -> None:
    settings, rules = config_path(args.rules).load()
    usage = _usage_today(Path(args.data))
    rows = [_rule_json(r, settings, usage) for r in rules]
    cap = capacity(settings, rules)
    lines = [_capacity_line(cap), ""]
    for r, d in zip(rules, rows):
        state = "off" if not r.enabled else "on" if d["active_now"] else "on, not now"
        today = f"  today: {d['today']['minutes']:g} min, {d['today']['sessions']} sessions" if "today" in d else ""
        lines.append(f"{r.id:<12} [{state}] threshold {r.threshold:g}{today}\n    {d['summary']}")
    if not rules:
        lines.append("no rules. `qualm rules starters` lists ready-made ones; `rules add` makes your own.")
    _out(args, {"capacity": cap, "rules": rows}, "\n".join(lines))


def rules_show(args) -> None:
    from .review import exceptions

    settings, rules = config_path(args.rules).load()
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
            _fail(f"no starter rule {args.id!r}; `rules starters` lists them", "not_found")
        cfg.add("rules", {"id": args.id}, text=starters[args.id][1])
    else:
        if not re.fullmatch(ID_RE, args.id):
            _fail(f"id {args.id!r}: lowercase letters, digits and _, starting with a letter")
        if not args.what:
            _fail('say what the rule is about: --what "short videos made for endless swiping"')
        entry = {"id": args.id, "kind": "check_in" if args.check_in else "deny", "description": args.what}
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
        entry |= apply_assignments(Rule, entry, args.set or [])[0]
        cfg.add("rules", entry)
    settings, rules = cfg.load()
    r = _find(rules, args.id, "rule")
    _changed(args, {"added": _rule_json(r, settings, {})}, f"added {r.id}: {summary(r, settings)}\n"
             f"Untested: see what it would catch with `qualm rules test {r.id}`.")


def _assignments(args, table: str, cls, id: str, pairs: list[str]) -> tuple[dict, list[str]]:
    settings, rules = config_path(args.rules).load()
    obj = _find(rules if table == "rules" else settings.allow, id, "rule" if table == "rules" else "allow class")
    # `what` is whichever description the model reads.
    pairs = [obj.text_field(settings.lang) + p[4:] if p.startswith(("what=", "what+=")) else p for p in pairs]
    return apply_assignments(cls, asdict(obj), pairs)


def _rule_changed(args, verb: str) -> None:
    settings, rules = config_path(args.rules).load()
    r = _find(rules, args.id, "rule")
    _changed(args, {"rule": _rule_json(r, settings, {})}, f"{verb} {r.id}: {summary(r, settings)}")


def rules_set(args) -> None:
    set_, unset = _assignments(args, "rules", Rule, args.id, args.pairs)
    config_path(args.rules).edit("rules", args.id, set_, unset)
    _rule_changed(args, "saved")


def rules_on(args) -> None:
    config_path(args.rules).edit("rules", args.id, {}, ["enabled"])
    _rule_changed(args, "on")


def rules_off(args) -> None:
    config_path(args.rules).edit("rules", args.id, {"enabled": False}, [])
    _rule_changed(args, "off")


def rules_remove(args) -> None:
    config_path(args.rules).remove("rules", args.id)
    _changed(args, {"removed": args.id}, f"removed {args.id} (`config undo` brings it back)")


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
    _out(args, rows, text + "\n\nAdd one: qualm rules add ID --from-starter (allow classes: allow add ID --from-starter)")


def rules_test(args) -> None:
    """A rule as it is, a rule with new wording (--what), or a draft that isn't saved (a new id with --what)."""
    from dataclasses import replace

    from .decide import make_client
    from .trial import run

    settings, rules = config_path(args.rules).load()
    r = next((x for x in rules if x.id == args.id), None)
    if r is None:
        if not args.what:
            _find(rules, args.id, "rule")
        r = Rule(args.id, args.kind or "deny", args.what, threshold=args.threshold or 0.2)
    elif args.what or args.kind or args.threshold:
        r = replace(r, description=args.what or r.text(settings.lang), description_en="",
                    kind=args.kind or r.kind, threshold=args.threshold or r.threshold)
    draft = r not in rules
    data = Path(args.data)

    def progress(n, total):
        if not args.json and sys.stderr.isatty():
            print(f"\r  asking the model: {n}/{total}", end="" if n < total else "\n", file=sys.stderr, flush=True)

    try:
        rows = run(make_client(), data, r, settings, last=args.last, on_progress=progress)
    except Exception as e:  # the server is down or slow
        _fail(f"couldn't ask the model ({type(e).__name__}: {e}). Is the Kev server running? `qualm status`", "unreachable")
    if not rows:
        _fail(f"no screens in {data}/judgements.jsonl yet: run `qualm app` for a while first", "not_found")
    fires = [x for x in rows if x["does"] in ("pops up", "counts")]
    lines = [f"{r.id}{' (draft, not saved)' if draft else ''} on your last {len(rows)} distinct screens, at threshold "
             f"{r.threshold:g}: {len(fires)} would {'pop up' if r.kind == 'deny' else 'count'}.", ""]
    shown = rows if args.all else rows[:args.show]
    for x in shown:
        you = f"  you: {x['you_said']}" if x["you_said"] else ""
        why = f" ({x['why']})" if x["does"] not in ("nothing", "pops up", "counts") else ""
        lines.append(f"#{x['id']}  {x['p_hit']:.2f}  {x['does']}{why}{you}\n      {(x['title'] or x['app'])[:70]}  {x['url'][:70]}")
    if not args.all and len(rows) > args.show:
        lines.append(f"… {len(rows) - args.show} more, lower scores (--all to see them)")
    if draft:
        lines += ["", "Keep this wording: `rules add` (new) or `rules set ID what=...` (existing), then label and tune."]
    else:
        lines += ["", f"Tell it which are right:  qualm rules label {r.id} --yes ID ... --no ID ...",
                  f"Then:                     qualm rules tune {r.id} --apply"]
    _out(args, {"rule": r.id, "draft": draft, "reads": r.text(settings.lang), "threshold": r.threshold,
                "would_fire": len(fires), "screens": rows}, "\n".join(lines))


def rules_label(args) -> None:
    from .trial import label

    _, rules = config_path(args.rules).load()
    _find(rules, args.id, "rule")
    if not (args.yes or args.no):
        _fail("give --yes and/or --no with judgement ids from `rules test`")
    if SESSION["dry_run"]:
        SESSION["appends"].append({"file": str(Path(args.data) / "reviews.jsonl"),
                                   "line": {"rule": args.id, "yes": args.yes or [], "no": args.no or []}})
        return
    try:
        n = label(Path(args.data), args.id, args.yes or [], args.no or [])
    except ValueError as e:
        _fail(str(e), "not_found")
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
            lines.append(f"  apply it: qualm rules tune {r.id} --apply")
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
    if args.from_starter:
        starters = starter_blocks()
        if args.id not in starters or starters[args.id][0] != "allow":
            _fail(f"no starter allow class {args.id!r}; `rules starters` lists them", "not_found")
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
    _changed(args, {"added": args.id}, f"added {args.id}: pages the model reads as this are never flagged, "
             "except by a rule's own sites or patterns")


def allow_set(args) -> None:
    set_, unset = _assignments(args, "allow", AllowClass, args.id, args.pairs)
    config_path(args.rules).edit("allow", args.id, set_, unset)
    _changed(args, {"saved": args.id}, f"saved {args.id}")


def allow_toggle(args) -> None:
    off = args.cmd2 == "off"
    config_path(args.rules).edit("allow", args.id, {"enabled": False} if off else {}, [] if off else ["enabled"])
    _changed(args, {args.cmd2: args.id}, f"{args.id} {args.cmd2}")


def allow_remove(args) -> None:
    config_path(args.rules).remove("allow", args.id)
    _changed(args, {"removed": args.id}, f"removed {args.id}")


# -- exceptions and never-here ----------------------------------------------


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
    if args.rule:
        _find(rules, args.rule, "rule")
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
    _out(args, {"rule": r.id, "added": args.text}, f"{r.id}: the model now reads “{args.text}” as fine")


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
            _fail(f"{r.id} has no exception {args.text!r}; `except list {r.id}` shows them", "not_found")
        _append(Path(args.data), {k: match[k] for k in ("rule", "text", "title", "url") if match.get(k)} | {"removed": True})
    _out(args, {"rule": r.id, "removed": args.text}, f"removed from {r.id}: {args.text}")


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
    place = _place(args)
    _append(Path(args.data), {"never": place})
    _out(args, {"added": place}, "saved: no rule fires there")


def never_remove(args) -> None:
    from .review import never_places

    place = _place(args)
    key = place.get("host") or place.get("app")
    if not any((n.get("host") or n.get("app")) == key for n in never_places(Path(args.data))):
        _fail(f"{key} isn't a never-here place; `never list` shows them", "not_found")
    _append(Path(args.data), {"never": place, "removed": True})
    _out(args, {"removed": place}, "removed")


# -- settings -----------------------------------------------------------------


def _session_json(data_dir: Path) -> dict:
    from .policy import read_session

    s = read_session(data_dir)
    now = datetime.now().timestamp()
    focus = s["focus"]
    return {
        "paused": bool(s["paused_until"]),
        "paused_until": datetime.fromtimestamp(s["paused_until"]).isoformat(timespec="minutes") if s["paused_until"] else None,
        "focus": {"intent": focus["intent"], "until": datetime.fromtimestamp(focus["until"]).isoformat(timespec="minutes"),
                  "minutes_left": max(0, round((focus["until"] - now) / 60))} if focus else None,
    }


def _session_line(sj: dict) -> str:
    f = sj["focus"]
    parts = [f"focus: {f['intent']} until {f['until'][11:]} ({f['minutes_left']} min left)" if f else "no focus session"]
    parts.append(f"paused until {sj['paused_until'][11:]}" if sj["paused"] else "not paused")
    return "; ".join(parts)


def focus(args) -> None:
    """Start, show or end a focus session (the running app picks it up within a second)."""
    from .policy import end_focus, start_focus

    data = Path(args.data)
    if args.stop:
        ended = end_focus(data)
        _out(args, {"ended": ended["intent"] if ended else None, **_session_json(data)},
             f"ended: {ended['intent']}" if ended else "no focus session was running")
        return
    if args.intent:
        intent = " ".join(args.intent)
        start_focus(data, intent, args.minutes)
    sj = _session_json(data)
    text = _session_line(sj)
    if args.intent:
        text = (f"Focus on “{sj['focus']['intent']}” until {sj['focus']['until'][11:]}. Every rule hit steps in at once, "
                "check-ins included, and the pop-up reminds you of this. End it: qualm focus --stop")
    _out(args, sj, text)


def pause(args) -> None:
    """Pause every rule for a while, or resume."""
    from .policy import pause_for

    data = Path(args.data)
    if not args.stop and not 0 < args.minutes <= 24 * 60:
        _fail("pause for 1 minute to 24 hours (qualm pause --stop resumes)")
    pause_for(data, 0 if args.stop else args.minutes)
    sj = _session_json(data)
    _out(args, sj, f"paused until {sj['paused_until'][11:]}; qualm pause --stop resumes" if sj["paused"] else "resumed: watching")


def doctor(args) -> None:
    """Everything Qualm needs, checked; exit 2 if something must be fixed."""
    from .doctor import FAIL, checks, report

    results = checks(args.rules, args.data, args.kev_dir)
    _out(args, {"checks": results}, report(results))
    if any(r["status"] == FAIL for r in results):
        raise SystemExit(EXIT["invalid"])


def settings_show(args) -> None:
    settings, _ = config_path(args.rules).load()
    d = {k: v for k, v in asdict(settings).items() if k != "allow"}
    _out(args, d, "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}\n    {FIELDS['settings'][k]}" for k, v in d.items()))


def settings_set(args) -> None:
    cfg = config_path(args.rules)
    settings, _ = cfg.load()
    set_, unset = apply_assignments(Settings, asdict(settings), args.pairs)
    cfg.edit_settings(set_, unset)
    _changed(args, {"set": set_, "unset": unset}, "saved: " + ", ".join([*set_, *unset]))


# -- the whole config, schema, status ----------------------------------------


def config_export(args) -> None:
    data = config_path(args.rules).export()
    print(json.dumps(data, ensure_ascii=False, indent=2))


def config_apply(args) -> None:
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    try:
        desired = json.loads(raw)
    except json.JSONDecodeError as e:
        _fail(f"not JSON: {e}")
    cfg = config_path(args.rules)
    changes = cfg.apply(desired, prune=args.prune)
    text = "\n".join(f"{c['op']} {c.get('table', '')} {c.get('id', '')}".rstrip() +
                     (f": set {json.dumps(c['set'], ensure_ascii=False)}" if c.get("set") else "") +
                     (f" unset {c['unset']}" if c.get("unset") else "") for c in changes) or "no changes"
    _changed(args, {"changes": changes}, text)


def config_check(args) -> None:
    settings, rules = config_path(args.rules).load()  # raises with what's wrong
    cap = capacity(settings, rules)
    _out(args, {"ok": True, "capacity": cap, "rules": len(rules), "allow": len(settings.allow)},
         f"ok: {len(rules)} rules, {len(settings.allow)} allow classes. {_capacity_line(cap)}")


def config_migrate(args) -> None:
    changed = config_path(args.rules).migrate()
    _changed(args, {"changes": changed}, "\n".join(changed) or "nothing to migrate")


def config_undo(args) -> None:
    config_path(args.rules).undo()
    _changed(args, {"undone": True}, "back to the version before the last change (undo again to redo)")


def _help_of(cls, key: str) -> str:
    return FIELDS["rules" if cls is Rule else "allow" if cls is AllowClass else "settings"].get(key, "")


def schema(args) -> None:
    """Every field, its type, default and meaning, and the grammar of the
    values that have one: enough to write rules.toml or `config apply` input
    without reading the code."""
    out = {}
    for table, cls in (("rules", Rule), ("allow", AllowClass), ("settings", Settings)):
        out[table] = {}
        for f in fields(cls):
            if f.name == "allow":
                continue
            t = field_type(cls, f.name)
            default = None if f.default is MISSING else list(f.default) if isinstance(f.default, tuple) else f.default
            out[table][f.name] = {"type": {list: "list of strings", str: "string", bool: "bool", int: "int", float: "number"}[t],
                                  "required": f.default is MISSING, "default": default, "help": _help_of(cls, f.name)}
    out["grammar"] = GRAMMAR
    out["commands"] = "qualm --help; every command: --help, --json; every change: --dry-run"
    text = []
    for table in ("rules", "allow", "settings"):
        text.append(f"[{table}]")
        for k, v in out[table].items():
            req = " (required)" if v["required"] else f" = {json.dumps(v['default'], ensure_ascii=False)}"
            text.append(f"  {k}: {v['type']}{req}\n      {v['help']}")
    text.append("")
    text += [f"{k}: {v}" for k, v in GRAMMAR.items()]
    _out(args, out, "\n".join(text))


def status(args) -> None:
    """Is the app up, is the model up, does the config load, how full is it."""
    import os
    import socket
    import urllib.request

    from .review import PORT, load_judgements

    def listening(port: int) -> bool:
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0

    out: dict = {"app_running": listening(PORT), "review_page": f"http://127.0.0.1:{PORT}/"}
    kev = os.environ.get("KEV_URL", "http://127.0.0.1:8009")
    try:
        with urllib.request.urlopen(f"{kev}/v1/models", timeout=3) as r:
            models = json.loads(r.read())
        m = (models.get("models") or models.get("data") or [{}])[0]
        out["model"] = {"reachable": True, "url": kev, "id": m.get("run") or m.get("name") or m.get("id", "")}
    except Exception as e:
        out["model"] = {"reachable": False, "url": kev, "error": type(e).__name__}
    try:
        settings, rules = config_path(args.rules).load()
        out["config"] = {"ok": True, "rules_on_now": [r.id for r in rules if r.active()],
                         "capacity": capacity(settings, rules)}
    except ValueError as e:
        out["config"] = {"ok": False, "error": str(e)}
    js = load_judgements(Path(args.data))
    if js:
        j = js[-1]
        out["last_judgement"] = {"at": j["at"], "app": j["screen"].get("app", ""), "title": j["screen"].get("window_title", ""),
                                 "decisions": j["decisions"]}
    out["today"] = _usage_today(Path(args.data))
    out["session"] = _session_json(Path(args.data))
    m, c = out["model"], out["config"]
    lines = [f"app: {'running' if out['app_running'] else 'not running (qualm app)'}",
             f"model: {'up, ' + m['id'] if m['reachable'] else 'unreachable at ' + m['url'] + ' (HANDOFF.md, Run it)'}",
             f"config: {'ok, ' + _capacity_line(c['capacity']) if c['ok'] else 'broken: ' + c['error']}"]
    if c.get("ok"):
        lines.append(f"on now: {', '.join(c['rules_on_now']) or 'no rules'}")
    lines.append(_session_line(out["session"]))
    if "last_judgement" in out:
        lj = out["last_judgement"]
        acts = "; ".join(f"{d['action']} {d['rule']}".strip() for d in lj["decisions"]) or "nothing"
        lines.append(f"last judged: {lj['at'][11:]} {lj['app']} | {lj['title'][:50]} -> {acts}")
    _out(args, out, "\n".join(lines))
    if not (out["app_running"] and m["reachable"] and c["ok"]):
        raise SystemExit(EXIT["unreachable"] if c["ok"] else EXIT["invalid"])


FIELDS = {
    "rules": {
        "id": "short name, used in logs: lowercase letters, digits and _, starting with a letter; can't change later",
        "kind": "deny: step in at once; check_in: ask what for and for how long on arrival, step in when that time is up",
        "description": "what the rule is about, in your words; the model reads it. Describe the mode, not the site",
        "description_en": "optional English version, sent instead when [settings] lang = \"en\"",
        "exceptions": "things that look like a hit but are fine; the model reads them with the rule",
        "threshold": "the model's score (0-1) that counts as a hit; Kev-4B scores run low, so 0.15-0.5. `rules tune` sets it",
        "target": "content: judge the opened item, feeds never hit; page: judge the page itself, feeds and home pages can hit",
        "sites": "always a hit on these sites, without the model",
        "patterns": "the same as URL regexes, for what sites can't say",
        "allow_learning": "lectures, tutorials and docs never hit this rule",
        "allow_intentional": "one item opened from search, a work app or a chat link is fine",
        "feed_hit": "any page the model reads as an entertainment feed hits this rule",
        "enabled": "false: kept in the file, but never asked and never fires",
        "when": "only at these times; empty = always",
        "note": "why it's set this way, for whoever reads the file next",
    },
    "allow": {
        "id": "short name: lowercase letters, digits and _",
        "description": "a kind of page no rule fires on, in your words (\"an online store\")",
        "description_en": "optional English version",
        "threshold": "the model's yes-probability that counts",
        "sites": "known to be this kind: allowed without the model",
        "patterns": "the same as URL regexes",
        "apps": "bundle ids known to be this kind",
        "enabled": "false: kept, but not asked",
        "note": "why it's there",
    },
    "settings": {
        "lang": "en: rules with a description_en send that; anything else sends description",
        "no_monitor": "apps (bundle ids) never read at all",
        "allow_sites": "sites never judged; links from them count as opened on purpose",
        "allow_urls": "the same, as URL regexes",
        "max_wait_s": "the longest wait, in seconds, before a check-in or \"I need it\" unlocks; it doubles with each "
                      "session today (the first is free) and again when you come back within 20 min of one ending",
        "extensions": "how many \"5 more\" a check-in session may get when its time is up (0: none)",
        "block_clicks": "true: while a pop-up is open the dimmed screen takes the clicks, so you answer it first; "
                        "false: you can click through the dim (also in the menu bar)",
        "max_questions": "questions one reading may ask (rules on at once + allow classes + 3 shared); "
                         "more slows the model sharply (25 is ~1.5 s on Kev-4B, 24 GB Mac)",
    },
}

GRAMMAR = {
    "sites": '"douyin.com" = the site and its subdomains; "youtube.com/shorts" = that path and under it; '
             '"youtube.com/" = the home page only',
    "when": 'days and/or hours: "mon-fri 09:00-18:00", "weekends", "sat,sun", "22:00-02:00" (past midnight belongs '
            'to the day it starts), "daily 12:00-13:00"; several entries = any of them',
    "cli assignments": "key=value sets; key+=item / key-=item edit a list; key= removes the key (back to default); "
                       'lists also take a JSON array: sites=\'["a.com","b.com"]\'; what=... sets the description the model reads',
    "limit": "rules on at the same moment + enabled allow classes + 3 shared questions <= max_questions; "
             "a change that would go over is refused (exit code 4)",
}


# -- the parser ---------------------------------------------------------------


def run(args) -> None:
    """Run one command with dry runs, --json errors and exit codes."""
    SESSION.update(dry_run=getattr(args, "dry_run", False), configs=[], appends=[])
    as_json = getattr(args, "json", False)
    try:
        if SESSION["dry_run"]:
            with contextlib.redirect_stdout(io.StringIO()):
                args.cmd_fn(args)
            diff = "".join(c.diff() for c in SESSION["configs"])
            _out(args, {"dry_run": True, "diff": diff, "appends": SESSION["appends"]},
                 (diff or "rules.toml: no change") +
                 "".join(f"\nwould append to {a['file']}: {json.dumps(a['line'], ensure_ascii=False)}" for a in SESSION["appends"]) +
                 "\n(dry run: nothing saved)")
        else:
            args.cmd_fn(args)
    except (CliError, OverLimit, ValueError, KeyError) as e:
        code = e.code if isinstance(e, CliError) else "over_limit" if isinstance(e, OverLimit) else \
            "not_found" if isinstance(e, KeyError) else "invalid"
        msg = e.message if isinstance(e, CliError) else e.args[0] if e.args else str(e)
        if as_json:
            print(json.dumps({"error": {"code": code, "message": msg}}, ensure_ascii=False, indent=2))
        else:
            print(f"error: {msg}", file=sys.stderr)
        raise SystemExit(EXIT[code])


def register(sub, defaults: dict) -> None:
    """Adds rules / allow / except / never / settings / config / schema / status to the main parser."""

    def group(name, help):
        g = sub.add_parser(name, help=help, description=help)
        return g.add_subparsers(dest="cmd2", required=True, metavar="ACTION")

    def cmd(g, name, fn, help, changes=False):
        sp = g.add_parser(name, help=help, description=help)
        sp.add_argument("--rules", default=defaults["rules"], help="the rules file")
        sp.add_argument("--data", default=defaults["data"], help="the data folder")
        sp.add_argument("--json", action="store_true", help="machine-readable output, errors included")
        if changes:
            sp.add_argument("--dry-run", action="store_true", help="show the diff; save nothing")
        sp.set_defaults(fn=run, cmd_fn=fn)
        return sp

    g = group("rules", "list, add, change, test and tune your rules")
    cmd(g, "list", rules_list, "every rule in a sentence: on or off, active now, today's usage, question budget")
    cmd(g, "show", rules_show, "one rule, every field").add_argument("id")
    sp = cmd(g, "add", rules_add, "a new rule, in your words (or a starter: --from-starter)", changes=True)
    sp.add_argument("id", help="short name: lowercase, digits, _")
    sp.add_argument("--what", help='what it is about, in your words: "short videos made for endless swiping"')
    sp.add_argument("--check-in", action="store_true",
                    help="ask what for and for how long on arrival, and step in when that time is up, instead of stepping in at once")
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
    sp = cmd(g, "set", rules_set, "change fields: threshold=0.3 what=\"...\" sites+=tiktok.com when-=weekends note=", changes=True)
    sp.add_argument("id")
    sp.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    cmd(g, "on", rules_on, "switch a rule on", changes=True).add_argument("id")
    cmd(g, "off", rules_off, "switch a rule off (kept in the file)", changes=True).add_argument("id")
    cmd(g, "remove", rules_remove, "delete a rule from the file", changes=True).add_argument("id")
    cmd(g, "starters", rules_starters, "the ready-made rules and allow classes, and which you have")
    sp = cmd(g, "test", rules_test, "what a rule would do on your recent screens (asks the model); "
             "--what tries other wording, or a draft rule under a new id, without saving")
    sp.add_argument("id")
    sp.add_argument("--what", help="wording to try instead of the saved one (or for a draft)")
    sp.add_argument("--kind", choices=("deny", "check_in"), help="for a draft, or to try the other kind")
    sp.add_argument("--threshold", type=float, help="threshold to try")
    sp.add_argument("--last", type=int, default=100, help="how many recent distinct screens")
    sp.add_argument("--show", type=int, default=20, help="how many to print, highest score first")
    sp.add_argument("--all", action="store_true", help="print all of them")
    sp = cmd(g, "label", rules_label, "say which screens really are this rule (ids from `rules test`)", changes=True)
    sp.add_argument("id")
    sp.add_argument("--yes", nargs="+", metavar="JID")
    sp.add_argument("--no", nargs="+", metavar="JID")
    sp = cmd(g, "tune", rules_tune, "the threshold from your answers", changes=True)
    sp.add_argument("id")
    sp.add_argument("--apply", action="store_true", help="write it to rules.toml")
    sp.add_argument("--precision", type=float, default=0.9, help="share of hits that must be right")

    g = group("allow", "kinds of page no rule fires on (shopping, music)")
    cmd(g, "list", allow_list, "every allow class")
    sp = cmd(g, "add", allow_add, "a kind of page never flagged, in your words", changes=True)
    sp.add_argument("id")
    sp.add_argument("--what", help='"a music player or music streaming site"')
    sp.add_argument("--threshold", type=float, default=0.5)
    sp.add_argument("--site", action="append", help="known to be this: allowed without the model; repeat")
    sp.add_argument("--app", action="append", help="bundle id known to be this; repeat")
    sp.add_argument("--from-starter", action="store_true")
    sp = cmd(g, "set", allow_set, "change fields, like `rules set`", changes=True)
    sp.add_argument("id")
    sp.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    cmd(g, "on", allow_toggle, "switch on", changes=True).add_argument("id")
    cmd(g, "off", allow_toggle, "switch off", changes=True).add_argument("id")
    cmd(g, "remove", allow_remove, "delete", changes=True).add_argument("id")

    g = group("except", "things that look like a rule but are fine")
    cmd(g, "list", except_list, "per rule: typed, and learned from “Not this one”").add_argument("rule", nargs="?")
    sp = cmd(g, "add", except_add, "an exception in your words; the model reads it with the rule", changes=True)
    sp.add_argument("rule")
    sp.add_argument("text", help='"a lecture or conference talk"')
    sp = cmd(g, "remove", except_remove, "remove one (the exact text, title or URL `except list` shows)", changes=True)
    sp.add_argument("rule")
    sp.add_argument("text")

    g = group("never", "apps and sites where no rule ever fires")
    cmd(g, "list", never_list, "every never-here place")
    for name, fn, help in (("add", never_add, "no rule fires here again"), ("remove", never_remove, "undo")):
        sp = cmd(g, name, fn, help, changes=True)
        sp.add_argument("--site", help="example.com")
        sp.add_argument("--app", help="bundle id, e.g. net.whatsapp.WhatsApp")
        sp.add_argument("--name", help="the app's name, for display")

    g = group("settings", "apps never read, sites never judged, check-in waits, the question limit")
    cmd(g, "show", settings_show, "every setting, with what it does")
    sp = cmd(g, "set", settings_set, "max_wait_s=90, no_monitor+=com.example.App, allow_sites+=github.com", changes=True)
    sp.add_argument("pairs", nargs="+", metavar="KEY=VALUE")

    g = group("config", "the whole config as JSON: export, edit, apply in one checked step; check; undo")
    cmd(g, "export", config_export, "settings, allow classes and rules as JSON, as written (defaults left out)")
    sp = cmd(g, "apply", config_apply, "make rules.toml match a JSON file (or - for stdin) in the shape `export` "
             "prints: entries added or changed; a top-level key left out is left alone. All of it or nothing", changes=True)
    sp.add_argument("file", help="JSON file, or - for stdin")
    sp.add_argument("--prune", action="store_true", help="also remove rules and allow classes missing from the file")
    cmd(g, "check", config_check, "does rules.toml load, and how full is the question budget")
    cmd(g, "undo", config_undo, "back to the version before the last change; again to redo", changes=True)
    cmd(g, "migrate", config_migrate, "a rules.toml from before check-ins: time_cap rules become check_in, "
        "daily budgets are removed", changes=True)

    sp = sub.add_parser("doctor", help="is everything Qualm needs in place? each problem with its fix",
                        description="Checks the Mac, memory and swap, Accessibility, rules.toml, the Kev repo, the model "
                                    "server, the app and start-at-login, and says how to fix what isn't right.")
    sp.add_argument("--rules", default=defaults["rules"])
    sp.add_argument("--data", default=defaults["data"])
    sp.add_argument("--kev-dir", default="~/Documents/kev")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=run, cmd_fn=doctor)
    sp = sub.add_parser("focus", help="a focus session: say what you're here to do; every rule steps in at once until it ends",
                        description="Start a focus session (qualm focus write the report --minutes 50), show it (qualm focus), "
                                    "or end it (--stop). While it runs, every rule hit steps in at once, check-ins included, "
                                    "and the pop-up reminds you what you said.")
    sp.add_argument("intent", nargs="*", help="what you're here to do, in a few words")
    sp.add_argument("--minutes", type=float, default=50, help="how long (default 50)")
    sp.add_argument("--stop", action="store_true", help="end the session now")
    sp.add_argument("--data", default=defaults["data"])
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=run, cmd_fn=focus)
    sp = sub.add_parser("pause", help="pause every rule for a while (default 30 min); --stop resumes",
                        description="Pause every rule for MINUTES (default 30), or resume with --stop.")
    sp.add_argument("minutes", nargs="?", type=float, default=30)
    sp.add_argument("--stop", action="store_true", help="resume now")
    sp.add_argument("--data", default=defaults["data"])
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=run, cmd_fn=pause)
    sp = sub.add_parser("schema", help="every field: type, default, meaning, and the value grammar",
                        description="every field: type, default, meaning, and the value grammar")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=run, cmd_fn=schema)
    sp = sub.add_parser("status", help="app running? model up? config ok? what it judged last",
                        description="app running? model up? config ok? what it judged last (exit 5 if something is down)")
    sp.add_argument("--rules", default=defaults["rules"])
    sp.add_argument("--data", default=defaults["data"])
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=run, cmd_fn=status)
