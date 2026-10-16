"""The verifiable Source 2 reward: deterministic, model-free checkers (plan task T17).

`grade()` is the training signal for the small local specialist model (cpompa-ai-cloud lab5 GRPO).
It must be an *exact ground-truth* grader like lab5's `rfs_grading.grade_answer` — never a model
call, never a network call, never random. Each checker delegates to a validator this repo already
ships and that the CLI/MCP already expose:

    s2docs_lookup  the answer resolves, in the index, to the expected identifier
    s2docs_cfg     the answer validates as console commands against the indexed convars/commands
    s2docs_script  the answer type-checks as a cs_script against point_script.d.ts (tsc)
    exact          the answer equals the expected string (normalised)

An unknown checker raises: a silent default would score every problem as passed, and a reward that
cannot be inspected is a preference model in disguise.
"""

from __future__ import annotations

import pytest

from s2docs.build.validate_script import ToolchainMissing, find_tsc
from s2docs.db.repository import Repository
from s2docs.learning import reward


def _have_tsc() -> bool:
    try:
        find_tsc()
        return True
    except ToolchainMissing:
        return False


needs_tsc = pytest.mark.skipif(not _have_tsc(), reason="TypeScript not installed (npm ci --prefix tools/tsc)")

# A clean cs_script and one that invents an API: the same pair test_build.py uses.
GOOD_SCRIPT = """import { Instance } from "cs_script/point_script";
Instance.OnPlayerJump((event) => { Instance.Msg(event.player.GetHealth()); });
"""

BAD_SCRIPT = """import { Instance } from "cs_script/point_script";
Instance.TeleportEveryone();
"""


# ------------------------------------------------------------------ exact

def test_exact_normalises_case_and_whitespace():
    assert reward.grade("OnTrigger", "  ontrigger ", checker="exact") is True
    assert reward.grade("OnTrigger", "OnStartTouch", checker="exact") is False


# ------------------------------------------------------------------ lookup

def test_lookup_treats_the_index_as_the_oracle(db):
    """A hit means the identifier is real; a wrong-but-real identifier is a near miss."""
    assert reward.grade("mp_roundtime", "mp_roundtime", checker="s2docs_lookup", db=db) is True
    # a different indexed identifier: real, but not the expected one
    assert reward.grade("sv_cheats", "mp_roundtime", checker="s2docs_lookup", db=db) is False
    # an identifier the index does not know: a hallucination
    assert reward.grade("zzz_not_a_convar", "zzz_not_a_convar", checker="s2docs_lookup", db=db) is False


def test_lookup_needs_a_db_rather_than_guessing(db):
    with pytest.raises(ValueError):
        reward.grade("mp_roundtime", "mp_roundtime", checker="s2docs_lookup")


# ------------------------------------------------------------------ cfg

def test_cfg_validates_the_answer_against_the_indexed_commands(db):
    assert reward.grade("bot_kick", "bot_kick", checker="s2docs_cfg", db=db) is True
    # a typo is an unknown command (validate_cfg's unknown_command error)
    assert reward.grade("bot_kickk", "bot_kick", checker="s2docs_cfg", db=db) is False


def test_cfg_rejects_a_valid_but_wrong_answer_and_an_out_of_range_value(db):
    # validates cleanly, but does not carry the expected command: still wrong
    assert reward.grade("hostname my-server", "bot_kick", checker="s2docs_cfg", db=db) is False
    # mp_roundtime max is 60 in the fixture: out_of_range is an error
    assert reward.grade("mp_roundtime 100", "mp_roundtime 5", checker="s2docs_cfg", db=db) is False


# ------------------------------------------------------------------ script

@needs_tsc
def test_script_delegates_to_the_tsc_validator(db):
    assert reward.grade(GOOD_SCRIPT, "0", checker="s2docs_script", db=db) is True
    assert reward.grade(BAD_SCRIPT, "0", checker="s2docs_script", db=db) is False


@needs_tsc
def test_script_honours_the_expected_exit_code(db):
    # a clean script offered where a failure was expected does not pass
    assert reward.grade(GOOD_SCRIPT, "1", checker="s2docs_script", db=db) is False


# ------------------------------------------------------------------ routing

def test_unknown_checker_raises_never_defaults():
    with pytest.raises(reward.UnknownChecker):
        reward.grade("x", "y", checker="vibes")


def test_grade_is_deterministic(db):
    """Same inputs, same verdict — a training signal must not drift between calls."""
    a = reward.grade("mp_roundtime", "mp_roundtime", checker="s2docs_lookup", db=db)
    b = reward.grade("mp_roundtime", "mp_roundtime", checker="s2docs_lookup", db=db)
    assert a is b is True


# ------------------------------------------------------------------ checker_for

def test_checker_for_maps_every_supported_evidence_shape():
    script = reward.checker_for({"kind": "other", "ref": "scripts/foo.js", "data": {"expect_exit": 0}})
    assert (script.name, script.expected) == ("s2docs_script", "0")

    lookup = reward.checker_for({"data": {"expect_identifier": "sv_cheats"}})
    assert (lookup.name, lookup.expected) == ("s2docs_lookup", "sv_cheats")

    cfg = reward.checker_for({"data": {"expect_cfg": "mp_roundtime 5"}})
    assert (cfg.name, cfg.expected) == ("s2docs_cfg", "mp_roundtime 5")

    exact = reward.checker_for({"data": {"expect_exact": "OnTrigger"}})
    assert (exact.name, exact.expected) == ("exact", "OnTrigger")


def test_checker_for_declines_evidence_it_cannot_check_mechanically():
    assert reward.checker_for({"data": {}}) is None
    # a .js reference that declares no expected exit code is not a script problem
    assert reward.checker_for({"ref": "scripts/foo.js", "data": {}}) is None
    assert reward.checker_for({"ref": "notes.txt", "data": {"note": "hi"}}) is None


def test_checker_for_output_is_accepted_by_grade(db):
    """The mapping and the grader agree: routing an evidence row through `checker_for` then `grade`
    is the whole contract T16's `export.problems` relies on."""
    row = {"data": {"expect_identifier": "mp_roundtime"}}
    check = reward.checker_for(row)
    assert reward.grade("mp_roundtime", check.expected, checker=check.name, db=db) is True
