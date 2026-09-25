"""What your AI agent (Claude Code, Codex, ...) gets from Qualm: the menu
bar's prompt, and a Claude Code skill.

Rules are sentences a small model reads, and wording that reads right can
make it worse: an exception naming WeChat, WhatsApp and iMessage pushed
WhatsApp's score on the social rule from ~0.17 to ~0.50. An agent that runs
`qualm` scores each change on your own screens before saving it, which a
text editor can't. `qualm guide` tells it how.

The skill is that guide in Claude Code's own folder
(~/.claude/skills/qualm/SKILL.md), so Claude Code knows Qualm in any
session, prompt or not: setup adds it the first time when Claude Code is on
this Mac, the menu and `qualm skill` add or remove it, and the app rewrites
it when Qualm updates. A skill there that Qualm didn't write is left alone.
"""

from __future__ import annotations

import os
import shlex
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import autostart, jsonl, paths

RECENT_H = 24  # pop-ups you said were wrong in this many hours go into the prompt
RECENT_N = 3
SAID = {"fine": "not this one", "never": "never here"}
# What Claude Code reads to decide when the skill applies (YAML: no ": " in it).
SKILL_DESCRIPTION = (
    "Qualm is the menu bar app on this Mac that steps in when the user drifts into feeds, short videos and "
    "the like. Use this skill to set up, change or check its rules - what it blocks or time-limits, when, "
    "exceptions, apps and sites it leaves alone, settings. Use it when the user asks to block, limit, allow, "
    "pause (now, by schedule or for a weekend) or stop flagging something, asks why Qualm popped up or missed "
    "something, how their week went or what their rules are, or says Qualm isn't working (`qualm doctor`, "
    "its logs). Drives the `qualm` CLI; never edits rules.toml by hand.")
SKILL_MARK = "Written by Qualm"  # the line that makes a SKILL.md Qualm's own, to rewrite or remove


def command() -> str:
    """How a terminal on this Mac runs qualm, in a form a stock macOS shell
    can run: plain `qualm` only when it's on PATH (a stock shell has no
    ~/.local/bin), else the full path of the `qualm` command Qualm.app added,
    Qualm.app itself, or this checkout through uv. On another Qualm folder
    (QUALM_HOME), with it in front: without it, a command changes the main one."""
    home = f"QUALM_HOME={shlex.quote(str(paths.home()))} " if paths.custom_home() else ""
    return home + _program()


def _program(full: bool = False) -> str:
    """With `full`, the `qualm` command Qualm.app added by its full path even when it's on this PATH."""
    if b := paths.bundle():
        shim = autostart.SHIM
        if autostart._ours(shim):
            found = shutil.which("qualm")
            on_path = found and Path(found).resolve() == shim.resolve()
            return "qualm" if on_path and not full else shlex.quote(str(shim))
        return f"{shlex.quote(str(b / 'Contents' / 'MacOS' / 'Qualm'))} -m qualm"
    repo = Path(__file__).resolve().parents[2]
    return f"uv run --project {shlex.quote(str(repo))} qualm" if (repo / "pyproject.toml").exists() else "qualm"


def claude_dir() -> Path:
    """Claude Code's folder for this user: CLAUDE_CONFIG_DIR, else ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def claude_code() -> bool:
    """Claude Code has run for this user: its folder is there."""
    return claude_dir().is_dir()


def skill_file() -> Path:
    return claude_dir() / "skills" / "qualm" / "SKILL.md"


def skill_text() -> str:
    """The skill: its description, how to run qualm here, then the guide."""
    from .personalize import guide
    from .updates import version

    cmd = _program(full=True)  # the shell Claude Code runs commands in may not have ~/.local/bin on PATH
    how = "" if cmd == "qualm" else f" Here the command is `{cmd}`: write it wherever the guide below says `qualm`."
    return (f"---\nname: qualm\ndescription: {SKILL_DESCRIPTION}\n---\n\n"
            f"{SKILL_MARK} {version()} for this Mac, and rewritten when Qualm updates; `{cmd} skill remove` "
            f"removes it.{how}\n\n" + guide())


def skill_state() -> str:
    """"none", "current" (Qualm's, as this copy writes it), "old" (Qualm's, from
    another version or copy) or "other" (a skill of that name Qualm didn't write)."""
    try:
        text = skill_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return "none"
    except (OSError, UnicodeDecodeError):
        return "other"
    if SKILL_MARK not in text.split("\n---\n", 1)[-1].split("\n# ", 1)[0]:  # the lines before the guide
        return "other"
    return "current" if text == skill_text() else "old"


def install_skill() -> Path | None:
    """Write the skill, or bring it up to date. None, and nothing written, when
    another skill has the name or this is another Qualm folder (QUALM_HOME:
    Claude Code's skill is the main install's, like the `qualm` command)."""
    if paths.custom_home() or (state := skill_state()) == "other":
        return None
    path = skill_file()
    if state != "current":
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(".SKILL.md.part")
        part.write_text(skill_text(), encoding="utf-8")
        part.replace(path)  # whole or not at all: Claude Code may read it any time
    return path


def remove_skill() -> bool:
    """Take Qualm's skill away (and its folder, if nothing else is in it). False if there's none of Qualm's."""
    if paths.custom_home() or skill_state() not in ("current", "old"):
        return False
    path = skill_file()
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:  # something of the user's in it: kept
        pass
    return True


def refresh_skill() -> bool:
    """Qualm's skill, if there is one, rewritten for this version (the app does this when it starts)."""
    try:
        return skill_state() == "old" and install_skill() is not None
    except OSError:
        return False


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
