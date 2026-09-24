"""Try a rule on your own recent screens before you trust it.

A new rule, or new wording, has no measured threshold, and Kev-4B's scores
run low, so a guess is either silent or trigger-happy. `rules test` asks the
model this one rule's question on screens you've actually had
(data/judgements.jsonl keeps exactly what the model read), and shows what
the live policy would do on each: the same gate, the rule's hours, the allow
classes (asked too when the log has no score for one), never-here places and
the pages you said were fine. `rules label` records which of those really
are the rule, and `rules tune` picks the threshold from those answers.

Scores go to data/trials.jsonl with the model that gave them, on Kev's scale
as the live app shifts Jev's, and keyed by the exact question asked, so a
rewording or the other model is never tuned on the old scores. A model that
stops answering partway leaves what it scored (`Stopped`): a rerun goes on
from there.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from typesafe_sdk import TypeSafeError

from . import jsonl
from .config import Config
from .decide import Reading, backend, score_shift, shifted
from .policy import OWN_TITLES, OWN_URLS, Policy, gate
from .review import MIN_EACH, _pick, _pr, _score, load_judgements, load_reviews, rule_answers, save_review
from .rules import AllowClass, Rule, Settings, allow_question, in_window, parse_config, question_key, rule_question

HITS = ("pops up", "checks in")  # what a row `does` when the rule would step in


def allow_key(c: AllowClass, lang: str) -> str:
    q = allow_question(c, lang).model_dump(mode="json", exclude_none=True)
    return hashlib.sha1(json.dumps(["allow", q], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


class Stopped(Exception):
    """The model stopped answering partway through a test: the rows scored
    until then, highest first (their scores are saved, so a rerun goes on
    from there), how many screens the test was for, and how many of the
    rows it answered in this run (0: it didn't answer at all, and the rows
    are scores from before)."""

    def __init__(self, error: Exception, rows: list[dict], total: int, answered: int = 0):
        super().__init__(str(error))
        self.error, self.rows, self.total, self.answered = error, rows, total, answered


def recent_screens(data_dir: Path, last: int, judgements: list[dict] | None = None) -> list[dict]:
    """The latest judgement of each distinct thing the model read, newest first."""
    seen, out = set(), []
    for j in reversed(load_judgements(data_dir) if judgements is None else judgements):
        s = j["screen"]
        if not j.get("state") or s.get("url", "").startswith(OWN_URLS) or s.get("window_title", "").startswith(OWN_TITLES):
            continue  # our own review page, logged before it was skipped
        key = json.dumps(j["state"], sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            out.append(j)
            if len(out) >= last:
                break
    return out


def _reading(j: dict, allow: dict[str, float]) -> Reading:
    """The logged answers to the shared questions, as a Reading for the gate."""
    return Reading(
        sensitive=j.get("sensitive", 0.0), page_kind=j.get("page_kind", "other"), page_probs=j.get("page_probs", {}),
        purpose=j.get("purpose", ""), purpose_probs=j.get("purpose_probs", {}), rules=[], latency_ms=0.0,
        allow=allow,
    )


def _trial_rows(data_dir: Path) -> list[dict]:
    return jsonl.read(data_dir / "trials.jsonl")


def home_backend(judgements: list[dict]) -> str:
    """The model a score logged before Qualm recorded which one gave it came
    from: the first one the log names, else Kev, the default."""
    return next((j["backend"] for j in judgements if j.get("backend")), "kev")


def load_trials(data_dir: Path, rule_id: str, qkey: str, model: str, field: str = "q",
                rows: list[dict] | None = None, before: str = "kev") -> dict[str, float]:
    """judgement id -> this rule's score for this question from this model,
    on Kev's scale. A score saved before trials recorded the model (and the
    exact question) is `before`'s, the home's model then: a rule's saved as
    the model answered, an allow class's already shifted."""
    out = {}
    for t in _trial_rows(data_dir) if rows is None else rows:
        if t["rule"] != rule_id or t.get(field, t.get("q")) != qkey or t.get("backend", before) != model:
            continue
        raw = t.get("raw", None if rule_id.startswith("allow:") else t.get("p_hit"))
        out[t["jid"]] = t["p_hit"] if raw is None else shifted(raw, score_shift(model))
    return out


def _save(log, rule_id: str, jid: str, model: str, raw: float, **keys) -> float:
    """One score to trials.jsonl: the model's own answer, and the shifted one the policy compares."""
    p = shifted(raw, score_shift(model))
    log.write(json.dumps({"rule": rule_id, **keys, "jid": jid, "backend": model, "raw": raw, "p_hit": round(p, 4),
                          "at": datetime.now().isoformat(timespec="seconds")}) + "\n")
    return p


def outcome(policy: Policy, rule: Rule, p: float, j: dict, reading: Reading) -> tuple[str, str]:
    """What the live policy would do with this rule on this logged screen,
    and why: ("hit" | "allow" | "skip" | "none", why). What only lasts a
    while (a pause, a snooze, a check-in session) is left out."""
    s = j["screen"]
    url, bundle, title = s.get("url", ""), s.get("bundle_id", ""), s.get("window_title", "")
    if (d := policy.known_place(bundle, url, title, s.get("app", ""), [rule])) is not None:
        return d.action, d.reason
    if not in_window(rule.when, datetime.fromisoformat(j["at"])):
        return "none", f"outside its hours: {'; '.join(rule.when)}"
    if reading.sensitive >= 0.5:
        return "skip", "sensitive page"
    g = gate(rule, p, j["state"], reading, policy.settings, j.get("opened_on_purpose", False), bundle)
    if g is None:
        if p >= rule.threshold and rule.kind == "deny" and rule.target == "content" and reading.page_kind == "feed":
            return "none", "a feed, and this rule judges only what's opened; target=page counts feeds and home pages"
        return "none", ""
    if g[0] == "hit" and (fine := policy.marked_fine(rule.id, url, bundle, title, p)):
        return "allow", fine
    return g


def run(client, data_dir: Path, rule: Rule, settings: Settings, last: int = 100, on_progress=None) -> list[dict]:
    """Ask the model this rule's question on your last `last` distinct screens
    (reusing scores this model already gave the same question), and say
    what the live policy would do on each. Rows, highest score first; none
    when there are no screens yet. Raises Stopped if the model stops answering."""
    judgements = load_judgements(data_dir)
    screens = recent_screens(data_dir, last, judgements)
    if not screens:
        return []
    model, lang, before = getattr(client, "backend", "kev"), settings.lang, home_backend(judgements)
    policy = Policy(settings, [rule], data_dir)
    asked = policy.taught(rule)  # with what "Not this one" and the review page taught it, as the app asks it
    wording, exact = question_key(rule, lang), question_key(asked, lang)
    saved = _trial_rows(data_dir)
    known = load_trials(data_dir, rule.id, exact, model, "asked", saved, before)
    classes = [c for c in settings.allow if c.enabled]
    akeys = {c.id: allow_key(c, lang) for c in classes}
    allow_known = {c.id: load_trials(data_dir, f"allow:{c.id}", akeys[c.id], model, rows=saved, before=before)
                   for c in classes}
    reviews = load_reviews(data_dir)
    hit = "violates" if rule.kind == "deny" else "in_scope"
    rows, answered = [], 0
    with jsonl.appending(data_dir / "trials.jsonl") as log:
        for n, j in enumerate(screens, 1):
            allow = j.get("allow", {}) | {c: v[j["id"]] for c, v in allow_known.items() if j["id"] in v}
            questions = {} if j["id"] in known else {"rule": rule_question(asked, lang)}
            # An allow class added since this screen was judged: the app would ask it now.
            questions |= {f"allow_{c.id}": allow_question(c, lang) for c in classes if c.id not in allow}
            if questions:
                try:
                    a = client.system_one(state=j["state"], questions=questions).answers
                except TypeSafeError as e:
                    rows.sort(key=lambda r: -r["p_hit"])
                    raise Stopped(e, rows, len(screens), answered) from e
                answered += 1
                if "rule" in questions:
                    raw = round(float(a["rule"].probabilities.get(hit, 0.0)), 4)
                    known[j["id"]] = _save(log, rule.id, j["id"], model, raw, q=wording, asked=exact)
                for c in classes:
                    if f"allow_{c.id}" in questions:
                        raw = round(float(a[f"allow_{c.id}"].noul), 4)
                        allow[c.id] = _save(log, f"allow:{c.id}", j["id"], model, raw, q=akeys[c.id])
                log.flush()
            if on_progress:
                on_progress(n, len(screens))
            p = known[j["id"]]
            action, why = outcome(policy, rule, p, j, _reading(j, allow))
            # Pages an allow class would let through, where this rule steps in anyway.
            over = [c.id for c in classes if action == "hit" and c.id in rule.overrides_allow
                    and allow.get(c.id, 0) >= c.threshold]
            does = {"hit": "pops up" if rule.kind == "deny" else "checks in", "none": "nothing"}.get(action, action)
            answer = rule_answers(j, reviews[j["id"]]).get(rule.id) if j["id"] in reviews else None
            s = j["screen"]
            rows.append({
                "id": j["id"], "at": j["at"], "p_hit": round(p, 3), "does": does, "why": why, "overrides": over,
                "app": s.get("app", ""), "title": s.get("window_title", ""), "url": s.get("url", ""),
                "page_kind": j.get("page_kind", ""), "purpose": j.get("purpose", ""),
                "you_said": None if answer is None else "yes" if answer else "no",
            })
    rows.sort(key=lambda r: -r["p_hit"])
    return rows


def run_allow(client, data_dir: Path, c: AllowClass, settings: Settings, last: int = 100, on_progress=None) -> list[dict]:
    """Ask an allow class's yes/no question on your last `last` distinct
    screens. Each row says which rules stepped in there (or would have,
    as far as the log knows) and so would now be let through. Highest first.
    Raises Stopped if the model stops answering."""
    judgements = load_judgements(data_dir)
    screens = recent_screens(data_dir, last, judgements)
    if not screens:
        return []
    model = getattr(client, "backend", "kev")
    q, qkey, key = allow_question(c, settings.lang), allow_key(c, settings.lang), f"allow:{c.id}"
    known = load_trials(data_dir, key, qkey, model, before=home_backend(judgements))
    rows, answered = [], 0
    with jsonl.appending(data_dir / "trials.jsonl") as log:
        for n, j in enumerate(screens, 1):
            if j["id"] in known:
                p = known[j["id"]]
            else:
                try:
                    answer = client.system_one(state=j["state"], questions={"a": q}).answers["a"]
                except TypeSafeError as e:
                    rows.sort(key=lambda r: -r["p"])
                    raise Stopped(e, rows, len(screens), answered) from e
                answered += 1
                p = _save(log, key, j["id"], model, round(float(answer.noul), 4), q=qkey)
                log.flush()
            if on_progress:
                on_progress(n, len(screens))
            s = j["screen"]
            stepped_in = sorted({d["rule"] for d in j.get("decisions", []) if d.get("action") == "intervene" and d.get("rule")})
            rows.append({"id": j["id"], "at": j["at"], "p": round(p, 3), "is_it": p >= c.threshold,
                         "app": s.get("app", ""), "title": s.get("window_title", ""), "url": s.get("url", ""),
                         "stepped_in": stepped_in})
    rows.sort(key=lambda r: -r["p"])
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


def _point(policy: Policy, rule: Rule, p: float, j: dict, reading: Reading) -> float:
    """The score as the threshold sees it: inf where the live policy steps in
    whatever the threshold (the rule's own sites), -inf where it never does
    (an allow class, a feed for a content rule)."""
    if outcome(policy, replace(rule, threshold=1.0), 0.0, j, reading)[0] == "hit":
        return math.inf
    return p if outcome(policy, replace(rule, threshold=1e-9), p, j, reading)[0] == "hit" else -math.inf


def reworded_since(rules: list[Rule], lang: str, judgements: list[dict], rules_path: Path | None) -> dict[str, str]:
    """Rule id -> the last time Qualm knew the rule was worded otherwise than
    now: from judgements that recorded their wording, and from the versions
    of rules.toml `config undo` keeps (each in force until the next was
    saved). A score logged before Qualm recorded the wording is taken as
    the current wording's if it's newer than that."""
    now = {r.id: question_key(r, lang) for r in rules}
    out: dict[str, str] = {}
    for j in judgements:
        for rid, w in j.get("w", {}).items():
            if w != now.get(rid, w):
                out[rid] = max(out.get(rid, ""), j["at"])
    if rules_path is not None and Path(rules_path).exists():
        versions = [*Config(rules_path)._versions(), Path(rules_path)]
        for v, after in zip(versions, versions[1:]):
            try:
                vs, vrules = parse_config(v.read_bytes().decode("utf-8-sig"))
                then = {r.id: question_key(r, vs.lang) for r in vrules}
            except (OSError, ValueError):  # a version that doesn't load: its wording is unknown
                then = {}
            until = datetime.fromtimestamp(after.stat().st_mtime).isoformat(timespec="seconds")
            for rid, w in now.items():
                if then.get(rid) != w:
                    out[rid] = max(out.get(rid, ""), until)
    return out


def tune_all(data_dir: Path, rules: list[Rule], settings: Settings, precision: float = 0.9,
             judgements: list[dict] | None = None, rules_path: Path | None = None,
             log: list[dict] | None = None) -> list[dict]:
    """Per rule, how its threshold does on your answers (from the review page
    and `rules label`), and a threshold that does better, if one does. The
    review page's suggestions and `rules tune` are this.

    A score counts when it's from the rule's current wording and the model
    in use: `rules test`'s, or the live log's. Scores logged before Qualm
    recorded the wording and the model count as the home's model then
    (home_backend) and as the current wording, unless the log or rules.toml's
    kept versions (`rules_path`) say it was worded otherwise since. Answers
    from outside the rule's hours are left out: it never steps in there,
    whatever the threshold. Each point counts what the live policy would do
    there, so a page an allow class lets through is never a hit, whatever
    the threshold. `judgements`: the log, if the caller has read it already
    (the review page reads only the answered ones in full); `log`: the
    judgements to find rewordings and the home's model in, if not those
    (the review page's, answered or not, without what the model read)."""
    model = backend(settings)
    policy = Policy(settings, rules, data_dir)
    reviews = load_reviews(data_dir)
    js = load_judgements(data_dir) if judgements is None else judgements
    answered = [j for j in js if j["id"] in reviews and j.get("state")]
    log = js if log is None else log
    before, reworded = home_backend(log), reworded_since(rules, settings.lang, log, rules_path)
    saved = _trial_rows(data_dir)
    classes = [c for c in settings.allow if c.enabled]
    allow_known = {c.id: load_trials(data_dir, f"allow:{c.id}", allow_key(c, settings.lang), model, rows=saved,
                                     before=before) for c in classes}
    out = []
    for rule in rules:
        wording = question_key(rule, settings.lang)
        fresh = load_trials(data_dir, rule.id, wording, model, rows=saved, before=before)
        elsewhere = {t["jid"] for t in saved if t["rule"] == rule.id and t.get("q") == wording
                     and t.get("backend", before) != model}
        pts = []
        n_test = n_live = 0
        dropped = {"other_wording": 0, "other_model": 0, "not_scored": 0, "outside_hours": 0}
        for j in answered:
            rv = reviews[j["id"]]
            y = rule_answers(j, rv).get(rule.id)
            if y is None:
                continue
            if not in_window(rule.when, datetime.fromisoformat(j["at"])):
                dropped["outside_hours"] += 1
                continue
            if "w" in j:
                logged, by = j["w"].get(rule.id), j.get("backend")
            else:  # logged before Qualm recorded the wording and the model
                logged = wording if j["at"] > reworded.get(rule.id, "") else "reworded since"
                by = before
            if rule.id not in rv.get("rules", {}) and logged not in (None, wording):
                dropped["other_wording"] += 1  # "right" said about what an older wording did
                continue
            if j["id"] in fresh:
                p, n_test = fresh[j["id"]], n_test + 1
            elif rule.id not in j.get("p_hit", {}):
                dropped["other_model" if j["id"] in elsewhere else "not_scored"] += 1
                continue
            elif logged != wording:
                dropped["other_model" if j["id"] in elsewhere else "other_wording"] += 1
                continue
            elif by != model:
                dropped["other_model"] += 1
                continue
            else:
                p, n_live = j["p_hit"][rule.id], n_live + 1
            pts.append((j, p, y))
        scored = []
        for j, p, y in pts:
            allow = j.get("allow", {}) | {c: v[j["id"]] for c, v in allow_known.items() if j["id"] in v}
            scored.append((_point(policy, rule, p, j, _reading(j, allow)), y))
        yes, no = sum(y for _, y in scored), sum(not y for _, y in scored)
        cur_p, cur_r = _pr(scored, rule.threshold)
        row = {"rule": rule.id, "threshold": rule.threshold, "backend": model, "yes": yes, "no": no,
               "scored_with_current_wording": n_test + n_live, "scored_by_test": n_test, "scored_live": n_live,
               "dropped": dropped, "precision": cur_p, "recall": cur_r, "suggested": None,
               "suggested_precision": None, "suggested_recall": None, "keep_current": False,
               "enough_answers": yes >= MIN_EACH and no >= MIN_EACH}
        if row["enough_answers"]:
            t, now = _pick(scored, precision), _score(scored, rule.threshold, precision)
            # Only a threshold that does better on your answers than the one you have.
            if t is not None and t != rule.threshold and _score(scored, t, precision) > now:
                sp, sr = _pr(scored, t)
                row |= {"suggested": t, "suggested_precision": sp, "suggested_recall": sr}
            else:  # nothing better; or every answer is decided whatever the threshold, and it's right
                row["keep_current"] = t is not None or now[0]
        out.append(row)
    return out


def tune(data_dir: Path, rule: Rule, settings: Settings, precision: float = 0.9,
         rules_path: Path | None = None) -> dict:
    """The threshold from your answers, for one rule: see tune_all."""
    return tune_all(data_dir, [rule], settings, precision, rules_path=rules_path)[0]
