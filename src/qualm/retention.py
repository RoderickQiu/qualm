"""Forget old screens: the logs keep what you read for a while, not forever.

    judgements.jsonl   every judgement (what was on screen): kept keep_days,
                       except ones you reviewed, which the review page shows
    decisions.jsonl    pop-ups, answers, focus and check-in sessions: kept
                       keep_days, and always today's (sessions are rebuilt
                       from it on restart)
    shots/             a screenshot per new screen, the heaviest part: kept
                       keep_shots_days, even for judgements that are kept

What you taught Qualm is never pruned: reviews, exceptions, "never here",
rule trials, labels, daily usage. `[settings] keep_days = 0` (or
keep_shots_days) keeps everything.

`prune` is cheap when there's nothing to do (it reads one line per log and
lists shots/), so the app calls it at start and once a day. A log is
rewritten through a temp file and os.replace, and whatever the app appended
meanwhile is carried over, so no line is lost.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path


def _first_at(path: Path) -> str | None:
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    return json.loads(line).get("at")
                except ValueError:
                    return None
    return None


def _rewrite(path: Path, keep) -> int:
    """Keep the lines `keep(event)` says to; returns how many were dropped."""
    if not path.exists():
        return 0
    size = path.stat().st_size
    tmp = path.with_name(path.name + ".prune")
    dropped, pos = 0, 0
    with path.open("rb") as src, tmp.open("wb") as out:
        for raw in iter(src.readline, b""):
            if pos + len(raw) > size or not raw.endswith(b"\n"):
                break  # being appended as we read: copied whole below
            pos += len(raw)
            try:
                ok = keep(json.loads(raw))
            except ValueError:
                ok = True  # a line we can't read isn't ours to throw away
            if ok:
                out.write(raw)
            else:
                dropped += 1
        if dropped:
            src.seek(pos)  # everything from here on is newer: keep it as it is
            out.write(src.read())
            end = src.tell()
            out.flush()
            os.fsync(out.fileno())
    if not dropped:
        tmp.unlink()
        return 0
    with path.open("rb") as src, tmp.open("ab") as out:  # what the app wrote in the meantime
        src.seek(end)
        out.write(src.read())
    os.replace(tmp, path)
    return dropped


def prune(data_dir: str | Path, settings, now: datetime | None = None) -> dict:
    """Drop what's older than the settings keep. Returns {"judgements", "decisions", "shots"}: how many went."""
    data_dir = Path(data_dir)
    now = now or datetime.now()
    out = {"judgements": 0, "decisions": 0, "shots": 0}

    if settings.keep_days:
        cutoff = (now - timedelta(days=settings.keep_days)).isoformat(timespec="seconds")
        today = now.date().isoformat()

        judgements = data_dir / "judgements.jsonl"
        if judgements.exists() and (first := _first_at(judgements)) and first < cutoff:
            reviewed = set()
            if (rv := data_dir / "reviews.jsonl").exists():
                for line in rv.open(encoding="utf-8"):
                    try:
                        reviewed.add(json.loads(line).get("id"))
                    except ValueError:
                        pass
            out["judgements"] = _rewrite(judgements, lambda e: e.get("at", "") >= cutoff or e.get("id") in reviewed)

        decisions = data_dir / "decisions.jsonl"
        if decisions.exists() and (first := _first_at(decisions)) and first < cutoff:
            out["decisions"] = _rewrite(decisions, lambda e: e.get("at", "") >= cutoff or e.get("at", "")[:10] >= today)

    shots = data_dir / "shots"
    if settings.keep_shots_days and shots.is_dir():
        oldest = (now - timedelta(days=settings.keep_shots_days)).timestamp()
        for entry in os.scandir(shots):
            if not entry.name.endswith(".jpg"):
                continue
            stem = entry.name[:-4]
            taken = int(stem) / 1000 if stem.isdigit() else entry.stat().st_mtime  # named by ms since epoch
            if taken < oldest:
                try:
                    os.unlink(entry.path)
                    out["shots"] += 1
                except OSError:
                    pass
    return out
