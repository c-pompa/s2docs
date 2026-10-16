"""The lab-shape export (learning-loop task T16): corpus.txt, problems.json, golden_cases.jsonl.

Three artifacts, three consumers, three shapes. Two rules the tests pin down: the corpus is *behaviour*
(the human's words) and never the indexed API facts, and a problem with no mechanical checker is
dropped rather than emitted with a guessed checker — an unverifiable reward is a preference pair in
disguise.
"""

from __future__ import annotations

import json
from pathlib import Path

from s2docs import evidence, jobs, test_runs
from s2docs.learning import export, learning_dir
from tests.test_learning import _enqueue, db_for

SUMMARY = {"what": "the wire is occluded", "why": "the wall hides it",
           "learned": "occluders need a trigger volume", "next": "reuse the volume"}

GOLDEN = {
    "frozen": True,
    "note": "Never edit an expectation to make a failing case pass. Add a new question instead.",
    "questions": [
        {"id": "g1", "kind": "retrieval", "query": "roundtime",
         "expect_kinds": ["convar"], "expect_key": "mp_roundtime"},
        {"id": "g3", "kind": "trap", "query": "how do I bake a cake in Hammer",
         "expect_no_hits": True},
    ],
}


# --------------------------------------------------------------------- helpers


def _run(db):
    return test_runs.start(db, "iso_test")


def _verdict(db, job_id, verdict, *, step="s3", run=None):
    run = run or _run(db)
    jobs.link(db, job_id, kind="test_run", ref_id=run.id)
    return evidence.add(db, kind="human-confirm", test_run_id=run.id, ref=job_id,
                        data={"verdict": verdict, "source": "chat", "step": step})


def _seed_approved(tmp_path, *, title="occluder wire", extra_evidence=None):
    """An approved job (`passed`, pass verdict, real `learned` words)."""
    db = db_for(tmp_path)
    job = _enqueue(db, origin="human", title=title, target=f"ws/{title}@abc123")
    jobs.start(db, job.id, worker="ws/learning")
    jobs.event(db, job.id, level="info", message="launch", data={"step": "s3"})
    _verdict(db, job.id, "pass")
    if extra_evidence:
        run = _run(db)
        for ref, data in extra_evidence:
            row = evidence.add(db, kind="file_hash", ref=ref, data=data, test_run_id=run.id)
            jobs.link(db, job.id, kind="evidence", ref_id=row.id)
    jobs.finish(db, job.id, status="passed", summary=dict(SUMMARY))
    return db, jobs.get(db, job.id)


def _write_golden():
    (learning_dir() / "golden_questions.json").write_text(json.dumps(GOLDEN), encoding="utf-8")


# --------------------------------------------------------------------- corpus


def test_corpus_is_behaviour_one_block_per_approved_job(tmp_path, monkeypatch):
    """Plain text, `## job <id> — <title>` blocks carrying request/proved/learned/next — and it
    self-reports how many blocks and bytes it produced (lab-4 chunks it at seq_len 256)."""
    _seed_approved(tmp_path, title="occluder wire")
    _seed_approved(tmp_path, title="also approved")
    db = db_for(tmp_path)

    corpus = Path(export.corpus(db, origin="human")).read_text()
    assert corpus.lower().count("## job") == 2
    assert "learned" in corpus.lower() and "proved:" in corpus.lower()
    assert "job block" in corpus.lower() and "bytes" in corpus.lower(), "the file states its size"


def test_corpus_skips_a_run_no_human_judged(tmp_path, monkeypatch):
    """Only *approved* runs are behaviour worth imitating: a passed-but-unjudged run is unverified."""
    db = db_for(tmp_path)
    job = _enqueue(db, origin="human", title="unjudged", target="ws/unjudged@abc123")
    jobs.start(db, job.id, worker="ws/learning")
    jobs.finish(db, job.id, status="passed", summary=dict(SUMMARY))

    corpus = Path(export.corpus(db)).read_text()
    assert "## job" not in corpus.lower()


# -------------------------------------------------------------------- problems


def test_problems_keeps_only_checker_backed_evidence(tmp_path, monkeypatch):
    """A `.js` evidence row with an expected exit code becomes an `s2docs_script` problem; a row with
    no mechanical checker is dropped, never emitted with a guessed one."""
    _seed_approved(tmp_path, extra_evidence=[
        ("scripts/foo.js", {"expect_exit": 0}),          # checkable -> kept
        ("notes.txt", {"note": "not mechanically checkable"}),   # no checker -> dropped
    ])
    problems = json.loads(Path(export.problems(db_for(tmp_path), origin="human")).read_text())
    assert len(problems) == 1, problems
    assert set(problems[0]) >= {"problem", "answer", "checker", "job_id", "evidence_ref"}
    assert problems[0]["checker"] == "s2docs_script"
    assert problems[0]["answer"] == "0"


def test_problems_ignores_unapproved_runs(tmp_path, monkeypatch):
    """A failed run's evidence is not a training problem: it was contradicted, not demonstrated."""
    db = db_for(tmp_path)
    job = _enqueue(db, origin="human", title="failed", target="ws/failed@abc123")
    jobs.start(db, job.id, worker="ws/learning")
    _verdict(db, job.id, "fail")
    run = _run(db)
    row = evidence.add(db, kind="file_hash", ref="scripts/foo.js", data={"expect_exit": 0},
                       test_run_id=run.id)
    jobs.link(db, job.id, kind="evidence", ref_id=row.id)
    jobs.finish(db, job.id, status="failed", summary=dict(SUMMARY))

    problems = json.loads(Path(export.problems(db, origin="human")).read_text())
    assert problems == []


# ----------------------------------------------------------------------- cases


def test_cases_are_the_labs_golden_shape(tmp_path, monkeypatch):
    """The frozen set in `{id, category, prompt, checker, expected}` — a trap maps to `exact_empty`."""
    _write_golden()
    path = export.cases(db_for(tmp_path))
    cases = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    assert cases and all(set(c) >= {"id", "category", "prompt", "checker", "expected"}
                         for c in cases)
    by_id = {c["id"]: c for c in cases}
    assert by_id["g1"] == {"id": "g1", "category": "retrieval", "prompt": "roundtime",
                           "checker": "exact", "expected": "mp_roundtime"}
    assert by_id["g3"]["checker"] == "exact_empty"


# -------------------------------------------------------------------- together


def test_everything_writes_all_three_in_the_consumers_shapes(tmp_path, monkeypatch):
    _write_golden()
    _seed_approved(tmp_path, extra_evidence=[("scripts/foo.js", {"expect_exit": 0})])
    out = export.everything(db_for(tmp_path))
    for key in ("corpus", "problems", "cases", "dir"):
        assert Path(out[key]).exists() or key == "dir"

    corpus = Path(out["corpus"]).read_text()
    assert "learned" in corpus.lower() and "## job" in corpus.lower()
    problems = json.loads(Path(out["problems"]).read_text())
    assert problems and set(problems[0]) >= {"problem", "answer", "checker"}
    cases = [json.loads(line) for line in Path(out["cases"]).read_text().splitlines() if line.strip()]
    assert cases and set(cases[0]) >= {"id", "category", "prompt", "checker", "expected"}
