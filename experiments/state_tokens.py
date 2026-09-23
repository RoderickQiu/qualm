"""How many tokens the state alone costs, at several --budget values.

Step 1 (this env):  uv run python experiments/state_tokens.py dump data/auto_labels.jsonl > /tmp/states.json
Step 2 (Kev env):   cd ~/Documents/kev && uv run python ~/Documents/qualm/experiments/state_tokens.py count /tmp/states.json
"""
import json, sys

BUDGETS = (300, 500, 700, 1000, 1500)

if sys.argv[1] == "dump":
    from qualm.state import ScreenState
    recs = [json.loads(l) for l in open(sys.argv[2])]
    print(json.dumps({b: [ScreenState(**r["screen"]).to_state(b) for r in recs] for b in BUDGETS}, ensure_ascii=False))
else:
    import statistics
    from transformers import AutoTokenizer
    from kev.api import render
    from kev.model import user_tokens
    tok = AutoTokenizer.from_pretrained("jaredpalmer/kev-0.8b")
    data = json.load(open(sys.argv[2]))
    for b, states in data.items():
        n = sorted(len(user_tokens(tok, render(s))) + 1 for s in states)
        over = sum(x > 384 for x in n)
        print(f"budget {b:>5} chars: state tokens p50={statistics.median(n):.0f} p95={n[int(0.95 * len(n)) - 1]} max={n[-1]}  over 384: {over}/{len(n)}")
