"""`qualm doctor`: everything Qualm needs, checked, with the fix for each.

Written for someone setting Qualm up for the first time (or an agent doing
it for them): each check says what it found and, if it's not right, the
one command or setting that fixes it.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import urllib.request
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"


def _sysctl(name: str) -> str:
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def checks(rules_path: str, data_dir: str, kev_dir: str) -> list[dict]:
    out = []

    def add(name, status, found, fix=""):
        out.append({"check": name, "status": status, "found": found, "fix": fix})

    mac = platform.mac_ver()[0]
    arm = platform.machine() == "arm64"
    add("Mac with Apple silicon", OK if mac and arm else FAIL, f"macOS {mac or '?'}, {platform.machine()}",
        "" if arm else "The local model runs on MLX, which needs Apple silicon. Hosted Jev: QUALM_BACKEND=jev.")
    gb = int(_sysctl("hw.memsize") or 0) / 2**30
    add("Memory", OK if gb >= 16 else WARN, f"{gb:.0f} GB",
        "" if gb >= 16 else "Kev-4B at 8 bits needs about 6 GB on top of your apps; 16 GB or more is comfortable.")
    swap = _sysctl("vm.swapusage")
    try:
        used = float(swap.split("used = ")[1].split("M")[0]) / 1024
    except (IndexError, ValueError):
        used = 0.0
    add("Swap", OK if used < 8 else WARN, f"{used:.1f} GB in use",
        "" if used < 8 else "Heavy swapping makes readings take seconds. Run the model at 8 bits (the default of `qualm serve`) "
        "and one model server at a time.")

    from ApplicationServices import AXIsProcessTrusted

    trusted = bool(AXIsProcessTrusted())
    add("Accessibility permission", OK if trusted else FAIL,
        "granted to this terminal" if trusted else "not granted: Qualm can only see app names",
        "" if trusted else "System Settings > Privacy & Security > Accessibility: add your terminal app (and, after "
        "`qualm install`, the Python it prints), then restart it.")

    from .rules import capacity, load_config

    try:
        settings, rules = load_config(rules_path)
        cap = capacity(settings, rules)
        add("Rules", OK, f"{rules_path}: {sum(r.enabled for r in rules)} rules on, {cap['peak']} of {cap['limit']} questions per reading")
    except FileNotFoundError:
        add("Rules", WARN, f"{rules_path} doesn't exist yet", "Any command creates it from the starters: `qualm rules list`.")
    except ValueError as e:
        add("Rules", FAIL, str(e)[:200], "`qualm config check` says what to fix; `qualm config undo` goes back one change.")

    kev = Path(kev_dir).expanduser()
    has_kev = (kev / "kev" / "serve.py").exists()
    add("Kev (the local model)", OK if has_kev else FAIL, str(kev) if has_kev else f"not found at {kev}",
        "" if has_kev else f"git clone https://github.com/jaredpalmer/kev {kev}")

    url = os.environ.get("KEV_URL", "http://127.0.0.1:8009")
    try:
        with urllib.request.urlopen(f"{url}/v1/models", timeout=3) as r:
            m = (json.loads(r.read()).get("models") or [{}])[0]
        dtype = m.get("dtype", "")
        quantized = dtype.startswith("uint")  # mlx reports packed quantized weights as uint32
        add("Model server", OK if quantized else WARN,
            f"{m.get('run', '?')} at {url}, {'quantized' if quantized else dtype or '?'}",
            "" if quantized else "Serving bf16 uses about twice the memory for the same answers: restart it with `qualm serve` (8-bit).")
    except Exception as e:
        add("Model server", FAIL, f"nothing answers at {url} ({type(e).__name__})",
            "`qualm serve` in another terminal, or `qualm install` to start it at login.")

    from .review import PORT

    add("Qualm app", OK if _listening(PORT) else WARN, "running" if _listening(PORT) else "not running",
        "" if _listening(PORT) else "`qualm app`, or `qualm install` to start it at login.")
    agents = Path.home() / "Library" / "LaunchAgents"
    installed = [p.name for p in (agents / "com.qualm.kev.plist", agents / "com.qualm.app.plist") if p.exists()]
    add("Start at login", OK if len(installed) == 2 else WARN, ", ".join(installed) or "not installed",
        "" if len(installed) == 2 else "`qualm install` (undo: `qualm uninstall`).")
    data = Path(data_dir)
    add("Your data", OK, f"{data.resolve()}: stays on this Mac, git-ignored" + (" (empty so far)" if not data.exists() else ""))
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
