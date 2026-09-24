"""Edit rules.toml from code: what every `qualm rules|allow|settings`
command, and the review page, goes through.

Edits are surgical: only the keys you change are rewritten, so comments and
the rest of the file survive. Every edit is parsed with the same checks as
loading before it's written (a broken file is never saved). A change is one
step: read, edit and save under a lock (rules.toml.lock), so the CLI, the app
and the dashboard never lose each other's changes, and the file is replaced
whole, so nothing ever reads half of it. The last KEEP versions are kept in
backups/ for `config undo` (the latest also as rules.toml.bak), and so is
the file as Qualm last wrote it (an older Qualm's file: as first seen, if it
loads): after a hand edit, undo goes back to that first. What an undo
replaces or passes over is kept there too, never read again by Qualm.
Several edits can go in one batch, checked and saved together
(`apply`: the whole config as data, for agents), and any of it can be a dry
run that only shows the diff.
"""

from __future__ import annotations

import contextlib
import difflib
import fcntl
import functools
import json
import os
import re
import tempfile
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import MISSING, fields
from datetime import datetime
from pathlib import Path

from . import paths
from .rules import (AllowClass, ConfigError, Rule, Settings, _q, blank, correct_it, parse_config, read_rules,
                    unknown_overrides)

EXAMPLE = Path(__file__).resolve().parent / "rules.example.toml"
TABLES = {"rules": Rule, "allow": AllowClass}
KEEP = 20  # saved versions `config undo` can step back through
LOCK_WAIT_S = 10.0


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
    raise ValueError(f"can't write {json.dumps(v, default=str)[:60]} to rules.toml: use text, a number, true or "
                     "false, or a list of text")


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


def _comment(lines: list[str], key: str) -> str:
    """The comment at the end of a key's lines ("threshold = 0.3  # tuned"), with the space before it."""
    text = "".join(lines).rstrip("\n")
    for m in re.finditer("#", text):
        head = text[:m.start()]
        if "\n" in text[m.start():]:
            continue  # inside a multi-line array, not at its end
        try:
            if key in tomllib.loads(head):
                return head[len(head.rstrip()):] + text[m.start():]
        except tomllib.TOMLDecodeError:
            continue  # a # inside the value's text
    return ""


def _edit_block(block: str, set_: dict, unset: list[str], defaults: dict | None = None) -> str:
    """A comment at the end of a changed line stays on it; a key unset while
    it has one keeps its line, at the default (`defaults`)."""
    lines = block.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    for key in unset:
        span = _key_span(lines, key)
        if span:
            comment = _comment(lines[span[0]:span[1]], key) if defaults and key in defaults else ""
            lines[span[0]:span[1]] = [f"{key} = {toml_value(defaults[key])}{comment}\n"] if comment else []
    for key, v in set_.items():
        span = _key_span(lines, key)
        if span:
            lines[span[0]:span[1]] = [f"{key} = {toml_value(v)}{_comment(lines[span[0]:span[1]], key)}\n"]
        else:
            # After the last line that isn't blank or a comment.
            end = len(lines)
            while end and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
                end -= 1
            lines.insert(end, f"{key} = {toml_value(v)}\n")
    out = "".join(lines)
    return out[:-1] if block and not block.endswith("\n") and out.endswith("\n") else out


def _defaults(cls) -> dict:
    return {f.name: f.default for f in fields(cls) if f.default is not MISSING and f.name != "allow"}


# One lock per rules.toml for this process's threads (the menu, the
# dashboard), and flock on rules.toml.lock for other processes: [thread
# lock, the open lock file, how deep we are in it].
_LOCKS: dict[str, list] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def locked(path: str | Path):
    """rules.toml to ourselves: other commands, the app and the dashboard wait
    until what we read has been saved. Reentrant."""
    lock_file = Path(path).with_name(Path(path).name + ".lock")
    busy = f"{path} is being changed by something else that hasn't finished; try again"
    with _LOCKS_GUARD:
        entry = _LOCKS.setdefault(str(lock_file.resolve()), [threading.RLock(), None, 0])
    if not entry[0].acquire(timeout=LOCK_WAIT_S):
        raise ValueError(busy)
    try:
        if entry[2] == 0:
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        os.close(fd)
                        raise ValueError(busy) from None
                    time.sleep(0.02)
            entry[1] = fd
        entry[2] += 1
        try:
            yield
        finally:
            entry[2] -= 1
            if entry[2] == 0:
                os.close(entry[1])  # and with it the flock
                entry[1] = None
    finally:
        entry[0].release()


def _staged(path: Path, data: bytes, like: Path | None = None) -> str:
    """data in a temp file next to path, ready to os.replace it: nothing ever
    reads half a file. With `like`, its permissions and times are copied."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        src = like if like and like.exists() else path if path.exists() else None
        os.chmod(tmp, os.stat(src).st_mode & 0o777 if src else 0o644)
        if like and like.exists():
            os.utime(tmp, ns=(os.stat(like).st_atime_ns, os.stat(like).st_mtime_ns))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return tmp


def _replace(path: Path, data: bytes, like: Path | None = None) -> None:
    tmp = _staged(path, data, like)
    try:
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _merge(want: dict, have: dict, prune: bool) -> tuple[dict, list[str]]:
    """What to set and unset so an entry (or settings) as written matches
    `want`: keys it sets to None go, and with `prune` so do keys it leaves out."""
    set_ = {k: v for k, v in want.items() if k != "id" and v is not None and have.get(k) != v}
    unset = [k for k in have if k != "id" and (k in want and want[k] is None or prune and k not in want)]
    return set_, unset


def _check_settings(keys) -> None:
    for key in keys:
        if key == "allow" or key not in Settings.__dataclass_fields__:
            raise ValueError(f"unknown setting {key!r}; known: {', '.join(sorted(set(Settings.__dataclass_fields__) - {'allow'}))}")


def _unified(old: str, new: str, a: str, b: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), a, b))


def _unloadable(version: Path) -> str | None:
    """Why a kept version can't be put back (it may have been saved under
    looser checks, by an older Qualm), or None if it can."""
    try:
        text = version.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        return f"{version} isn't UTF-8 text"
    if blank(text):
        return f"{version} is empty"
    try:
        parse_config(text, version, advice=False)
    except ValueError as e:
        return str(e)
    return None


def undo_to(path: str | Path) -> str | None:
    """Where `config undo` would take this rules.toml, in words (for the
    advice its load errors, the menu, doctor and the dashboard give), or
    None: nothing kept that loads to go back to. A dry run of it, which
    saves nothing, so the advice never names an undo that would fail."""
    try:
        done = Config(path, dry_run=True).undo()
    except Exception:  # nothing kept, nothing that loads, or rules.toml unreadable
        return None
    return "the version Qualm last saved" if done["undid"] == "hand_edit" else \
        "the version before the last change Qualm saved"


def _locked(method):
    """The read, the change and the save as one step (see `locked`)."""
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        with self.locked():
            return method(self, *args, **kwargs)
    return run


class Config:
    """rules.toml, for reading and editing. Each change is checked and saved at
    once, or, inside `batch()`, all together at the end. With dry_run, nothing
    is saved: `pending` holds what would have been."""

    def __init__(self, path: str | Path, dry_run: bool = False):
        self.path = Path(path)
        self.dry_run = dry_run
        self.pending: str | None = None
        self._buffer: str | None = None

    def _read(self) -> str:
        """The file as saved. Before there is one, a dry run starts from the starter rules."""
        if self.dry_run and not self.path.exists():
            return EXAMPLE.read_text(encoding="utf-8")
        return read_rules(self.path)

    def text(self) -> str:
        if self._buffer is not None:
            return self._buffer
        if self.dry_run and self.pending is not None:
            return self.pending
        return self._read()

    def locked(self):
        """See `locked`; a dry run saves nothing, so it needs no lock."""
        return contextlib.nullcontext() if self.dry_run else locked(self.path)

    def create(self) -> bool:
        """rules.toml from the starter rules, if there's none yet. True if it was made now."""
        if self.dry_run or self.path.exists():
            return False
        with self.locked():
            if self.path.exists():  # made by another command in the meantime
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            starters = EXAMPLE.read_bytes()
            if self._saved().exists() and self._saved().read_bytes() != starters:
                # rules.toml was deleted: what Qualm last saved there is one undo away.
                for p in self._push(self._saved().read_bytes())[KEEP:]:
                    p.unlink(missing_ok=True)
            _replace(self.path, starters)
            self._remember(starters)
            return True

    @contextmanager
    def batch(self):
        """Edits inside are checked once, as a whole, and saved together or not at all."""
        with self.locked():
            self._buffer = self.text()
            try:
                yield self
                text = self._buffer
            finally:
                self._buffer = None
            self._save(text)

    def diff(self) -> str:
        """What a dry run would change, as a unified diff."""
        if self.pending is None:
            return ""
        if not self.path.exists():
            return _unified(EXAMPLE.read_text(encoding="utf-8"), self.pending,
                            f"{self.path} (not there yet: the starter rules)", f"{self.path} (after)")
        return _unified(self.path.read_bytes().decode("utf-8-sig", "replace"), self.pending,
                        str(self.path), f"{self.path} (after)")

    # -- the saved versions, for undo -----------------------------------------

    def _bak(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def _versions(self) -> list[Path]:
        """backups/rules.toml.N, oldest first: the file as it was before each saved change."""
        folder = self.path.with_name("backups")
        found = folder.glob(f"{self.path.name}.*") if folder.is_dir() else []
        return sorted((p for p in found if p.suffix[1:].isdigit()), key=lambda p: int(p.suffix[1:]))

    def _saved(self) -> Path:
        """rules.toml as Qualm last wrote it: when the file differs, it was changed by hand since."""
        return self.path.with_name("backups") / f"{self.path.name}.saved"

    def _replaced(self) -> Path:
        """The file the last undo replaced (a hand edit, say): kept, not lost."""
        return self.path.with_name("backups") / f"{self.path.name}.replaced"

    def _backups(self) -> Path:
        """backups/, made the first time something is kept there. In Qualm's
        folder, a rules.toml.bak without it was left by a Qualm from before
        backups/, which setup had set up: setup's mark is written first, as
        backups/ would hide that (setup.needed)."""
        folder = self.path.with_name("backups")
        if not folder.is_dir():
            done = paths.home() / paths.SETUP_DONE
            if (self.path.parent.resolve() == paths.home().resolve() and self._bak().exists() and self.path.exists()
                    and not done.exists()):
                with contextlib.suppress(OSError):
                    done.write_text(datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8")
            folder.mkdir(exist_ok=True)
        return folder

    def _remember(self, data: bytes, like: Path | None = None) -> None:
        try:
            self._backups()
            _replace(self._saved(), data, like=like)
        except OSError:  # without it, undo steps back one saved change, as if there had been no hand edit
            with contextlib.suppress(OSError):
                self._saved().unlink(missing_ok=True)

    def adopt(self) -> bool:
        """A rules.toml with no record of how Qualm last wrote it (an older
        Qualm's, which kept only rules.toml.bak, or one written by hand) is
        taken as the version Qualm last saved the first time it's seen, if
        it loads: a hand edit after that is told apart from the changes Qualm
        saved, and `config undo` drops only the edit. True if recorded now."""
        if self.dry_run or self._saved().exists() or not self.path.exists():
            return False
        try:
            with self.locked():
                if self._saved().exists():  # recorded by another command in the meantime
                    return False
                data = self.path.read_bytes()
                parse_config(read_rules(self.path))
                self._remember(data, like=self.path)  # its time: when it was saved
                return self._saved().exists()
        except (OSError, ValueError):  # it doesn't load (undo then says what it goes back to), or it's busy: next time
            return False

    def _aside(self, version: Path) -> Path:
        """Where a kept version undo passes over goes instead of being deleted:
        backups/NAME.aside (.aside2, ... if taken), which undo never reads."""
        to, n = self.path.with_name("backups") / f"{version.name}.aside", 1
        while to.exists():
            n += 1
            to = to.with_name(f"{version.name}.aside{n}")
        return to

    def _push(self, old: bytes) -> list[Path]:
        """Keep the version about to be replaced: as backups/rules.toml.N and as
        rules.toml.bak. Returns what it wrote, to take back if the save fails."""
        versions = self._versions()
        folder = self._backups()
        if not versions and self._bak().exists() and self._bak().read_bytes() != old:
            # A .bak from before backups/ (an older Qualm, or a checkout's): the oldest version, not lost.
            first = folder / f"{self.path.name}.1"
            _replace(first, self._bak().read_bytes(), like=self._bak())
            versions = [first]
        n = int(versions[-1].suffix[1:]) + 1 if versions else 1
        new = folder / f"{self.path.name}.{n}"
        _replace(new, old, like=self.path)
        try:
            _replace(self._bak(), old, like=self.path)
        except OSError:
            new.unlink(missing_ok=True)
            raise
        return [new, *reversed(versions)]

    @_locked
    def undo(self) -> dict:
        """If rules.toml was changed by hand since Qualm last saved it (broken
        or not), back to the version Qualm last saved, and the saved versions
        stay as they are. Otherwise back one saved change: the file as it was
        before the last change saved through Qualm (up to KEEP of them, one
        per undo). A kept version that no longer loads is passed over, said
        so, and set aside (see _aside), not deleted; the file undo replaces is
        kept as backups/rules.toml.replaced. Returns what it undid, when the
        version it put back was saved, how many are left, and the diff."""
        versions = self._versions()
        kept = versions[::-1] or ([self._bak()] if self._bak().exists() else [])  # newest first
        cur = self.path.read_bytes() if self.path.exists() else b""
        recorded = self._saved().exists()
        edited = recorded and self._saved().read_bytes() != cur
        skipped, passed = [], []
        for src in ([self._saved()] if edited else []) + kept:
            problem = _unloadable(src)
            if problem is None:
                break
            skipped.append(problem)
            passed.append(src)
        else:
            if skipped:  # the checks are stricter than when it was saved
                raise KeyError(f"nothing to undo that loads: {'; '.join(skipped)}. Nothing was changed. To put it "
                               "back anyway, correct it in that file first, then undo again")
            raise KeyError(f"nothing to undo: no earlier version of rules.toml is kept (one per change saved "
                           f"through Qualm, the last {KEEP})")
        popped = kept[:kept.index(src) + 1] if src in kept else []
        old = src.read_bytes()
        aside = [self._aside(p) for p in passed]
        done = {"undid": "hand_edit" if src == self._saved() else "saved_change",
                "restored": datetime.fromtimestamp(src.stat().st_mtime).isoformat(timespec="seconds"),
                "left": len(kept) - len(popped),
                "diff": _unified(cur.decode("utf-8-sig", "replace"), old.decode("utf-8-sig"),
                                 f"{self.path} (before undo)", f"{self.path} (after)")}
        if skipped:
            done |= {"skipped": skipped, "kept_aside": [str(p) for p in aside]}
        if cur:
            done["replaced_file"] = str(self._replaced())
        if not recorded and cur and _unloadable(self.path) is not None:
            # A file from before Qualm kept its last save (it would have been recorded, had it loaded).
            done["note"] = ("Qualm kept no copy of the version it last saved (rules.toml is from before it kept "
                            "one), so the last change it saved is undone too. To keep that change, correct the "
                            f"mistake in {self._replaced()} and copy it over rules.toml")
        if self.dry_run:
            self.pending = old.decode("utf-8-sig")
            return done
        if cur:
            self._backups()
            _replace(self._replaced(), cur, like=self.path)
        for p, to in zip(passed, aside):
            os.replace(p, to)
        _replace(self.path.resolve(), old)
        self._remember(old)
        for p in popped:
            p.unlink(missing_ok=True)
        rest = kept[len(popped):]
        if rest and popped:
            _replace(self._bak(), rest[0].read_bytes(), like=rest[0])
        elif not rest:
            self._bak().unlink(missing_ok=True)
        return done

    def export(self) -> dict:
        """The whole config as data, as written (defaults left out): what `apply` takes back."""
        data = self._toml()
        return {"settings": data.get("settings", {}), "allow": data.get("allow", []), "rules": data.get("rules", [])}

    @_locked
    def apply(self, desired: dict, prune: bool = False) -> list[dict]:
        """Make the file match `desired` (the shape `export` returns). Only what
        it names changes: a top-level key, an entry, or a key inside settings
        or an entry that it leaves out is kept, and a key set to null goes
        back to its default. With `prune`, entries and keys left out are
        removed too. One batch: all of it, checked together, or nothing.
        Returns the changes."""
        if not isinstance(desired, dict):
            raise ValueError("expected a JSON object with settings, allow and/or rules, the shape `config export` prints")
        unknown = set(desired) - {"settings", "allow", "rules"}
        if unknown:
            raise ValueError(f"unknown top-level keys {sorted(unknown)}; expected settings, allow, rules")
        if not isinstance(desired.get("settings", {}), dict):
            raise ValueError('settings: an object of setting: value, like {"max_wait_s": 90}')
        for table in ("allow", "rules"):
            if not isinstance(desired.get(table, []), list) or not all(isinstance(e, dict) for e in desired.get(table, [])):
                raise ValueError(f'{table}: a list of entries, each an object with an "id"')
        current = self.export()
        changes = []
        with self.batch():
            if "settings" in desired:
                _check_settings(desired["settings"])
                set_, unset = _merge(desired["settings"], current["settings"], prune)
                if set_ or unset:
                    self.edit_settings(set_, unset)
                    changes.append({"op": "settings", "set": set_, "unset": unset})
            for table in ("allow", "rules"):
                if table not in desired:
                    continue
                want = {e.get("id"): e for e in desired[table]}
                if None in want or len(want) != len(desired[table]):
                    raise ValueError(f"{table}: every entry needs a unique id")
                have = {e["id"]: e for e in current[table]}
                for id in have:
                    if id not in want and prune:
                        self.remove(table, id)
                        changes.append({"op": "remove", "table": table, "id": id})
                for id, entry in want.items():
                    for key in entry:
                        field_type(TABLES[table], key)
                    if id not in have:
                        self.add(table, {k: v for k, v in entry.items() if v is not None})
                        changes.append({"op": "add", "table": table, "id": id})
                        continue
                    set_, unset = _merge(entry, have[id], prune)
                    if set_ or unset:
                        self.edit(table, id, set_, unset)
                        changes.append({"op": "change", "table": table, "id": id, "set": set_, "unset": unset})
        return changes

    def load(self) -> tuple[Settings, list[Rule]]:
        return parse_config(self.text(), self.path if self.pending is None and self._buffer is None else None)

    def _toml(self) -> dict:
        try:
            return tomllib.loads(self.text())
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{self.path} doesn't load: {e}. {correct_it(self.path)}") from None

    def raw(self, table: str, id: str) -> dict:
        """One entry as written in the file (no defaults filled in)."""
        for entry in self._toml().get(table, []):
            if entry.get("id") == id:
                return entry
        raise KeyError(self._missing(table, id))

    def _missing(self, table: str, id: str) -> str:
        ids = [e.get("id") for e in self._toml().get(table, [])]
        return f"no {'rule' if table == 'rules' else table} {id!r}; there are: {', '.join(map(str, ids)) or 'none'}"

    def _write(self, text: str) -> None:
        if self._buffer is not None:  # in a batch: checked at the end
            self._buffer = text
            return
        self._save(text)

    @_locked
    def _save(self, text: str) -> None:
        """Checked, then saved whole; a change that changes nothing writes
        nothing. A refused or failed save leaves the file and its saved
        versions as they were."""
        if blank(text):  # the last entry removed: an empty file wouldn't load, a [settings] line does
            text = (text.rstrip() + "\n\n" if text.strip() else "") + "[settings]\n"
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError as e:
            line = e.object[e.object.rfind("\n", 0, e.start) + 1:].split("\n", 1)[0]
            raise ValueError(f"can't save {line.strip().encode('utf-8', 'backslashreplace').decode()}: it has bytes "
                             "that aren't text (is the terminal set to UTF-8?). Nothing was changed.") from None
        try:
            settings, rules = parse_config(text)  # refuse to save a file that no longer loads
        except ValueError:
            if self.path.exists():
                parse_config(self._read(), self.path)  # broken before this change: say so, and where
            raise
        self._check_overrides(settings, rules)
        if self.dry_run:
            self.pending = text
            return
        old = self.path.read_bytes() if self.path.exists() else None
        if data == old:
            return
        target = self.path.resolve()  # a symlinked rules.toml stays a link
        pushed, tmp = [], None
        try:
            tmp = _staged(target, data)  # first, so a full disk stops it before anything changed
            if old is not None:
                pushed = self._push(old)
            os.replace(tmp, target)
        except OSError as e:
            for p in [*pushed[:1], *([Path(tmp)] if tmp else [])]:
                p.unlink(missing_ok=True)
            raise ValueError(f"couldn't save {self.path} ({e.strerror or e}); nothing was changed") from None
        self._remember(data)
        for p in pushed[KEEP:]:
            p.unlink(missing_ok=True)

    def _check_overrides(self, settings: Settings, rules: list[Rule]) -> None:
        """An overrides_allow naming no allow class loads (it's ignored), but a
        change doesn't make one: a mistyped class, or an allow class taken
        away while a rule names it. One the file had before is left alone."""
        try:
            old = parse_config(self._read()) if self.path.exists() else None
        except ValueError:
            old = None
        had = set(unknown_overrides(*old)) if old else set()
        classes = [c.id for c in settings.allow]
        for rid, cid in unknown_overrides(settings, rules):
            if (rid, cid) in had:
                continue
            if old and any(c.id == cid for c in old[0].allow):
                raise ConfigError(f"rule {rid!r} steps in on {cid} pages too (overrides_allow {cid}); take that out "
                                  f"first: `{_q()} rules set {rid} overrides_allow-={cid}`", "rules", rid, "overrides_allow")
            raise ConfigError(f"rule {rid!r}: overrides_allow {cid}: there's no allow class {cid!r} (there are: "
                              f"{', '.join(classes) or 'none'})", "rules", rid, "overrides_allow")

    @_locked
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
                blocks[i] = _edit_block(b, set_ or {}, list(unset), _defaults(cls))
                return self._write("".join(blocks))
        raise KeyError(self._missing(table, id))

    @_locked
    def add(self, table: str, entry: dict, text: str | None = None) -> None:
        """A new [[rules]] / [[allow]] entry at the end of its kind, or a block of `text` as is."""
        cls = TABLES[table]
        for key in entry:
            field_type(cls, key)
        data = self._toml()
        if any(e.get("id") == entry["id"] for t in TABLES for e in data.get(t, [])):
            raise ValueError(f"{entry['id']!r} already exists")
        block = text or f"[[{table}]]\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in entry.items())
        blocks = _blocks(self.text())
        last = max((i for i, b in enumerate(blocks) if _header(b) == table), default=len(blocks) - 1)
        if blocks and not blocks[last].endswith("\n\n"):
            blocks[last] = blocks[last].rstrip("\n") + "\n\n"
        blocks.insert(last + 1, block.rstrip("\n") + "\n" + ("\n" if last + 1 < len(blocks) else ""))
        self._write("".join(blocks))

    @_locked
    def remove(self, table: str, id: str) -> None:
        blocks = _blocks(self.text())
        keep = [b for b in blocks if not (_header(b) == table and _block_id(b) == id)]
        if len(keep) == len(blocks):
            raise KeyError(self._missing(table, id))
        self._write("".join(keep))

    @_locked
    def edit_settings(self, set_: dict | None = None, unset: list[str] = ()) -> None:
        _check_settings([*(set_ or {}), *unset])
        blocks = _blocks(self.text())
        i = next((i for i, b in enumerate(blocks) if _header(b) == "settings"), None)
        if i is None:
            blocks.insert(min(1, len(blocks)), "[settings]\n\n")
            i = min(1, len(blocks) - 1)
        blocks[i] = _edit_block(blocks[i], set_ or {}, list(unset), _defaults(Settings))
        if not blocks[i].endswith("\n\n"):
            blocks[i] += "\n"
        self._write("".join(blocks))

    @_locked
    def migrate(self) -> list[str]:
        """Daily budgets -> check-ins: time_cap rules become check_in, and
        minutes_per_day, visits_per_day and [settings] budgets go, with the
        comments right above them. Returns what changed; saved like any edit."""
        changed = []
        blocks = _blocks(self.text())
        for i, b in enumerate(blocks):
            data = tomllib.loads(b) if _header(b) in ("rules", "settings") else {}
            if _header(b) == "settings":
                if "budgets" in data.get("settings", {}):
                    blocks[i] = _drop_keys(b, ["budgets"])
                    changed.append("settings: budgets removed")
                continue
            entry = (data.get("rules") or [{}])[0]
            old = [k for k in ("minutes_per_day", "visits_per_day") if k in entry]
            if old or entry.get("kind") == "time_cap":
                blocks[i] = _edit_block(_drop_keys(b, old), {"kind": "check_in"}, [])
                changed.append(f"{entry['id']}: kind check_in" + "".join(f", {k} removed" for k in old))
        if changed:
            self._write("".join(blocks))
        return changed


def _drop_keys(block: str, keys: list[str]) -> str:
    """Remove keys and the comment lines right above each."""
    lines = block.splitlines(keepends=True)
    for key in keys:
        span = _key_span(lines, key)
        if span:
            start = span[0]
            while start and lines[start - 1].lstrip().startswith("#"):
                start -= 1
            del lines[start:span[1]]
    return "".join(lines)



def starter_blocks() -> dict[str, tuple[str, str]]:
    """The shipped rules and allow classes: id -> (table, block text, comments included)."""
    out = {}
    for b in _blocks(EXAMPLE.read_text(encoding="utf-8")):
        if _header(b) in TABLES and (id := _block_id(b)):
            out[id] = (_header(b), b)
    return out
