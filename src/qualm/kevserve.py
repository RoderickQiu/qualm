# Kev's server (kev.serve), the way Qualm runs it. Runs inside the local
# model's runtime (paths.kev_env), not Qualm's: it imports kev and mlx only.
#
# - MLX's buffer cache is capped (MLX_CACHE_GB, 1), so freed Metal buffers
#   go back to the OS instead of growing to 17 GB.
# - KEV_QUANT_BITS=4|8 quantizes the backbone after the LoRA merge (the
#   pointer head stays fp32); KEV_QUANT_GROUP sets the group size (64).
# - With QUALM_MODEL_CACHE set, the quantized weights are saved there the
#   first time and loaded from there after that. The first start loads bf16
#   (~9 GB for Kev-4B), merges and quantizes; every later start reads the
#   8-bit weights directly (~4.5 GB) and never holds bf16.
import json, os, runpy, shutil, sys
from pathlib import Path

import mlx.core as mx

mx.set_cache_limit(int(os.environ.get("MLX_CACHE_GB", "1")) << 30)

if bits := int(os.environ.get("KEV_QUANT_BITS", "0")):
    import mlx.nn as nn
    from mlx.utils import tree_flatten
    from kev.checkpoint import Checkpoint, resolve_run
    from kev.model import pad_id

    group, load = int(os.environ.get("KEV_QUANT_GROUP", "64")), Checkpoint._load_mlx
    cache_root = os.environ.get("QUALM_MODEL_CACHE")

    def _cache_dir(self):
        # The checkpoint's snapshot (its commit) and the base's revision name the weights.
        base = f"{self.meta.base}@{self.meta.base_revision or 'main'}".replace("/", "--")
        return Path(cache_root) / f"{Path(self.path).name[:12]}-{base}-q{bits}g{group}"

    def _load_mlx(self, tok, opts):
        from kev.mlx_model import MLXDecisionModel

        cached = _cache_dir(self) if cache_root and opts.lora_scale == 1 else None
        if cached and (cached / "model.safetensors").exists():
            from kev.model import PointerHead

            print(f"{bits}-bit weights from {cached}", flush=True)
            m = MLXDecisionModel(cached, pad_id(tok), head_dim=self.meta.head_dim)
            # The head is sized from the embedding's width, which is packed on
            # quantized weights (1024 -> 256 at 8 bits): size it from the real one.
            m.head = PointerHead(m.text.embed_tokens.dims, dp=self.meta.head_dim).eval()
            return m
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
            tmp.rename(cached)
            print(f"saved the {bits}-bit weights to {cached}: later starts load those", flush=True)
        return m

    Checkpoint._load_mlx = _load_mlx

sys.argv = ["kev.serve", *sys.argv[1:]]
runpy.run_module("kev.serve", run_name="__main__")
