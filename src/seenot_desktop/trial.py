"""Try a rule on your own recent screens before you trust it.

A new rule, or new wording, has no measured threshold, and Kev-4B's scores
run low, so a guess is either silent or trigger-happy. `rules test` asks the
model this one rule's question on screens you've actually had
(data/judgements.jsonl keeps exactly what the model read), and shows what
the rule would do on each, through the same gate the live policy uses.
`rules label` records which of those really are the rule, and `rules tune`
picks the threshold from those answers.

Scores go to data/trials.jsonl, keyed by the exact question asked, so a
rewording is never tuned on the old wording's scores.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .decide import Reading
from .policy import OWN_TITLES, OWN_URLS, gate
from .review import MIN_EACH, _pick, _pr, load_judgements, load_reviews, rule_answers, save_review
from .rules import Rule, Settings, rule_question


def question_key(rule: Rule, lang: str) -> str:
    q = rule_question(rule, lang).model_dump(mode="json", exclude_none=True)
    return hashlib.sha1(json.dumps([rule.kind, q], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def recent_screens(data_dir: Path, last: int) -> list[dict]:
    """The latest judgement of each distinct thing the model read, newest first."""
    seen, out = set(), []
    for j in reversed(load_judgements(data_dir)):
        s = j["screen"]
        if not j.get("state") or s.get("url", "").startswith(OWN_URLS) or s.get("window_title", "").startswith(OWN_TITLES):
            continue  # SeeNot's own review page, logged before it was skipped
        key = json.dumps(j["state"], sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            out.append(j)
            if len(out) >= last:
                break
    return out


def _reading(j: dict) -> Reading:
    """The logged answers to the shared questions, as a Reading for the gate."""
    return Reading(
        sensitive=j.get("sensitive", 0.0), page_kind=j.get("page_kind", "other"), page_probs=j.get("page_probs", {}),
        purpose=j.get("purpose", ""), purpose_probs=j.get("purpose_probs", {}), rules=[], latency_ms=0.0,
        allow=j.get("allow", {}),
    )


def load_trials(data_dir: Path, rule_id: str, qkey: str) -> dict[str, float]:
    """judgement id -> this rule's score, for this exact question."""
    path = data_dir / "trials.jsonl"
    out = {}
    if path.exists():
        for line in path.open(encoding="utf-8"):
            t = json.loads(line)
            if t["rule"] == rule_id and t["q"] == qkey:
                out[t["jid"]] = t["p_hit"]
    return out


def run(client, data_dir: Path, rule: Rule, settings: Settings, last: int = 100, on_progress=None) -> list[dict]:
    """Ask the model this rule's question on your last `last` distinct screens
    (reusing scores already asked with the same wording). Rows, highest score first."""
    qkey = question_key(rule, settings.lang)
    known = load_trials(data_dir, rule.id, qkey)
    reviews = load_reviews(data_dir)
    hit = "violates" if rule.kind == "deny" else "in_scope"
    screens = recent_screens(data_dir, last)
    rows = []
    with (data_dir / "trials.jsonl").open("a", encoding="utf-8") as log:
        for n, j in enumerate(screens, 1):
            if j["id"] in known:
                p = known[j["id"]]
            else:
                resp = client.system_one(state=j["state"], questions={"rule": rule_question(rule, settings.lang)})
                p = float(resp.answers["rule"].probabilities.get(hit, 0.0))
                log.write(json.dumps({"rule": rule.id, "q": qkey, "jid": j["id"], "p_hit": round(p, 4),
                                      "at": datetime.now().isoformat(timespec="seconds")}) + "\n")
                log.flush()
            if on_progress:
                on_progress(n, len(screens))
            reading = _reading(j)
            g = None if reading.sensitive >= 0.5 else gate(rule, p, j["state"], reading, settings, j.get("opened_on_purpose", False))
            action, why = g or ("none", "")
            does = {"hit": "pops up" if rule.kind == "deny" or not settings.budgets else "counts", "none": "nothing"}.get(action, action)
            answer = rule_answers(j, reviews[j["id"]]).get(rule.id) if j["id"] in reviews else None
            s = j["screen"]
            rows.append({
                "id": j["id"], "at": j["at"], "p_hit": round(p, 3), "does": does, "why": why,
                "app": s.get("app", ""), "title": s.get("window_title", ""), "url": s.get("url", ""),
                "page_kind": j.get("page_kind", ""), "purpose": j.get("purpose", ""),
                "you_said": None if answer is None else "yes" if answer else "no",
            })
    rows.sort(key=lambda r: -r["p_hit"])
    return rows


def label(data_dir: Path, rule_id: str, yes: list[str], no: list[str]) -> int:
    """Is judgement X this rule? Saved as review answers, which the review page,
    `eval --reviews` and `export` use too."""
    have = {j["id"] for j in load_judgements(data_dir)}
    missing = [j for j in [*yes, *no] if j not in have]
    if missing:
        raise ValueError(f"no judgements with ids {missing}; ids come from `rules test` or `review`")
    for jid in yes:
        save_review(data_dir, jid, rules={rule_id: "yes"})
    for jid in no:
        save_review(data_dir, jid, rules={rule_id: "no"})
    return len(yes) + len(no)


def tune(data_dir: Path, rule: Rule, lang: str, precision: float = 0.9) -> dict:
    """The threshold from your answers: fresh `rules test` scores for the
    current wording where there are any, the live log's scores otherwise."""
    trials = load_trials(data_dir, rule.id, question_key(rule, lang))
    reviews = load_reviews(data_dir)
    pts, fresh = [], 0
    for j in load_judgements(data_dir):
        if j["id"] not in reviews:
            continue
        y = rule_answers(j, reviews[j["id"]]).get(rule.id)
        if y is None:
            continue
        if j["id"] in trials:
            pts.append((trials[j["id"]], y))
            fresh += 1
        elif rule.id in j.get("p_hit", {}):
            pts.append((j["p_hit"][rule.id], y))
    yes, no = sum(y for _, y in pts), sum(not y for _, y in pts)
    cur_p, cur_r = _pr(pts, rule.threshold)
    out = {"rule": rule.id, "threshold": rule.threshold, "yes": yes, "no": no, "scored_with_current_wording": fresh,
           "precision": cur_p, "recall": cur_r, "suggested": None, "suggested_precision": None, "suggested_recall": None}
    if yes >= MIN_EACH and no >= MIN_EACH:
        t = _pick(pts, precision)
        if t is not None:
            sp, sr = _pr(pts, t)
            out |= {"suggested": t, "suggested_precision": sp, "suggested_recall": sr}
    return out
