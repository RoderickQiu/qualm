"""Trial dataset: load each page of manifest.PAGES in a background Safari
window, capture it by pid, and write a record in the `label` format.

    uv run python experiments/collect.py [--out data/auto_labels.jsonl]
"""
import argparse, json, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bg import app_by_bundle, capture_app
from manifest import PAGES

SAFARI = "com.apple.Safari"


def nav(url):
    subprocess.run(["osascript", "-e", f'tell application "Safari" to set URL of front document to "{url}"'], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/auto_labels.jsonl")
    ap.add_argument("--wait", type=float, default=9.0)
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {json.loads(l)["note"].split(" | ")[0] for l in out.open()} if out.exists() else set()
    for page in PAGES:
        target = page["target"]
        if f"auto: {target}" in done:
            continue
        if target.startswith("app:"):
            bundle = target[4:]
            if app_by_bundle(bundle) is None:
                print("skip (not running)", target, flush=True)
                continue
            s = capture_app(bundle)
        else:
            nav(target)
            time.sleep(args.wait)
            s = capture_app(SAFARI)
            if len(s.text) + len(s.headings) < 3:  # still loading
                time.sleep(6)
                s = capture_app(SAFARI)
        rec = {"captured_at": datetime.now(timezone.utc).isoformat(), "screen": s.as_record(),
               "labels": page["labels"], "note": f"auto: {target}" + (f" | {page['note']}" if page["note"] else "")}
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"{len(s.headings):2d}h {len(s.text):2d}t  {s.window_title[:50]!r:52} {target[:70]}", flush=True)


if __name__ == "__main__":
    main()
