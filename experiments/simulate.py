"""Replay `eval --dump` readings through the real Policy (thresholds, URL
patterns, feed_hit, exemptions, allow_urls) and score what it would do.
Each record is judged cold: no history, so "opened on purpose" never
applies, and time caps count as a hit whether or not the budget is spent.

--no-patterns drops every URL pattern: the policy on sites nobody listed.

    uv run python experiments/simulate.py data/runs/mvp-4b-en.jsonl --labels data/auto_labels_v2.jsonl [--no-patterns]
"""
import argparse, json, sys, tempfile
from dataclasses import replace

from qualm.decide import Reading, RuleVerdict
from qualm.policy import Policy
from qualm.rules import HIT_LABELS, load_config

ap = argparse.ArgumentParser()
ap.add_argument("dump"); ap.add_argument("--labels", required=True); ap.add_argument("--rules", default="rules.example.toml")
ap.add_argument("--no-patterns", action="store_true"); ap.add_argument("--errors", action="store_true")
a = ap.parse_args()
settings, rules = load_config(a.rules)
if a.no_patterns:
    rules = [replace(r, patterns=(), sites=()) for r in rules]
labels = {json.loads(l)["note"]: json.loads(l)["labels"] for l in open(a.labels, encoding="utf-8")}
tally = {r.id: [0, 0, 0] for r in rules}  # tp, fp, fn
exempt = []
for line in open(a.dump, encoding="utf-8"):
    row = json.loads(line)
    gold = labels[row["note"]]
    policy = Policy(settings, rules, tempfile.mkdtemp())
    url = row.get("url", "")
    pre = policy.precheck("com.apple.Safari", url)
    if pre is not None:
        acted = set()
    else:
        reading = Reading(row["sensitive"], row["page_kind"], row["page_probs"], row["purpose"], row["purpose_probs"],
                          [RuleVerdict(k, "", v, {}) for k, v in row["p_hit"].items()], 0.0)
        ds = policy.decide({"url": url}, reading, "com.apple.Safari")
        acted = {d.rule for d in ds if d.action in ("intervene", "count")}
        exempt += [(d.rule, d.reason, row["note"][6:70]) for d in ds if d.action == "allow"]
    for r in rules:
        g = gold["rules"].get(r.id)
        if g in (None, "unknown"):
            continue
        y, pred = g in HIT_LABELS, r.id in acted
        tally[r.id][0] += pred and y; tally[r.id][1] += pred and not y; tally[r.id][2] += (not pred) and y
        if a.errors and pred != y:
            print(f"    {'FN' if y else 'FP'} {r.id:<10} {row['note'][6:80]}")
print(f"policy on {a.dump}{' without URL patterns' if a.no_patterns else ''}:")
for rid, (tp, fp, fn) in tally.items():
    p = f"{tp / (tp + fp):.2f}" if tp + fp else "-"
    print(f"  {rid:<11} precision {p} ({tp}/{tp + fp})  recall {tp / (tp + fn):.2f} ({tp}/{tp + fn})")
print(f"exemptions applied: {len(exempt)}")
for e in exempt:
    print("   ", *e)
