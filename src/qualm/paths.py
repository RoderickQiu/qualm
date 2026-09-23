"""Where Qualm keeps things: one folder per user, not the checkout.

    ~/Library/Application Support/Qualm/   (QUALM_HOME overrides)
        rules.toml, rules.toml.bak    your rules
        data/                         judgements, answers, sessions: what you read on screen
        kev-env/                      the local model's runtime (PyTorch, MLX, Kev), installed on demand
        models/                       Kev's 8-bit weights, saved once so later starts skip the bf16 load
    ~/Library/Logs/Qualm/             the app's and the model server's logs

A checkout from before this folder existed kept rules.toml and data/ in the
working directory. Until `qualm setup` copies them here, a rules.toml in the
current directory still wins, so nothing changes under a running copy.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

LOGS = Path.home() / "Library" / "Logs" / "Qualm"


def home() -> Path:
    return Path(os.environ.get("QUALM_HOME") or Path.home() / "Library" / "Application Support" / "Qualm").expanduser()


def legacy() -> bool:
    """A checkout's rules.toml in the current directory, not yet copied home."""
    return not (home() / "rules.toml").exists() and Path("rules.toml").exists()


def rules_file() -> Path:
    return Path("rules.toml") if legacy() else home() / "rules.toml"


def data_dir() -> Path:
    return Path("data") if legacy() else home() / "data"


def kev_env() -> Path:
    return home() / "kev-env"


def models_dir() -> Path:
    return home() / "models"


def bundle() -> Path | None:
    """Qualm.app when running from it: the executable is <App>/Contents/MacOS/Qualm."""
    exe = Path(sys.executable).resolve()
    app = exe.parents[2] if len(exe.parents) > 2 else None
    return app if app and app.suffix == ".app" and exe.parent.name == "MacOS" else None


def uv() -> str | None:
    """uv, to install the local model's runtime: the app's own copy, else the one on PATH."""
    if (b := bundle()) and (p := b / "Contents" / "Resources" / "bin" / "uv").exists():
        return str(p)
    return shutil.which("uv") or next((str(p) for p in (Path.home() / ".local/bin/uv", Path("/opt/homebrew/bin/uv"))
                                       if p.exists()), None)
