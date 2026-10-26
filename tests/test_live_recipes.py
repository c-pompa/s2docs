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
    assert plan.lines == ["sv_cheats 1", "hs_spawn chicken 4 line 48 1", *recipes.SAFE_TEST_LINES]
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
    # a Pose z IS the feet (getpos_exact): no eye offset is subtracted any more
    plan = compile_plan(env({"action": "spawn", "what": "chicken", "count": 2}), proven, sandbox=False,
                        pose=Pose(0.0, 0.0, 0.0, 0.0))
    assert plan.lines[1:] == ['ent_create chicken {"origin" "-48.0 0.0 0.0"}',
                              'ent_create chicken {"origin" "-96.0 0.0 0.0"}', *recipes.SAFE_TEST_LINES]


@pytest.mark.parametrize("raw", [
    {"action": "bots", "team_size": 5, "me": "ct"},
    {"action": "spawn", "what": "chicken", "count": 16, "formation": "grid", "spacing": 512},
    {"action": "clear", "what": "chicken"},
])
def test_every_line_matches_the_allowlist(raw):
    for line in compile_plan(env(raw), STOCK, sandbox=True).lines:
        assert ALLOWED.fullmatch(line), line


def test_sandbox_load_lines_are_in_the_allowlist():
    assert ALLOWED.fullmatch("spawn_group_load hermes_sandbox")
    for bad in ("spawn_group_load hermes_sandbox; quit", "spawn_group_load evil", "spawn_group_load",
                "spawn_group_load hermes_sandbox extra"):
        assert not ALLOWED.fullmatch(bad), bad


def test_hs_model_lines_are_in_the_allowlist():
    """Spike S2's runtime-precache command: a vmdl path and an optional scale, nothing else."""
    for good in ("hs_model models/props_vehicles/bus01_2.vmdl", "hs_model models/tsr_eefa8331/tsr_eefa8331.vmdl",
                 "hs_model models/props_vehicles/van.vmdl 1.5", "hs_model models/a/b/c.vmdl 2"):
        assert ALLOWED.fullmatch(good), good
    for bad in ("hs_model", "hs_model models/x.vmdl; quit", "hs_model Models/x.vmdl", "hs_model /models/x.vmdl",
                "hs_model models/../x.vmdl", "hs_model models/x.vmdl 1.5 2", "hs_model models/x.mdl",
                "hs_model models/x.vmdl -1", "hs_model models/X.vmdl", "hs_model models/x.vmdl extra"):
        assert not ALLOWED.fullmatch(bad), bad


def test_clear_expects_a_readback_in_both_modes():
    """A clear is judged, not assumed: the removal count, with an ent_find fallback for zero."""
    console = compile_plan(env({"action": "clear", "what": "chicken"}), STOCK, sandbox=False)
    assert console.lines == ["sv_cheats 1", "ent_remove_all chicken"]
    assert console.expect == [{"check": "clear", "classname": "chicken", "count": r"Removed (\d+) chicken\b"}]

    sandbox = compile_plan(env({"action": "clear", "what": "chicken"}), STOCK, sandbox=True)
    assert sandbox.expect == [{"check": "clear", "classname": "chicken", "count": r"\[hs\] cleared (\d+)"}]

    everything = compile_plan(env({"action": "clear"}), STOCK, sandbox=True)
    assert everything.expect == [{"check": "reply", "pattern": r"\[hs\] cleared \d+"}]


# While we test, bots must not kill the player: live on pomps-pc 2026-10-25 a bot killed the user right after
# a test. A round end also wipes everything spawned, so bots killing each other must not end it either.
SAFE = ["bot_ignore_players 1", "bot_dont_shoot 1", "god 1", "mp_ignore_round_win_conditions 1"]


def test_bots_plan_ends_in_safe_test_mode_after_the_restart_settles():
    plan = compile_plan(env({"action": "bots", "team_size": 5, "me": "t"}), STOCK, sandbox=True)
    assert tuple(SAFE) == recipes.SAFE_TEST_LINES
    # god is per player and the restart respawns the player, so the safe lines come after it settles
    restart = plan.lines.index("mp_restartgame 1")
    assert plan.lines[-len(SAFE) - 1:] == ["sleep 3", *SAFE] and restart < len(plan.lines) - len(SAFE) - 1
    assert "sv_cheats 1" in plan.lines[: len(plan.lines) - len(SAFE)], "every safe line is a cheat"


def test_spawn_plan_is_safe_once_and_clear_alone_is_unchanged():
    plan = compile_plan(env({"action": "bots", "t": 2, "ct": 2, "restart": False},
                            {"action": "spawn", "what": "chicken", "count": 4, "follow": True}), STOCK, sandbox=True)
    assert plan.lines[-len(SAFE):] == SAFE and plan.lines.count("god 1") == 1
    cleared = compile_plan(env({"action": "clear"}), STOCK, sandbox=True)
    assert "god 1" not in cleared.lines


def test_safe_lines_are_allowlisted():
    for line in SAFE:
        assert ALLOWED.fullmatch(line), line
