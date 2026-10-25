import pytest

from s2docs.live import recipes, spawnables
from s2docs.live.intent import IntentEnvelope
from s2docs.live.recipes import ALLOWED, LiveUnavailable, Pose, compile_plan

STOCK = spawnables.stock()


def env(*intents, say=None):
    return IntentEnvelope.model_validate({"intents": list(intents), "say": say})


def test_five_v_five_me_on_t():
    plan = compile_plan(env({"action": "bots", "team_size": 5, "me": "t"}), STOCK, sandbox=False)
    assert plan.lines.count("bot_add_t") == 4 and plan.lines.count("bot_add_ct") == 5
    assert recipes.ME_TEAM_LINE.format(num=2) in plan.lines
    assert plan.lines.index("bot_kick") < plan.lines.index("bot_add_t")
    assert plan.expect == [{"check": "bot_count", "total": 9}]


def test_no_bots_still_kicks():
    plan = compile_plan(env({"action": "bots", "t": 0, "ct": 0, "restart": False}), STOCK, sandbox=False)
    assert "bot_kick" in plan.lines and "bot_add_t" not in plan.lines


def test_follow_chickens_need_sandbox():
    e = env({"action": "spawn", "what": "chicken", "count": 4, "formation": "line", "follow": True})
    with pytest.raises(LiveUnavailable):
        compile_plan(e, STOCK, sandbox=False)
    plan = compile_plan(e, STOCK, sandbox=True)
    assert plan.lines == ["sv_cheats 1", "hs_spawn chicken 4 line 48 1"]
    assert plan.expect == [{"check": "reply", "pattern": r"\[hs\] spawned 4 chicken"}]


def test_unknown_spawnable_is_refused():
    with pytest.raises(LiveUnavailable, match="unknown spawnable"):
        compile_plan(env({"action": "spawn", "what": "dragon"}), STOCK, sandbox=True)


def test_bots_then_spawn_waits_for_restart():
    e = env({"action": "bots", "t": 1, "ct": 1}, {"action": "spawn", "what": "chicken"})
    plan = compile_plan(e, STOCK, sandbox=True)
    assert (plan.lines.index("mp_restartgame 1") < plan.lines.index("sleep 3")
            < plan.lines.index("hs_spawn chicken 1 line 48 0"))


def test_say_is_sanitised_and_never_a_command():
    plan = compile_plan(env({"action": "clear"}, say='!h ok; quit "x"'), STOCK, sandbox=True)
    assert plan.lines[-1] == "say Hermes: h ok quit x"


def test_console_spawn_uses_pose_when_proven(monkeypatch):
    proven = {"chicken": spawnables.Spawnable("chicken", "Chicken", "hs_tpl_chicken", "chicken", None, 24.0, True)}
    plan = compile_plan(env({"action": "spawn", "what": "chicken", "count": 2}), proven, sandbox=False,
                        pose=Pose(0.0, 0.0, 64.0, 0.0))
    assert plan.lines[1:] == ['ent_create chicken {"origin" "-48.0 0.0 0.0"}',
                              'ent_create chicken {"origin" "-96.0 0.0 0.0"}']


@pytest.mark.parametrize("raw", [
    {"action": "bots", "team_size": 5, "me": "ct"},
    {"action": "spawn", "what": "chicken", "count": 16, "formation": "grid", "spacing": 512},
    {"action": "clear", "what": "chicken"},
])
def test_every_line_matches_the_allowlist(raw):
    for line in compile_plan(env(raw), STOCK, sandbox=True).lines:
        assert ALLOWED.fullmatch(line), line
