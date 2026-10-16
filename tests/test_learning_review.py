"""The HITL review queue (plan T15): a human promotes a correction into the frozen golden set.

Nothing here is automatic by design. A failing run is *evidence something is wrong*, not a question
with a known answer — inventing the expected answer to fill the queue is how an eval set starts
lying. A human writes the expectation; the code only files it, deduped on the normalized query and
append-only (ported from cpompa-ai-cloud's ``scripts/eval/add_correction.py``).

The load-bearing test is the last one: a promoted case must be able to FAIL the gate. A golden set
derived from what the index already returns is circular — recall comes out 100% and the gate measures
nothing (proved: seeding cases from ``Repository.search`` gave recall@5 100%, MRR 1.000). So
``promote()`` must never call the index to fill in ``expect_key``, and a wrong human expectation must
produce a case the gate fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2docs import evidence, jobs, test_runs
from s2docs.config import GAMES
from s2docs.ingest.loader import load_build
from s2docs.ingest.wiki import load_wiki
from s2docs.learning import gate, review
from tests.conftest import write_tree, write_wiki
from tests.test_learning import _enqueue, db_for


# --------------------------------------------------------------------- helpers


def _seed_failed(tmp_path: Path, *, title: str = "teleport a bot", purpose: str = "teleport"):
    """One failed job with a human `fail` verdict — a correction-queue row.

    The `human-confirm` row is anchored to the job by ``ref == job_id``, exactly what
    ``jobs.verdict`` writes and what ``harvest.corrections`` reads.
    """
    db = db_for(tmp_path)
    job = _enqueue(db, origin="human", title=title, target=f"ws/{title}@abc123", purpose=purpose)
    jobs.start(db, job.id, worker="ws/learning")
    run = test_runs.start(db, "iso_test")
    jobs.link(db, job.id, kind="test_run", ref_id=run.id)
    evidence.add(db, kind="human-confirm", test_run_id=run.id, ref=job.id,
                 data={"verdict": "fail", "source": "chat"})
    jobs.finish(db, job.id, status="failed",
                summary={"what": "w", "why": "y", "learned": "l", "next": "n"})
    return db, jobs.get(db, job.id)


@pytest.fixture
def seeded_db(tmp_path: Path):
    """A miniature cs2 index at the *resolved* db path, so the gate can score a promoted case."""
    db = db_for(tmp_path)
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "src"))
        load_wiki(conn, write_wiki(tmp_path / "wiki"))
    return db


# ----------------------------------------------------------------- T15: queue


def test_review_queue_lists_unlabeled_corrections(tmp_path: Path):
    """The queue is every correction, still unlabeled, and each row says whether it was promoted."""
    db, _ = _seed_failed(tmp_path)

    q = review.queue(db)

    assert len(q) == 1
    assert q[0]["needs_human_label"] is True
    assert q[0]["promoted"] is False

    # a passing run is not a correction and must not appear
    _seed_ok = _enqueue(db, origin="human", title="passing", target="ws/pass@abc123",
                        purpose="nothing to correct")
    assert len(review.queue(db)) == 1


# --------------------------------------------------------------- T15: promote


def test_promote_files_a_frozen_case_and_a_repeat_is_a_no_op(tmp_path: Path):
    """A human's answer becomes a new frozen case; promoting the same correction again is a duplicate
    — it returns ``appended: False`` instead of raising, because a double click must not double the
    golden set."""
    db, job = _seed_failed(tmp_path)

    out = review.promote(db, job.id, expected="iso:bot_to", checker="exact", source="hitl")
    assert out["appended"] is True
    assert out["category"] == "correction"
    assert out["source"] == "hitl" and out["checker"] == "exact"
    assert out["source_job"] == job.id

    cases = gate.questions_file()
    assert [c["id"] for c in cases if c["id"] == out["id"]] == [out["id"]]

    again = review.promote(db, job.id, expected="iso:bot_to", checker="exact", source="hitl")
    assert again["appended"] is False, "a duplicate must not add a second case"
    assert again["reason"] == "duplicate"
    assert len(gate.questions_file()) == 1

    # and the queue now shows it as drained
    assert review.queue(db)[0]["promoted"] is True


def test_promotion_is_append_only_an_existing_case_is_never_edited(tmp_path: Path):
    """An existing case is never edited, not even by a later, better correction: promoting a second
    correction leaves the first byte-for-byte unchanged."""
    db, first = _seed_failed(tmp_path, title="alpha", purpose="alpha thing")
    out_a = review.promote(db, first.id, expected="key_alpha")
    before = [c for c in gate.questions_file() if c["id"] == out_a["id"]][0]

    _, second = _seed_failed(tmp_path, title="beta", purpose="beta thing")
    out_b = review.promote(db, second.id, expected="key_beta")

    cases = gate.questions_file()
    assert len(cases) == 2 and out_a["id"] != out_b["id"]
    after = [c for c in cases if c["id"] == out_a["id"]][0]
    assert after == before, "the first case changed when a second was appended"

    # a differing expectation for the SAME correction is still a duplicate on the query, and is not
    # filed as a second case — dedupe is on the normalized query, not the expected value.
    dup = review.promote(db, first.id, expected="a_better_answer")
    assert dup["appended"] is False and dup["reason"] == "duplicate"
    assert len(gate.questions_file()) == 2


# ------------------------------------ the circularity guard: a case must be able to fail


def test_promote_never_fills_the_expected_value_from_the_index(tmp_path: Path, monkeypatch):
    """The circularity guard, directly: promote() must not touch the index to discover the answer.
    Any call to ``Repository.search`` during promotion is a bug — it would seed the golden set from
    what the index already returns."""
    from s2docs.db.repository import Repository

    def _boom(*_a, **_k):                     # pragma: no cover - only runs on a regression
        raise AssertionError("promote() must never call the index to fill in `expected`")

    monkeypatch.setattr(Repository, "search", _boom)
    db, job = _seed_failed(tmp_path, title="roundtime limit", purpose="round time")

    out = review.promote(db, job.id, expected="the_human_said_this")

    assert out["expect_key"] == "the_human_said_this"


def test_a_promoted_case_can_fail_the_gate_when_the_human_is_wrong(tmp_path: Path, seeded_db):
    """The point of the whole task: a promoted case carries a human EXPECTATION and must be able to
    fail. Two corrections over the same index — one whose expectation the index can ground, one the
    human got wrong — and the gate passes the first and fails the second."""
    db = seeded_db
    right, wrong = _seed_two_corrections(db, tmp_path)

    good = review.promote(db, right.id, expected="mp_roundtime")
    bad = review.promote(db, wrong.id, expected="definitely_not_in_the_index")

    good_case = _case(good["id"])
    bad_case = _case(bad["id"])
    assert good_case["expect_key"] == "mp_roundtime"
    assert bad_case["expect_key"] == "definitely_not_in_the_index"

    s_good = gate.run(db, questions=[good_case], record=False)
    assert s_good["recall"] == 1.0
    assert gate.gate(s_good, min_recall=0.9, min_questions=1) is None

    s_bad = gate.run(db, questions=[bad_case], record=False)
    assert s_bad["recall"] == 0.0, "a wrong human expectation must be able to fail the gate"
    assert gate.gate(s_bad, min_recall=0.9, min_questions=1) is not None


def _case(case_id: str) -> dict:
    return next(c for c in gate.questions_file() if c["id"] == case_id)


def _seed_two_corrections(db, tmp_path):
    """Two failed jobs over an existing index, both whose goal the index can *ground*.

    ``Repository.search`` ANDs up to 12 query tokens (``build_match_query``), so the correction's
    question — ``title`` + ``target`` + ``Purpose:``/``Expected:`` lines — is made retrievable by
    padding the title with twelve ``roundtime`` tokens, which fills the token budget before the
    literals ``Purpose``/``Expected`` are reached. Both queries therefore return ``mp_roundtime``;
    the only thing that decides pass/fail is the human's ``expected`` value.
    """
    out = []
    for purpose in ("alpha", "beta"):        # distinct questions -> no dedupe
        job = _enqueue(db, origin="human", title=" ".join(["roundtime"] * 12),
                       target="roundtime", purpose=purpose, expected=purpose)
        jobs.start(db, job.id, worker="ws/learning")
        run = test_runs.start(db, "iso_test")
        jobs.link(db, job.id, kind="test_run", ref_id=run.id)
        evidence.add(db, kind="human-confirm", test_run_id=run.id, ref=job.id,
                     data={"verdict": "fail", "source": "chat"})
        jobs.finish(db, job.id, status="failed",
                    summary={"what": "w", "why": "y", "learned": "l", "next": "n"})
        out.append(jobs.get(db, job.id))
    return out
