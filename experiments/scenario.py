"""A browsing sequence through the real watcher, model and policy. Checks
the cases docs/POLICY.md argues for: the same site, good and bad.

The screens are the trial captures in data/auto_labels.jsonl, replayed in
order: a background Safari window parked in Stage Manager has no web
Accessibility tree, so live background capture isn't reliable.

    uv run python experiments/scenario.py
"""
import json, tempfile

from seenot_desktop.decide import make_client
from seenot_desktop.policy import Policy
from seenot_desktop.rules import load_config
from seenot_desktop.state import ScreenState
from seenot_desktop.watcher import Watcher

STEPS = [
    ("a web search", "https://www.google.com/search?q=macos+accessibility+api"),
    ("a Reddit thread opened from it", "https://www.reddit.com/r/programming/comments/1w06vn1/how_we_saved_100_terabytes_of_memory_by/"),
    ("then r/popular (drift)", "https://www.reddit.com/r/popular/"),
    ("a Bilibili calculus course", "https://www.bilibili.com/video/BV1Eb411u7Fw/"),
    ("a Bilibili comedy clip", "https://www.bilibili.com/video/BV1XjhH6fEk4/"),
    ("Bilibili home", "https://www.bilibili.com/"),
    ("a YouTube Short", "https://www.youtube.com/shorts/d99vrWc2m7E"),
    ("swipe to the next Short", "https://www.youtube.com/shorts/hpRIodZG6Dk"),
    ("a stock quote", "https://finance.yahoo.com/quote/NVDA/"),
    ("Apple's 10-K", "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"),
]


def main():
    screens = {}
    for line in open("data/auto_labels.jsonl", encoding="utf-8"):
        r = json.loads(line)
        screens[r["note"].split(" | ")[0].removeprefix("auto: ")] = r["screen"]
    settings, rules = load_config("rules.example.toml")
    events = []
    w = Watcher(Policy(settings, rules, tempfile.mkdtemp()), events.append)
    client = make_client()
    for what, url in STEPS:
        w._judge(client, ScreenState(**screens[url]))
        ev = events[-1]
        r = ev.reading
        seen = f"page={r.page_kind} purpose={r.purpose}" if r else "no model call"
        acts = "; ".join(f"{d.action} {d.rule} ({d.reason})".replace(" ()", "") for d in ev.decisions) or "nothing"
        print(f"{what:<42} {seen:<34} -> {acts}", flush=True)


if __name__ == "__main__":
    main()
