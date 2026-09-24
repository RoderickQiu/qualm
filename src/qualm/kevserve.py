# Kev's server (kev.serve), the way Qualm runs it. Runs inside the local
# model's runtime (paths.kev_env), not Qualm's: it imports kev and mlx only
# (the helpers above main() are plain Python, for the tests).
#
# - MLX's buffer cache is capped (MLX_CACHE_GB, 1), so freed Metal buffers
#   go back to the OS instead of growing to 17 GB.
# - KEV_QUANT_BITS=4|8 quantizes the backbone after the LoRA merge (the
#   pointer head stays fp32); KEV_QUANT_GROUP sets the group size (64).
# - With QUALM_MODEL_CACHE set, the quantized weights are saved there the
#   first time and loaded from there after that. Building them loads bf16
#   (~9 GB for Kev-4B), merges and quantizes (100 s, a 16 GB peak); every
#   later start reads the 8-bit weights directly (11 s, 4.8 GB).
# - With QUALM_PREBUILT set (a Hugging Face repo@revision), the first start
#   downloads those 8-bit weights instead (4.5 GB, no bf16 at all), if they
#   were built from this very checkpoint and base, the same way, and their
#   SHA-256 matches (QUALM_PREBUILT_SHA256, pinned in localmodel, else the
#   copy's provenance.json). A download cut off goes on where it stopped,
#   next start. They're built here instead only if that copy is gone and
#   QUALM_BUILD=1 (localmodel sets it when the Mac has the memory and disk for
#   it); any other failure (offline, disk full) stops the start and says why.
#   Once a new copy lands, the older copies Qualm itself made (their MARK) go.
# - One server per models folder at a time (a lock), and none where a server
#   already answers or another app holds the port: a second start, say
#   `qualm serve` while the app downloads, says so and exits before any
#   weights load.
# - With QUALM_PARENT set (the app's pid), the server ends when the app does.
# - Lines starting "qualm: " tell the app what it's doing (localmodel.ManagedServer):
#   "downloading BYTES", "building", "loading", or "error REASON what happened",
#   REASON one of offline, blocked, busy, missing, damaged, wrong (the file
#   served isn't the pinned one), disk, denied, memory, running, port or error;
#   then it exits with EXIT.
import errno
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

EXIT = 3  # the start failed, for the reason on the "qualm: error" line


def say(*words) -> None:
    print("qualm:", *words, flush=True)


def fail(reason: str, text: str):
    say("error", reason, text)
    sys.exit(EXIT)


def reason(e: BaseException) -> str:
    """Why a start failed, as one word the app can put in plain words: from the
    error or what caused it, the most telling first (a 429 behind "offline")."""
    chain = []
    while e is not None and e not in chain:
        chain.append(e)
        e = e.__cause__ or e.__context__
    for e in chain:
        name = type(e).__name__
        if isinstance(e, WrongFile):
            return "wrong"
        if isinstance(e, Damaged):
            return "damaged"
        status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "code", None)
        if name in ("RepositoryNotFoundError", "RevisionNotFoundError", "EntryNotFoundError",
                    "RemoteEntryNotFoundError") or status == 404:
            return "missing"
        if status in (401, 403, 407, 451):
            return "blocked"
        if status == 429 or isinstance(status, int) and 500 <= status < 600:
            return "busy"
        if isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EDQUOT):
            return "disk"
        if isinstance(e, OSError) and e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return "denied"
        if isinstance(e, MemoryError) or "out of memory" in str(e).lower():
            return "memory"
    if any(isinstance(e, (ConnectionError, TimeoutError)) or type(e).__name__ in (
            "LocalEntryNotFoundError", "OfflineModeIsEnabled", "URLError", "gaierror", "IncompleteRead",
            "ConnectError", "ConnectTimeout", "ReadTimeout", "ReadError", "RemoteProtocolError") for e in chain):
        return "offline"
    return "error"


def lock(folder: Path):
    """The models folder's lock, held until this process ends; None if another server holds it."""
    import fcntl

    folder.mkdir(parents=True, exist_ok=True)
    f = open(folder / ".lock", "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def holder(port: int) -> str | None:
    """What already holds this Mac's `port`: "model" (a model server answers
    there), "other", or None (it's free). Checked before any weights load: a
    second 4.5 GB model would only fail to bind, after swapping the Mac."""
    import socket
    import urllib.request

    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=5) as r:
            return "model" if json.loads(r.read()).get("models") else "other"
    except Exception:
        return "other"


MARK = "qualm.json"  # in each saved copy of the pinned Kev-4B: which it is; pruned once a newer one lands


def mine(folder: Path) -> bool:
    """A copy of the pinned Kev-4B that Qualm downloaded or built (its MARK), or
    a download of one it left unfinished; not a copy a developer built with
    `qualm serve --model`."""
    return (folder / MARK).exists() or (folder / "model.safetensors.part").exists()


def mark(folder: Path) -> None:
    """Note in `folder` that it's a copy of the pinned Kev-4B Qualm made (what mine() looks for)."""
    try:
        (folder / MARK).write_text(json.dumps({"prebuilt": os.environ.get("QUALM_PREBUILT", "")}) + "\n")
    except OSError:  # a folder we can't write to: it just isn't pruned later
        pass


def prune(cached: Path) -> None:
    """Copies of an earlier Kev-4B that Qualm made, next to the one just saved
    (same base and quantization, another checkpoint): 4.5 GB each, never used
    again. A developer's own builds (`qualm serve --model`) are left alone."""
    base, quant = cached.name.split("-", 1)[1].split("@")[0], cached.name.rsplit("-", 1)[1]
    for old in cached.parent.glob(f"*-{base}@*-{quant}*"):
        if old != cached and old.is_dir() and mine(old):
            shutil.rmtree(old, ignore_errors=True)
            print(f"removed {old}: an earlier checkpoint's weights", flush=True)


def expected(prov: dict) -> tuple[str, int | None]:
    """The prebuilt weights' SHA-256 and size: pinned in localmodel for its own
    copy (a mirror serves provenance.json too, so it can't vouch for them), else
    what the copy's provenance.json says (QUALM_PREBUILT set to another copy)."""
    return (os.environ.get("QUALM_PREBUILT_SHA256") or prov["model.safetensors"]["sha256"],
            int(os.environ.get("QUALM_PREBUILT_BYTES") or 0) or prov["model.safetensors"].get("bytes"))


class Damaged(Exception):
    """What was downloaded isn't the file expected: it ends short of its size."""


class WrongFile(Damaged):
    """The file served isn't the one expected: its size says so before a byte
    of it is kept. The same address serves the same file next time (a mirror's
    own copy, say), so the app says so rather than trying it again and again."""


def download(url: str, part: Path, size: int | None) -> None:
    """`url` into `part`, going on from what an earlier try left there."""
    import http.client
    import urllib.error
    import urllib.request

    have = part.stat().st_size if part.exists() else 0
    if size is not None and have >= size:
        return
    headers = {"User-Agent": "qualm", **({"Range": f"bytes={have}-"} if have else {})}
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and size is not None:  # nothing left to fetch, short of the size expected
            raise Damaged(f"{url} ends at {have:,} bytes, not {size:,}") from None
        if e.code == 416:
            return
        raise
    total = None if r.length is None else r.length + (have if r.status == 206 else 0)
    if size is not None and total is not None and total != size:  # a mirror's own file, say: not worth a byte
        r.close()
        raise WrongFile(f"{url} is {total:,} bytes, not {size:,}")
    with r, open(part, "ab" if r.status == 206 else "wb") as f:
        have = have if r.status == 206 else 0
        step = max((size or 0) // 10, 500 << 20)  # a line for every tenth, in kev.log or the terminal
        while chunk := r.read(1 << 20):
            f.write(chunk)
            if (have + len(chunk)) // step != have // step:
                print(f"{(have + len(chunk)) / 1e9:.1f} of {size / 1e9:.1f} GB" if size else
                      f"{(have + len(chunk)) / 1e9:.1f} GB", flush=True)
            have += len(chunk)
        if (size is not None and have < size) or r.length:  # the connection dropped: keep what came
            raise http.client.IncompleteRead(b"")


def sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 24), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    if parent := int(os.environ.get("QUALM_PARENT") or 0):
        def orphaned():
            while os.getppid() == parent:
                time.sleep(2)
            os._exit(0)  # the app is gone (quit, crashed or killed): so is its server

        threading.Thread(target=orphaned, daemon=True).start()
    cache_root = os.environ.get("QUALM_MODEL_CACHE")
    held = cache_root and lock(Path(cache_root))
    if cache_root and not held:
        fail("running", "another model server is already running or getting ready on this Mac (the Qualm app starts "
                        "its own): nothing to do")
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv[:-1] else 8008  # kev.serve's own
    if (there := holder(port)) == "model":  # one from before the lock, or for another Qualm folder
        fail("running", f"a model server already answers on port {port}: nothing to do")
    if there:
        fail("port", f"port {port} is used by another app: nothing was loaded. Quit that app, or pick a free port "
                     "(`qualm serve --port`, KEV_URL for Qualm)")

    import mlx.core as mx

    mx.set_cache_limit(int(os.environ.get("MLX_CACHE_GB", "1")) << 30)
    if bits := int(os.environ.get("KEV_QUANT_BITS", "0")):
        patch(bits, cache_root)
    import runpy

    sys.argv = ["kev.serve", *sys.argv[1:]]
    runpy.run_module("kev.serve", run_name="__main__")


def patch(bits: int, cache_root: str | None) -> None:
    """Kev's MLX loader, quantizing to `bits`, with the weights saved once (or downloaded ready-made)."""
    import mlx.core as mx
    import mlx.nn as nn
    from kev.checkpoint import Checkpoint, resolve_run
    from kev.model import pad_id
    from mlx.utils import tree_flatten

    group, load = int(os.environ.get("KEV_QUANT_GROUP", "64")), Checkpoint._load_mlx

    def _cache_dir(self):
        # The checkpoint's snapshot (its commit) and the base's revision name the weights.
        base = f"{self.meta.base}@{self.meta.base_revision or 'main'}".replace("/", "--")
        return Path(cache_root) / f"{Path(self.path).name[:12]}-{base}-q{bits}g{group}"

    def _fetch(self, cached):
        """The prebuilt 8-bit weights into `cached`: True, or False if there are none for this checkpoint.
        Anything else that goes wrong (offline, blocked, disk full) ends the start, with why."""
        from huggingface_hub import hf_hub_download, hf_hub_url

        repo, _, rev = os.environ["QUALM_PREBUILT"].partition("@")
        try:
            prov = json.loads(Path(hf_hub_download(repo, "provenance.json", revision=rev or None)).read_text())
        except Exception as e:
            if reason(e) != "missing":
                raise
            print(f"{repo} isn't there ({type(e).__name__}: {str(e).strip().splitlines()[0]})", flush=True)
            return False
        want = (Path(self.path).name, self.meta.base_revision, {"bits": bits, "group_size": group})
        got = (prov["kev"]["revision"], prov["base"]["revision"], prov["quantization"])
        if want != got:
            print(f"{repo} was built from {got}, not {want}", flush=True)
            return False
        tmp = cached.with_name(cached.name + ".partial")  # kept between starts: what's downloaded stays
        (sha, size), part = expected(prov), tmp / "model.safetensors.part"
        tmp.mkdir(parents=True, exist_ok=True)
        left, free = (size or 0) - (part.stat().st_size if part.exists() else 0), shutil.disk_usage(tmp).free
        if left + (200 << 20) > free:
            fail("disk", f"the model needs {left / 1e9:.1f} GB more on disk; {free / 1e9:.1f} GB free")
        hf_hub_download(repo, "config.json", revision=rev or None, local_dir=tmp)
        print(f"downloading the 8-bit weights from {repo} ({(size or 0) / 1e9:.1f} GB)", flush=True)
        say("downloading", size or 0)
        url = hf_hub_url(repo, "model.safetensors", revision=rev or None)
        for attempt in range(3):  # a dropped connection goes on where it stopped
            try:
                download(url, part, size)
                break
            except WrongFile:
                raise  # none of it was kept; what earlier tries fetched of the right file stays
            except Damaged:
                part.unlink(missing_ok=True)  # not the file pinned: nothing of it is worth keeping
                raise
            except Exception as e:
                if reason(e) != "offline" or attempt == 2:
                    raise
                print(f"the download stopped ({type(e).__name__}: {e}); going on in 5 s", flush=True)
                time.sleep(5)
        if sha256(part) != sha:
            part.unlink()
            fail("damaged", f"{repo}: the downloaded weights' SHA-256 doesn't match; the next start downloads them again")
        part.rename(tmp / "model.safetensors")
        mark(tmp)
        tmp.rename(cached)
        return True

    def _load_mlx(self, tok, opts):
        from kev.mlx_model import MLXDecisionModel

        cached = _cache_dir(self) if cache_root and opts.lora_scale == 1 else None
        if cached and not (cached / "model.safetensors").exists() and os.environ.get("QUALM_PREBUILT"):
            if _fetch(self, cached):
                prune(cached)
            elif os.environ.get("QUALM_BUILD") != "1":
                fail("missing", "there's no 8-bit copy of this checkpoint to download, and building one here "
                                "needs about 16 GB of free memory and 14 GB of disk")
            else:
                print("building the 8-bit weights here instead", flush=True)
        if cached and (cached / "model.safetensors").exists():
            from kev.model import PointerHead

            if os.environ.get("QUALM_PREBUILT") and not (cached / MARK).exists():
                mark(cached)  # the pinned copy, saved before copies were marked: Qualm's to prune later
            say("loading")
            print(f"{bits}-bit weights from {cached}", flush=True)
            m = MLXDecisionModel(cached, pad_id(tok), head_dim=self.meta.head_dim)
            # The head is sized from the embedding's width, which is packed on
            # quantized weights (1024 -> 256 at 8 bits): size it from the real one.
            m.head = PointerHead(m.text.embed_tokens.dims, dp=self.meta.head_dim).eval()
            return m
        say("building")
        m = load(self, tok, opts)   # merged in bf16 first: quantizing before the merge would drop the adapter
        nn.quantize(m.lm, group_size=group, bits=bits,   # Linear + Embedding; skip what doesn't split into groups
                    class_predicate=lambda _, x: hasattr(x, "to_quantized") and x.weight.shape[-1] % group == 0)
        mx.eval(m.lm.parameters())
        if cached:
            # mlx-lm loads a folder with config.json + model*.safetensors, and
            # quantizes exactly the layers that have `.scales` when the config
            # says "quantization". Written to a side folder, then renamed.
            base_dir = Path(resolve_run(f"{self.meta.base}@{self.meta.base_revision or ''}"))
            config = json.loads((base_dir / "config.json").read_text(encoding="utf-8"))
            config["quantization"] = {"group_size": group, "bits": bits}
            tmp = cached.with_name(cached.name + ".partial")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            mx.save_safetensors(str(tmp / "model.safetensors"), dict(tree_flatten(m.lm.parameters())),
                                metadata={"format": "mlx"})
            (tmp / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
            if os.environ.get("QUALM_PREBUILT"):  # the pinned Kev-4B, built here: Qualm's copy, not a developer's
                mark(tmp)
            tmp.rename(cached)
            print(f"saved the {bits}-bit weights to {cached}: later starts load those", flush=True)
            if os.environ.get("QUALM_PREBUILT"):
                prune(cached)
        return m

    Checkpoint._load_mlx = _load_mlx


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # offline, blocked, disk full, out of memory: in words for the app, then exit
        import traceback

        traceback.print_exc()
        fail(reason(e), f"{type(e).__name__}: {e}".replace("\n", " ")[:500])
