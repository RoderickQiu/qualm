"""Edit rules.toml from code: what every `seenot-desktop rules|allow|settings`
command, and the review page, goes through.

Edits are surgical: only the keys you change are rewritten, so comments and
the rest of the file survive. Every edit is parsed with the same checks as
loading before it's written (a broken file is never saved), and the previous
version is kept as rules.toml.bak.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import MISSING, fields
from pathlib import Path

from .rules import AllowClass, Rule, Settings, parse_config

EXAMPLE = Path(__file__).resolve().parents[2] / "rules.example.toml"
TABLES = {"rules": Rule, "allow": AllowClass}


# -- values -----------------------------------------------------------------


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # a JSON string is a valid TOML basic string
    if isinstance(v, (list, tuple)):
        items = [toml_value(x) for x in v]
        one = "[" + ", ".join(items) + "]"
        return one if len(one) <= 80 else "[\n" + "".join(f"    {x},\n" for x in items) + "]"
    raise TypeError(f"can't write {v!r} to TOML")


def field_type(cls, key: str):
    """bool / int / float / str / list, from the dataclass default."""
    f = next((f for f in fields(cls) if f.name == key), None)
    if f is None:
        known = sorted(x.name for x in fields(cls))
        raise ValueError(f"unknown key {key!r}; known: {', '.join(known)}")
    return str if f.default is MISSING else list if isinstance(f.default, tuple) else type(f.default)


def parse_value(cls, key: str, raw: str):
    """A value typed on the command line, as the key's type. Lists take a JSON
    array, or one item (use key+=item to add more)."""
    t = field_type(cls, key)
    if t is bool:
        if raw.lower() in ("true", "yes", "on", "1"):
            return True
        if raw.lower() in ("false", "no", "off", "0"):
            return False
        raise ValueError(f"{key}: true or false")
    if t in (int, float):
        try:
            return t(raw)
        except ValueError:
            raise ValueError(f"{key}: a number") from None
    if t is list:
        if raw.startswith("["):
            v = json.loads(raw)
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(f"{key}: a JSON array of strings")
            return v
        return [raw] if raw else []
    return raw


def apply_assignments(cls, current: dict, assignments: list[str]) -> tuple[dict, list[str]]:
    """["threshold=0.2", "sites+=douyin.com", "when-=weekends", "note="] ->
    (values to set, keys to remove). `key=` with nothing removes the key."""
    out, unset = {}, []
    for a in assignments:
        m = re.fullmatch(r"([a-z_]+)\s*([+-]?=)(.*)", a, re.S)
        if not m:
            raise ValueError(f"{a!r}: write key=value, key+=item or key-=item")
        key, op, raw = m.groups()
        if op == "=":
            if raw == "":
                unset.append(key)
                field_type(cls, key)
            else:
                out[key] = parse_value(cls, key, raw)
            continue
        if field_type(cls, key) is not list:
            raise ValueError(f"{key}: += and -= are for lists")
        items = list(out.get(key, current.get(key, [])))
        if op == "+=":
            if raw not in items:
                items.append(raw)
        elif raw in items:
            items.remove(raw)
        else:
            raise ValueError(f"{key}: {raw!r} isn't there; it has {items}")
        out[key] = items
    return out, unset


# -- the file, as blocks ----------------------------------------------------


def _blocks(text: str) -> list[str]:
    """The file cut at each [table] / [[table]] header. Comments right above a
    header travel with it, so removing a rule removes its comments too."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, l in enumerate(lines) if re.match(r"\s*\[", l)]
    cuts = [0]
    for s in starts:
        c = s
        while c > cuts[-1] and (lines[c - 1].lstrip().startswith("#") or not lines[c - 1].strip()):
            c -= 1
        # Keep blank lines with the block above, comments with the one below.
        while c < s and not lines[c].strip():
            c += 1
        if c > cuts[-1]:
            cuts.append(c)
    cuts.append(len(lines))
    return ["".join(lines[a:b]) for a, b in zip(cuts, cuts[1:]) if a < b]


def _header(block: str) -> str:
    m = re.search(r"(?m)^\s*\[\[?\s*([A-Za-z_]+)\s*\]\]?", block)
    return m.group(1) if m else ""


def _block_id(block: str) -> str | None:
    try:
        data = tomllib.loads(block)
    except tomllib.TOMLDecodeError:
        return None
    for v in data.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v[0].get("id")
    return None


def _key_span(lines: list[str], key: str) -> tuple[int, int] | None:
    """Lines [start, end) holding `key = ...`, multi-line arrays included:
    the shortest run of lines from the key that parses."""
    for i, l in enumerate(lines):
        if re.match(rf"\s*{re.escape(key)}\s*=", l):
            for j in range(i + 1, len(lines) + 1):
                try:
                    if key in tomllib.loads("".join(lines[i:j])):
                        return i, j
                except tomllib.TOMLDecodeError:
                    continue
    return None


def _edit_block(block: str, set_: dict, unset: list[str]) -> str:
    lines = block.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    for key in unset:
        span = _key_span(lines, key)
        if span:
            del lines[span[0]:span[1]]
    for key, v in set_.items():
        new = f"{key} = {toml_value(v)}\n"
        span = _key_span(lines, key)
        if span:
            lines[span[0]:span[1]] = [new]
        else:
            # After the last line that isn't blank or a comment.
            end = len(lines)
            while end and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
                end -= 1
            lines.insert(end, new)
    return "".join(lines)


class Config:
    """rules.toml, for reading and editing. Each change is checked and saved at once."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def load(self) -> tuple[Settings, list[Rule]]:
        return parse_config(self.text())

    def raw(self, table: str, id: str) -> dict:
        """One entry as written in the file (no defaults filled in)."""
        for entry in tomllib.loads(self.text()).get(table, []):
            if entry.get("id") == id:
                return entry
        raise KeyError(self._missing(table, id))

    def _missing(self, table: str, id: str) -> str:
        ids = [e.get("id") for e in tomllib.loads(self.text()).get(table, [])]
        return f"no {'rule' if table == 'rules' else table} {id!r}; there are: {', '.join(map(str, ids)) or 'none'}"

    def _write(self, text: str) -> None:
        parse_config(text)  # refuse to save a file that no longer loads
        if self.path.exists():
            shutil.copyfile(self.path, self.path.with_name(self.path.name + ".bak"))
        self.path.write_text(text, encoding="utf-8")

    def edit(self, table: str, id: str, set_: dict | None = None, unset: list[str] = ()) -> None:
        cls = TABLES[table]
        for key in [*(set_ or {}), *unset]:
            field_type(cls, key)
        if "id" in (set_ or {}) or "id" in unset:
            raise ValueError("the id can't change: logs and exceptions refer to it. Add a new rule instead.")
        # An emptied list or text is the default: drop the key.
        unset = [*unset, *(k for k, v in (set_ or {}).items() if v in ([], (), ""))]
        set_ = {k: v for k, v in (set_ or {}).items() if k not in unset}
        blocks = _blocks(self.text())
        for i, b in enumerate(blocks):
            if _header(b) == table and _block_id(b) == id:
                blocks[i] = _edit_block(b, set_ or {}, list(unset))
                return self._write("".join(blocks))
        raise KeyError(self._missing(table, id))

    def add(self, table: str, entry: dict, text: str | None = None) -> None:
        """A new [[rules]] / [[allow]] entry at the end of its kind, or a block of `text` as is."""
        cls = TABLES[table]
        for key in entry:
            field_type(cls, key)
        if any(r.id == entry["id"] for r in self._all_entries()):
            raise ValueError(f"{entry['id']!r} already exists")
        block = text or f"[[{table}]]\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in entry.items())
        blocks = _blocks(self.text())
        last = max((i for i, b in enumerate(blocks) if _header(b) == table), default=len(blocks) - 1)
        if blocks and not blocks[last].endswith("\n\n"):
            blocks[last] = blocks[last].rstrip("\n") + "\n\n"
        blocks.insert(last + 1, block.rstrip("\n") + "\n" + ("\n" if last + 1 < len(blocks) else ""))
        self._write("".join(blocks))

    def remove(self, table: str, id: str) -> None:
        blocks = _blocks(self.text())
        keep = [b for b in blocks if not (_header(b) == table and _block_id(b) == id)]
        if len(keep) == len(blocks):
            raise KeyError(self._missing(table, id))
        self._write("".join(keep))

    def edit_settings(self, set_: dict | None = None, unset: list[str] = ()) -> None:
        for key in [*(set_ or {}), *unset]:
            if key == "allow" or key not in Settings.__dataclass_fields__:
                raise ValueError(f"unknown setting {key!r}; known: {', '.join(sorted(set(Settings.__dataclass_fields__) - {'allow'}))}")
        blocks = _blocks(self.text())
        i = next((i for i, b in enumerate(blocks) if _header(b) == "settings"), None)
        if i is None:
            blocks.insert(min(1, len(blocks)), "[settings]\n\n")
            i = min(1, len(blocks) - 1)
        blocks[i] = _edit_block(blocks[i], set_ or {}, list(unset))
        if not blocks[i].endswith("\n\n"):
            blocks[i] += "\n"
        self._write("".join(blocks))

    def _all_entries(self):
        settings, rules = self.load()
        return [*rules, *settings.allow]


def starter_blocks() -> dict[str, tuple[str, str]]:
    """The shipped rules and allow classes: id -> (table, block text, comments included)."""
    out = {}
    for b in _blocks(EXAMPLE.read_text(encoding="utf-8")):
        if _header(b) in TABLES and (id := _block_id(b)):
            out[id] = (_header(b), b)
    return out
