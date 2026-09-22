# Run kev.serve with MLX's buffer cache capped, so freed Metal buffers
# (the fp32 LoRA merge, per-request activations) go back to the OS.
import os, runpy, sys
import mlx.core as mx
mx.set_cache_limit(int(os.environ.get("MLX_CACHE_GB", "1")) << 30)
sys.argv = ["kev.serve", *sys.argv[1:]]
runpy.run_module("kev.serve", run_name="__main__")
