"""The local model on this Mac: Kev-4B at 8 bits, served by kevserve.py.

    runtime    ~/Library/Application Support/Qualm/kev-env: Python with Kev,
               PyTorch and MLX (~1 GB), installed with uv on first use
    weights    Kev-4B's adapter (0.2 GB) and its 8-bit copy, merged and
               quantized, from Hugging Face (PREBUILT, 4.5 GB), into models/;
               only if that copy is gone, and the Mac has the room, the 8.7 GB
               bf16 base, merged and quantized here
    server     started, started again if it fails (later each time), and
               stopped by the app (ManagedServer), or in a terminal with
               `qualm serve`; http://127.0.0.1:8009 (KEV_URL overrides; if
               another app holds 8009, the app's runs on the next free port
               and notes it in models/server.json for terminal commands)

Memory, measured (HANDOFF, Measured): the server holds 6.1-7.1 GB while it
answers (the weights 4.2 GB, the rest MLX's buffers, capped at 1 GB, and
the Python around it). What matters is whether your Mac has that to spare
next to what you run, not its total: `memory()` says both.
"""

from __future__ import annotations

import fcntl
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import paths

KEV_REPO = "https://github.com/jaredpalmer/kev"
KEV_COMMIT = "08ab0b87d27cb5577a3b371ad7ed4e4686b0502b"  # tested with Qualm; bump deliberately
# An archive of that commit, not a git+ URL: installing it needs no git, and a
# Mac without Apple's Command Line Tools has none. No uv setting redirects a
# URL like this one (uv.toml mirrors PyPI and Python only): where GitHub is
# blocked, QUALM_KEV_ARCHIVE names a copy of the same archive to use instead.
KEV_ARCHIVE = f"{KEV_REPO}/archive/{KEV_COMMIT}.zip"
KEV_SPEC = f"kev[serve] @ {KEV_ARCHIVE}"
# Kev-4B pinned the way the code is: the thresholds in rules.toml were tuned on
# this checkpoint, and a new upstream commit changes nothing until it's bumped
# here, together with PREBUILT. The checkpoint names its base's revision
# itself (head.pt): BASE is that, for the saved copy's folder name.
MODEL, PORT, BITS = "jaredpalmer/kev-4b@485ace8703592fcf405488b262449990824cfed1", 8009, 8
BASE = "Qwen/Qwen3.5-4B-Base@1001bb4d826a52d1f399e183466143f4da7b741b"
# Kev-4B already merged and quantized to 8 bits (docs/MODELS.md), built from
# exactly MODEL and BASE (its provenance.json says so): the first start
# downloads 4.5 GB and loads in seconds, instead of fetching the 9 GB base and
# building it (100 s, a 16 GB peak).
PREBUILT = "RoderickQiu/kev-4b-mlx-8bit@f56017669221a6baab2b380c0b0c06ef826d0897"
# Its model.safetensors, pinned here with it: a mirror (hf_endpoint) serves
# provenance.json too, so the hash to check the download against can't come
# from there.
PREBUILT_SHA256, PREBUILT_BYTES = "59f136a6c8f620c21a5b26acd8b9c44550db23e09328dc80ba768c508c502c36", 4_469_640_165
NEED_GB = (6.1, 7.1)  # the 8-bit server while answering, measured on the trial pages
HEADROOM_GB = 2.0  # so opening a browser tab doesn't push it into swap
DOWNLOAD_GB, RUNTIME_GB = 5.0, 1.0  # first start, on disk: the adapter + the 8-bit copy; Python, PyTorch, MLX, Kev
# Building the 8-bit copy here, should PREBUILT be gone: the bf16 base comes
# down first (8.7 GB more on disk), and the merge peaks at 16 GB (measured).
BUILD_GB, BUILD_DISK_GB = 16.0, 14.0
LOG_MAX = 2 << 20  # past this, kev.log is set aside as kev.log.1 at the next start


def apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def unsupported() -> str | None:
    """Why the local model can't run on this Mac, or None if it can."""
    if not apple_silicon():
        return "The local model needs Apple silicon (M1 or later)."
    mac = platform.mac_ver()[0]
    if mac and int(mac.split(".")[0]) < 14:
        return f"The local model needs macOS 14 or later: MLX, which runs it, has no build for macOS {mac}."
    return None


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

    @property
    def too_small(self) -> bool:
        """Not even an empty Mac of this size has room: closing apps won't help."""
        return self.total_gb < NEED_GB[1] + HEADROOM_GB

    def summary(self) -> str:
        need = f"{NEED_GB[0]:.0f}-{NEED_GB[1]:.0f} GB"
        swap = f" ({self.swap_gb:.0f} GB of it swapped out)" if self.swap_gb >= 1 else ""
        verdict = ("There's room for it." if self.fits else
                   "Too little for it on this Mac, whatever you close: the hosted model is the one to use." if self.too_small else
                   "Tight: it would push other apps into swap, and readings would slow to seconds." if self.spare_gb >= NEED_GB[0]
                   else "Not enough: it would swap.")
        return (f"The local model uses {need} while it runs. This Mac has {self.total_gb:.0f} GB; "
                f"what's open now uses about {self.in_use_gb:.0f} GB{swap}, leaving about {self.spare_gb:.0f} GB. {verdict}")


def _run(argv: list[str]) -> str:
    """A system tool's output, by its full path (an agent's PATH may lack /usr/sbin); "" if it can't run."""
    try:
        return subprocess.run(argv, capture_output=True, text=True).stdout
    except OSError:
        return ""


def _sysctl(name: str) -> str:
    return _run(["/usr/sbin/sysctl", "-n", name]).strip()


def _footprint_gb(pid: int) -> float:
    for line in _run(["/usr/bin/footprint", str(pid)]).splitlines():
        if "Footprint:" in line:
            n, unit = line.split("Footprint:")[1].split()[:2]
            return float(n) / (1024 if unit.startswith("MB") else 1 if unit.startswith("GB") else 1024 ** 2)
    return 0.0


def memory() -> Memory:
    total = int(_sysctl("hw.memsize") or 0) / 2**30
    page = int(_sysctl("hw.pagesize") or 16384)
    vm = _run(["/usr/bin/vm_stat"])
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
    return [int(p) for p in _run(["/usr/sbin/lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN"]).split()]


# -- runtime --------------------------------------------------------------------

def _marker() -> Path:
    return paths.kev_env() / ".qualm-installed"


def runtime_ready() -> bool:
    # Any spec of this commit is this code: a runtime installed from the git
    # URL, before the archive, doesn't need installing again.
    m = _marker()
    return m.exists() and KEV_COMMIT in m.read_text(encoding="utf-8")


def needs_git() -> bool:
    """Whether installing the runtime needs git (a git+ URL in KEV_SPEC)."""
    return "git+" in KEV_SPEC


def git_available() -> bool:
    """A git that works, found without running it: on a Mac without the Command
    Line Tools, /usr/bin/git is a stub that asks to install them."""
    found = shutil.which("git")
    if found and found != "/usr/bin/git":
        return True
    return subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0


def kev_spec() -> str:
    """What uv installs Kev from: the archive on GitHub, or the copy of it QUALM_KEV_ARCHIVE names."""
    url = os.environ.get("QUALM_KEV_ARCHIVE", "").strip()
    return f"kev[serve] @ {url}" if url else KEV_SPEC


def install_runtime(say=print) -> None:
    """Python 3.12 with Kev and its serving extras, in paths.kev_env(). Once:
    a second install meanwhile (the app's, `qualm serve`) waits for the first."""
    uv = paths.uv()
    if not uv:
        raise RuntimeError("uv is needed to install the local model: https://docs.astral.sh/uv/")
    env = paths.kev_env()
    env.parent.mkdir(parents=True, exist_ok=True)
    with (env.parent / "kev-env.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say("another Qualm is installing the local model's runtime; waiting for it…")
            fcntl.flock(lock, fcntl.LOCK_EX)
        if runtime_ready():
            return
        spec = kev_spec()
        steps = ([uv, "venv", "--quiet", "--allow-existing", "--python", "3.12", str(env)],
                 [uv, "pip", "install", "--quiet", "--python", str(env / "bin" / "python"), spec])
        say("installing the local model's runtime (Kev, PyTorch, MLX: about 1 GB)…")
        for argv in steps:
            r = subprocess.run(argv, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"{Path(argv[0]).name} {argv[1]} failed: {(r.stderr or r.stdout).strip()[-2000:]}")
        # The commit, even when a copy of the archive (QUALM_KEV_ARCHIVE) doesn't name it: runtime_ready looks for it.
        _marker().write_text(f"{spec}\ncommit {KEV_COMMIT}\n", encoding="utf-8")


# -- disk -----------------------------------------------------------------------

def saved_copy(bits: int = BITS) -> Path:
    """The folder kevserve keeps MODEL's quantized weights in, once it has them (its _cache_dir)."""
    return paths.models_dir() / f"{MODEL.partition('@')[2][:12]}-{BASE.replace('/', '--')}-q{bits}g64"


def downloaded() -> int:
    """Bytes of the 8-bit copy downloaded so far: kevserve keeps them between starts, and goes on from there."""
    return sum(p.stat().st_size for p in paths.models_dir().glob("*.partial/model.safetensors.part"))


def disk_free_gb() -> float:
    where = paths.home()
    while not where.exists():
        where = where.parent
    return shutil.disk_usage(where).free / 1e9


def disk_needed_gb() -> float:
    """What the first start still puts on disk: the runtime and the weights, less what's there already."""
    need = 0.0 if runtime_ready() else RUNTIME_GB
    if not (saved_copy() / "model.safetensors").exists():
        need += max(0.0, DOWNLOAD_GB - downloaded() / 1e9)
    return need


def disk_short() -> str | None:
    """What's missing, if the disk hasn't room for what the first start still downloads (and a GB to spare)."""
    need, free = disk_needed_gb(), disk_free_gb()
    if need and free < need + 1:
        return f"not enough disk space for the model: needs {need + 1:.0f} GB free, {free:.1f} GB left"
    return None


# -- server ---------------------------------------------------------------------

def can_build() -> bool:
    """Room to build the 8-bit copy here: memory for its 16 GB peak, and disk for the bf16 base."""
    return memory().spare_gb >= BUILD_GB and disk_free_gb() >= BUILD_DISK_GB


def hf_endpoint() -> str | None:
    """A Hugging Face mirror, where huggingface.co is blocked: HF_ENDPOINT, else
    [settings] hf_endpoint (Qualm.app, opened from the Finder or at login,
    sees no shell exports)."""
    if url := os.environ.get("HF_ENDPOINT"):
        return url
    try:
        from .rules import load_config

        return load_config(paths.rules_file())[0].hf_endpoint or None
    except Exception:  # no rules yet, or a file that doesn't load: huggingface.co
        return None


def server_command(model: str = MODEL, port: int = PORT, bits: int = BITS,
                   kev_dir: Path | None = None) -> tuple[list[str], dict[str, str], Path]:
    """(argv, environment, working directory) for the Kev server. `kev_dir`: a
    Kev checkout run with its own uv project (development), else the runtime."""
    if model == MODEL.partition("@")[0]:
        model = MODEL  # `qualm serve --model jaredpalmer/kev-4b`: the pinned one
    script = Path(__file__).resolve().parent / "kevserve.py"
    env = {"KEV_DTYPE": "bf16", "MLX_CACHE_GB": "1", "QUALM_MODEL_CACHE": str(paths.models_dir()),
           "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
    if endpoint := hf_endpoint():
        env["HF_ENDPOINT"] = endpoint
    if model == MODEL and bits == BITS:
        env["QUALM_PREBUILT"] = os.environ.get("QUALM_PREBUILT", PREBUILT)
        if env["QUALM_PREBUILT"] == PREBUILT:  # another copy (QUALM_PREBUILT) is checked against its own provenance
            env["QUALM_PREBUILT_SHA256"], env["QUALM_PREBUILT_BYTES"] = PREBUILT_SHA256, str(PREBUILT_BYTES)
        if not (saved_copy(bits) / "model.safetensors").exists():
            # Should the prebuilt copy turn out to be gone: build it here only
            # if this Mac has the room, never as a quiet fallback.
            env["QUALM_BUILD"] = os.environ.get("QUALM_BUILD") or ("1" if can_build() else "0")
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


def _entry(base: str, timeout: float) -> dict | None:
    with urllib.request.urlopen(f"{base}/v1/models", timeout=timeout) as r:
        entry = (json.loads(r.read()).get("models") or [None])[0]
    return entry if isinstance(entry, dict) and entry.get("name") else None


def answering(port: int = PORT, url: str | None = None) -> dict | None:
    """The server's /v1/models entry, or None while it isn't up (or isn't a model server)."""
    try:
        return _entry(url or f"http://127.0.0.1:{port}", 3)
    except Exception:
        return None


LOCAL = ("127.0.0.1", "localhost", "::1")


def _kev_listening(port: int) -> bool:
    """A Kev server (Qualm's kevserve.py, or Kev's own kev.serve) listens on `port` on this Mac."""
    return any(name in _run(["/bin/ps", "-o", "command=", "-p", str(pid)])
               for pid in _server_pids(port) for name in ("kevserve.py", "kev.serve"))


def _look(url: str, timeout: float, given: bool) -> tuple[str, dict | None]:
    """What's at `url`, and its /v1/models entry: "model", "busy" (a model server
    too busy to say so in time), "none" (nothing there, or no way to reach it) or
    "other" (something else holds the port). `given`: the address is KEV_URL,
    someone's own model server, so a slow answer there is a busy one."""
    where = urllib.parse.urlsplit(url)
    port = where.port or (443 if where.scheme == "https" else 80)
    try:  # a host that's down or off the network never takes the connection: nothing there
        socket.create_connection((where.hostname or "127.0.0.1", port), timeout).close()
    except OSError:
        return "none", None
    try:
        entry = _entry(url, timeout)
        return ("model", entry) if entry else ("other", None)
    except Exception as e:  # HTTP errors are URLErrors too, with a string for a reason
        why = e.reason if isinstance(e, urllib.error.URLError) else e
        if isinstance(why, ConnectionRefusedError):  # gone since
            return "none", None
        if not isinstance(why, TimeoutError):  # an HTTP error, not JSON, not HTTP at all
            return "other", None
    # It took the connection and said nothing in time. Kev answers /v1/models
    # while it reads a screen, but on a Mac deep in swap that can take seconds:
    # a Kev server is busy.
    if given or where.hostname not in LOCAL or _kev_listening(port):
        return "busy", None
    # Another program on this Mac: an ssh tunnel or a container's port (ssh -L,
    # Docker) can carry a model server that's busy as well. Given three times as
    # long, one answers like a model server; anything still silent isn't one.
    try:
        entry = _entry(url, 3 * timeout)
    except Exception:
        return "other", None
    return ("model", entry) if entry else ("other", None)


def probe(port: int = PORT, url: str | None = None, timeout: float = 10) -> str:
    """What's at a model server's address: "model" (a System One server, like
    Kev, answering or busy), "none" (nothing there) or "other" (something else
    holds the port). `url`: KEV_URL, else this Mac's `port`."""
    state = _look(url or f"http://127.0.0.1:{port}", timeout, given=url is not None)[0]
    return "model" if state == "busy" else state


def _moved_file() -> Path:
    """Where the app notes the address its model server runs on when that isn't
    :8009 (another app held it), for terminal commands: the KEV_URL it sets is its own."""
    return paths.models_dir() / "server.json"


def server_url() -> str:
    """The model server's address, for every model call and `status`/`doctor`:
    KEV_URL, else the address the running app's server moved to, else :8009."""
    if url := os.environ.get("KEV_URL"):
        return url
    try:
        moved = json.loads(_moved_file().read_text(encoding="utf-8"))
        if int(moved["pid"]) > 0:
            os.kill(int(moved["pid"]), 0)  # the app that wrote it is still running
            return str(moved["url"])
    except (OSError, ValueError, KeyError, TypeError):  # none, or from an app that's gone
        pass
    return f"http://127.0.0.1:{PORT}"


def find_server() -> tuple[str, dict | None]:
    """Where the model server is (server_url()), and its /v1/models entry: None
    if none answers there, {"busy": True} if one is there but too busy to say."""
    url = server_url()
    state, entry = _look(url, 3, given=bool(os.environ.get("KEV_URL")))
    _looked[url] = state
    return url, entry if state == "model" else {"busy": True} if state == "busy" else None


_looked: dict[str, str] = {}  # what find_server found at each address, so why_down needn't look again


def starting() -> bool:
    """A model server holds models/ (kevserve's lock): it's downloading or loading the model, or serving it."""
    lock = paths.models_dir() / ".lock"
    if not lock.exists():
        return False
    with lock.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def why_down(url: str) -> str:
    """Why no model server answers at `url` (find_server's), for `status` and
    `doctor` alike: "remote" (KEV_URL on another machine: nothing here starts
    one), "starting" (a server holds models/: downloading or loading the
    model), "taken" (another app holds the port) or "" (nothing there)."""
    where = urllib.parse.urlsplit(url)
    if where.hostname not in LOCAL:
        return "remote"
    if starting():
        return "starting"
    state = _looked.pop(url, "") or _look(url, 3, given=bool(os.environ.get("KEV_URL")))[0]
    return "taken" if state == "other" else ""


def log_file() -> Path:
    return paths.LOGS / "kev.log"


def _open_log():
    """kev.log to append to; past LOG_MAX the old one is set aside first (kev.log.1)."""
    path = log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX:
        path.replace(path.with_name(path.name + ".1"))
    return path.open("a", encoding="utf-8")


BACKOFF = (5, 15, 60, 300)  # seconds before the next try, after 1, 2, 3, 4+ failures in a row
WRONG_WAIT = 30  # how often a mirror that served the wrong file is looked at again: changed yet?

# What went wrong, in words: the status line ({wait}: until the next try;
# {log}: kev.log), and what to try, for the menu bar icon's tooltip. Keys are
# kevserve's "qualm: error REASON". Every line fits the menu's 80 characters.
MIRROR = ("If Hugging Face is blocked where you are, use a mirror: "
          "`qualm settings set hf_endpoint=https://hf-mirror.com`. Qualm uses it from its next try.")
HOSTED = "Or use the hosted model: Model > Hosted by TypeSafe, in this menu."
WORDS = {
    "offline": ("no internet, or Hugging Face is blocked here; retrying in {wait}", MIRROR),
    "blocked": ("Hugging Face refused the download (blocked here?); retrying in {wait}", MIRROR),
    "busy": ("Hugging Face is busy; retrying the download in {wait}", ""),
    "disk": ("not enough disk space for the model; retrying in {wait}", "Free up about 6 GB on this Mac's disk."),
    "denied": ("can't save the model on disk: see {log}", ""),
    "memory": ("the model server was stopped by macOS, likely out of memory; retrying in {wait}",
               "Quit apps you don't need. " + HOSTED),
    "missing": ("can't download the model, and building it needs 16 GB free: try hosted",
                "The ready-made 8-bit model can't be downloaded, and building it here needs about 16 GB of free "
                "memory and 14 GB of disk. " + HOSTED),
    "damaged": ("the model's download was damaged; downloading it again in {wait}", ""),
    "wrong": ("the download sent another file, not the model; retrying in {wait}",
              "Something between this Mac and Hugging Face (a proxy, a Wi-Fi sign-in page) sent another file in "
              "the model's place. " + HOSTED),
    "running": ("another Qualm is starting the model server; waiting for it", ""),
    "port": ("another app took the model server's port; retrying in {wait}",
             "Quit that app, or point KEV_URL at another port."),
    "runtime": ("can't download the model's runtime: offline, or blocked?; retrying in {wait}",
                "Kev comes from GitHub, the rest from PyPI. Mirrors for PyPI and Python go in ~/.config/uv/uv.toml "
                "(index-url, python-install-mirror); nothing there redirects GitHub. If GitHub is blocked, run "
                "`QUALM_KEV_ARCHIVE=<a copy of " + KEV_ARCHIVE + "> qualm serve` in a terminal once. " + HOSTED),
    "unsupported": ("the local model can't run on this Mac: use Hosted, in the Model menu", ""),
}
OFFLINE = ("dns error", "failed to lookup", "failed to fetch", "error sending request", "tcp connect error",
           "connection refused", "could not connect", "couldn't connect", "timed out", "network is unreachable")


def _when(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s"


def _install_words(error: str) -> tuple[str, str]:
    """A runtime that didn't install (uv's error), in words."""
    low = error.lower()
    if "no space left" in low:
        return WORDS["disk"]
    if any(w in low for w in OFFLINE):
        return WORDS["runtime"]
    if "uv is needed" in low:
        return "uv is needed to install the local model: https://docs.astral.sh/uv/", ""
    return "couldn't install the model's runtime; retrying in {wait}", "What went wrong is in {log}."


def _exit_words(code: int, error: tuple[str, str] | None, failures: int, mirror: str = "") -> tuple[str, str]:
    """A server that stopped before it answered, in words: its own reason, else
    its exit code. `mirror`: the Hugging Face mirror it downloaded from, if any."""
    host = (urllib.parse.urlsplit(mirror).hostname or mirror)[:24]
    if error and error[0] in ("offline", "blocked") and mirror:
        return (f"can't download the model from {host}; retrying in {{wait}}",
                f"Check the mirror's address, {mirror} (HF_ENDPOINT, or hf_endpoint in your settings: "
                "`qualm settings set hf_endpoint=` goes back to huggingface.co). " + HOSTED)
    if error and error[0] == "wrong" and mirror:  # tried again only once another mirror is set (ManagedServer)
        return (f"{host} serves the wrong model file: set another mirror",
                f"The mirror {mirror} (HF_ENDPOINT, or hf_endpoint in your settings) serves a model file that "
                "isn't the one Qualm pins, so Qualm stopped downloading from it. Set another mirror with "
                "`qualm settings set hf_endpoint=https://…`, or go back to huggingface.co with "
                "`qualm settings set hf_endpoint=`; Qualm tries again once it changes. " + HOSTED)
    if error and error[0] == "damaged" and mirror:
        return (WORDS["damaged"][0], f"It came from the mirror {mirror}: if this keeps happening, set another "
                "one, or go back to huggingface.co with `qualm settings set hf_endpoint=`. " + HOSTED)
    if error and error[0] in WORDS:
        return WORDS[error[0]]
    if code == -9:  # SIGKILL: macOS ends the biggest process when memory runs out
        return WORDS["memory"]
    if failures >= 3:
        return "the model server keeps stopping: see {log}", "Qualm tries again, less and less often."
    how = f"exit {code}" if code >= 0 else f"signal {-code}"
    return f"the model server stopped ({how}); retrying in {{wait}}", "What it said is in {log}."


class _Said:
    """kevserve's "qualm: ..." lines in kev.log, from where this start began:
    what it's doing (downloading, building, loading) and why it stopped."""

    def __init__(self, start: int):
        self.pos, self.phase, self.total = start, "", 0
        self.error: tuple[str, str] | None = None

    def read(self) -> None:
        try:
            with log_file().open("rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return
        data = data[:data.rfind(b"\n") + 1]  # whole lines only
        self.pos += len(data)
        for line in data.decode("utf-8", "replace").splitlines():
            if not line.startswith("qualm: "):
                continue
            word, _, rest = line[len("qualm: "):].partition(" ")
            if word == "error":
                reason, _, text = rest.partition(" ")
                self.error = (reason, text)
            else:
                self.phase, self.total = word, int(rest) if rest.isdigit() else self.total


class ManagedServer:
    """The app's own Kev server: installed if needed, started if no model
    server answers, started again if it stops (later after each failure in a
    row, saying why), stopped when the app quits. One already running (`qualm
    serve` in a terminal, or at KEV_URL) is used and left alone."""

    poll = 2.0  # seconds between looks at a server that's starting

    def __init__(self, say, port: int | None = None):
        self.say = say
        self.url = os.environ.get("KEV_URL")  # a server of your own: one is started there only if it's on this Mac
        where = urllib.parse.urlsplit(self.url or f"http://127.0.0.1:{PORT}")
        self.local = where.hostname in ("127.0.0.1", "localhost", "::1")
        self.host = where.hostname or "?"
        self.port = port or where.port or PORT
        self.proc: subprocess.Popen | None = None
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.status = self.hint = ""  # the last line said; what to try, while it's failing
        self.failing = False
        self.failures = 0  # in a row
        self._note = ""
        self._wrong: str | None = None  # a mirror that served another file than the model: not tried again
        self._lock = threading.Lock()

    def start(self) -> None:
        self._pick_port()
        url = self.url or f"http://127.0.0.1:{self.port}"
        if url != f"http://127.0.0.1:{PORT}":  # terminal commands find it there (server_url)
            try:
                _moved_file().parent.mkdir(parents=True, exist_ok=True)
                _moved_file().write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")
            except OSError:
                pass
        threading.Thread(target=self._run, daemon=True, name="kev-server").start()

    def stop(self) -> None:
        """Stop the server this app started, if any, and say nothing more."""
        with self._lock:
            self.stopped.set()
            proc = self.proc
        try:
            if json.loads(_moved_file().read_text(encoding="utf-8")).get("pid") == os.getpid():
                _moved_file().unlink()
        except (OSError, ValueError, AttributeError):
            pass
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _say(self, text: str) -> None:
        if not self.stopped.is_set() and text != self.status:
            self.status = text
            self.say(text)

    def _pick_port(self) -> None:
        """The port held by something that isn't a model server: the next free
        one, for this app's own client too (decide.make_client reads KEV_URL)."""
        if self.url or not listening(self.port) or probe(self.port, timeout=3) != "other":
            return
        for port in range(self.port + 1, min(self.port + 10, 65536)):  # the last port there is: 65535
            if not listening(port) or probe(port, timeout=3) == "model":
                self._note = f"port {self.port} is used by another app: the model runs on {port}"
                self.port = port
                os.environ["KEV_URL"] = f"http://127.0.0.1:{port}"
                return

    def _run(self) -> None:
        if self._note:
            with _open_log() as log:
                log.write(f"qualm: {self._note}\n")
            self._say(self._note)
        while not self.stopped.is_set():
            try:
                wait = self._attempt()
            except Exception as e:  # never let the thread end: say so, try again later
                with _open_log() as log:
                    log.write(f"qualm: the app's model server loop: {type(e).__name__}: {e}\n")
                wait = self._failed(f"local model: {type(e).__name__}; see {{log}}")
            if wait is None:
                return
            self._wait(wait)

    def _attempt(self) -> float | None:
        """One try: use a server that's there, or install and start ours. The
        seconds before the next try (None: never)."""
        state = probe(self.port, self.url) if self.url or listening(self.port) else "none"
        if state == "model":
            return self._use_running()
        if not self.local:
            return self._failed(f"the model server at {self.host[:26]} doesn't answer; retrying in {{wait}}")
        if state == "other":
            return self._failed(f"port {self.port} is used by another app; retrying in {{wait}}",
                                "Quit that app, or point KEV_URL at another port.")
        if why := unsupported():
            self._failed(*WORDS["unsupported"])
            self.hint = why + " " + HOSTED
            return None
        if not runtime_ready():
            if short := disk_short():
                return self._failed(short, WORDS["disk"][1])
            try:
                install_runtime(self._say)
            except Exception as e:
                with _open_log() as log:
                    log.write(f"qualm: installing the local model's runtime failed: {e}\n")
                return self._failed(*_install_words(str(e)))
            if self.stopped.is_set():
                return None
        if short := disk_short():
            return self._failed(short, WORDS["disk"][1])
        if self._wrong is not None and not (saved_copy() / "model.safetensors").exists():
            if (hf_endpoint() or "") == self._wrong:
                return WRONG_WAIT  # it would serve the same wrong file: wait for another mirror to be set
            self._wrong, self.failures = None, 0
        return self._serve()

    def _serve(self) -> float | None:
        """Start our server and watch it: what it's doing while it loads, then
        whether it keeps running. The seconds before the next try, once it stops."""
        argv, env, cwd = server_command(port=self.port)
        log = _open_log()
        said = _Said(log.tell())
        first = not (saved_copy() / "model.safetensors").exists()
        self._say(f"downloading the model (about {DOWNLOAD_GB:.0f} GB, the first time only)…" if first
                  else "loading the model…")
        inherited = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        # QUALM_PARENT: the server ends when this app does, however it ends.
        env = {**inherited, **env, "QUALM_PARENT": str(os.getpid())}
        with self._lock:
            if not self.stopped.is_set():
                self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
        log.close()  # the server has its own
        if self.stopped.is_set():
            return None
        while self.proc.poll() is None:
            if not self.ready.is_set():
                said.read()
                if answering(self.port):
                    self.failures, self.failing, self.hint = 0, False, ""
                    self.ready.set()
                    self._say("watching")
                elif said.phase == "downloading" and said.total:
                    self._say(f"downloading the model: {downloaded() / 1e9:.1f} of {said.total / 1e9:.1f} GB…")
                elif said.phase == "building":
                    self._say("building the model here (first time: a 9 GB download, 16 GB of memory)…")
                elif said.phase == "loading":
                    self._say("loading the model…")
            if self.stopped.wait(self.poll):
                return None
        if self.stopped.is_set():
            return None
        self.ready.clear()
        said.read()
        if said.error and said.error[0] == "running":  # `qualm serve`, or another Qualm, got there first
            self._say(WORDS["running"][0])
            return 15
        mirror = env.get("HF_ENDPOINT", "")
        if said.error and said.error[0] == "wrong" and mirror:
            self._wrong = mirror
        return self._failed(*_exit_words(self.proc.returncode, said.error, self.failures + 1, mirror))

    def _use_running(self) -> float:
        """A model server someone else started: used while it's there, then ours starts."""
        self.failures, self.failing, self.hint = 0, False, ""
        self.ready.set()
        self._say("using the model server already running")
        while not self.stopped.wait(5 if self.local else 30):
            if not (listening(self.port) if self.local else probe(self.port, self.url) == "model"):
                break
        self.ready.clear()
        return 0

    def _failed(self, text: str, hint: str = "") -> float:
        """Say what went wrong; the seconds before the next try, longer after each failure in a row."""
        self.failures += 1
        wait = BACKOFF[min(self.failures, len(BACKOFF)) - 1]
        where = str(log_file()).replace(str(Path.home()), "~", 1)
        self.failing, self.hint = True, hint.replace("{log}", where)
        self._say(text.replace("{wait}", _when(wait)).replace("{log}", where))
        return wait

    def _wait(self, seconds: float) -> None:
        """Until the next try; sooner if a model server turns up meanwhile (another Qualm's, `qualm serve`)."""
        was = self.local and listening(self.port)
        end = time.monotonic() + seconds
        while not self.stopped.wait(max(0.0, min(5.0, end - time.monotonic()))):
            if time.monotonic() >= end or (self.local and not was and listening(self.port)):
                return
