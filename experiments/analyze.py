"""Threshold sweep over `eval --dump` files.

Per rule: ROC AUC (ranking quality, threshold-free), the best recall at
precision >= 0.9 when the threshold is picked on all records (optimistic),
and the same with sites held out (--by-site): for each site, the threshold
is picked on the other sites and scored on that one. The held-out number is
the one to trust for sites nobody labelled.

    uv run python experiments/analyze.py data/runs/mvp-4b-en.jsonl [--labels data/auto_labels_v2.jsonl] [--errors]
"""
import argparse, json
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

HIT = ("violates", "in_scope")
TARGET = 0.9


def auc(pos, neg):
    if not pos or not neg:
        return float("nan")
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def pick(pts):
    """Lowest threshold with the most true positives at precision >= TARGET; None if unreachable."""
    best = None
    for t in sorted({p for p, _ in pts}):
        tp = sum(p >= t and y for p, y in pts); pp = sum(p >= t for p, _ in pts)
        if pp and tp / pp >= TARGET and (best is None or tp > best[1]):
            best = (t, tp)
    return best[0] if best else None


def score(pts, t):
    tp = sum(p >= t and y for p, y in pts); fp = sum(p >= t and not y for p, y in pts)
    return tp, fp, sum(y for _, y in pts) - tp


def site(note):
    target = note.split(" | ")[0].removeprefix("auto: ")
    if target.startswith("app:"):
        return target
    host = urlparse(target).netloc.removeprefix("www.")
    return ".".join(host.split(".")[-2:])  # live.bilibili.com and bilibili.com are one site


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dumps", nargs="+")
    ap.add_argument("--labels", help="score against these labels instead of the ones in the dump")
    ap.add_argument("--errors", action="store_true")
    a = ap.parse_args()
    relabel = {}
    if a.labels:
        relabel = {json.loads(l)["note"]: json.loads(l)["labels"] for l in open(a.labels, encoding="utf-8")}
    for f in a.dumps:
        rows = [json.loads(l) for l in Path(f).open()]
        for r in rows:
            r["labels"] = relabel.get(r["note"], r["labels"])
        print(f"{Path(f).stem}: n={len(rows)}")
        print(f"  {'rule':<11} {'pos':>4} {'neg':>4}  {'AUC':>5}   {'all sites: t / P / R':<24} held-out sites: P / R")
        for rule in rows[0]["p_hit"]:
            data = [(r["p_hit"][rule], r["labels"]["rules"][rule] in HIT, site(r["note"])) for r in rows
                    if r["labels"].get("rules", {}).get(rule) not in (None, "unknown")]
            pts = [(p, y) for p, y, _ in data]
            pos = [p for p, y in pts if y]; neg = [p for p, y in pts if not y]
            t = pick(pts)
            if t is None:
                allsites = "unreachable"
            else:
                tp, fp, fn = score(pts, t)
                allsites = f"{t:.3f} / {tp / (tp + fp):.2f} / {tp / (tp + fn):.2f}"
            TP = FP = FN = 0
            for s in {s for *_, s in data}:
                train = [(p, y) for p, y, x in data if x != s]
                test = [(p, y) for p, y, x in data if x == s]
                ts = pick(train)
                tp, fp, fn = score(test, ts) if ts is not None else (0, 0, sum(y for _, y in test))
                TP, FP, FN = TP + tp, FP + fp, FN + fn
            held = f"{TP / (TP + FP):.2f} / {TP / (TP + FN):.2f}" if TP + FP and TP + FN else "-"
            print(f"  {rule:<11} {len(pos):>4} {len(neg):>4}  {auc(pos, neg):>5.2f}   {allsites:<24} {held}")
            if a.errors and t is not None:
                for p, y, s in sorted(data, key=lambda d: -d[0]):
                    if (p >= t) != y:
                        print(f"      {'FN' if y else 'FP'} p={p:.3f} {s}")


if __name__ == "__main__":
    main()
