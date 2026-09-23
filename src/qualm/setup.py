"""First run: where the model runs, the permission, the rules, start at login.

The same steps back `qualm setup` in a terminal and the app's setup window
(onboard.py), so both leave the same state behind:

    ~/Library/Application Support/Qualm/rules.toml   the rules you picked, and [settings] backend
    the login keychain                               the TypeSafe key, for the hosted model
    ~/Library/LaunchAgents/com.qualm.app.plist       start at login, if you asked

A checkout that kept rules.toml and data/ in its own folder is copied over
first (`migrate`), so nothing you taught Qualm is lost. Nothing is deleted.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import autostart, keychain, localmodel, paths
from .config import EXAMPLE, Config
from .rules import load_config

ACCESSIBILITY_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


@dataclass
class Choices:
    backend: str  # "kev" or "jev"
    rules: list[str]  # starter rule ids to turn on; the rest are kept, switched off
    login: bool = True
    key: str | None = None  # a new TypeSafe key, for "jev"
    shim: bool = True  # from Qualm.app: a `qualm` command for the terminal
    done: list[str] = field(default_factory=list)


def needed() -> bool:
    """No rules yet where the app looks: a first run."""
    return not paths.rules_file().exists()


def migrate(src: Path = Path(".")) -> list[str]:
    """A checkout's rules.toml (and .bak) and data/ copied home, if home has none yet."""
    home = paths.home()
    if (home / "rules.toml").exists() or not (src / "rules.toml").exists():
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
    """("kev" or "jev", why), from this Mac: Apple silicon, and memory to spare now."""
    if not localmodel.apple_silicon():
        return "jev", "The local model needs Apple silicon (M1 or later); this Mac can use the hosted model."
    m = localmodel.memory()
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
    """Everything chosen, in one go. Returns what was done, in words."""
    if c.backend not in ("kev", "jev"):
        raise ValueError("backend: kev or jev")
    if c.backend == "jev" and not (c.key or keychain.api_key()):
        raise ValueError("the hosted model needs a TypeSafe API key")
    if c.key:
        keychain.store(c.key)
        c.done.append("saved the TypeSafe key in your keychain")
    if paths.legacy():
        raise ValueError("this folder still has its own rules.toml: `qualm setup` copies it home first")
    path = paths.home() / "rules.toml"  # setup only ever writes the per-user copy
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE, path)
        c.done.append(f"created {path} from the starter rules")
    cfg = Config(path)
    _, rules = cfg.load()
    starter_ids = {s["id"] for s in starters()}
    with cfg.batch():
        cfg.edit_settings({"backend": "jev"} if c.backend == "jev" else {}, [] if c.backend == "jev" else ["backend"])
        for r in rules:
            if r.id in starter_ids:
                on = r.id in c.rules
                cfg.edit("rules", r.id, {} if on else {"enabled": False}, ["enabled"] if on else [])
    c.done.append(f"the model runs {'hosted by TypeSafe (Jev)' if c.backend == 'jev' else 'on this Mac (Kev-4B, 8-bit)'}")
    c.done.append("rules on: " + (", ".join(c.rules) or "none"))
    if c.login:
        autostart.install(quiet=True)
        c.done.append("starts at login")
    elif autostart.installed():
        autostart.uninstall(quiet=True)
        c.done.append("won't start at login")
    if c.shim and (shim := autostart.cli_shim()):
        c.done.append(f"`qualm` in the terminal runs this app's copy ({shim})")
    return c.done
