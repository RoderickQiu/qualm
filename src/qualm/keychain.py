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


def store(key: str) -> None:
    key = key.strip()
    if not key or any(c in key for c in " \"'\\\n"):
        raise ValueError("that doesn't look like an API key")
    r = subprocess.run(["security", "-i"], input=f'add-generic-password -U -s {SERVICE} -a {ACCOUNT} -w "{key}"\n',
                       capture_output=True, text=True)
    if r.returncode != 0 or stored() != key:
        raise RuntimeError(f"couldn't save the key in the keychain: {(r.stderr or r.stdout).strip()[:200]}")
