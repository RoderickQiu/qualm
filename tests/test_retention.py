"""Old screens are forgotten; what you taught Qualm isn't; no line is lost on the way."""

import json
import os
from datetime import datetime, timedelta

import pytest

from qualm import retention
from qualm.rules import Settings

NOW = datetime(2026, 12, 31, 12, 0, 0)


def write(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def ago(days):
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


def test_old_judgements_go_but_reviewed_ones_and_recent_ones_stay(tmp_path):
    write(tmp_path / "judgements.jsonl", [{"id": "old", "at": ago(100)}, {"id": "seen", "at": ago(200)},
                                          {"id": "new", "at": ago(10)}])
    write(tmp_path / "reviews.jsonl", [{"id": "seen", "verdict": "right"}])
    out = retention.prune(tmp_path, Settings(keep_days=90), now=NOW)
    assert out["judgements"] == 1
    assert [e["id"] for e in read(tmp_path / "judgements.jsonl")] == ["seen", "new"]
    assert read(tmp_path / "reviews.jsonl") == [{"id": "seen", "verdict": "right"}]  # never touched
    assert not (tmp_path / "judgements.jsonl.prune").exists()


def test_decisions_keep_the_window_and_today(tmp_path):
    write(tmp_path / "decisions.jsonl", [{"type": "intervention", "at": ago(120)},
                                         {"type": "focus", "at": ago(3)},
                                         {"type": "session", "event": "start", "at": NOW.isoformat()}])
    retention.prune(tmp_path, Settings(keep_days=90), now=NOW)
    assert [e["type"] for e in read(tmp_path / "decisions.jsonl")] == ["focus", "session"]


def test_nothing_old_leaves_the_file_as_it_was(tmp_path):
    path = tmp_path / "judgements.jsonl"
    write(path, [{"id": "a", "at": ago(1)}])
    before = path.stat().st_mtime_ns
    assert retention.prune(tmp_path, Settings(), now=NOW) == {"judgements": 0, "decisions": 0, "shots": 0}
    assert path.stat().st_mtime_ns == before


def test_zero_keeps_everything(tmp_path):
    write(tmp_path / "judgements.jsonl", [{"id": "a", "at": ago(5000)}])
    shots = tmp_path / "shots"
    shots.mkdir()
    old = shots / f"{int((NOW - timedelta(days=5000)).timestamp() * 1000)}.jpg"
    old.write_bytes(b"x")
    assert retention.prune(tmp_path, Settings(keep_days=0, keep_shots_days=0), now=NOW)["judgements"] == 0
    assert old.exists() and len(read(tmp_path / "judgements.jsonl")) == 1


def test_shots_go_by_their_own_age(tmp_path):
    shots = tmp_path / "shots"
    shots.mkdir()
    ms = lambda d: int((NOW - timedelta(days=d)).timestamp() * 1000)
    (shots / f"{ms(40)}.jpg").write_bytes(b"old")
    (shots / f"{ms(5)}.jpg").write_bytes(b"new")
    odd = shots / "notes.jpg"  # not named by time: its mtime decides
    odd.write_bytes(b"?")
    t = (NOW - timedelta(days=60)).timestamp()
    os.utime(odd, (t, t))
    (shots / "readme.txt").write_text("kept: not a screenshot")
    out = retention.prune(tmp_path, Settings(keep_shots_days=30), now=NOW)
    assert out["shots"] == 2
    assert sorted(p.name for p in shots.iterdir()) == sorted([f"{ms(5)}.jpg", "readme.txt"])


def test_lines_appended_while_pruning_are_kept(tmp_path, monkeypatch):
    path = tmp_path / "judgements.jsonl"
    write(path, [{"id": "old", "at": ago(100)}, {"id": "new", "at": ago(1)}])
    with path.open("a", encoding="utf-8") as f:
        f.write('{"id": "half", "at": "' + NOW.isoformat() + '"')  # the app is mid-write as we start
    real = os.fsync

    def fsync(fd):  # after the first pass, before the swap: the app finishes that line and logs another
        with path.open("a", encoding="utf-8") as f:
            f.write("}\n" + json.dumps({"id": "late", "at": NOW.isoformat()}) + "\n")
        real(fd)

    monkeypatch.setattr(retention.os, "fsync", fsync)
    assert retention.prune(tmp_path, Settings(keep_days=90), now=NOW)["judgements"] == 1
    assert [e["id"] for e in read(path)] == ["new", "half", "late"]


def test_settings_are_checked():
    from qualm.rules import parse_config

    text = '[settings]\nkeep_days = 30\nkeep_shots_days = 0\n'
    settings, _ = parse_config(text)
    assert (settings.keep_days, settings.keep_shots_days) == (30, 0)
    with pytest.raises(ValueError, match="keep_days"):
        parse_config('[settings]\nkeep_days = -1\n')
