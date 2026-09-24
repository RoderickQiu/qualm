"""The TypeSafe API key, in the login keychain (service "Qualm").

`security` is fed on stdin (`security -i`), so the key never shows in a
process list. Lookup order: the environment, a checkout's .env (for
development), then the keychain.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SERVICE, ACCOUNT = "Qualm", "TYPESAFE_API_KEY"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"  # a checkout's, git-ignored


def load_env(path: Path = ENV_FILE) -> None:
    """KEY=value lines from .env into the environment; what's already set wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def stored() -> str | None:
    r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def api_key() -> str | None:
    load_env()
    return os.environ.get(ACCOUNT) or stored()


def unsaved() -> bool:
    """The key is only in this shell's TYPESAFE_API_KEY: neither the keychain
    nor a checkout's .env has it, so Qualm started from Finder or at login finds none."""
    if not os.environ.get(ACCOUNT) or stored() is not None:
        return False
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    return not any(line.partition("=")[0].strip() == ACCOUNT for line in lines)


def forget() -> bool:
    """Remove the key from the keychain; False if there was none."""
    if stored() is None:
        return False
    subprocess.run(["security", "-i"], input=f"delete-generic-password -s {SERVICE} -a {ACCOUNT}\n",
                   capture_output=True, text=True)
    if stored() is not None:
        raise RuntimeError("couldn't remove the key from the keychain")
    return True


def migrate_env_key() -> bool:
    """A key only in a checkout's .env (or the environment) goes into the keychain,
    where Qualm.app, which reads neither, finds it. True if one was stored."""
    load_env()
    key = os.environ.get(ACCOUNT)
    if not key or stored() is not None:
        return False
    store(key)
    return True


def store(key: str) -> None:
    key = key.strip()
    if not key or any(c in key for c in " \"'\\\n"):
        raise ValueError("that doesn't look like an API key")
    r = subprocess.run(["security", "-i"], input=f'add-generic-password -U -s {SERVICE} -a {ACCOUNT} -w "{key}"\n',
                       capture_output=True, text=True)
    if r.returncode != 0 or stored() != key:
        raise RuntimeError(f"couldn't save the key in the keychain: {(r.stderr or r.stdout).strip()[:200]}")
