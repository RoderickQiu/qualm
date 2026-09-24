"""Qualm rules, and the typed questions they become.

The Android ScreenAnalyzer prompt asks one model call for everything in
prose: page type, sensitivity, a decision per constraint, and a 0-100
confidence. Here each of those is its own typed question, and the logic that
combines them lives in `policy.py`, not in the prompt. docs/POLICY.md says
what the rules are for.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
import unicodedata
from dataclasses import MISSING, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from typesafe_sdk import Choice, Noul

# "deny": step in at once. "check_in": ask what for and for how long when
# you arrive, stay out of the way until then, step in when the time is up
# (seenot-variant's TIME_CAP, without a daily budget). NO_MONITOR is
# [settings] no_monitor: those apps are never read.
Kind = Literal["deny", "check_in"]
# "content": judge the opened item; a feed of candidates never hits.
# "page": judge the page itself, so feeds and hot lists can hit.
Target = Literal["content", "page"]

DENY_OPTIONS = ("violates", "safe", "unknown")
CHECK_IN_OPTIONS = ("in_scope", "out_of_scope", "unknown")
HIT_LABELS = ("violates", "in_scope")
PAGE_KINDS = ("feed", "single_item", "search", "work", "other")
PURPOSES = ("learn", "task", "entertain")
# "rule" asks whether the screen breaks the user's rule; "direct" asks what
# the screen is. See rule_question(). Trials: "rule" is better on Kev-4B.
QUESTION_STYLE = os.environ.get("QUALM_QUESTION_STYLE", "rule")


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_WORDS = {"daily": DAYS, "weekdays": DAYS[:5], "weekends": DAYS[5:]}

# Password managers are never read, whatever [settings] no_monitor says:
# Apple's Passwords (every Mac since macOS 15), Keychain Access, and the
# common ones people install.
PASSWORD_MANAGERS = (
    "com.apple.Passwords", "com.apple.keychainaccess",
    "com.1password.1password", "com.agilebits.onepassword7", "com.agilebits.onepassword-osx",
    "com.bitwarden.desktop", "com.dashlane.Dashlane", "com.dashlane.dashlanephonefinal",
    "in.sinew.Enpass-Desktop", "org.keepassxc.keepassxc", "com.hicknhacksoftware.MacPass",
    "com.keepersecurity.passwordmanager", "com.lastpass.LastPass", "com.nordsec.nordpass", "me.proton.pass.electron",
    "com.markmcguill.strongbox", "com.markmcguill.strongbox.pro", "com.markmcguill.strongbox.mac",
    "com.markmcguill.strongbox.mac.pro", "com.markmcguill.strongbox.graphene",
)


def app_listed(apps, bundle_id: str, app: str = "") -> bool:
    """Whether a list of apps (no_monitor, never here, a rule's or an allow
    class's apps) names this one: by bundle id or by name, either one
    case-insensitively, so "Figma" works as well as com.figma.Desktop."""
    mine = {bundle_id.lower(), app.strip("‎").lower()} - {""}
    return any(a.strip().lower() in mine for a in apps)


# A lenient read of one list in a rules.toml that doesn't load: its items,
# quoted (a missing closing quote ends at the line) or bare, and where it ends.
_ITEM = re.compile(r'''"((?:[^"\\\n]|\\.)*)"?|'([^'\n]*)'?|\#[^\n]*|(\])|(\n)|([^\s,\[\]"'\#]+)|[^\S\n]+|[,\[]''')
_NEXT = re.compile(r"""[ \t]*(\[|["']?[\w.-]+["']?[ \t]*=)""")  # a line that starts a table or a key


def listed_anyway(text: str, key: str = "no_monitor") -> tuple[str, ...]:
    """What a rules.toml that doesn't load lists under `key`, as far as a
    lenient read finds it: every item after `key =`, up to the closing
    bracket, or where the next key or table starts if that bracket is
    missing. The app, started on another version or keeping the rules it
    had, skips these apps too: the mistake may be in the very edit that
    added an app to no_monitor."""
    found = []
    for m in re.finditer(rf"""(?m)^[ \t]*(?:settings\.)?["']?{key}["']?[ \t]*=[ \t]*(\[?)""", text):
        array = bool(m[1])
        for t in _ITEM.finditer(text, m.end()):
            quoted, literal, close, newline, bare = t.groups()
            if close is not None or newline is not None and (not array or _NEXT.match(text, t.end())):
                break
            item = next((x for x in (quoted, literal, bare) if x is not None), "").strip(" \t,")
            if item:
                found.append(item)
    return tuple(dict.fromkeys(found))


DOMAIN_RE = r"[a-z0-9.-]+\.([a-z]{2,}|xn--[a-z0-9-]+)"


def _punycode(host: str) -> str:
    """The host as browsers send it today (IDNA 2008, UTS 46 non-transitional):
    each label NFKC-normalized and, if it isn't ASCII, in xn-- form. Unlike
    Python's IDNA 2003 codec it keeps ß, ς and the joiners, so straße.de is
    xn--strae-oqa.de, not strasse.de (a different domain)."""
    labels = re.split(r"[.。．｡]", unicodedata.normalize("NFKC", host))  # the full stops IDNA takes
    try:
        return ".".join(x if x.isascii() else "xn--" + x.encode("punycode").decode("ascii") for x in labels)
    except UnicodeError:
        return ""


def site_pattern(site: str) -> str:
    """A site as people write it -> a URL regex. "douyin.com" is the site and
    its subdomains; "youtube.com/shorts" is that path and everything under
    it; "youtube.com/" (just a slash) is the home page only. Letter case
    doesn't matter, and a name in any script ("例子.中国") matches its xn--
    forms too, which is what browsers report: IDNA 2008's, and IDNA 2003's
    where it differs (straße.de: xn--strae-oqa.de and strasse.de)."""
    s = re.sub(r"^[a-z]+://", "", site.strip(), flags=re.I)
    host, slash, path = s.partition("/")
    host = host.lower().removeprefix("www.")
    try:
        puny = host.encode("idna").decode("ascii")
    except UnicodeError:
        puny = ""
    if not re.fullmatch(DOMAIN_RE, puny):
        raise ValueError(f"site {site!r}: write a domain, like douyin.com or youtube.com/shorts")
    now = _punycode(host)
    forms = list(dict.fromkeys((puny, now if re.fullmatch(DOMAIN_RE, now) else puny, host)))
    h = re.escape(puny) if len(forms) == 1 else "(" + "|".join(map(re.escape, forms)) + ")"
    if slash and not path:
        return rf"(?i)^https?://(www\.)?{h}/?([?#]|$)"
    return rf"(?i)^https?://([^/?#]*\.)?{h}" + (re.escape("/" + path.rstrip("/")) if path else "") + r"([/?#:]|$)"


def _parse_when(spec: str) -> tuple[frozenset[int], tuple[tuple[int, int], ...]]:
    """ "mon-fri 09:00-18:00", "weekends", "22:00-02:00", "mon,wed 12:00-13:00",
    "mon-fri 09:00-12:00, 13:00-18:00" -> (days, ((start minute, end minute), ...)).
    Days first, then any number of hours. No days: every day; no hours: all day."""
    days, hours, order = set(), [], ""
    for part in re.split(r"[\s,]+", spec.lower().strip()):
        if not part:
            continue
        if m := re.fullmatch(r"(\d{1,2}):(\d\d)-(\d{1,2}):(\d\d)", part):
            h1, m1, h2, m2 = map(int, m.groups())
            start, end = h1 * 60 + m1, h2 * 60 + m2
            if h1 > 23 or m1 > 59 or m2 > 59 or end > 24 * 60:
                raise ValueError(f"when {spec!r}: bad time")
            if start == end:
                raise ValueError(f"when {spec!r}: {part} starts and ends at the same time")
            hours.append((start, end))
            order += "h"
            continue
        order += "d"
        if part in DAY_WORDS:
            days |= {DAYS.index(d) for d in DAY_WORDS[part]}
        elif m := re.fullmatch(r"([a-z]{3})-([a-z]{3})", part):
            a, b = (DAYS.index(x) if x in DAYS else -1 for x in m.groups())
            if a < 0 or b < 0:
                raise ValueError(f"when {spec!r}: days are {', '.join(DAYS)}")
            days |= {(a + i) % 7 for i in range((b - a) % 7 + 1)}
        elif part in DAYS:
            days.add(DAYS.index(part))
        else:
            raise ValueError(f"when {spec!r}: can't read {part!r}; e.g. \"mon-fri 09:00-18:00\" or \"weekends\"")
    if "dh" in order and "hd" in order:  # "mon 09:00-12:00, tue 13:00-18:00" would give both days both hours
        raise ValueError(f"when {spec!r}: one set of days per entry; give each its own, like "
                         '["mon 09:00-12:00", "tue 13:00-18:00"]')
    return frozenset(days or range(7)), tuple(hours) or ((0, 24 * 60),)


def in_window(specs: tuple[str, ...], now: datetime) -> bool:
    """True if `now` falls in any of the windows; no windows means always.
    A window past midnight ("fri 22:00-02:00") belongs to the day it starts."""
    if not specs:
        return True
    day, minute = now.weekday(), now.hour * 60 + now.minute
    for spec in specs:
        days, hours = _parse_when(spec)
        for start, end in hours:
            if start < end and day in days and start <= minute < end:
                return True
            if start >= end and (day in days and minute >= start or (day - 1) % 7 in days and minute < end):
                return True
    return False


@dataclass
class Rule:
    id: str
    kind: Kind
    description: str  # what the rule is about, in your words; the model reads it
    description_en: str = ""  # optional English version; `lang = "en"` sends it instead
    exceptions: tuple[str, ...] = ()  # repair rules: typed, or added by "Not this one"
    # p_hit at or above this is a hit. Kev-4B ranks well but its p_hit runs
    # low: tuned thresholds are 0.15-0.5. `rules tune` sets it from your answers.
    threshold: float = 0.2
    target: Target = "content"
    sites: tuple[str, ...] = ()  # "douyin.com", "youtube.com/shorts": hit without the model's say
    patterns: tuple[str, ...] = ()  # the same as URL regexes, for what sites can't say
    # Bundle ids or app names ("com.valvesoftware.steam", "TV"): a hit whenever
    # that app is in front, without the model's say (it isn't asked about this
    # rule there). Games and players show too little text for it to judge.
    apps: tuple[str, ...] = ()
    allow_learning: bool = False  # lectures, tutorials, docs never hit this rule
    allow_intentional: bool = False  # one item opened from search or a link is exempt, not on the rule's own sites
    # Any page the model reads as a feed for entertainment hits this rule too,
    # on sites the rule never names. In the trials it lifted feed recall from
    # 0.40 to 0.67 with no false positives.
    feed_hit: bool = False
    enabled: bool = True  # off: never asked, never fires
    when: tuple[str, ...] = ()  # "mon-fri 09:00-18:00"; empty = always
    # Allow classes this rule steps in on anyway: "online shopping" and the
    # shopping class are about the same pages, and the class would win.
    overrides_allow: tuple[str, ...] = ()
    note: str = ""  # for you (or an agent) reading the file: why it's set this way

    def text(self, lang: str) -> str:
        return self.description_en if lang == "en" and self.description_en else self.description

    def text_field(self, lang: str) -> str:
        """The field the model reads: what `rules set ID what=...` edits."""
        return "description_en" if lang == "en" and self.description_en else "description"

    def matches_url(self, url: str) -> bool:
        return bool(url) and any(re.search(p, url) for p in (*map(site_pattern, self.sites), *self.patterns))

    def matches_app(self, bundle_id: str, app: str = "") -> bool:
        return app_listed(self.apps, bundle_id, app)

    def active(self, now: datetime | None = None) -> bool:
        return self.enabled and in_window(self.when, now or datetime.now())


@dataclass
class AllowClass:
    """A kind of page no rule may fire on, in your words ("an online store").
    Each is one yes/no question per reading."""

    id: str
    description: str
    description_en: str = ""
    threshold: float = 0.5  # P(yes) at or above this: the page is this class
    sites: tuple[str, ...] = ()  # known to be this class: allowed without the model
    patterns: tuple[str, ...] = ()  # the same as URL regexes
    apps: tuple[str, ...] = ()  # bundle ids or app names known to be this class
    enabled: bool = True
    note: str = ""

    def text(self, lang: str) -> str:
        return self.description_en if lang == "en" and self.description_en else self.description

    def text_field(self, lang: str) -> str:
        return "description_en" if lang == "en" and self.description_en else "description"

    def matches(self, bundle_id: str, url: str, app: str = "") -> bool:
        return app_listed(self.apps, bundle_id, app) or bool(url) and any(
            re.search(p, url) for p in (*map(site_pattern, self.sites), *self.patterns))


@dataclass
class Settings:
    lang: str = "en"  # "en": rules with a description_en send that instead of description
    no_monitor: tuple[str, ...] = ()  # bundle ids or app names never read at all (password managers never are)
    allow_sites: tuple[str, ...] = ()  # sites that are never judged ("github.com")
    allow_urls: tuple[str, ...] = ()  # the same as URL regexes
    # The longest wait before a check-in or "I need it" unlocks, in seconds.
    # The wait doubles with each session today (the first is free) and again
    # when you come back soon after one ended.
    max_wait_s: int = 60
    # "5 more" when a check-in session's time is up: how many per session.
    extensions: int = 1
    # While a pop-up is open, the dimmed screen takes the clicks: answer it
    # before you go on. False: the dim is only a veil you can click through.
    block_clicks: bool = True
    # Questions one reading may ask: rules on at the same time + allow classes
    # + the 3 shared ones. Kev-4B on a 24 GB Mac: 13 questions 0.6 s, 28 1.8 s,
    # 53 4.5-18 s, 103 timed out and swapped the machine (HANDOFF, Measured).
    max_questions: int = 25
    # Where the model runs. "kev": on this Mac, nothing leaves it. "jev":
    # TypeSafe's hosted model, each new screen's text is sent there. The
    # environment's QUALM_BACKEND wins over this.
    backend: str = "kev"
    # Where the local model downloads from, if huggingface.co is blocked: a
    # mirror's https address (HF_ENDPOINT in the environment wins). Empty: huggingface.co.
    hf_endpoint: str = ""
    # How long the logs keep what you read on screen (retention.py): every
    # judgement and pop-up for keep_days, screenshots for keep_shots_days.
    # 0 keeps them forever. Your reviews and what you taught Qualm are kept.
    keep_days: int = 90
    keep_shots_days: int = 30
    # The dashboard's port on 127.0.0.1 (QUALM_DASHBOARD_PORT wins). If another
    # program holds it, the app takes a free one (review.start_dashboard).
    dashboard_port: int = 8765
    allow: tuple[AllowClass, ...] = ()  # [[allow]]: kinds of page never flagged

    def allowed_url(self, url: str) -> bool:
        return bool(url) and any(re.search(p, url) for p in (*map(site_pattern, self.allow_sites), *self.allow_urls))

    def never_read(self, bundle_id: str, app: str = "") -> bool:
        return app_listed((*PASSWORD_MANAGERS, *self.no_monitor), bundle_id, app)


RULE_FIELDS = set(Rule.__dataclass_fields__)
ALLOW_FIELDS = set(AllowClass.__dataclass_fields__)
SETTINGS_FIELDS = set(Settings.__dataclass_fields__) - {"allow"}
BACKENDS = ("kev", "jev")
ID_RE = r"[a-z][a-z0-9_]*"


def undo_hint(path: str | Path) -> str:
    """ "run `qualm config undo` to go back to ...", as a load error of this
    rules.toml advises, or "" when nothing is kept to go back to."""
    from .agent import command
    from .config import undo_to

    to = undo_to(path)
    return f"run `{command()} config undo` to go back to {to}" if to else ""


def correct_it(path: str | Path) -> str:
    """The end of a load error: what to do about it."""
    hint = undo_hint(path)
    return f"Correct it in the file{f', or {hint}' if hint else ''}."


def _q() -> str:
    """How a terminal here runs qualm (agent.command), for messages."""
    from .agent import command

    return command()


class ConfigError(ValueError):
    """Something in rules.toml to fix, and where it is when known: the table, the entry's id, the key."""

    def __init__(self, message: str, table: str = "", id: str | None = None, key: str | None = None):
        super().__init__(message)
        self.table, self.id, self.key = table, id, key


def _shown(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)[:60]


def _typed(cls, where: str, table: str, id: str | None, raw: dict) -> dict:
    """raw's values, each checked against its field's type (lists become
    tuples): "false" is not false, and "a.com" is not ["a.com"]."""
    out = {}
    for f in cls.__dataclass_fields__.values():
        if f.name not in raw:
            if f.default is MISSING:
                raise ConfigError(f"{where}: needs {f.name}", table, id, f.name)
            continue
        v, d = raw[f.name], f.default
        if isinstance(d, bool):
            ok, want = isinstance(v, bool), "true or false, without quotes"
        elif isinstance(d, int):
            ok, want = isinstance(v, int) and not isinstance(v, bool), "a whole number"
        elif isinstance(d, float):
            ok, want = isinstance(v, (int, float)) and not isinstance(v, bool), "a number"
        elif isinstance(d, tuple):
            ok = isinstance(v, list) and all(isinstance(x, str) for x in v)
            want = f"a list of text, like {f.name} = [{_shown(v) if isinstance(v, str) else '...'}]"
            if ok and not all(x.strip() for x in v):
                raise ConfigError(f"{where}: {f.name} has an empty item", table, id, f.name)
        else:
            ok, want = isinstance(v, str), "text in quotes"
        if not ok:
            raise ConfigError(f"{where}: {f.name} must be {want}, not {_shown(v)}", table, id, f.name)
        out[f.name] = tuple(v) if isinstance(v, list) else v
    return out


def _make(cls, fields: set[str], table: str, raw: dict):
    """One [[rules]] or [[allow]] entry, checked, with a message that says what to fix."""
    rid = raw.get("id")
    if not isinstance(rid, str) or not re.fullmatch(ID_RE, rid):
        raise ValueError(f"[[{table}]] id {rid!r}: lowercase letters, digits and _, starting with a letter")
    where = f"rule {rid!r}" if table == "rules" else f"allow class {rid!r}"
    old = set(raw) & {"minutes_per_day", "visits_per_day"}
    if table == "rules" and (old or raw.get("kind") == "time_cap"):
        raise ValueError(
            f"rule {rid!r}: daily budgets are gone; a check_in rule asks what for and for how long when you "
            f"arrive, and steps in when that time is up. `{_q()} config migrate` updates the file.")
    unknown = set(raw) - fields
    if unknown:
        raise ConfigError(f"{table} {rid!r}: unknown keys {sorted(unknown)}; known: {sorted(fields)}", table, rid)
    if raw.get("description") is None or not str(raw["description"]).strip():
        raise ConfigError(f"{table} {rid!r}: needs a description (what it's about, in your words)", table, rid, "description")
    if table == "rules" and "kind" not in raw:
        raise ConfigError(f"rule {rid!r}: needs a kind: deny (step in at once) or check_in (ask what for, step in "
                          "when the time is up)", table, rid)
    if table == "rules" and raw.get("kind") not in ("deny", "check_in"):
        raise ConfigError(f"rule {rid!r}: kind is deny (step in at once) or check_in (ask what for, step in when "
                          "the time is up)", table, rid, "kind")
    obj = cls(**_typed(cls, where, table, rid, raw))
    if not 0 < obj.threshold < 1:
        raise ConfigError(f"{table} {rid!r}: threshold must be between 0 and 1", table, rid, "threshold")
    for p in obj.patterns:
        try:
            re.compile(p)
        except re.error as e:
            raise ConfigError(f"{where}: pattern {p!r} isn't a regular expression ({e}); a plain site goes in sites",
                              table, rid, "patterns") from None
    for site in obj.sites:
        try:
            site_pattern(site)
        except ValueError as e:
            raise ConfigError(f"{where}: {e}", table, rid, "sites") from None
    return obj


def parse_config(text: str, source: str | Path | None = None, advice: bool = True) -> tuple[Settings, list[Rule]]:
    """Settings and rules from rules.toml's text, every value checked. With
    `source`, the file it came from: a problem is then reported as that
    file's, with the line when it's known and (with `advice`) what to do
    about it."""
    if source is None:
        return _parse(text)
    try:
        return _parse(text)
    except ValueError as e:
        n = _line_of(text, e.table, e.id, e.key) if isinstance(e, ConfigError) else None
        why = str(e)
        if isinstance(e, tomllib.TOMLDecodeError) and (m := re.fullmatch(r"Illegal character (.+) \((at .+)\)", why)):
            why = f"a control character ({m[1]}, invisible in most editors) {m[2]}"  # a DEL or NUL, not tomllib's words
        msg = f"{source} doesn't load{f' (line {n})' if n else ''}: {why}"
        if advice and "config migrate`" not in msg:
            msg = msg.rstrip(".") + ". " + correct_it(source)
        raise (OverLimit if isinstance(e, OverLimit) else ValueError)(msg) from None


def _line_of(text: str, table: str, id: str | None, key: str | None) -> int | None:
    """The line (from 1) of an entry's key, or of its header, for messages."""
    if not table:
        return None
    lines = text.splitlines()
    heads = [i for i, line in enumerate(lines) if re.match(r"\s*\[", line)] + [len(lines)]
    for a, b in zip(heads, heads[1:]):
        body = lines[a:b]
        if not re.match(rf"\s*\[\[?\s*{table}\s*\]", body[0]) or id is not None and not any(
                re.match(rf"""\s*id\s*=\s*["']{re.escape(id)}["']""", line) for line in body):
            continue
        return a + 1 + next((i for i, line in enumerate(body) if key and re.match(rf"\s*{re.escape(key)}\s*=", line)), 0)
    return None


def _parse(text: str) -> tuple[Settings, list[Rule]]:
    data = tomllib.loads(text)
    extra = set(data) - {"settings", "rules", "allow"}
    if extra:
        raise ValueError(f"unknown {', '.join(map(repr, sorted(extra)))}: rules.toml has only [settings], [[rules]] "
                         "and [[allow]], and a setting goes under [settings]")
    s = data.get("settings", {})
    if not isinstance(s, dict):
        raise ValueError("settings must be a table: a [settings] line, then one setting = value per line")
    for table in ("rules", "allow"):
        if not isinstance(data.get(table, []), list) or not all(isinstance(e, dict) for e in data.get(table, [])):
            raise ValueError(f"{table}: each entry is a table that starts with a [[{table}]] line")
    if "budgets" in s:
        raise ValueError("[settings] budgets is gone: time rules check in when you arrive instead of counting a "
                         f"daily budget. `{_q()} config migrate` updates the file.")
    unknown = set(s) - SETTINGS_FIELDS
    if unknown:
        raise ConfigError(f"[settings]: unknown keys {sorted(unknown)}; known: {sorted(SETTINGS_FIELDS)}", "settings")
    settings = Settings(**_typed(Settings, "[settings]", "settings", None, s),
                        allow=tuple(_make(AllowClass, ALLOW_FIELDS, "allow", a) for a in data.get("allow", ())))
    for site in settings.allow_sites:
        try:
            site_pattern(site)
        except ValueError as e:
            raise ConfigError(f"[settings] allow_sites: {e}", "settings", None, "allow_sites") from None
    for p in settings.allow_urls:
        try:
            re.compile(p)
        except re.error as e:
            raise ConfigError(f"[settings] allow_urls: {p!r} isn't a regular expression ({e}); a plain site goes in "
                              "allow_sites", "settings", None, "allow_urls") from None
    if not 0 <= settings.max_wait_s <= 600:
        raise ConfigError("[settings] max_wait_s: seconds, between 0 and 600", "settings", None, "max_wait_s")
    if settings.backend not in BACKENDS:
        raise ConfigError(f"[settings] backend: one of {', '.join(BACKENDS)} (kev on this Mac, jev hosted by TypeSafe)",
                          "settings", None, "backend")
    # https only: the model's weights come through it, and plain http can be changed on the way.
    if settings.hf_endpoint and not re.fullmatch(r"https://[^\s/?#]+(/[^\s?#]*)?", settings.hf_endpoint):
        raise ConfigError("[settings] hf_endpoint: a Hugging Face mirror's https address, like https://hf-mirror.com",
                          "settings", None, "hf_endpoint")
    for key in ("keep_days", "keep_shots_days"):
        if not 0 <= getattr(settings, key) <= 3650:
            raise ConfigError(f"[settings] {key}: days, between 0 (forever) and 3650", "settings", None, key)
    if not 0 <= settings.extensions <= 5:
        raise ConfigError("[settings] extensions: between 0 and 5", "settings", None, "extensions")
    if settings.max_questions < SHARED + 1:
        raise ConfigError(f"[settings] max_questions: at least {SHARED + 1} (the {SHARED} shared questions and one rule)",
                          "settings", None, "max_questions")
    if not 1024 <= settings.dashboard_port <= 65535:
        raise ConfigError("[settings] dashboard_port: a port between 1024 and 65535", "settings", None, "dashboard_port")
    rules = [_make(Rule, RULE_FIELDS, "rules", r) for r in data.get("rules", ())]
    for r in rules:
        if r.target not in ("content", "page"):
            raise ConfigError(f"rule {r.id!r}: target is content or page", "rules", r.id, "target")
        for w in r.when:
            try:
                _parse_when(w)
            except ValueError as e:
                raise ConfigError(f"rule {r.id!r}: {e}", "rules", r.id, "when") from None
    ids = [r.id for r in rules] + [c.id for c in settings.allow]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate ids: {sorted({i for i in ids if ids.count(i) > 1})}")
    cap = capacity(settings, rules)
    if cap["peak"] > settings.max_questions:
        raise OverLimit(
            f"{cap['peak']} questions per reading at {cap['peak_at']}, over the limit of {settings.max_questions} "
            f"({cap['peak_rules']} rules on at once + {cap['allow']} allow classes + {cap['shared']} shared). "
            "Past the limit the model slows sharply. Switch rules off, give them hours (`when`) that don't "
            "overlap, or fold two rules into one description.")
    return settings, rules


def unknown_overrides(settings: Settings, rules: list[Rule]) -> list[tuple[str, str]]:
    """(rule id, class id) for each overrides_allow that names no allow class.
    Loading ignores them, as there's nothing to override: an allow class
    removed by hand mustn't stop Qualm. A change that makes one is refused
    (config.py)."""
    have = {c.id for c in settings.allow}
    return [(r.id, a) for r in rules for a in r.overrides_allow if a not in have]


class OverLimit(ValueError):
    """Too many questions per reading: see Settings.max_questions."""


SHARED = 3  # sensitive, page_kind, purpose: asked on every reading


def capacity(settings: Settings, rules: list[Rule], now: datetime | None = None) -> dict:
    """Questions per reading now and at the busiest moment of the week. Rules
    only switch on at a window's start, so those moments (and midnight) are
    the only ones to check."""
    allow = sum(c.enabled for c in settings.allow)
    on = [r for r in rules if r.enabled]
    starts = {0}
    for r in on:
        for w in r.when:
            starts |= {start for start, _ in _parse_when(w)[1]}
    monday = datetime(2024, 1, 1)  # any Monday
    peak, peak_at = -1, ""
    for day in range(7):
        for m in sorted(starts):
            t = monday.replace(day=1 + day, hour=m // 60 % 24, minute=m % 60)
            n = sum(in_window(r.when, t) for r in on)
            if n > peak:
                peak, peak_at = n, "any time" if all(not r.when for r in on) else f"{DAYS[day]} {m // 60:02d}:{m % 60:02d}"
    now_n = sum(r.active(now) for r in on)
    return {"limit": settings.max_questions, "now": SHARED + allow + now_n, "rules_now": now_n,
            "peak": SHARED + allow + peak, "peak_rules": peak, "peak_at": peak_at, "allow": allow, "shared": SHARED}


def read_rules(path: str | Path) -> str:
    """rules.toml's text (an editor's byte-order mark is fine), or a
    ValueError that names the file and says what to do."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        line = raw[:e.start].count(b"\n") + 1
        hint = undo_hint(path)
        raise ValueError(f"{path} isn't UTF-8 text (line {line}): save it as UTF-8{f', or {hint}' if hint else ''}") from None
    if blank(text):
        hint = undo_hint(path)
        raise ValueError(f"{path} is empty: {f'{hint}, or delete it' if hint else 'delete it'} to start again from the "
                         "starter rules")
    return text


def blank(text: str) -> bool:
    """Nothing but comments and blank lines: a rules.toml like that was cut
    short or emptied by mistake, so it isn't loaded. (A change that removes
    the last entry keeps a [settings] line: config.Config._save.)"""
    return not re.sub(r"(?m)^\s*#.*$", "", text).strip()


def load_config(path: str | Path) -> tuple[Settings, list[Rule]]:
    return parse_config(read_rules(path), path)


def load_rules(path: str | Path) -> list[Rule]:
    return load_config(path)[1]


SENSITIVE = Noul(
    instructions=(
        "The screen is asking the user to pay, log in, sign up, enter a verification code or password, "
        "confirm identity, or it shows bank cards, balances, transactions or saved passwords. "
        "A shopping cart, product page or checkout button alone does not count."
    )
)

PAGE_KIND = Choice(
    instructions="What kind of page is in front of the user right now?",
    criteria={
        "feed": "A home page, feed, recommendation list or grid of many candidate items the user has not opened",
        "single_item": "One opened item: an article, video, post, product, thread or chat",
        "search": "Search results or a search being typed",
        "work": "An editor, document, terminal, IDE, spreadsheet, design tool or other work tool",
        "other": "Anything else: settings, empty window, system dialog",
    },
)

PURPOSE = Choice(
    instructions="What is the content on screen for?",
    criteria={
        "learn": "Learning: a lecture, course, tutorial, documentation, paper, how-to or explainer",
        "task": "Getting something specific done: work, looking something up, shopping, booking, "
        "a message to or from a specific person, news the user went looking for",
        "entertain": "Entertainment or passing time: comedy, clips, memes, gossip, games, streams, trending lists, feeds",
    },
)


def rule_question(rule: Rule, lang: str = "zh") -> Choice:
    exceptions = ""
    if rule.exceptions:
        exceptions = " Exceptions the user has confirmed are fine: " + "; ".join(rule.exceptions) + "."
    judge = (
        "Judge the page as a whole, including feeds and lists."
        if rule.target == "page"
        else "Judge only the content currently open, not candidates listed in a feed."
    )
    what = rule.text(lang)
    if QUESTION_STYLE == "direct":
        # Ask what the screen is, not whether it breaks a prohibition.
        # Same answer keys, so the policy is unchanged.
        if rule.kind == "deny":
            return Choice(
                instructions=f"Is the content currently open {what}?{exceptions} {judge}",
                criteria={
                    "violates": f"Yes: the open content is {what}",
                    "safe": "No: the open content is something else",
                    "unknown": "The screen does not show enough to tell",
                },
            )
        return Choice(
            instructions=f"Is the user currently {what}?{exceptions}",
            criteria={
                "in_scope": f"Yes: the current screen is {what}",
                "out_of_scope": "No: the current screen is something else",
                "unknown": "The screen does not show enough to tell",
            },
        )
    if rule.kind == "deny":
        return Choice(
            instructions=f"The user set this rule: do not show me {what}.{exceptions} {judge}",
            criteria={
                "violates": "The open content is what the rule forbids",
                "safe": "The open content is not what the rule forbids",
                "unknown": "The screen does not show enough to tell",
            },
        )
    # check_in: the wording the trials' thresholds were tuned on, from when
    # these rules were daily time caps. Changing it moves every score.
    return Choice(
        instructions=f"The user set a time limit on: {what}.{exceptions} "
        "Does the current screen count toward that limit?",
        criteria={
            "in_scope": "The current screen is the limited activity",
            "out_of_scope": "The current screen is something else",
            "unknown": "The screen does not show enough to tell",
        },
    )


def question_key(rule: Rule, lang: str) -> str:
    """The rule's wording as the model reads it, as a short hash: scores are
    only compared with scores for the same wording."""
    q = rule_question(rule, lang).model_dump(mode="json", exclude_none=True)
    return hashlib.sha1(json.dumps([rule.kind, q], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def allow_question(c: AllowClass, lang: str = "zh") -> Noul:
    return Noul(instructions=f"The screen shows {c.text(lang)}.")


def build_questions(rules: list[Rule], lang: str = "zh", allow: tuple[AllowClass, ...] = ()) -> dict:
    questions = {"sensitive": SENSITIVE, "page_kind": PAGE_KIND, "purpose": PURPOSE}
    for c in allow:
        if not c.enabled:
            continue
        questions[f"allow_{c.id}"] = allow_question(c, lang)
    for rule in rules:
        questions[f"rule_{rule.id}"] = rule_question(rule, lang)
    return questions
