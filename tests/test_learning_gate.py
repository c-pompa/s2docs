"""The golden-question eval gate (learning-loop task T10) — and its falsification self-test.

Every number the learning loop reports sits *above* this gate, so the gate has to be able to fail
and the suite has to see it fail. The rules are ported from ask-hittle's ``eval.py`` (rank metrics,
a trap class that must return nothing, paraphrase invariance, an append-only ``eval_history.jsonl``,
a ``gate()`` that returns a reason or ``None``) and the hermes coding loop's ``gate.sh``
(COULD-NOT-EVALUATE is never a pass; a gate measures, it does not promote).

Nothing here needs anything external: the mini cs2 index is built in ``tmp_path`` by the shared
conftest builders, and the golden set is written per-test into ``<data_dir>/learning/`` — the same
directory the module resolves. The identifiers below are real keys of that miniature index
(``mp_roundtime`` is a convar, ``server:trigger_multiple`` an entity), which is why the seeded gate
can pass at all; a made-up key could only ever prove the gate fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2docs.config import GAMES
from s2docs.ingest.loader import load_build
from s2docs.ingest.wiki import load_wiki
from s2docs.learning import gate, learning_dir
from tests.conftest import write_tree, write_wiki
from tests.test_learning import db_for

# ---------------------------------------------------------------- golden set

# Three cases from the actual index: a convar, an entity, and a trap that must return nothing.
# These are the *seed*. New cases come only from corrections.jsonl after a human writes the answer.
GOLDEN = {
    "frozen": True,
    "note": "Never edit an expectation to make a failing case pass. Add a new question instead.",
    "questions": [
        {"id": "g1", "kind": "retrieval", "query": "roundtime",
         "expect_kinds": ["convar"], "expect_key": "mp_roundtime",
         "paraphrase": "minutes each round"},
        {"id": "g2", "kind": "retrieval", "query": "trigger volume output",
         "expect_kinds": ["entity"], "expect_key": "server:trigger_multiple",
         "paraphrase": "Trigger multiple wait"},
        {"id": "g3", "kind": "trap", "query": "how do I bake a cake in Hammer",
         "expect_no_hits": True},
    ],
}

THRESHOLDS = {"min_recall": 0.9, "min_questions": 3, "ks": [1, 3, 5], "max_regression": 0.0}


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    """The seeded golden set + thresholds, in the per-test learning dir."""
    d = learning_dir()
    (d / "golden_questions.json").write_text(json.dumps(GOLDEN), encoding="utf-8")
    (d / "thresholds.json").write_text(json.dumps(THRESHOLDS), encoding="utf-8")
    return d


@pytest.fixture
def seeded_db(tmp_path: Path):
    """A miniature cs2 index built at the *resolved* db path, so `gate.main()` sees it too."""
    db = db_for(tmp_path)
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "src"))
        load_wiki(conn, write_wiki(tmp_path / "wiki"))
    return db


def _retrieval(**over) -> dict:
    q = {"id": "q", "kind": "retrieval", "query": "roundtime",
         "expect_kinds": ["convar"], "expect_key": "mp_roundtime"}
    q.update(over)
    return q


# ------------------------------------------------------------------- scoring


def test_gate_scores_recall_mrr_and_paraphrase_on_the_seeded_index(seeded_db):
    """A good index scores recall 1.0, and recall@k / MRR / paraphrase are reported, not assumed."""
    s = gate.run(seeded_db, questions=GOLDEN["questions"][:2])
    assert s["n"] == 2 and s["questions_total"] == 2
    assert s["recall"] == 1.0 and s["mrr"] == 1.0
    assert s["recall@1"] == 1.0 and s["recall@3"] == 1.0
    assert s["paraphrase"] == {"total": 2, "passed": 2}
    assert gate.gate(s, min_recall=0.9) is None


def test_gate_flags_a_wrong_expectation_as_a_regression(seeded_db):
    """The point of the gate: a case whose expected key is not retrievable is a failure, and the
    failure reason names it — never a silent pass."""
    s = gate.run(seeded_db, questions=[_retrieval(expect_key="definitely_not_a_convar")])
    assert s["recall"] == 0.0 and s["mrr"] == 0.0 and s["recall@1"] == 0.0
    reason = gate.gate(s, min_recall=0.9)
    assert reason is not None and "recall" in reason


def test_trap_questions_must_return_nothing(seeded_db):
    """The s2docs analogue of 'the generator refused honestly': an out-of-corpus query must return
    zero hits. A trap that returns hits is a failure the gate must catch."""
    ok = gate.run(seeded_db, questions=[{"id": "t", "kind": "trap",
                                         "query": "how do I bake a cake in Hammer",
                                         "expect_no_hits": True}])
    assert ok["traps"] == {"total": 1, "passed": 1}
    assert gate.gate(ok, min_recall=0.0) is None

    leaky = gate.run(seeded_db, questions=[{"id": "t2", "kind": "trap", "query": "trigger",
                                            "expect_no_hits": True}])
    assert leaky["traps"] == {"total": 1, "passed": 0}
    reason = gate.gate(leaky, min_recall=0.0)
    assert reason is not None and "trap" in reason


def test_paraphrase_invariance_failure_is_caught(seeded_db):
    """Invariance is measured, not assumed: a paraphrase that loses the expected hit fails the gate."""
    s = gate.run(seeded_db, questions=[_retrieval(paraphrase="round duration")])
    assert s["paraphrase"] == {"total": 1, "passed": 0}
    reason = gate.gate(s, min_recall=0.9)
    assert reason is not None and "paraphrase" in reason


def test_gate_raises_could_not_evaluate_when_the_golden_set_is_too_small(seeded_db):
    """Too few questions is an error, not a pass — `min_questions` guards a shrunken set."""
    s = gate.run(seeded_db, questions=[_retrieval()])
    with pytest.raises(gate.GateError) as exc:
        gate.gate(s, min_recall=0.9, min_questions=3)
    assert "question" in str(exc.value)


def test_gate_regresses_below_the_last_recorded_recall():
    """max_regression: a recall drop against the last recorded run is a regression, even when it is
    still above the floor."""
    base = {"n": 2, "questions_total": 2, "recall": 1.0, "mrr": 1.0, "recall@1": 1.0,
            "recall@3": 1.0, "recall@5": 1.0, "traps": {"total": 0, "passed": 0},
            "paraphrase": {"total": 0, "passed": 0}}
    dropped = {**base, "recall": 0.8}
    assert gate.gate(dropped, min_recall=0.5, last_good=1.0, max_regression=0.0) is not None
    assert gate.gate(dropped, min_recall=0.5, last_good=1.0, max_regression=0.5) is None


# ------------------------------------------------------------ could not evaluate


def test_empty_data_dir_is_could_not_evaluate_never_a_pass(tmp_path, capsys):
    """Rule 6 / 'COULD-NOT-EVALUATE is never a pass': with no golden set, the gate exits 2 and says
    so on stderr — it does not score an empty set as 0 failures and exit 0."""
    with pytest.raises(gate.GateError) as exc:
        gate.questions_file()
    assert "golden question" in str(exc.value)

    rc = gate.main([])
    assert rc == 2
    assert "COULD NOT EVALUATE" in capsys.readouterr().err


def test_seeded_gate_main_passes_and_appends_history(golden, seeded_db, capsys):
    """End to end over the resolved paths: the seeded index passes (exit 0) and each run appends one
    line to eval_history.jsonl — the append-only record the regression check reads."""
    assert gate.main([]) == 0
    out = capsys.readouterr().out
    assert "recall@5" in out and "traps: 1/1" in out

    hist = learning_dir() / "eval_history.jsonl"
    assert hist.exists()
    rows = [json.loads(l) for l in hist.read_text().splitlines() if l.strip()]
    assert len(rows) == 1 and rows[0]["recall"] == 1.0 and rows[0]["questions_total"] == 3
    assert gate.last_good_recall() == 1.0


# ------------------------------------------------------------ falsification selftest


def test_self_test_plants_an_impossible_expectation_and_the_gate_fails_it(golden, seeded_db):
    """The coding loop's control before any number is published: an impossible expectation must make
    the gate fail. `self_test` returns 0 only when the gate actually failed the plant."""
    # What the plant looks like, directly:
    planted = [_retrieval(expect_key="zzz_definitely_not_indexed")]
    s = gate.run(seeded_db, questions=planted, record=False)
    assert s["recall"] == 0.0
    assert gate.gate(s, min_recall=1.0) is not None, "the gate passed an impossible expectation"

    # And the packaged control the operator runs: OK iff the gate can fail.
    assert gate.self_test(seeded_db) == 0


def test_self_test_does_not_pollute_the_history(golden, seeded_db):
    """A falsification plant is not a measurement of the index, so it must never enter the append-only
    history the regression check compares against."""
    gate.self_test(seeded_db)
    hist = learning_dir() / "eval_history.jsonl"
    assert not hist.exists() or hist.read_text().strip() == ""
