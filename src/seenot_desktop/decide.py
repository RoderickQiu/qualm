"""Ask the model. What to do about the answers is `policy.py`'s job.

The backend is anything that speaks TypeSafe's System One API: a local Kev
server by default, or TypeSafe's hosted Jev when TYPESAFE_API_KEY is set and
SEENOT_BACKEND=jev.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from typesafe_sdk import TypeSafeClient

from .rules import AllowClass, Rule, build_questions


def make_client() -> TypeSafeClient:
    backend = os.environ.get("SEENOT_BACKEND", "kev")
    if backend == "jev":
        return TypeSafeClient(model=os.environ.get("SEENOT_MODEL", "jev-1.13.0"))
    return TypeSafeClient(
        api_key=os.environ.get("KEV_API_KEY", "local"),
        base_url=os.environ.get("KEV_URL", "http://127.0.0.1:8009"),
        model=os.environ.get("SEENOT_MODEL", "kev-latest"),
        # Kev-4B's first call on MLX takes ~25 s; the SDK default of 10 s times
        # out and retries, queueing duplicate work on a one-request-at-a-time server.
        timeout=float(os.environ.get("KEV_TIMEOUT", "30")),
    )


@dataclass
class RuleVerdict:
    rule_id: str
    choice: str
    p_hit: float  # P(violates) for deny rules, P(in_scope) for time caps
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

    def verdict(self, rule_id: str) -> RuleVerdict | None:
        return next((v for v in self.rules if v.rule_id == rule_id), None)


def ask(client: TypeSafeClient, state: dict, rules: list[Rule], lang: str = "zh",
        allow: tuple[AllowClass, ...] = ()) -> Reading:
    t0 = time.perf_counter()
    resp = client.system_one(state=state, questions=build_questions(rules, lang, allow))
    latency = (time.perf_counter() - t0) * 1000
    a = resp.answers
    verdicts = []
    for rule in rules:
        ans = a[f"rule_{rule.id}"]
        hit = "violates" if rule.kind == "deny" else "in_scope"
        verdicts.append(
            RuleVerdict(rule.id, ans.choice, float(ans.probabilities.get(hit, 0.0)), dict(ans.probabilities))
        )
    return Reading(
        sensitive=float(a["sensitive"].noul),
        page_kind=a["page_kind"].choice,
        page_probs=dict(a["page_kind"].probabilities),
        purpose=a["purpose"].choice,
        purpose_probs=dict(a["purpose"].probabilities),
        rules=verdicts,
        latency_ms=latency,
        input_tokens=getattr(resp.usage, "input_tokens", None),
        raw=resp.model_dump(mode="json"),
        allow={c.id: float(a[f"allow_{c.id}"].noul) for c in allow},
    )
