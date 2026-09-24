from types import SimpleNamespace as NS

import pytest

from qualm.decide import ask, shifted
from qualm.rules import Rule


def choice(**probs):
    return NS(choice=max(probs, key=probs.get), probabilities=probs)


class FakeClient:
    def __init__(self, backend="kev"):
        self.backend = backend

    def system_one(self, state, questions):
        return NS(
            answers={
                "sensitive": NS(noul=0.95),
                "page_kind": choice(feed=0.9, single_item=0.1),
                "purpose": choice(entertain=0.99, learn=0.01),
                "rule_feeds": choice(violates=0.8, safe=0.2),
            },
            usage=NS(input_tokens=10),
            model_dump=lambda mode: {"sensitive": 0.95},
        )


RULE = Rule(id="feeds", kind="deny", description="feeds", threshold=0.3)


def test_shift_keeps_order_and_zero_is_identity():
    assert shifted(0.37, 0.0) == 0.37
    assert shifted(0.5, 2.0) == pytest.approx(0.1192, abs=1e-4)
    assert shifted(0.2, 2.0) < shifted(0.8, 2.0) < 0.8


def test_kev_scores_pass_through(monkeypatch):
    monkeypatch.delenv("QUALM_SHIFT", raising=False)
    r = ask(FakeClient("kev"), {}, [RULE], "en")
    assert (r.sensitive, r.rules[0].p_hit) == (0.95, 0.8)


def test_jev_scores_move_onto_kevs_scale(monkeypatch):
    monkeypatch.delenv("QUALM_SHIFT", raising=False)
    r = ask(FakeClient("jev"), {}, [RULE], "en")
    assert r.sensitive == pytest.approx(shifted(0.95, 2.0)) and r.sensitive < 0.75
    assert r.rules[0].p_hit == pytest.approx(shifted(0.8, 2.0))
    assert (r.page_kind, r.purpose) == ("feed", "entertain")  # choices are the model's own
    assert r.raw == {"sensitive": 0.95}


def test_qualm_shift_overrides(monkeypatch):
    monkeypatch.setenv("QUALM_SHIFT", "0")
    assert ask(FakeClient("jev"), {}, [RULE], "en").sensitive == 0.95


def test_backend_from_settings_and_the_environment_wins(monkeypatch):
    from qualm.decide import backend
    from qualm.rules import Settings

    monkeypatch.delenv("QUALM_BACKEND", raising=False)
    assert backend(Settings()) == "kev" and backend(Settings(backend="jev")) == "jev"
    monkeypatch.setenv("QUALM_BACKEND", "kev")
    assert backend(Settings(backend="jev")) == "kev"


def test_hosted_with_no_key_says_how_this_terminal_saves_one(monkeypatch):
    from qualm import agent, decide
    from qualm.rules import Settings

    q = "QUALM_HOME=/tmp/second uv run --project /src/qualm qualm"  # another Qualm folder: a bare qualm is the main one
    monkeypatch.delenv("QUALM_BACKEND", raising=False)
    monkeypatch.setattr(decide, "api_key", lambda: None)
    monkeypatch.setattr(agent, "command", lambda: q)
    with pytest.raises(RuntimeError) as e:
        decide.make_client(Settings(backend="jev"))
    assert str(e.value) == f"no TypeSafe API key: `{q} setup` stores one in the keychain"
