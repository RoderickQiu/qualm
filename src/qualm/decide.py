"""Ask the model. What to do about the answers is `policy.py`'s job.

The backend is anything that speaks TypeSafe's System One API: a local Kev
server by default, or TypeSafe's hosted Jev when TYPESAFE_API_KEY is set and
QUALM_BACKEND=jev.

Every score in a Reading is on Kev's scale, because rules.toml's thresholds
and the policy's constants were measured on Kev. Jev ranks pages as well but
answers every question more confidently (HANDOFF, "Hosted Jev"), so each of
its probabilities is moved down by one shift in log-odds before anything
compares it. `raw` keeps the answers as the model gave them.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from typesafe_sdk import TypeSafeClient

from .rules import AllowClass, Rule, build_questions


ENV_FILE = Path(__file__).resolve().parents[2] / ".env"  # the checkout's, git-ignored


def load_env(path: Path = ENV_FILE) -> None:
    """KEY=value lines from .env into the environment; what's already set wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


SHIFT = {"kev": 0.0, "jev": 2.0}  # log-odds; Jev's 1.5-3 all matched Kev on the trial pages


def score_shift() -> float:
    """This backend's shift onto Kev's scale; QUALM_SHIFT overrides."""
    if (s := os.environ.get("QUALM_SHIFT")) is not None:
        return float(s)
    return SHIFT.get(os.environ.get("QUALM_BACKEND", "kev"), 0.0)


def shifted(p: float, shift: float) -> float:
    if not shift:
        return p
    p = min(max(p, 1e-6), 1 - 1e-6)
    return 1 / (1 + math.exp(shift - math.log(p / (1 - p))))


def make_client() -> TypeSafeClient:
    load_env()
    backend = os.environ.get("QUALM_BACKEND", "kev")
    if backend == "jev":
        return TypeSafeClient(model=os.environ.get("QUALM_MODEL", "jev-1.13.0"))
    return TypeSafeClient(
        api_key=os.environ.get("KEV_API_KEY", "local"),
        base_url=os.environ.get("KEV_URL", "http://127.0.0.1:8009"),
        model=os.environ.get("QUALM_MODEL", "kev-latest"),
        # Kev-4B's first call on MLX takes ~25 s; the SDK default of 10 s times
        # out and retries, queueing duplicate work on a one-request-at-a-time server.
        timeout=float(os.environ.get("KEV_TIMEOUT", "30")),
    )


@dataclass
class RuleVerdict:
    rule_id: str
    choice: str
    p_hit: float  # P(violates) for deny rules, P(in_scope) for check-in rules
    probabilities: dict[str, float]


@dataclass
class Reading:
    sensitive: float
    page_kind: str
    page_probs: dict[str, float]
    purpose: str
    purpose_probs: dict[str, float]
    rules: list[RuleVerdict]
    latency_ms: float
    input_tokens: int | None = None
    raw: dict = field(default_factory=dict)
    allow: dict[str, float] = field(default_factory=dict)  # [[allow]] class id -> P(the page is that)
    cached: bool = False  # answered from the watcher's cache, not the model

    def verdict(self, rule_id: str) -> RuleVerdict | None:
        return next((v for v in self.rules if v.rule_id == rule_id), None)


def ask(client: TypeSafeClient, state: dict, rules: list[Rule], lang: str = "zh",
        allow: tuple[AllowClass, ...] = ()) -> Reading:
    t0 = time.perf_counter()
    resp = client.system_one(state=state, questions=build_questions(rules, lang, allow))
    latency = (time.perf_counter() - t0) * 1000
    a, k = resp.answers, score_shift()

    def probs(ans) -> dict[str, float]:
        return {c: shifted(float(p), k) for c, p in ans.probabilities.items()}

    verdicts = []
    for rule in rules:
        ans = probs(a[f"rule_{rule.id}"])
        hit = "violates" if rule.kind == "deny" else "in_scope"
        verdicts.append(RuleVerdict(rule.id, a[f"rule_{rule.id}"].choice, ans.get(hit, 0.0), ans))
    return Reading(
        sensitive=shifted(float(a["sensitive"].noul), k),
        page_kind=a["page_kind"].choice,
        page_probs=probs(a["page_kind"]),
        purpose=a["purpose"].choice,
        purpose_probs=probs(a["purpose"]),
        rules=verdicts,
        latency_ms=latency,
        input_tokens=getattr(resp.usage, "input_tokens", None),
        raw=resp.model_dump(mode="json"),
        allow={c.id: shifted(float(a[f"allow_{c.id}"].noul), k) for c in allow if c.enabled},
    )
