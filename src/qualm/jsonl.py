"""Reading and adding to the data logs (judgements, decisions, reviews,
exceptions, trials, reflections): one JSON object per line, appended to
while others read. A line cut short by a crash, a kill or a full disk is
skipped, with a note on stderr naming the file; the rest of the file still
counts, and the next record added starts a line of its own.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_told: set[str] = set()  # files already reported: a reload loop says it once


def lines(path: str | Path, where=None):
    """Each readable line of a JSONL file as a dict, oldest first; nothing if
    there's no file. `where(line)` skips lines before they're parsed."""
    path = Path(path)
    try:
        f = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    with f:
        for n, line in enumerate(f, 1):
            if not line.strip() or where and not where(line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                e = None
            if isinstance(e, dict):
                yield e
            elif str(path) not in _told:
                _told.add(str(path))
                print(f"skipped line {n} of {path}: it isn't a whole record (cut short by a crash?)", file=sys.stderr)


def read(path: str | Path) -> list[dict]:
    return list(lines(path))


def appending(path: str | Path):
    """The log opened to add lines at its end (made if it isn't there). If its
    last line was cut short, a newline goes first: otherwise the next record
    would join the torn line and be skipped with it."""
    path = Path(path)
    f = path.open("a", encoding="utf-8")
    try:
        if f.tell():
            with path.open("rb") as last:
                last.seek(-1, os.SEEK_END)
                if last.read(1) != b"\n":
                    f.write("\n")
    except BaseException:
        f.close()
        raise
    return f
