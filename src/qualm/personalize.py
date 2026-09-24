"""Every way to make Qualm yours, as commands: rules, allowed kinds of page,
exceptions, never-here places and settings, plus `config` (the whole thing
as data), `schema` (what every field means) and `status` (is it running).
The review page and the pop-up change the same files, so any of them can
undo the others.

Built to be driven by an agent (Claude Code: .claude/skills/qualm) as well
as by hand:
- every command takes --json, errors included: {"error": {"code", "message"}}
  (usage errors too; a command with no JSON output says so the same way);
- exit codes: 0 ok, 2 invalid, 3 not found (code "no_screens": nothing to
  test on yet), 4 over the question limit, 5 model or app unreachable,
  1 something unexpected (the message says what);
- every change takes --dry-run, which prints the diff and saves nothing;
- `config apply` changes many things at once, checked together;
- nothing that fails a check is ever saved, and `config undo` steps back
  through the last saved changes to rules.toml.
docs/PERSONALIZE.md walks through it.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from dataclasses import MISSING, asdict, fields
from datetime import datetime, timedelta
from pathlib import Path

from . import jsonl
from .config import KEEP, Config, apply_assignments, field_type, starter_blocks
from .rules import ID_RE, AllowClass, OverLimit, Rule, Settings, capacity, in_window

EXIT = {"invalid": 2, "not_found": 3, "no_screens": 3, "over_limit": 4, "unreachable": 5, "unexpected": 1}
NO_SCREENS = "no screens yet: use your Mac with Qualm running for a while, then test"


class CliError(Exception):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.message, self.code = message, code


def _fail(msg: str, code: str = "invalid"):
    raise CliError(msg, code)


def _q() -> str:
    """How a terminal here runs qualm, for hints (agent.command)."""
    from .agent import command

    return command()


# One command's run: whether it's a dry run, what it would have changed, what
# to warn about, and (for a command that changes things) the locks it holds
# until it's done.
SESSION: dict = {"dry_run": False, "configs": [], "appends": [], "warnings": [], "hold": None, "json": False,
                 "preview": {}}  # a dry run's own account, beside the diff: what `config undo` would do


def config_path(path: str) -> Config:
    """rules.toml, created from the starter rules the first time (a dry run
    works on them without creating it). A command that changes things keeps
    it locked from here to the end, so no other change lands between what it
    read and what it saves."""
    p = Path(path)
    for c in SESSION["configs"]:
        if c.path == p:
            return c
    cfg = Config(p, dry_run=SESSION["dry_run"])
    if SESSION["hold"] is not None:
        SESSION["hold"].enter_context(cfg.locked())
    if cfg.create():
        note = f"created {p} from the starter rules (rules.example.toml)"
        print(note, file=sys.stderr)
        if SESSION.get("json"):  # stderr is easy to miss: a rules.toml gone missing comes back as the starters
            SESSION["warnings"].append(note)
    else:
        cfg.adopt()  # an older Qualm's rules.toml: recorded as its last save, so a later hand edit is told apart
    SESSION["configs"].append(cfg)
    return cfg


def _out(args, obj, text: str) -> None:
    if SESSION["warnings"] and isinstance(obj, dict):
        obj = obj | {"warnings": SESSION["warnings"]}
        text += "".join(f"\nwarning: {w}" for w in SESSION["warnings"])
    print(json.dumps(obj, ensure_ascii=False, indent=2) if getattr(args, "json", False) else text)


def _append(data_dir: Path, e: dict) -> None:
    e = {**e, "at": datetime.now().isoformat(timespec="seconds")}
    if SESSION["dry_run"]:
        SESSION["appends"].append({"file": str(data_dir / "exceptions.jsonl"), "line": e})
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    with jsonl.appending(data_dir / "exceptions.jsonl") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _usage_today(data_dir: Path) -> dict:
    p = data_dir / "usage.json"
    if not p.exists():
        return {}
    try:
        saved = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:  # cut short (older versions wrote it in place): nothing to show for today
        return {}
    return saved.get("counts", {}) if saved.get("day") == datetime.now().date().isoformat() else {}


def _capacity_line(cap: dict) -> str:
    peak = "" if cap["peak"] == cap["now"] else f", {cap['peak']} at the busiest ({cap['peak_at']})"
    return f"Questions per reading: {cap['now']} of {cap['limit']} now{peak}."


def _pattern_host(pattern: str) -> str:
    """The site a URL pattern plainly names: the host before its path, after
    an optional scheme and "(www\\.)?" ("twitch.tv" in r"^https?://(www\\.)?twitch\\.tv/\\w+"
    or "twitch.tv/\\w+"), else "": a choice of hosts ("(youtube|youtu)\\.be"), a
    file name ("index\\.php\\?id=") or a path alone say too little in words."""
    head = re.sub(r"\[[^\]]*\]", "~", pattern.replace("\\/", "/"))  # a class like [^/] is no path
    head = re.split(r"(?<![/:])/(?!/)", head, 1)[0]  # up to the path
    head = head.split("//", 1)[-1]  # after the scheme
    found = list(re.finditer(r"(?:[a-z0-9-]+\\?\.)+[a-z]{2,}(?![a-z0-9-])", head, re.I))
    if not found:
        return ""
    m = found[-1]
    before, after = head[:m.start()], head[m.end():]
    # Only anchors, flags and an optional www or subdomain before it; only an end, a port or the like after.
    if "|" in before or not re.fullmatch(r"(?:[()?^:~*+.]|\\\.|www|https?|s\?)*", re.sub(r"\(\?[a-z]+\)", "", before)):
        return ""
    if not re.fullmatch(r"(?:[)?$~]|\([~$|]*\)|:\\d\+|:\d+)*", after):
        return ""
    return m[0].replace("\\.", ".").lower().removeprefix("www.")


def _pattern_words(patterns) -> list[str]:
    """URL patterns in words, by the site each names ("certain pages of
    twitch.tv and kick.com"); a pattern that names none plainly as it is."""
    hosts, other = [], []
    for p in patterns:
        host = _pattern_host(p)
        if not host:
            other.append(f"/{p}/")
        elif host not in hosts:
            hosts.append(host)
    out = [f"certain pages of {', '.join(hosts[:-1])} and {hosts[-1]}" if len(hosts) > 1 else
           f"certain pages of {hosts[0]}"] if hosts else []
    return out + other


def summary(r: Rule, settings: Settings, patterns: bool = True) -> str:
    """The rule in one sentence. `patterns=False`: URL patterns in words (the
    dashboard, where a regex runs out of the card and says little to most people)."""
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
        pats = [f"/{p}/" for p in r.patterns] if patterns else _pattern_words(r.patterns)
        extra.append("always on " + ", ".join([*r.sites, *pats]))
    if r.apps:
        extra.append("always in " + ", ".join(r.apps))
    if over := [c.id for c in settings.allow if c.id in r.overrides_allow]:  # one that's gone overrides nothing
        extra.append("also on pages the allow class " + ", ".join(over) + " would let through")
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
    """The rules, and with them everything else that decides what fires: allow
    classes, never-here places and exceptions, so one command shows it all.
    The pages let through with "Not this one" are counted per rule, not listed
    (--pages lists them): their titles and addresses are what you read, and
    this is the first thing an agent runs."""
    from .agent import command
    from .review import never_places

    settings, rules = config_path(args.rules).load()
    data = Path(args.data)
    usage = _usage_today(data)
    rows = [_rule_json(r, settings, usage) for r in rules]
    cap = capacity(settings, rules)
    allow = [asdict(c) | {"reads": c.text(settings.lang)} for c in settings.allow]
    never = never_places(data)
    exceptions = _exception_rows(rules, data)
    pages = [x for x in exceptions if x["source"] == "not this one"]
    per_rule = {r.id: n for r in rules if (n := sum(x["rule"] == r.id for x in pages))}
    if not args.pages:
        exceptions = [x for x in exceptions if x["source"] != "not this one"]
    lines = [_capacity_line(cap), ""]
    for r, d in zip(rules, rows):
        state = "off" if not r.enabled else "on" if d["active_now"] else "on, not now"
        today = f"  today: {d['today']['minutes']:g} min, {d['today']['sessions']} sessions" if "today" in d else ""
        lines.append(f"{r.id:<12} [{state}] threshold {r.threshold:g}{today}\n    {d['summary']}")
    if not rules:
        lines.append(f"no rules. `{command()} rules starters` lists ready-made ones; `rules add` makes your own.")
    lines += ["", "Never flagged (allow classes, `allow list`): " + (", ".join(
        f"{c.id}{'' if c.enabled else ' (off)'}" for c in settings.allow) or "none")]
    lines.append("Never here (`never list`): " + (", ".join(
        n.get("host") or n.get("name") or n.get("app", "") for n in never) or "none"))
    typed = sum(x["source"] != "not this one" for x in exceptions)
    lines.append(f"Exceptions (`except list`): {typed} in words; pages let through with Not this one: " +
                 (", ".join(f"{rid} {n}" for rid, n in per_rule.items()) or "none"))
    if args.pages:
        lines += [f"    {_exception_line(x)}" for x in pages]
    _out(args, {"capacity": cap, "rules": rows, "allow": allow, "never": never, "exceptions": exceptions,
                "not_this_one": per_rule}, "\n".join(lines))


def rules_show(args) -> None:
    settings, rules = config_path(args.rules).load()
    r = _find(rules, args.id, "rule")
    d = _rule_json(r, settings, _usage_today(Path(args.data)))
    learned = [e for e in live_exceptions(Path(args.data)) if e["rule"] == r.id]
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
        if args.content_only and args.whole_page:
            _fail("--whole-page or --content-only, not both")
        settings, _ = cfg.load()
        if any(c.id == args.id for c in settings.allow):
            _fail(f"{args.id!r} is the name of an allow class (a kind of page never flagged: `{_q()} allow list`); "
                  f"give the rule another name, like limit_{args.id}")
        entry = new_rule(args.id, args.what, "check_in" if args.check_in else "deny", args.threshold, settings,
                         content_only=args.content_only, on_purpose_ok=args.on_purpose_ok)
        for key, val in (("sites", args.site), ("when", args.when)):
            if val:
                entry[key] = val
        if args.app:
            entry["apps"] = [_app(a)[0] for a in args.app]
        for key, on in (("allow_learning", args.learning_ok), ("feed_hit", args.feeds)):
            if on:
                entry[key] = True
        if args.off:
            entry["enabled"] = False
        if args.note:
            entry["note"] = args.note
        set_, unset = _app_assignments(Rule, entry, args.set or [])
        entry = {k: v for k, v in (entry | set_).items() if k not in unset}
        if "overrides_allow" not in {*set_, *unset}:  # from both descriptions: a "not" in either one counts
            entry.pop("overrides_allow", None)
            if over := overlaps("\n".join(entry.get(k, "") for k in ("description", "description_en")), settings):
                entry["overrides_allow"] = over
        cfg.add("rules", entry)
    settings, rules = cfg.load()
    r = _find(rules, args.id, "rule")
    over = [] if args.from_starter else list(r.overrides_allow)
    maybe, maybe_line = ([], "") if args.from_starter else _may_cancel(r, settings, [r.description, r.description_en])
    _changed(args, {"added": _rule_json(r, settings, {}), "overrides_allow_added": over, "may_cancel": maybe,
                    "can_be_cancelled_by": cancelled_by(r, settings), "overrides_allow_note": _cancel_note(r)},
             f"added {r.id}: {summary(r, settings)}\n{_overrides_line(r.id, over)}{maybe_line}"
             f"{_cancel_line(r, settings)}Untested: see what it would catch with `{_q()} rules test {r.id}`.")


def _stem(word: str) -> str:
    """shopping, shops -> shop; chats, chatting -> chat: close enough to match a word."""
    for end in ("ing", "s"):
        if word.endswith(end) and len(word) - len(end) >= 3:
            word = word[:-len(end)]
            break
    return word[:-1] if len(word) > 3 and word[-1] == word[-2] else word


# Words that say what a rule isn't about: "videos, but not music or chat",
# "anything other than chat", "non-chat apps", "shopping (books are fine)",
# "刷视频，不包括聊天". With any of them anywhere, a class the words name may
# be the one they leave out, so Qualm doesn't set overrides_allow itself.
EXCLUDING = re.compile(r"\b(?:not|no|never|nor|non|cannot|except\w*|exclud\w*|without|besides|apart|aside|but|"
                       r"unless|instead|rather|other\s+than|fine)\b|\b\w+n['’]t\b|不|除了|除外|以外|之外", re.I)


def excluding(what: str) -> str:
    """The first word in a rule's words that says what it isn't about ("" if none)."""
    m = EXCLUDING.search(what)
    return m.group(0) if m else ""


def named_classes(what: str, settings: Settings) -> list[str]:
    """Allow classes a rule's English words name ("online shopping" names the shopping class)."""
    words = {_stem(w) for w in re.findall(r"[a-z]+", what.lower())}
    return [c.id for c in settings.allow if all(_stem(part) in words for part in c.id.split("_"))]


def overlaps(what: str, settings: Settings, also: str = "") -> list[str]:
    """The allow classes to put in a new wording's overrides_allow: those its
    English words name, when neither they nor the rule's other description
    (`also`) say anything about what the rule isn't about. Without
    overrides_allow the class would let every such page through, and the
    rule would never fire where it's meant to. From "videos other than music
    or chat" nothing: see may_cancel."""
    return [] if excluding(what) or excluding(also) else named_classes(what, settings)


def may_cancel(what: str, settings: Settings, r: Rule, also: str = "") -> list[dict]:
    """The allow classes a rule's words name where they, or its other
    description (`also`), say what it isn't about ("online shopping, not
    groceries"): Qualm leaves the choice to you, with the command that makes
    the rule step in there too."""
    if not (excluding(what) or excluding(also)):
        return []
    return [{"id": c, "command": f"{_q()} rules set {r.id} overrides_allow+={c}"}
            for c in named_classes(what, settings) if c not in r.overrides_allow]


def _may_cancel_line(r: Rule, maybe: list[dict], cue: str) -> str:
    if not maybe:
        return ""
    ids = [m["id"] for m in maybe]
    named = ", ".join(ids[:-1]) + " and " + ids[-1] if len(ids) > 1 else ids[0]
    how = (f"Only if {r.id} is about {ids[0]} pages too: `{maybe[0]['command']}`" if len(ids) == 1 else
           f"Only for a class it's about too: `{_q()} rules set {r.id} overrides_allow+=CLASS`")
    still = "those allow classes still let" if len(ids) > 1 else "that allow class still lets"
    return (f"Its words name {named} but also say what it isn't about (“{cue}”), so Qualm didn't make "
            f"it step in on {named} pages: {still} them through. {how}.\n")


def _may_cancel(r: Rule, settings: Settings, texts: list[str]) -> tuple[list[dict], str]:
    """may_cancel over a rule's new words (`texts`: its description, description_en or both), and the line that
    says so. A "not" in its other description counts too."""
    new, kept = "\n".join(texts), "\n".join(t for t in (r.description, r.description_en) if t not in texts)
    maybe = may_cancel(new, settings, r, also=kept)
    return maybe, _may_cancel_line(r, maybe, excluding(new) or excluding(kept))


def cancelled_by(r: Rule, settings: Settings) -> list[dict]:
    """The allow classes that let their pages through whatever this rule says."""
    return [{"id": c.id, "description": c.text(settings.lang)} for c in settings.allow
            if c.enabled and c.id not in r.overrides_allow]


def _cancel_note(r: Rule) -> str:
    return (f"Pages the model reads as one of these allow classes are let through whatever {r.id} says. Qualm sets "
            f"overrides_allow by itself only where the rule's English words name a class and say nothing about what "
            f"it isn't about (not, except, other than…: see may_cancel); if {r.id} is about one of "
            f"these kinds of page too, in any language, `{_q()} rules set {r.id} overrides_allow+=CLASS` makes it "
            "step in there as well.")


def _cancel_line(r: Rule, settings: Settings) -> str:
    ids = [c["id"] for c in cancelled_by(r, settings)]
    if not ids:
        return ""
    named = ", ".join(ids[:-1]) + " or " + ids[-1] if len(ids) > 1 else ids[0]
    return (f"{'Other allow' if r.overrides_allow else 'Allow'} classes win over it: pages the model reads as {named} "
            f"are let through whatever it says. If it's about one of those too: "
            f"`{_q()} rules set {r.id} overrides_allow+=CLASS`.\n")


def new_rule(id: str, what: str, kind: str, threshold: float, settings: Settings,
             content_only: bool = False, on_purpose_ok: bool = True) -> dict:
    """A new rule as `rules add` writes it, and as `rules test` tries a draft.
    A deny rule judges the whole page, so a news front page, a store or a
    scoreboard can hit (content_only: only what's opened, the way rules made
    before this work); one item opened on purpose is fine, as for the
    starters that have it; and an allow class its words name doesn't cancel it."""
    entry = {"id": id, "kind": kind, "description": what, "threshold": threshold}
    if kind == "deny" and not content_only:
        entry["target"] = "page"
    if on_purpose_ok:
        entry["allow_intentional"] = True
    if over := overlaps(what, settings):
        entry["overrides_allow"] = over
    return entry


def _overrides_line(rule_id: str, added: list[str]) -> str:
    if not added:
        return ""
    return (f"It's about the same pages as the allow class {', '.join(added)}, so it steps in there anyway "
            f"(`{_q()} rules set {rule_id} overrides_allow=` lets that class win again).\n")


def _assignments(args, table: str, cls, id: str, pairs: list[str]) -> tuple[dict, list[str]]:
    settings, rules = config_path(args.rules).load()
    obj = _find(rules if table == "rules" else settings.allow, id, "rule" if table == "rules" else "allow class")
    # `what` is whichever description the model reads.
    pairs = [obj.text_field(settings.lang) + p[4:] if p.startswith(("what=", "what+=")) else p for p in pairs]
    return _app_assignments(cls, asdict(obj), pairs)


APP_LISTS = ("apps", "no_monitor")  # lists of apps: a name typed is saved as the installed app's bundle id


def _app_assignments(cls, current: dict, pairs: list[str]) -> tuple[dict, list[str]]:
    """apply_assignments, with the apps typed into a list of apps (a rule's or
    an allow class's `apps`, `no_monitor`) resolved as `never add --app` does
    (see _app): `apps+=Steam` saves Steam's bundle id, or Steam as typed with
    a warning; `apps-=Steam` removes it saved either way."""
    fixed = []
    for p in pairs:
        m = re.fullmatch(r"(apps|no_monitor)\s*-=(.+)", p, re.S)
        have = current.get(m.group(1)) or () if m else ()
        if m and m.group(2) not in have and (bid := _app(m.group(2).strip(), adding=False)[0]) in have:
            p = f"{m.group(1)}-={bid}"
        fixed.append(p)
    set_, unset = apply_assignments(cls, current, fixed)
    for key in APP_LISTS:
        if isinstance(set_.get(key), list):
            have = current.get(key) or ()
            set_[key] = list(dict.fromkeys(a if a in have else _app(a.strip())[0] for a in set_[key]))
    return set_, unset


def _rule_changed(args, verb: str, over: list[str] | None = None, words: list[str] | None = None) -> None:
    """After a change to a rule; `over` for new wording (`words`): the allow classes it now steps in on."""
    settings, rules = config_path(args.rules).load()
    r = _find(rules, args.id, "rule")
    maybe, maybe_line = _may_cancel(r, settings, words or [])
    extra = {} if over is None else {"overrides_allow_added": over, "may_cancel": maybe,
                                     "can_be_cancelled_by": cancelled_by(r, settings),
                                     "overrides_allow_note": _cancel_note(r)}
    cancel = "" if over is None else _cancel_line(r, settings)
    _changed(args, {"rule": _rule_json(r, settings, {}), **extra},
             f"{verb} {r.id}: {summary(r, settings)}" + ("\n" + _overrides_line(r.id, over).rstrip() if over else "")
             + ("\n" + maybe_line.rstrip() if maybe_line else "") + ("\n" + cancel.rstrip() if cancel else ""))


def rules_set(args) -> None:
    set_, unset = _assignments(args, "rules", Rule, args.id, args.pairs)
    # New wording that names an allow class: the rule steps in on those pages too, as with `rules add`.
    # Either description counts, whichever the model reads now: a first description_en becomes the one it reads.
    settings, rules = config_path(args.rules).load()
    r = _find(rules, args.id, "rule")
    reworded, over = [k for k in ("description", "description_en") if k in {*set_, *unset}], None
    if reworded:
        new = "\n".join(set_[k] for k in reworded if k in set_)
        kept = "\n".join(getattr(r, k) for k in ("description", "description_en") if k not in reworded)
        found = overlaps(new, settings, also=kept)  # a "not" in the description that stays counts too
        over = [] if "overrides_allow" in {*set_, *unset} else [c for c in found if c not in r.overrides_allow]
        if over:
            set_["overrides_allow"] = [*r.overrides_allow, *over]
    config_path(args.rules).edit("rules", args.id, set_, unset)
    _rule_changed(args, "saved", over, [set_[k] for k in reworded if k in set_])


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

    from .trial import HITS, run

    settings, rules = config_path(args.rules).load()
    r = next((x for x in rules if x.id == args.id), None)
    if r is None:
        if not args.what:
            _find(rules, args.id, "rule")
        entry = new_rule(args.id, args.what, args.kind or "deny", args.threshold or 0.2, settings, args.content_only)
        r = Rule(**{k: tuple(v) if isinstance(v, list) else v for k, v in entry.items()})
    elif args.what or args.kind or args.threshold:
        over = r.overrides_allow + tuple(c for c in overlaps(args.what or "", settings) if c not in r.overrides_allow)
        r = replace(r, description=args.what or r.text(settings.lang), description_en="",
                    kind=args.kind or r.kind, threshold=args.threshold or r.threshold, overrides_allow=over)
    draft = r not in rules
    data = Path(args.data)

    def progress(n, total):
        if not args.json and sys.stderr.isatty():
            print(f"\r  asking the model: {n}/{total}", end="" if n < total else "\n", file=sys.stderr, flush=True)

    rows, model, stopped = _ask_each(lambda client: run(client, data, r, settings, last=args.last,
                                                        on_progress=progress), settings)
    fires = [x for x in rows if x["does"] in HITS]
    lines = [f"{r.id}{' (draft, not saved)' if draft else ''} on your last {len(rows)} distinct screens, at threshold "
             f"{r.threshold:g}: {len(fires)} would {'pop up' if r.kind == 'deny' else 'check in'}.", ""]
    shown = rows if args.all else rows[:args.show]
    for x in shown:
        you = f"  you: {x['you_said']}" if x["you_said"] else ""
        why = f" ({x['why']})" if x["why"] and x["does"] not in HITS else ""
        if x["overrides"]:
            why = f" (though {', '.join(x['overrides'])} would let it through: this rule overrides that)"
        lines.append(f"#{x['id']}  {x['p_hit']:.2f}  {x['does']}{why}{you}\n      {(x['title'] or x['app'])[:70]}  {x['url'][:70]}")
    if not args.all and len(rows) > args.show:
        lines.append(f"… {len(rows) - args.show} more, lower scores (--all to see them)")
    # Tune leaves out answers from outside the rule's hours: no threshold changes anything there.
    outside = sum(not in_window(r.when, datetime.fromisoformat(x["at"])) for x in rows)
    hours = "; ".join(r.when)
    if draft:
        lines += ["", "Keep this wording: `rules add` (new) or `rules set ID what=...` (existing), then label and tune."]
    elif outside == len(rows):
        lines += ["", f"All of these are from outside its hours ({hours}), where it never steps in, so answers on "
                  "them don't tune it. After a day or two with Qualm running in those hours, test it again, then "
                  "label and tune."]
    else:
        lines += ["", f"Tell it which are right:  {_q()} rules label {r.id} --yes ID ... --no ID ...",
                  f"Then:                     {_q()} rules tune {r.id} --apply"]
        if outside:
            lines.append(f"Label screens from inside its hours ({hours}): tune leaves out the {outside} from outside them.")
    _stopped(args, {"rule": r.id, "draft": draft, "reads": r.text(settings.lang), "summary": summary(r, settings),
                    "threshold": r.threshold, "overrides_allow": list(r.overrides_allow), "backend": model,
                    "would_fire": len(fires), "outside_hours": outside, "screens": rows}, "\n".join(lines), stopped)


def _ask_each(fn, settings: Settings) -> tuple[list[dict], str, str]:
    """The rows of a test that asks the model on each screen (`fn(client)`),
    which model answered, and why it stopped early ("" if it didn't): a
    model that stops answering partway still gives the rows it scored. No
    screens yet is an error; so is a model that doesn't answer at all,
    which says how to check it, with the rows scored before if there are any."""
    from typesafe_sdk import TypeSafeError

    from .agent import command
    from .decide import backend, make_client
    from .trial import Stopped

    try:
        client = make_client(settings)
    except RuntimeError as e:  # hosted, and no key saved
        _fail(str(e), "unreachable")
    hosted = backend(settings) == "jev"
    try:
        rows, stopped = fn(client), ""
    except (Stopped, TypeSafeError) as e:
        error, rows = (e.error, e.rows) if isinstance(e, Stopped) else (e, [])
        down = (f"TypeSafe's hosted model didn't answer ({error}). Check the internet connection, or save the key "
                f"again: `{command()} setup --backend jev`" if hosted else
                f"the model on this Mac didn't answer ({error}). Qualm runs it while the app is open: is Qualm "
                f"running? `{command()} status` checks")
        if rows and e.answered:
            who = "TypeSafe's hosted model" if hosted else "the model on this Mac"
            stopped = (f"{who} stopped answering after {len(rows)} of {e.total} screens ({error}). What it scored "
                       "is kept: run the same command again to go on from there.")
        elif rows:  # nothing answered now: these are scores from an earlier run
            stopped = (f"{down}. Scores from before cover {len(rows)} of the {e.total} screens, shown here; "
                       "the rest need the model.")
        else:
            _fail(down, "unreachable")
    if not rows:
        _fail(NO_SCREENS, "no_screens")
    return rows, client.backend, stopped


def _stopped(args, obj: dict, text: str, stopped: str) -> None:
    """A test's output; cut short by the model, with what it scored and why it stopped (exit 5)."""
    if not stopped:
        return _out(args, obj | {"complete": True}, text)
    if not args.json and sys.stderr.isatty():
        print(file=sys.stderr)  # past the progress line
    _out(args, obj | {"complete": False, "error": {"code": "unreachable", "message": stopped}},
         f"{text}\n\nnot complete: {stopped}")
    raise SystemExit(EXIT["unreachable"])


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

    if not 0 < args.precision <= 1:
        _fail(f"--precision {args.precision:g}: the share of hits that must be right, between 0 and 1 (0.9: 9 in 10)")
    cfg = config_path(args.rules)
    settings, rules = cfg.load()
    r = _find(rules, args.id, "rule")
    t = tune(Path(args.data), r, settings, args.precision, Path(args.rules))
    pct = lambda x: "–" if x is None else f"{round(x * 100)}%"
    lines = [f"{r.id}: {t['yes']} yes, {t['no']} no from you, scored with its current wording by {t['backend']}: "
             f"{t['scored_by_test']} by `rules test`, {t['scored_live']} while you used your Mac"]
    left_out = {"other_wording": "scored with other wording", "other_model": "scored by the other model",
                "not_scored": f"{r.id} wasn't asked there (off, or not made yet)",
                "outside_hours": f"from outside its hours ({'; '.join(r.when)}), where it never steps in, whatever the threshold"}
    for why, n in t["dropped"].items():
        if n:
            lines.append(f"  left out: {n} {left_out[why]}")
    if t["dropped"]["other_wording"] or t["dropped"]["other_model"]:
        lines.append(f"  `{_q()} rules test {r.id} --last 300` scores more of them with this wording and model")
    lines.append(f"  now: threshold {t['threshold']:g}, right on {pct(t['precision'])} of hits, catches {pct(t['recall'])}")
    if t["suggested"] is None:
        inside = " from inside its hours" if t["dropped"]["outside_hours"] else ""
        if t["keep_current"]:
            lines.append(f"  keep {t['threshold']:g}: no other threshold does better on your answers")
        elif not t["enough_answers"]:
            lines.append(f"  too few answers to tune it: it needs at least {MIN_EACH} yes and {MIN_EACH} no. "
                         f"`rules test {r.id}`, then `rules label` more screens{inside}.")
        else:
            lines.append(f"  no threshold reaches {pct(args.precision)} right on your answers. Reword it and "
                         f"`rules test {r.id}` again, or `rules label` more screens{inside}.")
    else:
        lines.append(f"  suggested: {t['suggested']:g}, right on {pct(t['suggested_precision'])}, catches {pct(t['suggested_recall'])}")
        if args.apply:
            cfg.edit("rules", r.id, {"threshold": t["suggested"]})
            t["applied"] = True
            lines.append("  applied: rules.toml updated; the app picks it up without a restart")
        else:
            precision = "" if args.precision == 0.9 else f" --precision {args.precision:g}"
            lines.append(f"  apply it: {_q()} rules tune {r.id}{precision} --apply")
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
    if any(r.id == args.id for r in cfg.load()[1]):
        _fail(f"{args.id!r} is the name of a rule (`{_q()} rules list`), and rules and allow classes share their ids: "
              + ("this starter can't be added while that rule has it" if args.from_starter else
                 f"give the allow class another name, like {args.id}_ok"))
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
            entry["apps"] = [_app(a)[0] for a in args.app]
        cfg.add("allow", entry)
    _changed(args, {"added": args.id}, f"added {args.id}: pages the model reads as this are never flagged, "
             "except by a rule's own sites, patterns or apps, or a rule naming it in overrides_allow")


def allow_test(args) -> None:
    """An allow class as saved, with new wording (--what), or a draft under a new id: what it would let through."""
    from dataclasses import replace

    from .trial import run_allow

    settings, _ = config_path(args.rules).load()
    c = next((x for x in settings.allow if x.id == args.id), None)
    if c is None:
        if not args.what:
            _find_allow(settings, args.id)
        c = AllowClass(args.id, args.what, threshold=args.threshold or 0.5)
    elif args.what or args.threshold:
        c = replace(c, description=args.what or c.text(settings.lang), description_en="",
                    threshold=args.threshold or c.threshold)
    draft = c not in settings.allow

    def progress(n, total):
        if not args.json and sys.stderr.isatty():
            print(f"\r  asking the model: {n}/{total}", end="" if n < total else "\n", file=sys.stderr, flush=True)

    rows, model, stopped = _ask_each(lambda client: run_allow(client, Path(args.data), c, settings, last=args.last,
                                                              on_progress=progress), settings)
    yes = [x for x in rows if x["is_it"]]
    cleared = [x for x in yes if x["stepped_in"]]
    lines = [f"{c.id}{' (draft, not saved)' if draft else ''} on your last {len(rows)} distinct screens, at threshold "
             f"{c.threshold:g}: {len(yes)} read as this; {len(cleared)} of them had a pop-up, which it would stop.", ""]
    for x in (rows if args.all else rows[:args.show]):
        was = f"  stopped: {', '.join(x['stepped_in'])}" if x["stepped_in"] else ""
        lines.append(f"#{x['id']}  {x['p']:.2f}  {'yes' if x['is_it'] else 'no '}{was}\n"
                     f"      {(x['title'] or x['app'])[:70]}  {x['url'][:70]}")
    if not args.all and len(rows) > args.show:
        lines.append(f"… {len(rows) - args.show} more, lower scores (--all to see them)")
    lines += ["", "Check the yes rows: a page you'd want flagged among them means the wording is too broad."]
    _stopped(args, {"allow": c.id, "draft": draft, "reads": c.text(settings.lang), "threshold": c.threshold,
                    "backend": model, "read_as_this": len(yes), "would_stop": len(cleared), "screens": rows},
             "\n".join(lines), stopped)


def guide() -> str:
    """How an agent should drive Qualm (.claude/skills/qualm/SKILL.md without its header)."""
    return (Path(__file__).parent / "guide.md").read_text(encoding="utf-8")


def _find_allow(settings, allow_id: str):
    c = next((x for x in settings.allow if x.id == allow_id), None)
    if c is None:
        _fail(f"no allow class {allow_id!r}; `allow list` shows them, or give --what for a draft", "not_found")
    return c


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
    """What the pop-up ("Not this one") and the review page taught, minus what
    was removed: what the app lets through (policy.removes)."""
    from .policy import removes
    from .review import exceptions

    out: list[dict] = []
    for e in exceptions(data_dir):
        if e.get("never") or not e.get("rule"):
            continue
        if e.get("removed"):
            out = [x for x in out if not removes(e, x)]
        else:
            out.append(e)
    return out


def _exception_rows(rules: list[Rule], data_dir: Path, only: str | None = None) -> list[dict]:
    """Every exception, per rule: typed into rules.toml, and learned from the pop-up and review page."""
    from .policy import FINE_MARGIN

    rows = []
    for r in rules:
        if only and r.id != only:
            continue
        rows += [{"rule": r.id, "text": x, "source": "rules.toml"} for x in r.exceptions]
    for e in live_exceptions(data_dir):
        if not only or e["rule"] == only:
            row = {"rule": e["rule"], "text": e.get("text") or e.get("title") or "", "url": e.get("url", ""),
                   "source": "typed" if e.get("text") else "not this one", "at": e.get("at", "")}
            if e.get("app") and e.get("p_hit") is not None:  # this window of this app, below a score
                row |= {"app": e["app"], "below": round(e["p_hit"] + FINE_MARGIN, 2)}
            rows.append(row)
    return rows


def _exception_line(x: dict) -> str:
    return (f"{x['rule']:<12} {x['text']}" + (f"  <{x['url']}>" if x.get("url") else "")
            + (f"  [{x['app']}, under {x['below']:.2f}]" if x.get("app") else "") + f"  ({x['source']})")


def except_list(args) -> None:
    _, rules = config_path(args.rules).load()
    if args.rule:
        _find(rules, args.rule, "rule")
    rows = _exception_rows(rules, Path(args.data), args.rule)
    _out(args, rows, "\n".join(map(_exception_line, rows)) or "no exceptions")


def except_add(args) -> None:
    cfg = config_path(args.rules)
    settings, rules = cfg.load()
    r = _find(rules, args.rule, "rule")
    if bool(args.text) == bool(args.from_):
        _fail('give the exception in words ("a lecture or conference talk"), or --from ID for one page that was fine')
    if args.from_:
        return _except_from(args, settings, rules, r)
    if args.text in r.exceptions:
        _fail("already there")
    cfg.edit("rules", r.id, {"exceptions": [*r.exceptions, args.text]})
    _out(args, {"rule": r.id, "added": args.text}, f"{r.id}: the model now reads “{args.text}” as fine")


def _except_from(args, settings: Settings, rules: list[Rule], r: Rule) -> None:
    """"That page was fine", after the fact: what the pop-up's Not this one
    saves, for a screen from `rules test` or `review`, or a pop-up's decision id."""
    from .policy import FINE_MARGIN, Policy, fine_entry
    from .review import _lines, load_judgements

    data = Path(args.data)
    src = next((j for j in load_judgements(data) if j["id"] == args.from_), None)
    decision = ""
    if src is None:
        events = _lines(data / "decisions.jsonl")
        src = next((e for e in events if e.get("type") == "intervention" and e.get("id") == args.from_), None)
        if src is not None and src.get("rule") != r.id:
            other = src.get("rule") or "another rule"
            _fail(f"#{args.from_} was a {other} pop-up, not {r.id}"
                  + (f": `except add {other} --from {args.from_}` lets it through for {other}" if other in
                     {x.id for x in rules} else ""))
        # The answer to the pop-up, as Not this one gives, unless it had one: how it ended stays as it was.
        answered = any(e.get("type") == "response" and e.get("id") == args.from_ for e in events)
        decision = args.from_ if src and not answered else ""
    if src is None:
        _fail(f"no screen or pop-up with id {args.from_!r}; ids come from `rules test`, `review`, "
              "or the pop-ups listed in the menu's prompt for your AI agent", "not_found")
    s, p = src.get("screen") or {}, (src.get("p_hit") or {}).get(r.id)
    url, title, bundle = s.get("url", ""), s.get("window_title", ""), s.get("bundle_id", "")
    # An app this rule names steps in without asking the model: no score, and let through at any.
    own_app = not url and bundle and p is None and r.matches_app(bundle, s.get("app", ""))
    if not url and not (bundle and p is not None) and not own_app:
        _fail(f"#{args.from_} has no address and no {r.id} score to let it through below; describe it in words "
              f"instead (`except add {r.id} \"…\"`), or leave the whole app alone (`never add --app`)")
    e = fine_entry(r.id, url, title, bundle, p)
    policy = Policy(settings, rules, data)
    what = url or f"the {s.get('app') or bundle} window “{title}”"
    if policy.marked_fine(r.id, url, bundle, title, p):  # a second screen of a page already let through
        if decision and not SESSION["dry_run"]:
            policy.log_response(decision, "fine", r.id)
        return _out(args, {"rule": r.id, "from": args.from_, "already": True},
                    f"{r.id}: {what} was already let through; nothing to add")
    if SESSION["dry_run"]:
        SESSION["appends"].append({"file": str(data / "exceptions.jsonl"), "line": e})
        return
    policy.mark_fine(r.id, url, title, decision, bundle, p, until_focus_ends=False)
    below = "" if p is None or url else f" below {p + FINE_MARGIN:.2f}"
    _out(args, {"rule": r.id, "from": args.from_, "added": e}, f"{r.id}: {what} is let through{below} from now on")


def except_remove(args) -> None:
    cfg = config_path(args.rules)
    _, rules = cfg.load()
    r = _find(rules, args.rule, "rule")
    if args.text in r.exceptions:
        cfg.edit("rules", r.id, {"exceptions": [x for x in r.exceptions if x != args.text]})
    else:
        # One exception: by its address, its words, or its title. A title can be many pages' ("Instagram").
        live = [e for e in live_exceptions(Path(args.data)) if e["rule"] == r.id]
        match = next((e for e in live if e.get("url") == args.text), None) or \
            next((e for e in live if e.get("text") == args.text), None)
        titled = [e for e in live if e.get("title") == args.text and not e.get("text")]
        if match is None and len({e.get("url") or e.get("app") for e in titled}) > 1:
            if any(e.get("url") for e in titled):
                _fail(f"{len(titled)} pages {r.id} lets through have the title {args.text!r}: give the address of the "
                      f"one to remove (`except list {r.id}` shows them)")
            match = {"rule": r.id, "title": args.text}  # app windows only: that title in every app
        match = match or (titled[0] if titled else None)
        if match is None:
            _fail(f"{r.id} has no exception {args.text!r}; `except list {r.id}` shows them", "not_found")
        _append(Path(args.data), {k: match[k] for k in ("rule", "text", "title", "url", "app") if match.get(k)}
                | {"removed": True})
    _out(args, {"rule": r.id, "removed": args.text}, f"removed from {r.id}: {args.text}")


def _app(value: str, adding: bool = True) -> tuple[str, str]:
    """An app as typed ("Figma", com.figma.Desktop) -> (what to save, its
    name): the installed app with that name or bundle id, else the value as
    typed, which still matches an app shown with exactly that name or id.
    Adding one nothing installed matches says so (a warning in the output)."""
    from .state import installed_app

    found = installed_app(value)
    if found:
        return found
    if adding:
        SESSION["warnings"].append(f"no installed app is called {value!r} or has that bundle id. Saved as typed: it "
                                   "matches an app shown with exactly that name or bundle id, so check the spelling")
    return value, value


def _place(args, adding: bool = True) -> dict:
    if bool(args.site) == bool(args.app):
        _fail("give one of --site example.com or --app WhatsApp (an app's name or bundle id)")
    if args.site:
        from .policy import host_of
        from .rules import site_pattern

        if adding:
            site_pattern(args.site)  # a domain, or it says how to write one
        return {"host": host_of(args.site if "://" in args.site else "https://" + args.site)}
    app, name = _app(args.app, adding)
    return {"app": app, "name": args.name or name}


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

    place = _place(args, adding=False)
    key = place.get("host") or place.get("app")
    typed = {key.lower(), (args.app or "").lower()} - {""}  # saved resolved, or as typed before
    saved = next(((n.get("host") or n.get("app")) for n in never_places(Path(args.data))
                  if (n.get("host") or n.get("app", "")).lower() in typed), None)
    if saved is None:
        _fail(f"{key} isn't a never-here place; `never list` shows them", "not_found")
    place = {"host": saved} if place.get("host") else place | {"app": saved}
    _append(Path(args.data), {"never": place, "removed": True})
    _out(args, {"removed": place}, "removed")


# -- settings -----------------------------------------------------------------


def _session_json(data_dir: Path) -> dict:
    from .policy import read_session

    def iso(t: float) -> str:
        return datetime.fromtimestamp(t).isoformat(timespec="minutes")

    s = read_session(data_dir)
    now = datetime.now().timestamp()
    focus, later = s["focus"], s["pause_later"]
    return {
        "paused": bool(s["paused_until"]),
        "paused_until": iso(s["paused_until"]) if s["paused_until"] else None,
        "pause_later": {"from": iso(later["from"]), "until": iso(later["until"])} if later else None,
        "focus": {"intent": focus["intent"], "until": iso(focus["until"]),
                  "minutes_left": max(0, round((focus["until"] - now) / 60))} if focus else None,
    }


def _clock(iso: str) -> str:
    """"22:34" today, "Mon 09:00" this week, "Oct 5 09:00" after: as the menu and the dashboard say it."""
    from .policy import clock

    return clock(datetime.fromisoformat(iso).timestamp())


def _planned(later: dict) -> str:
    return f"a pause is planned from {_clock(later['from'])} until {_clock(later['until'])}"


def _session_line(sj: dict) -> str:
    f = sj["focus"]
    parts = [f"focus: {f['intent']} until {_clock(f['until'])} ({f['minutes_left']} min left)" if f else "no focus session"]
    parts.append(f"paused until {_clock(sj['paused_until'])}" if sj["paused"] else "not paused")
    if sj["pause_later"]:
        parts.append(_planned(sj["pause_later"]))
    return "; ".join(parts)


def focus(args) -> None:
    """Start, show or end a focus session (the running app picks it up within a second).
    Starting one ends a pause (one planned for later stays)."""
    from .agent import command
    from .policy import end_focus, start_focus

    data = Path(args.data)
    if args.stop:
        ended = end_focus(data)
        _out(args, {"ended": ended["intent"] if ended else None, **_session_json(data)},
             f"ended: {ended['intent']}" if ended else "no focus session was running")
        return
    was = _session_json(data)
    if args.intent:
        intent = " ".join(args.intent)
        start_focus(data, intent, args.minutes)
    sj = _session_json(data)
    text = _session_line(sj)
    if args.intent:
        ended_pause = was["paused"]
        sj["pause_ended"] = ended_pause
        text = (f"Focus on “{sj['focus']['intent']}” until {_clock(sj['focus']['until'])}. Every rule hit steps in at once, "
                "check-ins included, and the pop-up reminds you of this."
                + (f" This ended the pause, which would have run until {_clock(was['paused_until'])}." if ended_pause else "")
                + f" End it: `{command()} focus --stop`")
    _out(args, sj, text)


PAUSE_DAYS = 7  # the longest pause, and how far ahead one can start: a weekend away, a holiday week
DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WHEN_HELP = "a time (09:00), a day and time (mon 09:00, tomorrow 09:00), a day (mon) or a date (2026-09-28 09:00)"


def _when(text: str, flag: str, after: datetime | None = None) -> datetime:
    """--until and --from: "09:00", "mon 09:00", "tomorrow 09:00", "mon" (its
    start), "2026-09-28 09:00" (with a UTC offset: in this Mac's time). The next
    such moment after `after` (now if None), so a --from is never in the past
    and an --until comes after its --from; a date, today and tomorrow are
    that day."""
    now = datetime.now()
    after = after or now
    try:
        at = datetime.fromisoformat(text.strip().upper())
        return at.astimezone().replace(tzinfo=None) if at.tzinfo else at
    except (ValueError, OverflowError):
        pass
    t = " ".join(text.lower().split())
    m = re.fullmatch(r"([a-z]+)?\s*(?:(\d{1,2}):(\d{2}))?", t)
    if not m or not (m.group(1) or m.group(2)) or (m.group(2) and (int(m.group(2)) > 23 or int(m.group(3)) > 59)):
        _fail(f"{flag} {text!r}: {WHEN_HELP}")
    day, hm = m.group(1), {"hour": int(m.group(2) or 0), "minute": int(m.group(3) or 0), "second": 0, "microsecond": 0}
    if day in ("today", "tomorrow"):
        return now.replace(**hm) + timedelta(days=day == "tomorrow")
    at = after.replace(**hm)
    if day is not None:
        wd = next((i for i, name in enumerate(DAY_NAMES) if len(day) >= 3 and name.startswith(day)), None)
        if wd is None:
            _fail(f"{flag} {text!r}: {day!r} isn't a day: mon, tue, wed, thu, fri, sat, sun, today or tomorrow")
        at += timedelta(days=(wd - after.weekday()) % 7)
    return at if at > after else at + timedelta(days=1 if day is None else 7)


def _last(text: str, flag: str, now: datetime) -> datetime | None:
    """The last such moment up to now (see _when), or None if it's only ahead."""
    at = _when(text, flag, after=now - timedelta(days=8))
    if at > now:
        return None
    while now >= (later := _when(text, flag, after=at)) > at:
        at = later
    return at


NOW_S = 300  # a --from this close behind is now: the minute it is, or an agent's "now" sent a bit later


def _pause_span(args) -> tuple[datetime | None, float]:
    """(when the pause starts, None for now; its minutes), from MINUTES ("30"),
    a span ("90m", "2h", "2d"), --until and --from. --from is its next
    occurrence ("sat 00:00" on a Sunday is next Saturday: `pause --until` is
    the weekend under way, and a note says so), --until the next after it."""
    now = datetime.now()
    if args.until is not None and args.minutes is not None:
        _fail("give how long (pause 2h) or until when (pause --until 'mon 09:00'), not both")
    if args.from_ is not None and args.until is None and args.minutes is None:
        _fail("--from needs how long (pause 2d --from 'sat 00:00') or until when (--until 'mon 09:00')")
    start = None
    if args.from_ is not None:
        start = _when(args.from_, "--from", after=now - timedelta(seconds=NOW_S))  # "05:21" typed at 05:21: now
        if start <= now - timedelta(seconds=NOW_S):
            _fail(f"--from {args.from_!r} is already past: leave --from out to pause from now")
        if start > now + timedelta(days=PAUSE_DAYS):
            _fail(f"--from {args.from_!r}: a pause can start at most {PAUSE_DAYS} days ahead")
        start = start if start > now else None
    if args.until is not None:
        until = _when(args.until, "--until", after=start)
        if until <= (start or now):
            _fail(f"--until {args.until!r} comes before --from {args.from_!r}: a pause ends after it starts" if start
                  else f"--until {args.until!r} is already past: give a time still to come")
        if start and (last := _last(args.from_, "--from", now)) and now < _when(args.until, "--until", after=last) < until:
            # "sat 00:00 until mon 09:00" asked on a Sunday: this weekend is under way, and not what this plans.
            from .agent import command

            SESSION["warnings"].append(
                f"this plans the next one. The {args.from_} to {args.until} under way now (since "
                f"{_clock(last.isoformat())}) stays watched: `{command()} pause --until '{args.until}'` pauses it from now")
    else:
        span = "30" if args.minutes is None else args.minutes
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|min|h|d)?", span.strip().lower())
        if not m:
            _fail(f"pause {args.minutes!r}: minutes (30), or a span like 90m, 2h or 2d")
        minutes = float(m.group(1)) * {None: 1, "m": 1, "min": 1, "h": 60, "d": 24 * 60}[m.group(2)]
        if not 0 < minutes <= PAUSE_DAYS * 24 * 60:
            return start, minutes  # refused by the caller, with the range
        until = (start or now) + timedelta(minutes=minutes)
    return start, (until - (start or now)).total_seconds() / 60


def pause(args) -> None:
    """Pause every rule for a while (up to a week), now or from later; or resume."""
    from .agent import command
    from .policy import cancel_pauses, pause_for

    data = Path(args.data)
    q = command()
    was = _session_json(data)
    if args.stop:
        cancel_pauses(data)
        text = "resumed: watching" if was["paused"] else "not paused: watching"
        if was["pause_later"]:
            text += f"; cancelled the pause planned from {_clock(was['pause_later']['from'])} until {_clock(was['pause_later']['until'])}"
        _out(args, _session_json(data) | {"cancelled": was["pause_later"]}, text)
        return
    start, minutes = _pause_span(args)
    if not 0 < minutes <= PAUSE_DAYS * 24 * 60:
        _fail(f"pause for 1 minute to {PAUSE_DAYS} days: `{q} pause 30`, `pause 2h`, `pause 2d`, "
              f"`pause --until 'mon 09:00'`, or from later: `pause --from 'sat 00:00' --until 'mon 09:00'` "
              f"(`pause --stop` resumes, and cancels a planned one)")
    pause_for(data, minutes, start.timestamp() if start else None)
    sj = _session_json(data)
    if start:
        later = sj["pause_later"]
        text = ((f"paused until {_clock(sj['paused_until'])}, and {_planned(later)}" if sj["paused"] else
                 f"Qualm watches until {_clock(later['from'])}, then pauses until {_clock(later['until'])}")
                + (f" (this replaces the pause planned from {_clock(was['pause_later']['from'])})" if was["pause_later"] else "")
                + f". `{q} pause --stop` cancels it" + (" and resumes now" if sj["paused"] else ""))
        from .review import app_running

        if (app := app_running(data)) and app.get("older"):
            SESSION["warnings"].append("the Qualm running now is an older copy, which knows no planned pauses: quit it "
                                       "and open it again before then")
    else:
        text = f"paused until {_clock(sj['paused_until'])}; `{q} pause --stop` resumes"
        if sj["pause_later"]:
            text += f". Also, {_planned(sj['pause_later'])} (`--stop` cancels that too)"
    _out(args, sj, text)


def doctor(args) -> None:
    """Everything Qualm needs, checked; exit 2 if something must be fixed."""
    from .doctor import FAIL, checks, report

    results = checks(args.rules, args.data)
    _out(args, {"checks": results}, report(results))
    if any(r["status"] == FAIL for r in results):
        raise SystemExit(EXIT["invalid"])


def settings_show(args) -> None:
    settings, _ = config_path(args.rules).load()
    d = {k: v for k, v in asdict(settings).items() if k != "allow"}
    _out(args, d, "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}\n    {FIELDS['settings'][k]}" for k, v in d.items()))


UNSAVED_KEY = ("the TypeSafe key is only in TYPESAFE_API_KEY in this shell, and Qualm started from Finder or at login "
               "doesn't see it: `{q} setup --backend jev` saves it in your keychain")


def _hosted_key() -> None:
    """Before switching to the hosted model: refused without a key (saved
    without one, the app would stop judging); a key only in this shell's
    environment is taken, with a warning that the app won't have it."""
    from . import keychain
    from .agent import command

    if not keychain.api_key():
        _fail(f"the hosted model needs a TypeSafe API key first (from console.typesafe.ai): "
              f"`{command()} setup --backend jev` asks for it, keeps it in your keychain and switches the model")
    if keychain.unsaved():
        SESSION["warnings"].append(UNSAVED_KEY.format(q=command()))


def settings_set(args) -> None:
    cfg = config_path(args.rules)
    settings, _ = cfg.load()
    set_, unset = _app_assignments(Settings, asdict(settings), args.pairs)
    if set_.get("backend") == "jev" and settings.backend != "jev":
        _hosted_key()
    cfg.edit_settings(set_, unset)
    _changed(args, {"set": set_, "unset": unset}, "saved: " + ", ".join([*set_, *unset]))


# -- the whole config, schema, status ----------------------------------------


def config_export(args) -> None:
    data = config_path(args.rules).export()
    print(json.dumps(data, ensure_ascii=False, indent=2))


def config_apply(args) -> None:
    try:
        desired = json.loads(sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        _fail(f"no file {args.file}", "not_found")
    except OSError as e:
        _fail(f"can't read {args.file}: {e.strerror or e}", "not_found")
    except UnicodeDecodeError:
        _fail(f"{args.file} isn't UTF-8 text: save it as UTF-8")
    except json.JSONDecodeError as e:
        _fail(f"not JSON: {e}")
    cfg = config_path(args.rules)
    wanted = desired.get("settings") if isinstance(desired, dict) else None
    if isinstance(wanted, dict) and wanted.get("backend") == "jev" and cfg.load()[0].backend != "jev":
        _hosted_key()  # as `settings set backend=jev`
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
    done = config_path(args.rules).undo()
    dry = SESSION["dry_run"]
    when = done["restored"].replace("T", " at ")
    if done["undid"] == "hand_edit":
        what = ("would undo the changes made to rules.toml by hand, going back to" if dry else
                "undid the changes made to rules.toml by hand: it's back to") + f" the version Qualm last saved, on {when}"
    elif done.get("skipped"):
        what = ("would go back to the newest kept version that loads, saved" if dry else
                "went back to the newest kept version that loads: rules.toml is back to the version saved") + f" {when}"
    else:
        what = ("would undo the last saved change, going back to the version saved" if dry else
                "undid the last saved change: rules.toml is back to the version saved") + f" {when}"
    passed = "".join(f"{'would pass' if dry else 'passed'} over (it can't be put back; {'it would be ' if dry else ''}"
                     f"kept aside as {a}): {s}\n" for s, a in zip(done.get("skipped", ()), done.get("kept_aside", ())))
    tail = f"; {done['left']} older version(s) left to go back to" + (
        f". The rules.toml it replace{'s would be' if dry else 'd is'} kept as {done['replaced_file']}"
        if "replaced_file" in done else "") + (f"\nNote: {done['note']}." if "note" in done else "")
    if dry:  # the dry run prints the diff itself: this says what it is, and what else would happen
        SESSION["preview"] = {"json": {k: v for k, v in done.items() if k != "diff"}, "text": passed + what + tail}
    _changed(args, {"undone": True, **done}, f"{passed}{done['diff']}{what}{tail}")


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


def _last_judgement(data_dir: Path) -> dict | None:
    """The newest line of judgements.jsonl, read from the end: the log holds months of them."""
    path = data_dir / "judgements.jsonl"
    if not path.exists():
        return None
    with path.open("rb") as f:
        pos = f.seek(0, 2)
        tail = b""
        while pos > 0:
            step = min(1 << 16, pos)
            pos -= step
            f.seek(pos)
            tail = f.read(step) + tail
            lines = tail.split(b"\n")
            for line in reversed(lines if pos == 0 else lines[1:]):  # the first may be cut off
                if line.strip():
                    try:
                        return json.loads(line)
                    except ValueError:  # being written right now
                        continue
    return None


def _model_status(name: str) -> dict:
    """Can the model in use answer: the local server, or the hosted one (the key, then a call that costs nothing)."""
    if name == "jev":
        from typesafe_sdk import RetryPolicy, TypeSafeClient

        from . import keychain

        key = keychain.api_key()
        if not key:
            return {"reachable": False, "key": False, "error": "no key"}
        saved = not keychain.unsaved()  # only in this shell's environment: the app has none
        try:
            with TypeSafeClient(api_key=key, timeout=5, retry=RetryPolicy(max_retries=0)) as client:
                client.models.list()  # lists what the key may use: no tokens
            return {"reachable": True, "key": True, "key_saved": saved}
        except Exception as e:
            name = type(e).__name__  # TypeSafe answered and turned the key down, or its limit is reached
            trouble = "key" if name in ("TypeSafeAuthenticationError", "TypeSafePermissionDeniedError") else \
                "quota" if name == "TypeSafeRateLimitError" else "down"
            return {"reachable": False, "key": True, "key_saved": saved, "trouble": trouble, "error": f"{name}: {e}"[:200]}
    from urllib.parse import urlsplit

    from . import localmodel

    kev, m = localmodel.find_server()  # KEV_URL, the port the app moved to, or :8009: what doctor asks too
    if m is None:  # why, as doctor says it: "remote", "starting", "taken" or ""
        why, host = localmodel.why_down(kev), urlsplit(kev).hostname
        return {"reachable": False, "url": kev, "why": why or "none",
                "error": f"the model server at {host} doesn't answer" if why == "remote" else
                f"port {urlsplit(kev).port or 80} is used by another app, not a model server" if why == "taken" else
                "not answering", "downloaded": (localmodel.saved_copy() / "model.safetensors").exists()}
    if m.get("busy"):  # up, but answering something else: slow, not down
        return {"reachable": True, "url": kev, "busy": True}
    return {"reachable": True, "url": kev, "id": m.get("run") or m.get("name") or m.get("id", "")}


def status(args) -> None:
    """Is the app up, is the model it uses up, does the config load, how full is it."""
    from . import review
    from .agent import command, start_hint
    from .decide import backend

    q, data = command(), Path(args.data)
    settings = None
    try:
        settings, rules = config_path(args.rules).load()
        config = {"ok": True, "rules_on_now": [r.id for r in rules if r.active()], "capacity": capacity(settings, rules)}
    except ValueError as e:
        config = {"ok": False, "error": str(e)}
    name = backend(settings)
    app = review.app_running(data, settings)
    out: dict = {"app_running": bool(app), "review_page": review.dashboard_url(data, settings), "backend": name,
                 "model": _model_status(name), "config": config}
    if app and app.get("older"):  # it can't load keys newer commands write (apps, overrides_allow)
        out["app_older"] = True
    if j := _last_judgement(data):
        out["last_judgement"] = {"at": j["at"], "app": j["screen"].get("app", ""), "title": j["screen"].get("window_title", ""),
                                 "decisions": j["decisions"]}
    out["today"] = _usage_today(data)
    out["session"] = _session_json(data)
    m, c = out["model"], out["config"]
    start = start_hint()
    if name == "jev":
        trouble = m.get("trouble")
        model = ("hosted by TypeSafe (Jev): key found, answering" if m["reachable"] else
                 f"hosted by TypeSafe (Jev): no key yet: `{q} setup --backend jev` adds one" if not m["key"] else
                 f"hosted by TypeSafe (Jev): TypeSafe refused the key ({m['error']}): get one that works at "
                 f"console.typesafe.ai and save it with `{q} setup --backend jev --key -` (paste it, then Enter)"
                 if trouble == "key" else
                 f"hosted by TypeSafe (Jev): TypeSafe's usage limit is reached ({m['error']}): nothing is judged until "
                 "it resets (console.typesafe.ai shows it)" if trouble == "quota" else
                 f"hosted by TypeSafe (Jev): key found, but TypeSafe didn't answer ({m['error']}): check the network, "
                 "and that the key still works (console.typesafe.ai)")
        if m["key"] and not m.get("key_saved", True):
            model += ". But " + UNSAVED_KEY.format(q=q)
    else:
        from .localmodel import disk_needed_gb

        need = disk_needed_gb()  # the runtime and the weights, less what's there: setup's 6 GB, to begin with
        first = "" if m.get("downloaded") or need < 0.5 else f" (a first start downloads about {need:.0f} GB)"
        why = m.get("why")  # as doctor says it
        if m.get("busy"):
            model = f"on this Mac, up at {m['url']}, busy answering (slow on a Mac short of memory)"
        elif m["reachable"]:
            model = f"on this Mac, up: {m['id']}"
        elif why == "remote":  # KEV_URL on another machine: nothing here starts it
            model = f"{m['error']} (KEV_URL={m['url']}): check that machine and the network; `{q} doctor` says more"
        elif why == "taken":  # the app moves its server off a taken port when it starts it
            model = (f"on this Mac, not answering: {m['error']}. "
                     + ("Quit Qualm and open it again: it runs the model on the next free port"
                        if app else "The app runs the model on the next free port when it starts")
                     + f", and terminal commands find it there; `{q} doctor` says more")
        elif app or why == "starting":
            model = (f"on this Mac, not answering yet at {m['url']}: "
                     f"{'the app is starting it' if app else 'a model server is getting it ready'}{first}; "
                     f"`{q} doctor` says more")
        else:
            model = f"on this Mac, not running: the app starts it ({start}); `{q} serve` runs it in a terminal"
    app_line = ("running" + (" (an older copy: quit and reopen it to update)" if app.get("older") else "")) if app \
        else f"not running: {start}"
    lines = [f"app: {app_line}",
             f"model: {model}",
             f"config: {'ok, ' + _capacity_line(c['capacity']) if c['ok'] else 'broken: ' + c['error']}"]
    if c.get("ok"):
        lines.append(f"on now: {', '.join(c['rules_on_now']) or 'no rules'}")
    lines.append(_session_line(out["session"]))
    if "last_judgement" in out:
        lj = out["last_judgement"]
        acts = "; ".join(f"{d['action']} {d['rule']}".strip() for d in lj["decisions"]) or "nothing"
        lines.append(f"last judged: {lj['at'][11:]} {lj['app']} | {lj['title'][:50]} -> {acts}")
    down = ([] if c["ok"] else [f"config: broken: {c['error'].rstrip('.')}"]) + ([] if app else [f"app: {app_line}"]) + \
        ([] if m["reachable"] else [f"model: {model}"])
    if down:  # an exit code comes with its error, as for every command
        out["error"] = {"code": "unreachable" if c["ok"] else "invalid", "message": "; ".join(down)}
    _out(args, out, "\n".join(lines))
    if down:
        raise SystemExit(EXIT[out["error"]["code"]])


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
        "apps": "always a hit while one of these apps is in front, without the model: bundle ids or app names "
                "(games, Steam, the TV app: windows with too little text for the model)",
        "allow_learning": "lectures, tutorials and docs never hit this rule",
        "allow_intentional": "one item opened from search, a work app or a chat link is fine (not on the rule's own sites)",
        "feed_hit": "any page the model reads as an entertainment feed hits this rule",
        "enabled": "false: kept in the file, but never asked and never fires",
        "when": "only at these times; empty = always",
        "overrides_allow": "allow classes this rule steps in on anyway: a rule about online shopping overrides the "
                           "shopping class, which would let every store through; `rules add` sets it when the words match",
        "note": "why it's set this way, for whoever reads the file next",
    },
    "allow": {
        "id": "short name: lowercase letters, digits and _",
        "description": "a kind of page no rule fires on, in your words (\"an online store\")",
        "description_en": "optional English version",
        "threshold": "the model's yes-probability that counts",
        "sites": "known to be this kind: allowed without the model",
        "patterns": "the same as URL regexes",
        "apps": "apps known to be this kind: bundle ids or app names",
        "enabled": "false: kept, but not asked",
        "note": "why it's there",
    },
    "settings": {
        "lang": "en: rules with a description_en send that; anything else sends description",
        "no_monitor": "apps never read at all: bundle ids or app names (password managers never are, listed or not)",
        "allow_sites": "sites never judged; links from them count as opened on purpose",
        "allow_urls": "the same, as URL regexes",
        "max_wait_s": "the longest wait, in seconds, before a check-in or \"I need it\" unlocks; it doubles with each "
                      "session today (the first is free) and again when you come back within 20 min of one ending",
        "extensions": "how many \"5 more\" a check-in session may get when its time is up (0: none)",
        "block_clicks": "true: while a pop-up is open the dimmed screen takes the clicks, so you answer it first; "
                        "false: you can click through the dim (also in the menu bar)",
        "max_questions": "questions one reading may ask (rules on at once + allow classes + 3 shared); "
                         "more slows the model sharply (25 is ~1.5 s on Kev-4B, 24 GB Mac)",
        "backend": "where the model runs: kev (on this Mac; nothing leaves it) or jev (TypeSafe's hosted model: "
                   "~0.2 s, little memory, each new screen's text is sent to TypeSafe; key via `qualm setup`)",
        "hf_endpoint": "where the local model downloads from, if huggingface.co is blocked where you are: a mirror's "
                       "https address (https://hf-mirror.com); empty: huggingface.co. The app uses it from its next try",
        "keep_days": "days the logs keep every judgement and pop-up (what you read on screen); 0: forever. "
                     "Reviewed judgements, your answers and what you taught Qualm are always kept",
        "keep_shots_days": "days screenshots (for the dashboard) are kept; 0: forever",
        "dashboard_port": "the dashboard's port on 127.0.0.1 (QUALM_DASHBOARD_PORT wins); if another program holds it, "
                          "the app takes a free one, and `qualm status` shows where",
    },
}

GRAMMAR = {
    "sites": '"douyin.com" = the site and its subdomains; "youtube.com/shorts" = that path and under it; '
             '"youtube.com/" = the home page only',
    "when": 'days and/or hours: "mon-fri 09:00-18:00", "weekends", "sat,sun", "22:00-02:00" (past midnight belongs '
            'to the day it starts), "daily 12:00-13:00", "mon-fri 09:00-12:00, 13:00-18:00" (the days, then any '
            'number of hours); several entries = any of them',
    "cli assignments": "key=value sets; key+=item / key-=item edit a list; key= removes the key (back to default); "
                       'lists also take a JSON array: sites=\'["a.com","b.com"]\'; what=... sets the description the model reads',
    "limit": "rules on at the same moment + enabled allow classes + 3 shared questions <= max_questions; "
             "a change that would go over is refused (exit code 4)",
}


# -- the parser ---------------------------------------------------------------


def run(args) -> None:
    """Run one command with dry runs, --json errors and exit codes."""
    previews = getattr(args, "previews", False)  # a change to rules.toml, with its own --dry-run
    with contextlib.ExitStack() as hold:  # a change keeps rules.toml locked till it's done (config_path)
        SESSION.update(dry_run=previews and args.dry_run, configs=[], appends=[], warnings=[],
                       hold=hold if previews else None, json=getattr(args, "json", False), preview={})
        _run(args)


def _run(args) -> None:
    as_json = getattr(args, "json", False)
    try:
        if SESSION["dry_run"]:
            with contextlib.redirect_stdout(io.StringIO()):
                args.cmd_fn(args)
            diff = "".join(c.diff() for c in SESSION["configs"])
            preview = SESSION["preview"]
            _out(args, {"dry_run": True, "diff": diff, "appends": SESSION["appends"], **preview.get("json", {})},
                 (diff or "rules.toml: no change") +
                 "".join(f"\nwould append to {a['file']}: {json.dumps(a['line'], ensure_ascii=False)}" for a in SESSION["appends"]) +
                 (f"\n{preview['text']}" if preview.get("text") else "") + "\n(dry run: nothing saved)")
        else:
            args.cmd_fn(args)
    except (CliError, OverLimit, ValueError, KeyError) as e:
        code = e.code if isinstance(e, CliError) else "over_limit" if isinstance(e, OverLimit) else \
            "not_found" if isinstance(e, KeyError) else "invalid"
        msg = e.message if isinstance(e, CliError) else e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        if as_json:
            print(json.dumps({"error": {"code": code, "message": msg}}, ensure_ascii=False, indent=2))
        else:
            print(f"error: {msg}", file=sys.stderr)
        raise SystemExit(EXIT[code])
    except Exception as e:  # a bug, not the input: a traceback, or under --json still an answer
        if not as_json:
            raise
        print(json.dumps({"error": {"code": "unexpected", "message": f"{type(e).__name__}: {e}"}}, ensure_ascii=False, indent=2))
        raise SystemExit(EXIT["unexpected"])


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
        sp.set_defaults(fn=run, cmd_fn=fn, previews=changes)
        return sp

    g = group("rules", "list, add, change, test and tune your rules")
    cmd(g, "list", rules_list, "every rule in a sentence: on or off, active now, today's usage, question budget; "
        "with the allow classes, never-here places and exceptions (pages let through with Not this one: counted)"
        ).add_argument("--pages", action="store_true", help="also list the pages let through with Not this one: "
                       "their titles and addresses")
    cmd(g, "show", rules_show, "one rule, every field").add_argument("id")
    sp = cmd(g, "add", rules_add, "a new rule, in your words (or a starter: --from-starter)", changes=True)
    sp.add_argument("id", help="short name: lowercase, digits, _")
    sp.add_argument("--what", help='what it is about, in your words: "short videos made for endless swiping"')
    sp.add_argument("--check-in", action="store_true",
                    help="ask what for and for how long on arrival, and step in when that time is up, instead of stepping in at once")
    sp.add_argument("--site", action="append", help="always counts here: douyin.com, youtube.com/shorts, youtube.com/ (home only); repeat")
    sp.add_argument("--app", action="append", help="always counts while this app is in front: its name or bundle id "
                                                   "(Steam, com.valvesoftware.steam); repeat")
    sp.add_argument("--when", action="append", help='only then: "mon-fri 09:00-18:00", "weekends", "22:00-02:00"; repeat')
    sp.add_argument("--learning-ok", action="store_true", help="lectures, tutorials and docs never hit it")
    sp.add_argument("--on-purpose-ok", action=argparse.BooleanOptionalAction, default=True,
                    help="one item opened from search, a work app or a chat link is fine (the default; "
                         "--no-on-purpose-ok counts those too)")
    sp.add_argument("--whole-page", action="store_true",
                    help="judge the page itself, so feeds, home pages and store fronts can hit (the default)")
    sp.add_argument("--content-only", action="store_true",
                    help="judge only the item that's open: feeds, home pages and listings never hit")
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
    sp.add_argument("--content-only", action="store_true", help="for a draft: judge only the item that's open, as "
                    "`rules add --content-only` would")
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
    sp = cmd(g, "test", allow_test, "what an allow class would let through on your recent screens (asks the model); "
             "--what tries other wording, or a draft under a new id, without saving")
    sp.add_argument("id")
    sp.add_argument("--what", help="wording to try instead of the saved one (or for a draft)")
    sp.add_argument("--threshold", type=float, help="threshold to try")
    sp.add_argument("--last", type=int, default=100, help="how many recent distinct screens")
    sp.add_argument("--show", type=int, default=15, help="how many to print, highest score first")
    sp.add_argument("--all", action="store_true", help="print all of them")
    sp = cmd(g, "set", allow_set, "change fields, like `rules set`", changes=True)
    sp.add_argument("id")
    sp.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    cmd(g, "on", allow_toggle, "switch on", changes=True).add_argument("id")
    cmd(g, "off", allow_toggle, "switch off", changes=True).add_argument("id")
    cmd(g, "remove", allow_remove, "delete", changes=True).add_argument("id")

    g = group("except", "things that look like a rule but are fine")
    cmd(g, "list", except_list, "per rule: typed, and learned from “Not this one”").add_argument("rule", nargs="?")
    sp = cmd(g, "add", except_add, "an exception in your words, which the model reads with the rule; or --from ID: "
             "one page or window that was fine, as the pop-up's Not this one", changes=True)
    sp.add_argument("rule")
    sp.add_argument("text", nargs="?", help='"a lecture or conference talk"')
    sp.add_argument("--from", dest="from_", metavar="ID",
                    help="a screen id from `rules test` or `review`, or a pop-up's decision id: let that page "
                         "(or that app window, below the score it had) through")
    sp = cmd(g, "remove", except_remove, "remove one (the exact text, title or URL `except list` shows)", changes=True)
    sp.add_argument("rule")
    sp.add_argument("text")

    g = group("never", "apps and sites where no rule ever fires")
    cmd(g, "list", never_list, "every never-here place")
    for name, fn, help in (("add", never_add, "no rule fires here again"), ("remove", never_remove, "undo")):
        sp = cmd(g, name, fn, help, changes=True)
        sp.add_argument("--site", help="example.com")
        sp.add_argument("--app", help="the app's name or bundle id, e.g. WhatsApp or net.whatsapp.WhatsApp")
        sp.add_argument("--name", help="the app's name, for display")

    g = group("settings", "apps never read, sites never judged, check-in waits, the question limit")
    cmd(g, "show", settings_show, "every setting, with what it does")
    sp = cmd(g, "set", settings_set, "max_wait_s=90, no_monitor+=com.example.App, allow_sites+=github.com", changes=True)
    sp.add_argument("pairs", nargs="+", metavar="KEY=VALUE")

    g = group("config", "the whole config as JSON: export, edit, apply in one checked step; check; undo")
    cmd(g, "export", config_export, "settings, allow classes and rules as JSON, as written (defaults left out)")
    sp = cmd(g, "apply", config_apply, "make rules.toml match a JSON file (or - for stdin) in the shape `export` "
             "prints: only what it names changes, and anything it leaves out (a rule, a setting, a key in a rule) "
             "is kept; a key set to null goes back to its default. All of it or nothing", changes=True)
    sp.add_argument("file", help="JSON file, or - for stdin")
    sp.add_argument("--prune", action="store_true",
                    help="also remove the rules, allow classes, settings and keys the file leaves out")
    cmd(g, "check", config_check, "does rules.toml load, and how full is the question budget")
    cmd(g, "undo", config_undo, "if rules.toml was changed by hand since Qualm last saved it (a broken file "
        "included), go back to the version Qualm last saved; otherwise step back one saved change. Shows what it put "
        f"back, and keeps the file it replaces as backups/rules.toml.replaced; again to go further back (the last "
        f"{KEEP}). Only rules.toml: focus, pause, never-here places and "
        "learned exceptions are undone with their own --stop and remove", changes=True)
    cmd(g, "migrate", config_migrate, "a rules.toml from before check-ins: time_cap rules become check_in, "
        "daily budgets are removed", changes=True)

    sp = sub.add_parser("doctor", help="is everything Qualm needs in place? each problem with its fix",
                        description="Checks where your rules and data live, the model (local: the runtime, memory to "
                                    "spare, the server; hosted: the key), Accessibility, rules.toml, the app and "
                                    "start-at-login, and says how to fix what isn't right.")
    sp.add_argument("--rules", default=defaults["rules"])
    sp.add_argument("--data", default=defaults["data"])
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
    sp = sub.add_parser("pause", help="pause every rule for a while (default 30 min, up to 7 days), now or from later; "
                        "--stop resumes",
                        description="Pause every rule for MINUTES (default 30) or a span (90m, 2h, 2d), until a time "
                                    "(--until 'mon 09:00'), at most 7 days; from now, or from later, at most 7 days "
                                    "ahead (--from 'sat 00:00' --until 'mon 09:00': the coming weekend). --stop "
                                    "resumes and cancels a planned pause.")
    sp.add_argument("minutes", nargs="?", help="minutes (30), or a span: 90m, 2h, 2d")
    sp.add_argument("--until", help="until then: 09:00, 'mon 09:00', 'tomorrow 09:00', mon, '2026-09-28 09:00'")
    sp.add_argument("--from", dest="from_", metavar="WHEN",
                    help="start then, not now: the same forms, the next such moment (--until is then the next one "
                         "after it). A weekend under way is `pause --until 'mon 09:00'`, without --from")
    sp.add_argument("--stop", action="store_true", help="resume now, and cancel a planned pause")
    sp.add_argument("--data", default=defaults["data"])
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=run, cmd_fn=pause)
    sp = sub.add_parser("guide", help="how an AI agent should change your rules: paste `qualm guide` into Claude Code, Codex, ...",
                        description="how an AI agent should change your rules; the same text as the Claude Code skill")
    sp.add_argument("--json", action="store_true", help='the text as {"guide": "..."}')
    sp.set_defaults(fn=run, cmd_fn=lambda args: _out(args, {"guide": guide()}, guide().rstrip("\n")))
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
