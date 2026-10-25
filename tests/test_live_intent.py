import pytest
from pydantic import ValidationError

from s2docs.live.intent import BotsIntent, IntentEnvelope, SpawnIntent


def test_five_v_five_me_on_t_counts():
    i = BotsIntent(team_size=5, me="t")
    assert i.counts() == (4, 5)


def test_exact_counts_and_no_bots():
    assert BotsIntent(t=0, ct=0).counts() == (0, 0)
    assert BotsIntent(t=2, ct=3).counts() == (2, 3)


def test_bots_needs_one_way_of_counting():
    with pytest.raises(ValidationError):
        BotsIntent()
    with pytest.raises(ValidationError):
        BotsIntent(team_size=5, t=4)


def test_spawn_rejects_console_injection_in_id():
    with pytest.raises(ValidationError):
        SpawnIntent(what="chicken;quit", count=1)


def test_envelope_discriminates_and_bounds():
    env = IntentEnvelope.model_validate(
        {"intents": [{"action": "spawn", "what": "chicken", "count": 4, "formation": "line", "follow": True}]}
    )
    assert isinstance(env.intents[0], SpawnIntent) and env.intents[0].follow is True
    with pytest.raises(ValidationError):
        IntentEnvelope.model_validate({"intents": []})
    with pytest.raises(ValidationError):
        IntentEnvelope.model_validate({"intents": [{"action": "spawn", "what": "chicken", "count": 99}]})
