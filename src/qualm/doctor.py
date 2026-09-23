"""`qualm doctor`: everything Qualm needs, checked, with the fix for each.

Written for someone setting Qualm up for the first time (or an agent doing
it for them): each check says what it found and, if it's not right, the
one command or setting that fixes it.
"""

from __future__ import annotations

import platform
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"


def checks(rules_path: str, data_dir: str) -> list[dict]:
    from . import autostart, keychain, localmodel, paths
    from .decide import backend
    from .rules import capacity, load_config

    out = []

    def add(name, status, found, fix=""):
        out.append({"check": name, "status": status, "found": found, "fix": fix})

    add("Where your rules and data live", WARN if paths.legacy() else OK,
        f"{Path(rules_path).resolve().parent}" + (" (this folder, from before)" if paths.legacy() else ""),
        f"`qualm setup` copies them to {paths.home()}, where the app looks" if paths.legacy() else "")

    settings = None
    try:
        settings, rules = load_config(rules_path)
        cap = capacity(settings, rules)
        add("Rules", OK, f"{rules_path}: {sum(r.enabled for r in rules)} rules on, {cap['peak']} of {cap['limit']} questions per reading")
    except FileNotFoundError:
        add("Rules", FAIL, f"{rules_path} doesn't exist yet", "`qualm setup` (or open Qualm.app) picks your rules.")
    except ValueError as e:
        add("Rules", FAIL, str(e)[:200], "`qualm config check` says what to fix; `qualm config undo` goes back one change.")

    name = backend(settings)
    mac = platform.mac_ver()[0]
    add("Model", OK, "hosted by TypeSafe (Jev): each new screen's text is sent there" if name == "jev"
        else "on this Mac (Kev-4B, 8-bit): nothing leaves it")
    if name == "jev":
        key = keychain.api_key()
        add("TypeSafe API key", OK if key else FAIL, "found" if key else "none",
            "" if key else "`qualm setup --backend jev` asks for it and keeps it in your keychain.")
    else:
        add("Apple silicon", OK if localmodel.apple_silicon() else FAIL, f"macOS {mac or '?'}, {platform.machine()}",
            "" if localmodel.apple_silicon() else "The local model needs Apple silicon: `qualm setup --backend jev` for the hosted one.")
        m = localmodel.memory()
        add("Memory for the local model", OK if m.fits else WARN, m.summary(),
            "" if m.fits else "Quit what you don't need, or use the hosted model: `qualm setup --backend jev`.")
        ready = localmodel.runtime_ready()
        add("Local model runtime", OK if ready else WARN, str(paths.kev_env()) if ready else "not installed yet",
            "" if ready else "The app installs it on first start (~1 GB); or `qualm serve` now.")
        info = localmodel.answering()
        if info is None:
            add("Model server", WARN, f"nothing answers on :{localmodel.PORT}",
                "The app starts it; in a terminal: `qualm serve`.")
        else:
            quantized = str(info.get("dtype", "")).startswith("uint")  # mlx reports packed quantized weights as uint32
            add("Model server", OK if quantized else WARN, f"{info.get('run', '?')}, {'8-bit' if quantized else info.get('dtype', '?')}",
                "" if quantized else "Serving bf16 takes twice the memory for the same answers: restart it with `qualm serve`.")

    try:
        from ApplicationServices import AXIsProcessTrusted

        trusted = bool(AXIsProcessTrusted())
    except ImportError:
        trusted = False
    who = "Qualm" if paths.bundle() else "this terminal"
    add("Accessibility permission", OK if trusted else FAIL,
        f"granted to {who}" if trusted else "not granted: Qualm can only see app names",
        "" if trusted else "`qualm setup --permission` opens System Settings > Privacy & Security > Accessibility: "
        f"turn on {who}.")

    from .review import PORT

    add("Qualm app", OK if localmodel.listening(PORT) else WARN, "running" if localmodel.listening(PORT) else "not running",
        "" if localmodel.listening(PORT) else "Open Qualm.app, or `qualm app`.")
    add("Start at login", OK if autostart.installed() else WARN, "on" if autostart.installed() else "off",
        "" if autostart.installed() else "`qualm install` (undo: `qualm uninstall`).")
    data = Path(data_dir)
    add("Your data", OK, f"{data.resolve()}: stays on this Mac" + (" (empty so far)" if not data.exists() else ""))
    return out


def report(results: list[dict]) -> str:
    mark = {OK: "✓", WARN: "!", FAIL: "✗"}
    lines = []
    for r in results:
        lines.append(f"{mark[r['status']]} {r['check']}: {r['found']}")
        if r["fix"]:
            lines.append(f"    {r['fix']}")
    bad = [r for r in results if r["status"] == FAIL]
    lines.append("")
    lines.append("All set." if not bad and all(r["status"] == OK for r in results) else
                 f"{len(bad)} to fix before Qualm can work." if bad else "Working; the notes above would make it better.")
    return "\n".join(lines)
