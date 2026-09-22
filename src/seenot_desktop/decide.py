"""Ask the model, then combine the typed answers into an action.

The backend is anything that speaks TypeSafe's System One API: a local Kev
server by default, or TypeSafe's hosted Jev when TYPESAFE_API_KEY is set and
SEENOT_BACKEND=jev.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from typesafe_sdk import TypeSafeClient

from .rules import Rule, build_questions

# Act only when the model is sure; below LOW, ignore; in between, wait for a
# second agreeing reading. Tune these from `seenot-desktop eval`, not by feel.
HIGH = 0.85
LOW = 0.5


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
    rules: list[RuleVerdict]
    latency_ms: float
    input_tokens: int | None = None
    raw: dict = field(default_factory=dict)


def ask(client: TypeSafeClient, state: dict, rules: list[Rule], lang: str = "zh") -> Reading:
    t0 = time.perf_counter()
    resp = client.system_one(state=state, questions=build_questions(rules, lang))
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
        rules=verdicts,
        latency_ms=latency,
        input_tokens=getattr(resp.usage, "input_tokens", None),
        raw=resp.model_dump(mode="json"),
    )


class Gate:
    """Turns readings into actions. Keeps the last reading per rule so the
    grey zone needs two agreeing readings in a row before it nudges."""

    def __init__(self, high: float = HIGH, low: float = LOW):
        self.high, self.low = high, low
        self._last: dict[str, float] = {}

    def actions(self, reading: Reading, rules: list[Rule]) -> list[tuple[str, str]]:
        if reading.sensitive >= 0.5:
            self._last.clear()
            return [("skip", "sensitive page")]
        out = []
        by_id = {r.id: r for r in rules}
        for v in reading.rules:
            rule = by_id[v.rule_id]
            prev = self._last.get(v.rule_id, 0.0)
            self._last[v.rule_id] = v.p_hit
            # A feed full of candidates is not a violation of a content rule;
            # the Android prompt spent a page of prose saying this.
            if rule.kind == "deny" and reading.page_kind == "feed":
                continue
            if v.p_hit >= self.high:
                out.append(("intervene", rule.id))
            elif v.p_hit >= self.low and prev >= self.low:
                out.append(("nudge", rule.id))
        return out
