"""First run: where the model runs, the permission, the rules, start at login.

The same steps back `qualm setup` in a terminal and the app's setup window
(onboard.py), so both leave the same state behind:

    ~/Library/Application Support/Qualm/rules.toml   the rules you picked, and [settings] backend
    ~/Library/Application Support/Qualm/.setup-done  setup ran (the app's setup window looks for it)
    the login keychain                               the TypeSafe key, for the hosted model
    ~/Library/LaunchAgents/com.qualm.app.plist       start at login, if you asked
    ~/.claude/skills/qualm/SKILL.md                  Claude Code's skill for Qualm, if Claude Code is here and you asked

A checkout that kept rules.toml and data/ in its own folder is copied over
first (`migrate`), so nothing you taught Qualm is lost. Nothing is deleted.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import autostart, keychain, localmodel, paths
from .config import EXAMPLE, Config, starter_blocks
from .rules import load_config

DONE = paths.SETUP_DONE

# The starter rules, as a person would name them (the setup window, the menu);
# the sentence the model reads stays in rules.toml.
RULE_LOOK = {
    "shortvideo": ("Short videos", "play.rectangle.on.rectangle"),
    "feeds": ("Recommendation feeds", "square.grid.2x2"),
    "livestream": ("Livestreams", "dot.radiowaves.left.and.right"),
    "videos": ("Entertainment videos", "tv"),
    "social": ("Social media", "bubble.left.and.bubble.right"),
}


def rule_name(rule_id: str, description: str = "") -> str:
    """A rule as a person would name it: a starter's name, or your rule's id,
    "Late news". An id is plain lowercase letters, so a rule described in
    other letters is named by its description's first words ("刷短视频", not
    the "Duanshipin" typed for its id)."""
    if rule_id in RULE_LOOK:
        return RULE_LOOK[rule_id][0]
    head = re.split(r",? such as |[,:;，：；、(（]", description, maxsplit=1)[0].strip().rstrip(".。!！")
    if any(c.isalpha() and not c.isascii() for c in head):
        from .explain import cut

        return cut(head[:1].upper() + head[1:], 28)
    return rule_id.replace("_", " ").capitalize()


ACCESSIBILITY_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


@dataclass
class Choices:
    backend: str  # "kev" or "jev"
    rules: list[str]  # starter rule ids to turn on; the rest are kept, switched off
    login: bool | None = True  # None: leave start at login as it is
    key: str | None = None  # a new TypeSafe key, for "jev"
    shim: bool = True  # from Qualm.app: a `qualm` command for the terminal
    skill: bool = False  # Claude Code's skill (agent.py), if Claude Code has run here: the first setup asks for it
    done: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what was left alone, and what to do next
    started: bool = False  # the login item was loaded just now, so the app is starting


def needed() -> bool:
    """A first run: setup never ran for this folder, and nothing shows it was
    set up before setup left its mark: judgements (an app or `watch` ran), a
    model chosen, or a rules.toml an earlier version saved (it kept only
    rules.toml.bak; now backups/ comes with it, and DONE is written when
    backups/ is first made next to such a .bak: config.Config._backups). A
    rules.toml alone doesn't count: the first `qualm status` or `rules list`
    creates one from the starter rules. Nor does decisions.jsonl: `qualm
    focus` writes it."""
    if paths.legacy():
        return False
    home = paths.home()
    earlier = all((home / f).exists() for f in ("rules.toml", "rules.toml.bak")) and not (home / "backups").exists()
    if (home / DONE).exists() or (home / "data" / "judgements.jsonl").exists() or earlier:
        return False
    try:
        return "backend" not in tomllib.loads((home / "rules.toml").read_text(encoding="utf-8")).get("settings", {})
    except FileNotFoundError:
        return True
    except (OSError, ValueError):  # a rules.toml that doesn't load: not a first run; loading it says what's wrong
        return False


def terminal_name() -> str:
    """The terminal app this runs in, as System Settings lists it."""
    t = os.environ.get("TERM_PROGRAM", "")
    return {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "vscode": "Visual Studio Code or Cursor",
            "WarpTerminal": "Warp", "ghostty": "Ghostty"}.get(t, t or "your terminal app")


def migrate(src: Path = Path(".")) -> list[str]:
    """A checkout's rules.toml (and .bak) and data/ copied home, if home has none yet."""
    home = paths.home()
    if (home / "rules.toml").exists() or not (src / "rules.toml").exists() or not paths.checkout(src):
        return []
    home.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("rules.toml", "rules.toml.bak"):
        if (src / name).exists():
            shutil.copy2(src / name, home / name)
            copied.append(name)
    if (src / "data").is_dir() and not (home / "data").exists():
        shutil.copytree(src / "data", home / "data")
        copied.append("data/")
    return copied


def starters() -> list[dict]:
    """The starter rules: id, the sentence, and whether they're on by default."""
    _, rules = load_config(EXAMPLE)
    return [{"id": r.id, "what": r.description_en or r.description, "on": r.enabled,
             "kind": "asks what for first" if r.kind == "check_in" else "steps in"} for r in rules]


def recommend() -> tuple[str, str]:
    """("kev" or "jev", why), from this Mac: Apple silicon and macOS 14, memory to spare now, and disk space."""
    if why := localmodel.unsupported():
        return "jev", f"{why} This Mac can use the hosted model."
    m = localmodel.memory()
    if m.fits and (short := localmodel.disk_short()):
        return "jev", f"{m.summary()} But there's {short}."
    return ("kev" if m.fits else "jev"), m.summary()


def accessibility() -> bool:
    from ApplicationServices import AXIsProcessTrusted

    return bool(AXIsProcessTrusted())


def ask_accessibility() -> None:
    """macOS's own prompt (it adds Qualm to the list), and the settings pane."""
    from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

    AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    subprocess.run(["open", ACCESSIBILITY_PANE], check=False)


def check_key(key: str) -> str | None:
    """One small question to Jev: None if the key works, else what went wrong."""
    from typesafe_sdk import Noul, TypeSafeClient

    try:
        TypeSafeClient(api_key=key.strip(), model="jev-1.13.0", timeout=20).system_one(
            state={"window_title": "Qualm setup"}, questions={"ok": Noul(instructions="Is this a test?")})
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:200]


def apply(c: Choices, say=print) -> list[str]:
    """Everything chosen, in one go. Returns what was done, in words; c.notes
    says what was left alone and why. Everything is checked before anything
    is written."""
    from .agent import claude_code, command, install_skill, skill_file, start_hint

    starter_rules = [id for id, (table, _) in starter_blocks().items() if table == "rules"]
    if c.backend not in ("kev", "jev"):
        raise ValueError("backend: kev or jev")
    if c.backend == "kev" and (why := localmodel.unsupported()):  # not Apple silicon, or before macOS 14
        raise ValueError(f"{why[0].lower()}{why[1:].rstrip('.')}. Pick jev, the hosted model")
    if c.backend == "jev" and not (c.key or keychain.api_key()):
        raise ValueError("the hosted model needs a TypeSafe API key")
    if unknown := [r for r in c.rules if r not in starter_rules]:
        raise ValueError(f"not a starter rule: {', '.join(unknown)}. The starter rules are: {', '.join(starter_rules)}")
    if paths.legacy():
        raise ValueError(f"this folder still has its own rules.toml: `{command()} setup` copies it home first")
    custom = paths.custom_home()  # QUALM_HOME: the login item and the `qualm` command are the main install's
    shim = c.shim and bool(paths.bundle()) and not custom
    if (c.login or shim) and autostart.temporary_bundle():
        raise ValueError(autostart.MOVE_FIRST)
    if c.login and custom and autostart.installed() and not autostart.login_is_ours():
        raise ValueError(f"the login item starts Qualm on {autostart.login_home()}, not on this QUALM_HOME. Leave start "
                         f"at login off to keep it; `{command()} install`, run with this QUALM_HOME, replaces it (and "
                         "quits the Qualm it started)")
    if c.key:
        keychain.store(c.key)
        c.done.append("saved the TypeSafe key in your keychain")
    path = paths.home() / "rules.toml"  # setup only ever writes the per-user copy
    cfg = Config(path)
    if cfg.create():  # kept as the version Qualm last saved, for `config undo` after a hand edit
        c.done.append(f"created {path} from the starter rules")
    with cfg.batch():
        cfg.edit_settings({"backend": "jev"} if c.backend == "jev" else {}, [] if c.backend == "jev" else ["backend"])
        have = {r.id for r in cfg.load()[1]}
        for rid in c.rules:  # a starter removed from the file comes back when asked for
            if rid not in have:
                cfg.add("rules", {"id": rid}, text=starter_blocks()[rid][1])
                c.done.append(f"added {rid} back from the starter rules")
        for r in cfg.load()[1]:
            if r.id in starter_rules:
                on = r.id in c.rules
                cfg.edit("rules", r.id, {} if on else {"enabled": False}, ["enabled"] if on else [])
    _, rules = cfg.load()
    c.done.append(f"the model runs {'hosted by TypeSafe (Jev)' if c.backend == 'jev' else 'on this Mac (Kev-4B, 8-bit)'}")
    c.done.append("rules on: " + (", ".join(r.id for r in rules if r.enabled) or "none"))
    if c.login and autostart.up_to_date():  # loading it again would restart the running app
        c.done.append("starts at login, as before")
    elif c.login:
        autostart.install(quiet=True)
        c.started = True
        c.done.append("starts at login" + (f" (on {paths.home()}, the QUALM_HOME folder)" if custom else ""))
    elif c.login is False and autostart.installed():
        if autostart.login_is_ours():
            say(f"Removing the login item. If it started the Qualm that's running, that copy quits now: "
                f"{start_hint()} to start it again.")
            autostart.uninstall(quiet=True)
            c.done.append("won't start at login")
        else:
            c.notes.append(f"the login item starts Qualm on {autostart.login_home()}, not on this QUALM_HOME: left alone")
    elif c.login is None and custom:
        c.notes.append("start at login is the main install's and was left alone (QUALM_HOME is set); --login points it here")
    if c.shim and custom and paths.bundle():
        c.notes.append("the `qualm` command is the main install's and was left alone (QUALM_HOME is set)")
    elif shim and (made := autostart.cli_shim()):
        c.done.append(f"`qualm` in the terminal runs this app's copy ({made})")
        if not autostart.on_path(made.parent):
            c.notes.append(f"{made.parent} isn't on your PATH, so a new terminal won't find `qualm` yet. Add it with\n"
                           f"    {autostart.path_line(made.parent)}\n  and open a new terminal; until then, run {made}")
    if c.skill and not custom and claude_code():
        try:
            if made := install_skill():
                c.done.append(f"Claude Code knows Qualm: its skill is in {made.parent}")
            else:
                c.notes.append(f"{skill_file()} is a skill Qualm didn't write: left alone")
        except OSError as e:
            c.notes.append(f"couldn't give Claude Code Qualm's skill ({e.strerror or e}): `{command()} skill install` "
                           "tries again")
    (paths.home() / DONE).write_text(datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8")
    return c.done
