"""Start Qualm at login: two LaunchAgents, the Kev model server and the app.

    qualm install      write and load them (both start now, and at every login)
    qualm uninstall    stop and remove them

launchd restarts either one if it crashes. Logs go to ~/Library/Logs/Qualm/.

Permissions: started by launchd, the app is no longer a child of your
terminal, so macOS asks for Accessibility (and Screen Recording, for the
review screenshots) for the Python binary itself. `install` prints which.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

AGENTS = Path.home() / "Library" / "LaunchAgents"
LOGS = Path.home() / "Library" / "Logs" / "Qualm"
KEV_LABEL, APP_LABEL = "com.qualm.kev", "com.qualm.app"


def _plist(label: str, args: list[str], cwd: Path, env: dict[str, str]) -> dict:
    return {
        "Label": label,
        "ProgramArguments": args,
        "WorkingDirectory": str(cwd),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:" + str(Path(args[0]).parent), **env},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after Quit
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOGS / f"{label}.log"),
        "StandardErrorPath": str(LOGS / f"{label}.log"),
    }


def install(repo: Path, kev_dir: Path, model: str = "jaredpalmer/kev-4b", port: int = 8009) -> None:
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv not found on PATH")
    if not (kev_dir / "kev" / "serve.py").exists():
        sys.exit(f"{kev_dir} doesn't look like the Kev repo (no kev/serve.py); pass --kev-dir")
    if not (repo / "rules.toml").exists():
        sys.exit(f"{repo}/rules.toml not found. Copy rules.example.toml first.")
    AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    kev = _plist(KEV_LABEL, [uv, "run", "--extra", "serve", "python", str(repo / "experiments" / "serve_capped.py"),
                             "--run", model, "--port", str(port)], kev_dir, {"KEV_DTYPE": "bf16"})
    app = _plist(APP_LABEL, [uv, "run", "qualm", "app"], repo,
                 {"KEV_URL": f"http://127.0.0.1:{port}", "PYTHONUNBUFFERED": "1"})
    for label, plist in ((KEV_LABEL, kev), (APP_LABEL, app)):
        path = AGENTS / f"{label}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
        path.write_bytes(plistlib.dumps(plist))
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, text=True)
        print(f"{'loaded' if r.returncode == 0 else 'FAILED to load'}: {path}" + (f"\n  {r.stderr.strip()}" if r.returncode else ""))
    print(f"logs: {LOGS}/")
    print("If a terminal copy of the app or the model server is running, quit it: both would compete for the same port.")
    print("macOS will ask for Accessibility (and Screen Recording, for screenshots) for this Python:")
    print(f"  {Path(sys.executable).resolve()}")
    print("Grant them in System Settings > Privacy & Security, then: qualm install  (again, to restart).")


def uninstall() -> None:
    for label in (APP_LABEL, KEV_LABEL):
        path = AGENTS / f"{label}.plist"
        if path.exists():
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
            path.unlink()
            print(f"removed: {path}")
        else:
            print(f"not installed: {label}")
