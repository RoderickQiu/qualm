"""The local model on this Mac: Kev-4B at 8 bits, served by kevserve.py.

    runtime    ~/Library/Application Support/Qualm/kev-env: Python with Kev,
               PyTorch and MLX (~1 GB), installed with uv on first use
    weights    Kev-4B's adapter and its Qwen3.5-4B base from Hugging Face
               (~9 GB, once), then an 8-bit copy in models/ (4.2 GB), so
               later starts never load the 9 GB bf16 weights
    server     started and stopped by the app (ManagedServer), or in a
               terminal with `qualm serve`; http://127.0.0.1:8009

Memory, measured (HANDOFF, Measured): the server holds 6.1-7.1 GB while it
answers (the weights 4.2 GB, the rest MLX's buffers, capped at 1 GB, and
the Python around it). What matters is whether your Mac has that to spare
next to what you run, not its total: `memory()` says both.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import paths

KEV_REPO = "https://github.com/jaredpalmer/kev"
KEV_COMMIT = "08ab0b87d27cb5577a3b371ad7ed4e4686b0502b"  # tested with Qualm; bump deliberately
KEV_SPEC = f"kev[serve] @ git+{KEV_REPO}@{KEV_COMMIT}"
MODEL, PORT, BITS = "jaredpalmer/kev-4b", 8009, 8
NEED_GB = (6.1, 7.1)  # the 8-bit server while answering, measured on the trial pages
HEADROOM_GB = 2.0  # so opening a browser tab doesn't push it into swap
DOWNLOAD_GB, DISK_GB = 9.0, 15.0  # first start: weights to fetch; everything on disk after


def apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


# -- memory -------------------------------------------------------------------

@dataclass
class Memory:
    total_gb: float
    in_use_gb: float  # what apps and the system hold now, in RAM or swapped out, less a Kev server's own share
    swap_gb: float  # of which swapped out
    kev_gb: float  # a Kev server already running, if any

    @property
    def spare_gb(self) -> float:
        return max(0.0, self.total_gb - self.in_use_gb)

    @property
    def fits(self) -> bool:
        return self.spare_gb >= NEED_GB[1] + HEADROOM_GB

    def summary(self) -> str:
        need = f"{NEED_GB[0]:.0f}-{NEED_GB[1]:.0f} GB"
        swap = f" ({self.swap_gb:.0f} GB of it swapped out)" if self.swap_gb >= 1 else ""
        verdict = ("There's room for it." if self.fits else
                   "Tight: it would push other apps into swap, and readings would slow to seconds." if self.spare_gb >= NEED_GB[0]
                   else "Not enough: it would swap.")
        return (f"The local model uses {need} while it runs. This Mac has {self.total_gb:.0f} GB; "
                f"what's open now uses about {self.in_use_gb:.0f} GB{swap}, leaving about {self.spare_gb:.0f} GB. {verdict}")


def _sysctl(name: str) -> str:
    r = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True)
    return r.stdout.strip()


def _footprint_gb(pid: int) -> float:
    r = subprocess.run(["footprint", str(pid)], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "Footprint:" in line:
            n, unit = line.split("Footprint:")[1].split()[:2]
            return float(n) / (1024 if unit.startswith("MB") else 1 if unit.startswith("GB") else 1024 ** 2)
    return 0.0


def memory() -> Memory:
    total = int(_sysctl("hw.memsize") or 0) / 2**30
    page = int(_sysctl("hw.pagesize") or 16384)
    vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    pages = {k.strip(): int(v.strip().rstrip(".")) for k, v in
             (line.split(":", 1) for line in vm.splitlines()[1:] if ":" in line) if v.strip().rstrip(".").isdigit()}
    used = sum(pages.get(k, 0) for k in ("Pages wired down", "Pages active", "Pages occupied by compressor")) * page / 2**30
    try:
        swap = float(_sysctl("vm.swapusage").split("used = ")[1].split("M")[0]) / 1024
    except (IndexError, ValueError):
        swap = 0.0
    # Swapped-out pages belong to running apps too: a Mac with 20 GB in swap
    # has no room, whatever its free RAM says right now.
    kev = sum(_footprint_gb(p) for p in _server_pids())
    return Memory(total, max(0.0, used + swap - kev), swap, kev)


def _server_pids(port: int = PORT) -> list[int]:
    r = subprocess.run(["lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN"], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split()]


# -- runtime --------------------------------------------------------------------

def _marker() -> Path:
    return paths.kev_env() / ".qualm-installed"


def runtime_ready() -> bool:
    m = _marker()
    return m.exists() and m.read_text(encoding="utf-8").strip() == KEV_SPEC


def install_runtime(say=print) -> None:
    """Python 3.12 with Kev and its serving extras, in paths.kev_env(). A few minutes, once."""
    uv = paths.uv()
    if not uv:
        raise RuntimeError("uv is needed to install the local model: https://docs.astral.sh/uv/")
    env = paths.kev_env()
    env.parent.mkdir(parents=True, exist_ok=True)
    steps = ([uv, "venv", "--quiet", "--allow-existing", "--python", "3.12", str(env)],
             [uv, "pip", "install", "--quiet", "--python", str(env / "bin" / "python"), KEV_SPEC])
    say("installing the local model's runtime (Kev, PyTorch, MLX: about 1 GB)…")
    for argv in steps:
        r = subprocess.run(argv, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"{Path(argv[0]).name} {argv[1]} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    _marker().write_text(KEV_SPEC + "\n", encoding="utf-8")


# -- server ---------------------------------------------------------------------

def server_command(model: str = MODEL, port: int = PORT, bits: int = BITS,
                   kev_dir: Path | None = None) -> tuple[list[str], dict[str, str], Path]:
    """(argv, environment, working directory) for the Kev server. `kev_dir`: a
    Kev checkout run with its own uv project (development), else the runtime."""
    script = Path(__file__).resolve().parent / "kevserve.py"
    env = {"KEV_DTYPE": "bf16", "MLX_CACHE_GB": "1", "QUALM_MODEL_CACHE": str(paths.models_dir()),
           "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
    if bits in (4, 8):
        env["KEV_QUANT_BITS"] = str(bits)
    tail = ["--run", model, "--port", str(port)]
    if kev_dir is not None:
        if not (kev_dir / "kev" / "serve.py").exists():
            raise RuntimeError(f"{kev_dir} doesn't look like the Kev repo (no kev/serve.py)")
        uv = paths.uv() or "uv"
        return [uv, "run", "--extra", "serve", "python", str(script), *tail], env, kev_dir
    return [str(paths.kev_env() / "bin" / "python"), str(script), *tail], env, paths.home()


def listening(port: int = PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def answering(port: int = PORT) -> dict | None:
    """The server's /v1/models entry, or None while it isn't up."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as r:
            return (json.loads(r.read()).get("models") or [{}])[0]
    except Exception:
        return None


class ManagedServer:
    """The app's own Kev server: installed if needed, started if nothing
    answers on the port, stopped when the app quits. A server that was
    already running (`qualm serve` in a terminal) is used and left alone."""

    def __init__(self, say, port: int = PORT):
        self.say, self.port = say, port
        self.proc: subprocess.Popen | None = None
        self.ready = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="kev-server").start()

    def _run(self) -> None:
        try:
            if listening(self.port):
                self.say("using the model server already running")
                self.ready.set()
                return
            if not runtime_ready():
                install_runtime(self.say)
            argv, env, cwd = server_command(port=self.port)
            paths.LOGS.mkdir(parents=True, exist_ok=True)
            log = (paths.LOGS / "kev.log").open("a", encoding="utf-8")
            first = not any(paths.models_dir().glob(f"*q{BITS}g*/model.safetensors"))
            self.say(f"loading the model (first time: downloading ~{DOWNLOAD_GB:.0f} GB, then saving 8-bit weights)…"
                     if first else "loading the model…")
            inherited = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
            self.proc = subprocess.Popen(argv, cwd=cwd, env={**inherited, **env}, stdout=log, stderr=subprocess.STDOUT)
            while self.proc.poll() is None:
                if answering(self.port):
                    self.say("watching")
                    self.ready.set()
                    return
                time.sleep(2)
            self.say(f"the model server stopped (exit {self.proc.returncode}); see {paths.LOGS / 'kev.log'}")
        except Exception as e:
            self.say(f"local model: {e}"[:200])

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
