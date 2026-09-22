"""Rewrite the labels of an auto_labels file for the current rule set
(manifest.labels_v2); the captures are kept as they are.

    uv run python experiments/relabel.py data/auto_labels.jsonl data/auto_labels_v2.jsonl
"""
import json, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from manifest import labels_v2

src, dst = sys.argv[1:3]
out, tally = [], Counter()
for line in open(src, encoding="utf-8"):
    r = json.loads(line)
    target = r["note"].split(" | ")[0].removeprefix("auto: ")
    r["labels"] = labels_v2(target, r["labels"])
    for k, v in r["labels"]["rules"].items():
        tally[k, v] += 1
    tally["purpose", r["labels"]["purpose"]] += 1
    out.append(json.dumps(r, ensure_ascii=False))
Path(dst).write_text("\n".join(out) + "\n", encoding="utf-8")
for k in sorted(tally, key=str):
    print(k, tally[k])
