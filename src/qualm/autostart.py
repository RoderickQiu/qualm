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
MOVE_FIRST = ("Qualm is running from its disk image (or from a temporary copy macOS made of it): move Qualm to "
              "Applications first and open it from there, so starting at login and the `qualm` command keep working.")


def temporary_bundle() -> bool:
    """Qualm.app opened straight from the DMG, or from a download macOS runs
    from a random read-only folder (App Translocation): gone after an eject
    or a restart, so a login item or `qualm` command there would break."""
    b = paths.bundle()
    return bool(b) and ("/AppTranslocation/" in str(b) or (str(b).startswith("/Volumes/") and not os.access(b, os.W_OK)))


def serve(kev_dir: Path | None = None, model: str = localmodel.MODEL, port: int = localmodel.PORT,
          bits: int = localmodel.BITS) -> None:
    """Run the model server in the foreground (Ctrl-C stops it); never a second one."""
    # Before anything installs or loads: a second model would take another 4.5 GB, only to find the port taken.
    if localmodel.starting():
        sys.exit("A model server is already running or getting ready for this Qualm folder (the app starts its own; "
                 "its menu bar item shows how far): nothing to do.")
    there = localmodel.probe(port, timeout=3) if localmodel.listening(port) else "none"
    if there == "model":
        sys.exit(f"A model server already answers at http://127.0.0.1:{port} (the app's, or another `qualm serve`): "
                 "nothing to do.")
    if there == "other":
        sys.exit(f"Port {port} is used by another app. Quit it, or run this on a free port (`--port {port + 1}`) and "
                 f"point Qualm at it: KEV_URL=http://127.0.0.1:{port + 1}.")
    if kev_dir is None and not localmodel.runtime_ready():
        localmodel.install_runtime()
    argv, env, cwd = localmodel.server_command(model, port, bits, kev_dir)
    first = bits != 16 and not (localmodel.saved_copy(bits) / "model.safetensors").exists()
    print(f"Kev server: {model}, {'bf16' if bits == 16 else f'{bits}-bit'}, http://127.0.0.1:{port}"
          + (f" (first start: downloads about {localmodel.DOWNLOAD_GB:.0f} GB, picked up where it stopped if cut off)"
             if first else ""), flush=True)
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
                                 "PYTHONUNBUFFERED": "1",
                                 # Asked for from a QUALM_HOME: the app it starts uses that folder too.
                                 **({"QUALM_HOME": str(paths.home())} if paths.custom_home() else {})},
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


def login_home() -> Path | None:
    """The Qualm folder the login item starts the app on; None without one."""
    path = AGENTS / f"{APP_LABEL}.plist"
    if not path.exists():
        return None
    try:
        env = plistlib.loads(path.read_bytes()).get("EnvironmentVariables", {})
    except Exception:
        env = {}
    return Path(env["QUALM_HOME"]).expanduser() if env.get("QUALM_HOME") else paths.default_home()


def up_to_date() -> bool:
    """The login item already starts this copy, just as install() would write it."""
    try:
        return plistlib.loads((AGENTS / f"{APP_LABEL}.plist").read_bytes()) == _plist(*app_command())
    except (Exception, SystemExit):  # none yet, unreadable, or no way to start this copy
        return False


def login_is_ours() -> bool:
    """The login item is this folder's: always for the main install, for a QUALM_HOME only if it was asked for there."""
    where = login_home()
    return where is not None and (not paths.custom_home() or where.resolve() == paths.home().resolve())


def install(quiet: bool = False) -> None:
    if paths.legacy():
        from .agent import command

        sys.exit(f"Your rules and data are still in this folder: `{command()} setup` copies them to "
                 f"{paths.home()} first, where the app started at login looks.")
    if temporary_bundle():
        raise RuntimeError(MOVE_FIRST)
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
SKILL = "Qualm's skill for Claude Code"  # agent.py: ~/.claude/skills/qualm
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
    """What `uninstall --all` removes: (what, where), where there's a path.
    For a QUALM_HOME, only that folder (and a login item asked for there):
    the rest belongs to the main install."""
    from .agent import skill_file, skill_state

    items: list[tuple[str, Path | None]] = []
    custom = paths.custom_home()
    for label in (APP_LABEL, *OLD_LABELS):
        if (p := AGENTS / f"{label}.plist").exists() and (not custom or (label == APP_LABEL and login_is_ours())):
            items.append(("the login item", p))
    if not custom and keychain.stored() is not None:
        items.append(("the TypeSafe API key in your keychain", None))
    if not custom and _ours(SHIM):
        items.append(("the `qualm` command", SHIM))
    if not custom and skill_state() in ("current", "old"):
        items.append((SKILL, skill_file()))
    if paths.home().exists():
        items.append(("your rules and data, the local model's runtime and its 8-bit weights", paths.home()))
    if not custom and paths.LOGS.exists():
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

    from .agent import remove_skill

    done = []
    for what, where in everything():
        if what == SKILL:
            remove_skill()  # and its folder, unless something of yours is in it
        elif what == "the login item":
            if "removed the login item" not in done:
                uninstall(quiet=True)  # every Qualm agent, old ones too
                done.append("removed the login item")
            continue
        elif where is None:
            keychain.forget()
        elif where.is_dir():
            shutil.rmtree(where)
        else:
            where.unlink()
        done.append(f"removed {what} ({where})" if where else f"removed {what}")
    return done


def on_path(folder: Path) -> bool:
    """`folder` is on this shell's PATH (a stock macOS shell has no ~/.local/bin)."""
    return any(Path(p).expanduser() == folder for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def path_line(folder: Path) -> str:
    """The line that puts `folder` on PATH in new terminals, for the user's shell."""
    rc = "~/.bash_profile" if os.environ.get("SHELL", "").endswith("bash") else "~/.zprofile"
    return f"echo 'export PATH=\"{str(folder).replace(str(Path.home()), '$HOME', 1)}:$PATH\"' >> {rc}"


def cli_shim() -> Path | None:
    """From Qualm.app: a `qualm` command in ~/.local/bin that runs the app's copy."""
    b = paths.bundle()
    if not b:
        return None
    if temporary_bundle():
        raise RuntimeError(MOVE_FIRST)
    target = SHIM
    if target.exists() and not _ours(target):
        return None  # someone else's qualm (a uv tool install): leave it
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f'#!/bin/sh\nexec "{b}/Contents/MacOS/Qualm" -m qualm "$@"\n', encoding="utf-8")
    target.chmod(0o755)
    return target
