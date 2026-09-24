"""The prompt the menu bar hands to your AI agent (Claude Code, Codex, ...).

Rules are sentences a small model reads, and wording that reads right can
make it worse: an exception naming WeChat, WhatsApp and iMessage pushed
WhatsApp's score on the social rule from ~0.17 to ~0.50. An agent that runs
`qualm` scores each change on your own screens before saving it, which a
text editor can't. `qualm guide` tells it how.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from . import autostart, paths

RECENT_H = 24  # pop-ups you said were wrong in this many hours go into the prompt
RECENT_N = 3
SAID = {"fine": "not this one", "never": "never here"}


def command() -> str:
    """How a terminal on this Mac runs qualm."""
    if b := paths.bundle():
        return "qualm" if autostart._ours(autostart.SHIM) else f'"{b}/Contents/MacOS/Qualm" -m qualm'
    repo = Path(__file__).resolve().parents[2]
    return f"uv run --project {repo} qualm" if (repo / "pyproject.toml").exists() else "qualm"


def wrong_popups(data_dir: Path, now: datetime | None = None) -> list[str]:
    """The latest pop-ups you answered "Not this one" or "Never here", newest first, one line each."""
    path = Path(data_dir) / "decisions.jsonl"
    if not path.exists():
        return []
    since = ((now or datetime.now()) - timedelta(hours=RECENT_H)).isoformat(timespec="seconds")
    shown, out = {}, []
    for line in path.open(encoding="utf-8"):
        if not line.strip():
            continue
        e = json.loads(line)
        if e.get("at", "") < since:
            continue
        if e.get("type") == "intervention":
            shown[e.get("id")] = e
        elif e.get("type") == "response" and e.get("response") in SAID and e.get("id") in shown:
            d, s = shown[e["id"]], shown[e["id"]].get("screen", {})
            where = f'{s.get("app", "").strip(chr(0x200e))}, window "{s.get("window_title", "")}"'
            if s.get("url"):
                where += f" <{s['url']}>"
            out.append(f"- {d['at'][11:16]} {d.get('rule', '')} popped up in {where}; "
                       f"I said {SAID[e['response']]} (decision {e['id']})")
    return out[::-1][:RECENT_N]


def prompt(data_dir: Path) -> str:
    cmd = command()
    lines = [
        "Help me adjust Qualm, the app on my Mac that steps in when I drift into feeds, short videos and the like.",
        f"Run `{cmd} guide` first and follow it. Change things only through that command, and score each change on my "
        "recent screens before saving it (`rules test`, `allow test`); don't edit rules.toml by hand. "
        "Tell me the numbers before and after.",
    ]
    if wrong := wrong_popups(data_dir):
        lines += ["", "Pop-ups I said were wrong lately:", *wrong]
    lines += ["", "What I want: "]
    return "\n".join(lines)
