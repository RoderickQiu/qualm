"""Threshold sweep over `eval --dump` files: per rule, the best recall at
precision >= 0.9, plus ROC AUC (threshold-free ranking quality).

    uv run python experiments/analyze.py runs/*.jsonl [--errors]
"""
import json, sys
from pathlib import Path

HIT = {"shortvideo": "violates", "stocks": "violates", "social": "in_scope"}


def auc(pos, neg):
    if not pos or not neg:
        return float("nan")
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def sweep(rows, rule):
    pts = [(r["p_hit"][rule], r["labels"]["rules"][rule] == HIT[rule]) for r in rows
           if r["labels"]["rules"].get(rule) not in (None, "unknown")]
    pos = [p for p, y in pts if y]; neg = [p for p, y in pts if not y]
    best = (0.0, None, None)  # recall, threshold, precision
    for t in sorted({p for p, _ in pts}):
        pred = [(p >= t, y) for p, y in pts]
        tp = sum(a and y for a, y in pred); pp = sum(a for a, _ in pred)
        if pp and tp / pp >= 0.9 and tp / len(pos) > best[0]:
            best = (tp / len(pos), t, tp / pp)
    return len(pos), len(neg), auc(pos, neg), best


def main():
    files = [a for a in sys.argv[1:] if not a.startswith("--")]
    for f in files:
        rows = [json.loads(l) for l in Path(f).open()]
        page = sum(r["page_kind"] == r["labels"]["page_kind"] for r in rows) / len(rows)
        sens = sum((r["sensitive"] >= 0.5) == r["labels"]["sensitive"] for r in rows) / len(rows)
        sp = [r["sensitive"] for r in rows if r["labels"]["sensitive"]]; sn = [r["sensitive"] for r in rows if not r["labels"]["sensitive"]]
        print(f"{Path(f).stem}: n={len(rows)} page_acc={page:.2f} sens_acc={sens:.2f} sens_auc={auc(sp, sn):.2f}")
        for rule in HIT:
            npos, nneg, a, (rec, t, prec) = sweep(rows, rule)
            at = f"recall {rec:.2f} at p_hit>={t:.3f} (precision {prec:.2f})" if t is not None else "never reaches precision 0.9"
            print(f"  {rule:<10} pos={npos:<3} neg={nneg:<3} AUC={a:.2f}  {at}")
        if "--errors" in sys.argv:
            for r in rows:
                for rule, hit in HIT.items():
                    g = r["labels"]["rules"].get(rule)
                    if g in (None, "unknown"):
                        continue
                    p = r["p_hit"][rule]
                    if (g == hit) != (p >= 0.5):
                        print(f"    {'FN' if g == hit else 'FP'} {rule:<10} p={p:.2f}  {r['note'][:90]}")


if __name__ == "__main__":
    main()
