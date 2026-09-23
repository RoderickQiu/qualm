"""`qualm serve`, and starting Qualm at login.

    qualm serve        the local model server, in this terminal (the app starts its own otherwise)
    qualm install      start the app at every login (a LaunchAgent); it starts the model server itself
    qualm uninstall    stop and remove it

The server is Kev-4B quantized to 8 bits: on the 119 trial pages it scores
like bf16 (the same AUC on every rule, the same page kind on all 119, scores
within 0.04), in about half the memory. 4 bits moved social recall from 0.95
to 0.85, so it's not offered. HANDOFF.md, Measured; localmodel.py.

launchd restarts the app if it crashes. Logs go to ~/Library/Logs/Qualm/.
Run from Qualm.app, the agent starts the app itself, so macOS asks for
Accessibility for "Qualm"; from a checkout, it runs `uv run qualm app`, and
macOS asks for the Python binary instead.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import keychain, localmodel, paths

AGENTS = Path.home() / "Library" / "LaunchAgents"
APP_LABEL = "com.qualm.app"
OLD_LABELS = ("com.qualm.kev",)  # the model server's own agent, before the app started it


def serve(kev_dir: Path | None = None, model: str = localmodel.MODEL, port: int = localmodel.PORT,
          bits: int = localmodel.BITS) -> None:
    """Run the model server in the foreground (Ctrl-C stops it)."""
    if kev_dir is None and not localmodel.runtime_ready():
        localmodel.install_runtime()
    argv, env, cwd = localmodel.server_command(model, port, bits, kev_dir)
    first = not any(paths.models_dir().glob(f"*q{bits}g*/model.safetensors"))
    print(f"Kev server: {model}, {'bf16' if bits == 16 else f'{bits}-bit'}, http://127.0.0.1:{port}"
          + (f" (first start: downloads ~{localmodel.DOWNLOAD_GB:.0f} GB and saves the {bits}-bit weights; a few minutes)"
             if first and bits != 16 else ""), flush=True)
    os.chdir(cwd)
    inherited = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}  # Qualm's venv, not Kev's
    os.execvpe(argv[0], argv, {**inherited, **env})


def app_command() -> tuple[list[str], Path]:
    """How launchd starts the app: Qualm.app's own executable, else this checkout through uv."""
    if b := paths.bundle():
        return [str(b / "Contents" / "MacOS" / "Qualm")], paths.home()
    uv = paths.uv()
    if not uv:
        sys.exit("uv not found on PATH: https://docs.astral.sh/uv/")
    repo = Path(__file__).resolve().parents[2]
    return [uv, "run", "--project", str(repo), "qualm", "app"], paths.home()


def _plist(args: list[str], cwd: Path) -> dict:
    return {
        "Label": APP_LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": str(cwd),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:" + str(Path(args[0]).parent),
                                 "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after Quit
        "ProcessType": "Interactive",
        "StandardOutPath": str(paths.LOGS / f"{APP_LABEL}.log"),
        "StandardErrorPath": str(paths.LOGS / f"{APP_LABEL}.log"),
    }


def _bootout(path: Path) -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)


def installed() -> bool:
    return (AGENTS / f"{APP_LABEL}.plist").exists()


def install(quiet: bool = False) -> None:
    if paths.legacy():
        sys.exit("Your rules and data are still in this folder: `qualm setup` copies them to "
                 f"{paths.home()} first, where the app started at login looks.")
    argv, cwd = app_command()
    AGENTS.mkdir(parents=True, exist_ok=True)
    paths.LOGS.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    for label in OLD_LABELS:
        if (old := AGENTS / f"{label}.plist").exists():
            _bootout(old)
            old.unlink()
    path = AGENTS / f"{APP_LABEL}.plist"
    _bootout(path)
    path.write_bytes(plistlib.dumps(_plist(argv, cwd)))
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl couldn't load {path}: {r.stderr.strip()}")
    if quiet:
        return
    print(f"loaded: {path}\nlogs: {paths.LOGS}/")
    print("If a terminal copy of the app is running, quit it: two copies would both step in.")
    if not paths.bundle():
        print("macOS will ask for Accessibility (and Screen Recording, for screenshots) for this Python:")
        print(f"  {Path(sys.executable).resolve()}")


def uninstall(quiet: bool = False) -> None:
    for label in (APP_LABEL, *OLD_LABELS):
        path = AGENTS / f"{label}.plist"
        if path.exists():
            _bootout(path)
            path.unlink()
            quiet or print(f"removed: {path}")
        elif label == APP_LABEL and not quiet:
            print(f"not installed: {label}")


SHIM = Path.home() / ".local" / "bin" / "qualm"
HF_HUB = Path.home() / ".cache" / "huggingface" / "hub"
HF_REPOS = {"models--jaredpalmer--kev-4b": "model/jaredpalmer/kev-4b",
            "models--Qwen--Qwen3.5-4B-Base": "model/Qwen/Qwen3.5-4B-Base"}


def _ours(shim: Path) -> bool:
    return shim.is_file() and "Qualm.app" in shim.read_text(encoding="utf-8", errors="ignore")


def _size(path: Path) -> int:
    """Bytes of the files under `path`, each counted once: the HF cache links a
    repo's files into a shared store (hub/blobs), so links are followed."""
    seen, total = set(), 0
    for f in path.rglob("*"):
        real = f.resolve()
        if real not in seen and real.is_file():
            seen.add(real)
            total += real.stat().st_size
    return total


def everything() -> list[tuple[str, Path | None]]:
    """What `uninstall --all` removes: (what, where), where there's a path."""
    items: list[tuple[str, Path | None]] = []
    for label in (APP_LABEL, *OLD_LABELS):
        if (p := AGENTS / f"{label}.plist").exists():
            items.append(("the login item", p))
    if keychain.stored() is not None:
        items.append(("the TypeSafe API key in your keychain", None))
    if _ours(SHIM):
        items.append(("the `qualm` command", SHIM))
    if paths.home().exists():
        items.append(("your rules and data, the local model's runtime and its 8-bit weights", paths.home()))
    if paths.LOGS.exists():
        items.append(("the logs", paths.LOGS))
    return items


def left_behind() -> list[tuple[str, int]]:
    """Downloads other tools may share, so `uninstall --all` leaves them: (repo id, bytes)."""
    return [(repo, _size(HF_HUB / d)) for d, repo in HF_REPOS.items() if (HF_HUB / d).exists()]


def forget_downloads_command(repos: list[str]) -> str:
    """How to remove them: Hugging Face's own tool frees the shared store too (deleting the folder doesn't)."""
    return f"{paths.uv() or 'uv'} tool run --from huggingface_hub hf cache rm {' '.join(repos)}"


def uninstall_all() -> list[str]:
    """Everything `everything()` lists, removed. Returns what was done, in words."""
    import shutil

    done = []
    for what, where in everything():
        if what == "the login item":
            if "removed the login item" not in done:
                uninstall(quiet=True)  # every Qualm agent, old ones too
                done.append("removed the login item")
            continue
        if where is None:
            keychain.forget()
        elif where.is_dir():
            shutil.rmtree(where)
        else:
            where.unlink()
        done.append(f"removed {what} ({where})" if where else f"removed {what}")
    return done


def cli_shim() -> Path | None:
    """From Qualm.app: a `qualm` command in ~/.local/bin that runs the app's copy."""
    b = paths.bundle()
    if not b:
        return None
    target = SHIM
    if target.exists() and not _ours(target):
        return None  # someone else's qualm (a uv tool install): leave it
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f'#!/bin/sh\nexec "{b}/Contents/MacOS/Qualm" -m qualm "$@"\n', encoding="utf-8")
    target.chmod(0o755)
    return target
