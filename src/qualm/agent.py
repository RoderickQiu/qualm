"""The prompt the menu bar hands to your AI agent (Claude Code, Codex, ...).

Rules are sentences a small model reads, and wording that reads right can
make it worse: an exception naming WeChat, WhatsApp and iMessage pushed
WhatsApp's score on the social rule from ~0.17 to ~0.50. An agent that runs
`qualm` scores each change on your own screens before saving it, which a
text editor can't. `qualm guide` tells it how.
"""

from __future__ import annotations

import shlex
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import autostart, jsonl, paths

RECENT_H = 24  # pop-ups you said were wrong in this many hours go into the prompt
RECENT_N = 3
SAID = {"fine": "not this one", "never": "never here"}


def command() -> str:
    """How a terminal on this Mac runs qualm, in a form a stock macOS shell
    can run: plain `qualm` only when it's on PATH (a stock shell has no
    ~/.local/bin), else the full path of the `qualm` command Qualm.app added,
    Qualm.app itself, or this checkout through uv. On another Qualm folder
    (QUALM_HOME), with it in front: without it, a command changes the main one."""
    home = f"QUALM_HOME={shlex.quote(str(paths.home()))} " if paths.custom_home() else ""
    return home + _program()


def _program() -> str:
    if b := paths.bundle():
        shim = autostart.SHIM
        if autostart._ours(shim):
            found = shutil.which("qualm")
            return "qualm" if found and Path(found).resolve() == shim.resolve() else shlex.quote(str(shim))
        return f"{shlex.quote(str(b / 'Contents' / 'MacOS' / 'Qualm'))} -m qualm"
    repo = Path(__file__).resolve().parents[2]
    return f"uv run --project {shlex.quote(str(repo))} qualm" if (repo / "pyproject.toml").exists() else "qualm"


def start_hint() -> str:
    """How to start the app: Qualm.app, or the command from a checkout."""
    return f"open Qualm.app, or run `{command()} app`" if paths.bundle() else f"run `{command()} app`"


def wrong_popups(data_dir: Path, now: datetime | None = None) -> list[str]:
    """The latest pop-ups you answered "Not this one" or "Never here", newest first, one line each."""
    since = ((now or datetime.now()) - timedelta(hours=RECENT_H)).isoformat(timespec="seconds")
    shown, out = {}, []
    for e in jsonl.lines(Path(data_dir) / "decisions.jsonl"):
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
        f"Run `{cmd} guide` first and follow it" + ("" if cmd == "qualm" else f", writing `{cmd}` wherever it says `qualm`")
        + ". Change things only through that command, and score each change on my "
        "recent screens before saving it (`rules test`, `allow test`); don't edit rules.toml by hand. "
        "Tell me the numbers before and after. If there's nothing to score it on yet, do what the guide "
        "says for a first day, and tell me what is untested.",
        "Scoring prints the titles and addresses of my recent screens, and `except list` those of pages I let "
        "through, and they reach you and your provider: look at no more of them than the job needs, and quote "
        "back only the rows you need.",
        "Answer me in the language I write in.",
    ]
    if wrong := wrong_popups(data_dir):
        lines += ["", "Pop-ups I said were wrong lately:", *wrong]
    lines += ["", "What I want: "]
    return "\n".join(lines)
