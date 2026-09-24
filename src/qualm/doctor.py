"""`qualm doctor`: everything Qualm needs, checked, with the fix for each.

Written for someone setting Qualm up for the first time (or an agent doing
it for them): each check says what it found and, if it's not right, the
one command or setting that fixes it.
"""

from __future__ import annotations

import platform
from pathlib import Path
from urllib.parse import urlsplit

OK, WARN, FAIL = "ok", "warn", "fail"
SETUP, APP = "Setup", "Qualm app"  # the checks the summary line speaks for


def _permissions(app: dict | None, q: str) -> tuple[str, bool | None, bool | None, str]:
    """(whose permissions, Accessibility, Screen Recording, who to turn on) for the
    process that runs Qualm. macOS answers a terminal command for the
    terminal, not for Qualm.app, so Qualm.app's own are known only while it
    runs (its dashboard reports them); None: can't be checked from here."""
    from . import paths
    from .setup import terminal_name

    if app and "accessibility" in app:
        who = "Qualm" if app.get("bundle") else f"your terminal app (or {app.get('exe')}, for the login item)"
        return "the running Qualm", app["accessibility"], app.get("screen_recording"), who
    if paths.bundle():
        return "Qualm.app", None, None, "Qualm"
    try:
        from ApplicationServices import AXIsProcessTrusted
        from Quartz import CGPreflightScreenCaptureAccess  # only looks: never asks

        ax, shots = bool(AXIsProcessTrusted()), bool(CGPreflightScreenCaptureAccess())
    except ImportError:
        ax = shots = False
    term = terminal_name()
    return f"this terminal ({term}), which `{q} app` run here uses", ax, shots, term


def checks(rules_path: str, data_dir: str) -> list[dict]:
    from . import autostart, keychain, localmodel, paths, review
    from . import setup as s
    from .agent import command, start_hint
    from .decide import backend
    from .rules import capacity, load_config

    q, start = command(), start_hint()
    Start = start[0].upper() + start[1:]
    out = []

    def add(name, status, found, fix=""):
        out.append({"check": name, "status": status, "found": found, "fix": fix})

    if s.needed():
        add(SETUP, FAIL, "not set up yet: where the model runs, your rules and start at login aren't chosen",
            f"Open Qualm.app (the setup window shows), or run `{q} setup`." if paths.bundle() else f"Run `{q} setup`.")
    add("Where your rules and data live", WARN if paths.legacy() else OK,
        f"{Path(rules_path).resolve().parent}" + (" (this folder, from before)" if paths.legacy() else ""),
        f"`{q} setup` copies them to {paths.home()}, where the app looks" if paths.legacy() else "")

    settings = None
    try:
        settings, rules = load_config(rules_path)
        cap = capacity(settings, rules)
        add("Rules", OK, f"{rules_path}: {sum(r.enabled for r in rules)} rules on, {cap['peak']} of {cap['limit']} questions per reading")
    except FileNotFoundError:
        add("Rules", FAIL, f"{rules_path} doesn't exist yet", f"`{q} setup` (or open Qualm.app) picks your rules.")
    except ValueError as e:
        from .config import undo_to

        to = undo_to(rules_path)  # None: nothing kept to go back to
        add("Rules", FAIL, str(e)[:200],
            f"`{q} config check` says what to fix" + (f"; `{q} config undo` goes back to {to}." if to else "."))

    name = backend(settings)
    app = review.app_running(Path(data_dir), settings)
    mac = platform.mac_ver()[0]
    add("Model", OK, "hosted by TypeSafe (Jev): each new screen's text is sent there" if name == "jev"
        else "on this Mac (Kev-4B, 8-bit): nothing leaves it")
    if name == "jev":
        key = keychain.api_key()
        unsaved = bool(key) and keychain.unsaved()  # Qualm started from Finder or at login won't have it
        add("TypeSafe API key", FAIL if not key else WARN if unsaved else OK,
            "none" if not key else "only in TYPESAFE_API_KEY in this shell, not in your keychain" if unsaved else "found",
            f"`{q} setup --backend jev` asks for it (from console.typesafe.ai) and keeps it in your keychain." if not key
            else f"`{q} setup --backend jev` saves it in your keychain, where Qualm started from Finder or at login "
            "finds it." if unsaved else "")
    elif why := localmodel.unsupported():
        add("This Mac", FAIL, f"macOS {mac or '?'}, {platform.machine()}",
            f"{why} `{q} setup --backend jev` switches to the hosted one.")
    else:
        add("This Mac", OK, f"macOS {mac or '?'}, {platform.machine()}")
        m = localmodel.memory()
        add("Memory for the local model", OK if m.fits else WARN, m.summary(),
            "" if m.fits else f"Use the hosted model: `{q} setup --backend jev`." if m.too_small else
            f"Quit what you don't need, or use the hosted model: `{q} setup --backend jev`.")
        ready = localmodel.runtime_ready()
        add("Local model runtime", OK if ready else WARN, str(paths.kev_env()) if ready else "not installed yet",
            "" if ready else ("The app installs it on first start (~1 GB): its menu bar item shows how far." if app
                              else f"The app installs it on first start (~1 GB); or `{q} serve` now.")
            + (" It needs git, which this Mac hasn't got: `xcode-select --install` adds it."
               if localmodel.needs_git() and not localmodel.git_available() else ""))
        saved = (localmodel.saved_copy() / "model.safetensors").exists()
        short, so_far = localmodel.disk_short(), localmodel.downloaded() / 1e9
        add("Local model weights", WARN if short else OK, str(localmodel.saved_copy()) if saved else
            f"not downloaded yet ({so_far:.1f} of 4.5 GB so far)" if so_far else "not downloaded yet",
            f"Free up disk space: the model {short.split(': ', 1)[1]}." if short else
            "" if saved else "The app downloads them when it starts the model (about 5 GB, once).")
        url, info = localmodel.find_server()  # the same server `status` asks, and every model call
        if info is None:
            why = localmodel.why_down(url)  # the same reasons `status` gives
            busy, taken = why == "starting", why == "taken"  # the app's server downloading or loading; another app
            add("Model server", WARN, "getting the model ready" if busy else
                f"port {urlsplit(url).port or 80} is used by another app" if taken else f"nothing answers at {url}",
                "The app is downloading or loading the model: its menu bar item shows how far." if busy else
                "The app runs the model on the next free port instead, and terminal commands find it there."
                if taken else "KEV_URL points at another machine: check it and the network (Qualm starts no model "
                "server there)." if why == "remote" else "The app is starting it; its menu says how far it got." if app else
                f"The app starts it ({start}); in a terminal: `{q} serve`.")
        elif info.get("busy"):
            add("Model server", OK, f"up at {url}, busy answering (on a Mac short of memory that can take seconds)")
        else:
            quantized = str(info.get("dtype", "")).startswith("uint")  # mlx reports packed quantized weights as uint32
            add("Model server", OK if quantized else WARN, f"{info.get('run', '?')}, {'8-bit' if quantized else info.get('dtype', '?')}",
                "" if quantized else f"Serving bf16 takes twice the memory for the same answers: restart it with `{q} serve`.")

    whose, ax, shots, who = _permissions(app, q)
    pane = "System Settings > Privacy & Security"
    unknown = f"not checked: {whose}'s own permission shows only while it runs"
    if ax is None:
        add("Accessibility permission", WARN, unknown + " (its menu says if it's missing)",
            f"{pane} > Accessibility: turn on {who}. `{q} setup --permission` opens it.")
    else:
        add("Accessibility permission", OK if ax else FAIL,
            f"granted to {whose}" if ax else f"not granted to {whose}: Qualm can only see app names",
            "" if ax else f"{pane} > Accessibility: turn on {who}. `{q} setup --permission` opens it.")
    # Screenshots for the dashboard, and reading windows that show no text (OCR).
    shots_fix = f"{pane} > Screen & System Audio Recording: turn on {who} (screenshots on the dashboard; windows that show no text)."
    if shots is None:
        add("Screen Recording permission", WARN, unknown, shots_fix)
    else:
        add("Screen Recording permission", OK if shots else WARN,
            f"granted to {whose}" if shots else f"not granted to {whose}: no screenshots on the dashboard, and windows "
            "that show no text can't be read", "" if shots else shots_fix)

    add(APP, OK if app else WARN,
        ("running" + (" (an older copy: quit and reopen it to update)" if app.get("older") else "")) if app else "not running",
        "" if app else f"{Start}.")
    add("Start at login", OK if autostart.installed() else WARN, "on" if autostart.installed() else "off",
        "" if autostart.installed() else f"`{q} install` (undo: `{q} uninstall`).")
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
    row = {r["check"]: r for r in results}
    lines.append("")
    if SETUP in row:
        lines.append(f"Not set up yet. {row[SETUP]['fix']}")
    elif bad:
        lines.append(f"{len(bad)} to fix before Qualm can work.")
    elif APP in row and row[APP]["status"] != OK:
        lines.append(f"Nothing is watching yet. {row[APP]['fix']}")
    else:
        lines.append("All set." if all(r["status"] == OK for r in results) else "Working; the notes above would make it better.")
    return "\n".join(lines)
