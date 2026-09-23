"""Start Qualm at login: two LaunchAgents, the Kev model server and the app.

    qualm serve        the model server, in this terminal
    qualm install      write and load them (both start now, and at every login)
    qualm uninstall    stop and remove them

The server is Kev-4B quantized to 8 bits by default: on the 119 trial pages
it scores like bf16 (the same AUC on every rule, the same page kind on all
119, scores within 0.04), in about half the memory (6 GB, not 11). On a 24
GB Mac the bf16 server swapped, and swap, not the model, made the slow
readings (p95 8.5 s in real use). 4 bits is smaller still but moved social
recall from 0.95 to 0.85, so it's not offered. HANDOFF.md, Measured.

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


KEV_REPO = "https://github.com/jaredpalmer/kev"
DEFAULT_MODEL, DEFAULT_PORT, DEFAULT_BITS = "jaredpalmer/kev-4b", 8009, 8


def server_command(repo: Path, kev_dir: Path, model: str = DEFAULT_MODEL, port: int = DEFAULT_PORT,
                   bits: int = DEFAULT_BITS) -> tuple[list[str], dict[str, str]]:
    """The Kev server as argv and environment, run from `kev_dir`: MLX's buffer
    cache capped (experiments/serve_capped.py), weights quantized to `bits` (16: none)."""
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv not found on PATH: https://docs.astral.sh/uv/")
    if not (kev_dir / "kev" / "serve.py").exists():
        sys.exit(f"{kev_dir} doesn't look like the Kev repo (no kev/serve.py). Get it with:\n"
                 f"  git clone {KEV_REPO} {kev_dir}\nor pass --kev-dir")
    if not (repo / "experiments" / "serve_capped.py").exists():
        sys.exit(f"{repo}/experiments/serve_capped.py not found: run Qualm from its git checkout")
    env = {"KEV_DTYPE": "bf16"}
    if bits in (4, 8):
        env["KEV_QUANT_BITS"] = str(bits)
    argv = [uv, "run", "--extra", "serve", "python", str(repo / "experiments" / "serve_capped.py"),
            "--run", model, "--port", str(port)]
    return argv, env


def serve(repo: Path, kev_dir: Path, model: str = DEFAULT_MODEL, port: int = DEFAULT_PORT, bits: int = DEFAULT_BITS) -> None:
    """Run the model server in the foreground (Ctrl-C stops it)."""
    argv, env = server_command(repo, kev_dir, model, port, bits)
    print(f"Kev server: {model}, {'bf16' if bits == 16 else f'{bits}-bit'}, http://127.0.0.1:{port} "
          "(first start downloads the model; loading takes a minute or more)", flush=True)
    os.chdir(kev_dir)
    inherited = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}  # Qualm's venv, not Kev's
    os.execvpe(argv[0], argv, {**inherited, **env})


def install(repo: Path, kev_dir: Path, model: str = DEFAULT_MODEL, port: int = DEFAULT_PORT, bits: int = DEFAULT_BITS) -> None:
    argv, env = server_command(repo, kev_dir, model, port, bits)
    if not (repo / "rules.toml").exists():
        sys.exit(f"{repo}/rules.toml not found. Copy rules.example.toml first.")
    AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    kev = _plist(KEV_LABEL, argv, kev_dir, env)
    app = _plist(APP_LABEL, [argv[0], "run", "qualm", "app"], repo,
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
