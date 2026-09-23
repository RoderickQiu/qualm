"""Three made-up weeks of Qualm logs, for screenshots and for trying the
dashboard without your own data (which is personal: never put it in a README).

    uv run python experiments/demo_data.py --out /tmp/qualm-demo/data
    uv run qualm review --web --data /tmp/qualm-demo/data --rules rules.example.toml

Same file formats as the app writes. Pop-ups thin out over the weeks and
cluster in the evening, you go back more often as time goes on, check-ins
mostly end when you said, and a focus session is running now.
"""
import argparse
import json
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PAGES = {
    "shortvideo": [("Google Chrome", "com.google.Chrome", "Try Not To Laugh Challenge #shorts - YouTube", "https://www.youtube.com/shorts/{id}"),
                   ("Safari", "com.apple.Safari", "Cat vs cucumber, part 4 | TikTok", "https://www.tiktok.com/@catclips/video/{id}")],
    "feeds": [("Google Chrome", "com.google.Chrome", "YouTube", "https://www.youtube.com/"),
              ("Google Chrome", "com.google.Chrome", "popular", "https://www.reddit.com/r/popular/"),
              ("Safari", "com.apple.Safari", "Home / X", "https://x.com/home"),
              ("Google Chrome", "com.google.Chrome", "小红书 - 你的生活兴趣社区", "https://www.xiaohongshu.com/explore")],
    "livestream": [("Google Chrome", "com.google.Chrome", "speedrunner_live - Twitch", "https://www.twitch.tv/speedrunner_live")],
    "videos": [("Google Chrome", "com.google.Chrome", "Top 10 Movie Fails of the Year", "https://www.bilibili.com/video/BV1{id}"),
               ("Safari", "com.apple.Safari", "Every Bake Off Disaster Ranked - YouTube", "https://www.youtube.com/watch?v={id}")],
    "social": [("Google Chrome", "com.google.Chrome", "What's the best mechanical keyboard under $100? : r/MechanicalKeyboards",
                "https://www.reddit.com/r/MechanicalKeyboards/comments/{id}"),
               ("Safari", "com.apple.Safari", "Launch day thread / X", "https://x.com/someone/status/{id}")],
}
QUIET = [("Google Chrome", "com.google.Chrome", "numpy.linalg.solve — NumPy Manual", "https://numpy.org/doc/stable/reference/generated/numpy.linalg.solve.html", "work", "learn"),
         ("Cursor", "com.todesktop.230313mzl4w4u92", "model.py — pitch-deck", "", "work", "task"),
         ("Google Chrome", "com.google.Chrome", "how to center a div - Google Search", "https://www.google.com/search?q=center+a+div", "search", "task"),
         ("Safari", "com.apple.Safari", "Attention Is All You Need", "https://arxiv.org/abs/1706.03762", "single_item", "learn"),
         ("Google Chrome", "com.google.Chrome", "MIT 18.06 Linear Algebra, Lecture 3 - YouTube", "https://www.youtube.com/watch?v=lecture3", "single_item", "learn")]
REASONS = ["checking a recipe a friend sent", "the band's new single", "looking up the keyboard I'm buying",
           "a video from my sister", "a tutorial linked in the docs", "break after the meeting", "reading the launch thread"]
FOCUS = [("write the pitch deck", 50), ("finish problem set 4", 90), ("reply to recruiters", 25), ("read the attention paper", 50)]
THRESHOLDS = {"shortvideo": 0.15, "feeds": 0.3, "livestream": 0.5, "videos": 0.25, "social": 0.3}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    judgements, decisions, reviews, history = [], [], [], {}
    today = date.today()
    now = datetime.now()

    def jid():
        return "".join(rnd.choice("0123456789abcdef") for _ in range(8))

    for back in range(a.days - 1, -1, -1):
        day = today - timedelta(days=back)
        week = (a.days - 1 - back) // 7
        n = max(1, round(rnd.gauss((9, 6, 4)[min(week, 2)], 1.5)))
        back_share = (0.45, 0.6, 0.72)[min(week, 2)]
        usage = {"videos": {"seconds": 0.0, "sessions": 0}, "social": {"seconds": 0.0, "sessions": 0}}
        history[day.isoformat()] = usage
        # A focus session on most weekdays.
        if day.weekday() < 5 and rnd.random() < 0.7 and back > 0:
            intent, minutes = rnd.choice(FOCUS)
            start = datetime.combine(day, datetime.min.time()) + timedelta(hours=rnd.choice((9, 10, 14, 15)), minutes=rnd.randint(0, 50))
            decisions.append({"at": start.isoformat(timespec="seconds"), "type": "focus", "intent": intent, "minutes": minutes})
            if rnd.random() < 0.3:
                end = start + timedelta(minutes=rnd.randint(10, minutes - 5))
                decisions.append({"at": end.isoformat(timespec="seconds"), "type": "focus_end", "intent": intent,
                                  "minutes": round((end - start).total_seconds() / 60, 1)})
        for _ in range(n):
            hour = rnd.choices(range(24), weights=[3, 2, 1, 0, 0, 0, 0, 1, 2, 3, 3, 3, 4, 4, 3, 3, 3, 4, 5, 6, 8, 9, 9, 6])[0]
            at = datetime.combine(day, datetime.min.time()) + timedelta(hours=hour, minutes=rnd.randint(0, 59), seconds=rnd.randint(0, 59))
            if at > now:
                continue
            rule = rnd.choices(list(PAGES), weights=[5, 6, 1, 3, 4])[0]
            app, bid, title, url = rnd.choice(PAGES[rule])
            url = url.format(id=jid())
            p = {r: round(rnd.uniform(0.02, 0.12), 3) for r in PAGES}
            p[rule] = round(rnd.uniform(THRESHOLDS[rule] * 1.2, 0.95), 3)
            screen = {"app": app, "bundle_id": bid, "window_title": title, "url": url, "headings": [], "text": [], "ax_trusted": True, "frame": []}
            pattern = rule in ("shortvideo", "feeds") and rnd.random() < 0.4
            reason = "matches URL pattern" if pattern else f"p_hit {p[rule]:.2f} >= {THRESHOLDS[rule]:.2f}"
            did = jid() + "abcd"
            j = {"id": jid(), "at": at.isoformat(timespec="seconds"), "screen": screen,
                 "decisions": [{"action": "intervene", "rule": rule, "reason": reason}], "thresholds": THRESHOLDS,
                 "came_from": None, "opened_on_purpose": False, "state": {"app": app, "window_title": title, "url": url},
                 "page_kind": "feed" if rule == "feeds" else "single_item", "purpose": "entertain",
                 "page_probs": {"feed": 0.8 if rule == "feeds" else 0.1, "single_item": 0.1 if rule == "feeds" else 0.8},
                 "purpose_probs": {"entertain": 0.9, "task": 0.06, "learn": 0.04}, "sensitive": 0.02, "p_hit": p,
                 "answers": {}, "latency_ms": rnd.randint(380, 900), "cached": False, "allow": {"shopping": 0.05, "music": 0.03}}
            judgements.append(j)
            check_in = rule in usage
            decisions.append({"at": j["at"], "type": "intervention", "id": did, "rule": rule, "reason": reason, "screen": screen,
                              "page_kind": j["page_kind"], "purpose": "entertain", "p_hit": p,
                              **({"panel": "check_in"} if check_in else {})})
            if check_in and rnd.random() > back_share - 0.15:
                # Checked in: what for, how long; mostly ended when you said.
                said = rnd.choices((5, 15, 30), weights=(5, 3, 1))[0]
                why, start = rnd.choice(REASONS), at + timedelta(seconds=rnd.randint(4, 25))
                extended = int(rnd.random() < 0.2)
                stayed = round(min(said + 5 * extended, rnd.uniform(0.5, 1.1) * (said + 5 * extended)), 1)
                usage[rule]["seconds"] += stayed * 60
                usage[rule]["sessions"] += 1
                until = start + timedelta(minutes=said)
                decisions += [
                    {"at": start.isoformat(timespec="seconds"), "type": "session", "event": "start", "id": did, "rule": rule,
                     "minutes": said, "for": why, "until": round(until.timestamp())},
                    {"at": start.isoformat(timespec="seconds"), "type": "response", "id": did, "rule": rule, "response": "session",
                     "reason": why, "minutes": said}]
                end = start + timedelta(minutes=stayed)
                if end > now:
                    continue  # still running
                if extended:
                    decisions.append({"at": until.isoformat(timespec="seconds"), "type": "session", "event": "extend", "id": did,
                                      "rule": rule, "minutes": 5, "until": round(until.timestamp()) + 300})
                decisions.append({"at": end.isoformat(timespec="seconds"), "type": "session", "event": "end", "id": did, "rule": rule,
                                  "how": "done" if extended or stayed >= said else "time", "for": why,
                                  "minutes": said + 5 * extended, "stayed": stayed, "extended": extended})
                continue
            r = rnd.random()
            answer = "back" if r < back_share else "snooze" if r < back_share + 0.18 else "fine" if r < back_share + 0.25 else "never" if r < back_share + 0.27 else None
            if answer:
                e = {"at": (at + timedelta(seconds=rnd.randint(3, 20))).isoformat(timespec="seconds"), "type": "response", "id": did,
                     "rule": rule, "response": answer}
                if answer == "snooze":
                    e |= {"reason": rnd.choice(REASONS), "minutes": 10}
                decisions.append(e)
            if back > 2 and rnd.random() < 0.5:
                reviews.append({"id": j["id"], "rules": {}, "verdict": "right" if answer != "fine" else "should_not_block",
                                "at": (at + timedelta(days=1)).isoformat(timespec="seconds")})
        for _ in range(rnd.randint(30, 60)):
            app, bid, title, url, kind, purpose = rnd.choice(QUIET)
            at = datetime.combine(day, datetime.min.time()) + timedelta(hours=rnd.randint(8, 23), minutes=rnd.randint(0, 59))
            if at > now:
                continue
            judgements.append({"id": jid(), "at": at.isoformat(timespec="seconds"),
                               "screen": {"app": app, "bundle_id": bid, "window_title": title, "url": url},
                               "decisions": [], "thresholds": THRESHOLDS, "came_from": None, "opened_on_purpose": False,
                               "state": {"app": app, "window_title": title, "url": url}, "page_kind": kind, "purpose": purpose,
                               "page_probs": {kind: 0.8}, "purpose_probs": {purpose: 0.8}, "sensitive": 0.01,
                               "p_hit": {r: round(rnd.uniform(0.01, 0.1), 3) for r in PAGES}, "answers": {},
                               "latency_ms": rnd.randint(380, 900), "cached": False, "allow": {}})
    judgements.sort(key=lambda j: j["at"])
    decisions.sort(key=lambda e: e["at"])
    # A focus session running now, and an answer to last week's question.
    intent, minutes = "write the pitch deck", 50
    started = time.time() - 18 * 60
    decisions.append({"at": datetime.fromtimestamp(started).isoformat(timespec="seconds"), "type": "focus", "intent": intent, "minutes": minutes})
    (out / "session.json").write_text(json.dumps({"paused_until": 0.0, "focus": {"intent": intent, "started": started, "until": started + minutes * 60}}))
    # Today: a check-in that ended when you said, and one running now.
    for rule, why, said, ago, stayed in (("social", "reading the launch thread", 5, 95, 4.2),
                                         ("videos", "the keynote everyone's talking about", 15, 6, None)):
        start = now - timedelta(minutes=ago)
        sid = jid() + "cafe"
        decisions += [{"at": start.isoformat(timespec="seconds"), "type": "session", "event": "start", "id": sid, "rule": rule,
                       "minutes": said, "for": why, "until": round((start + timedelta(minutes=said)).timestamp())},
                      {"at": start.isoformat(timespec="seconds"), "type": "response", "id": sid, "rule": rule, "response": "session",
                       "reason": why, "minutes": said}]
        if stayed is not None:
            decisions.append({"at": (start + timedelta(minutes=said)).isoformat(timespec="seconds"), "type": "session", "event": "end",
                              "id": sid, "rule": rule, "how": "time", "for": why, "minutes": said, "stayed": stayed, "extended": 0})
        usage[rule]["seconds"] += (stayed or ago) * 60
        usage[rule]["sessions"] += 1
    decisions.sort(key=lambda e: e["at"])
    monday = today - timedelta(days=today.weekday())
    refl = [{"week": (monday - timedelta(weeks=w)).isoformat(), "answer": ans, "note": "", "at": now.isoformat(timespec="seconds")}
            for w, ans in ((2, "not_really"), (1, "mixed"))]
    for name, rows in (("judgements", judgements), ("decisions", decisions), ("reviews", reviews), ("reflections", refl)):
        (out / f"{name}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (out / "usage_history.json").write_text(json.dumps(history))
    (out / "usage.json").write_text(json.dumps({"day": today.isoformat(), "counts": history[today.isoformat()]}))
    print(f"{len(judgements)} judgements, {sum(e['type'] == 'intervention' for e in decisions)} pop-ups -> {out}")


if __name__ == "__main__":
    main()
