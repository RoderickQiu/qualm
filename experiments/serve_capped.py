# Run kev.serve with MLX's buffer cache capped, so freed Metal buffers
# (the fp32 LoRA merge, per-request activations) go back to the OS.
# KEV_QUANT_BITS=4|8 quantizes the backbone after the LoRA merge (the
# pointer head stays fp32); KEV_QUANT_GROUP sets the group size (64).
import os, runpy, sys
import mlx.core as mx
mx.set_cache_limit(int(os.environ.get("MLX_CACHE_GB", "1")) << 30)

if bits := int(os.environ.get("KEV_QUANT_BITS", "0")):
    import mlx.nn as nn
    from kev.checkpoint import Checkpoint
    group, load = int(os.environ.get("KEV_QUANT_GROUP", "64")), Checkpoint._load_mlx

    def _load_mlx(self, tok, opts):
        m = load(self, tok, opts)   # merged in bf16 first: quantizing before the merge would drop the adapter
        nn.quantize(m.lm, group_size=group, bits=bits,   # Linear + Embedding; skip what doesn't split into groups
                    class_predicate=lambda _, x: hasattr(x, "to_quantized") and x.weight.shape[-1] % group == 0)
        mx.eval(m.lm.parameters())
        return m
    Checkpoint._load_mlx = _load_mlx

sys.argv = ["kev.serve", *sys.argv[1:]]
runpy.run_module("kev.serve", run_name="__main__")
