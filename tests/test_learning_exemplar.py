"""Exemplar export (learning-loop task T11): approved confirmations -> markdown docs, with the guard.

The guard is the point, not the happy path. An exemplar is the retrieval source for "what does a good
run of this look like", so a confirmation whose job recorded an unmet gate, a missing artifact or a
late/superseded verdict must never export — one wrong lesson in the corpus poisons the rest. The export
must also never raise for a bad row: approval must not fail because the corpus side did.

Seeding uses the shared `db_for`/`_enqueue` from `tests.test_learning`; verdicts are real
`human-confirm` evidence rows anchored to the job by `ref == job_id`, exactly as `jobs.verdict` writes
them (rule 4: one definition of "what did the human say").
"""

from __future__ import annotations

from pathlib import Path

from s2docs import evidence, jobs, test_runs
from s2docs.learning import exemplar
from tests.test_learning import _enqueue, db_for

SUMMARY = {"what": "the wire is occluded", "why": "the wall hides it",
           "learned": "occluders need a trigger volume", "next": "reuse the volume"}


# --------------------------------------------------------------------- helpers


def _run(db):
    return test_runs.start(db, "iso_test")


def _verdict(db, job_id, verdict, *, step="s3", run=None, extra=None):
    """A human-confirm evidence row for a job, exactly as `jobs.verdict` would write one."""
    run = run or _run(db)
    jobs.link(db, job_id, kind="test_run", ref_id=run.id)
    data = {"verdict": verdict, "source": "chat", "step": step}
    if extra:
        data.update(extra)
    return evidence.add(db, kind="human-confirm", test_run_id=run.id, ref=job_id, data=data)


def _seed(tmp_path, *, title="occluder wire", origin="human", status="passed", verdict="pass",
          extra=None, summary=None):
    """One finished job. `verdict=None` (given) attaches a human-confirm row only when not None."""
    db = db_for(tmp_path)
    job = _enqueue(db, origin=origin, title=title, target=f"ws/{title}@abc123")
    jobs.start(db, job.id, worker="ws/learning")
    jobs.event(db, job.id, level="info", message="launch", data={"step": "s3"})
    if verdict is not None:
        _verdict(db, job.id, verdict, extra=extra)
    jobs.finish(db, job.id, status=status, summary=summary or dict(SUMMARY))
    return db, jobs.get(db, job.id)


# ------------------------------------------------------------------ happy path


def test_exemplar_exports_only_approved_confirmations(tmp_path, monkeypatch):
    """One markdown doc per approved confirmation, carrying provenance in the frontmatter."""
    _seed(tmp_path, title="occluder wire", verdict="pass")
    _seed(tmp_path, title="spectate", status="failed", verdict="fail")

    out = exemplar.export_approved(db_for(tmp_path))
    assert len(out["written"]) == 1, out
    text = Path(out["written"][0]).read_text()
    assert "verdict: pass" in text and "job_id" in text
    assert "learned" in text.lower()
    # the failed run is reported, never exported
    assert len(out["skipped"]) == 1
    assert out["skipped"][0]["reasons"]


def test_exemplar_is_not_written_for_a_synthetic_origin(tmp_path, monkeypatch):
    """Provenance scopes the export: a driver run is not a person's approved run."""
    _seed(tmp_path, origin="driver", verdict="pass")
    out = exemplar.export_approved(db_for(tmp_path))
    assert out["written"] == [] and out["skipped"] == []


# ------------------------------------------------------------------- the guard


def test_exemplar_refuses_a_row_whose_gate_was_unmet(tmp_path, monkeypatch):
    """A pass verdict on a run whose gate was unmet is not an approval: it must not export."""
    _seed(tmp_path, verdict="pass", extra={"gate_unmet": "artifact x was missing"})
    out = exemplar.export_approved(db_for(tmp_path))
    assert out["written"] == [], "a gate-unmet row must not export"
    assert out["skipped"] and out["skipped"][0]["reasons"]


def test_exemplar_refuses_a_row_whose_artifact_was_missing(tmp_path, monkeypatch):
    _seed(tmp_path, verdict="pass", extra={"artifact_missing": True})
    out = exemplar.export_approved(db_for(tmp_path))
    assert out["written"] == [] and out["skipped"]


def test_exemplar_refuses_a_late_verdict(tmp_path, monkeypatch):
    """A verdict attributed after the window closed is superseded, not an approval."""
    _seed(tmp_path, verdict="pass", extra={"late": True})
    out = exemplar.export_approved(db_for(tmp_path))
    assert out["written"] == [] and out["skipped"]


def test_exemplar_refuses_a_run_no_human_judged(tmp_path, monkeypatch):
    """`passed` with zero verdicts is unverified, not approved."""
    _seed(tmp_path, verdict=None)
    out = exemplar.export_approved(db_for(tmp_path))
    assert out["written"] == [] and out["skipped"]


def test_exemplar_never_raises_for_a_bad_row(tmp_path, monkeypatch):
    """Approval must not fail because the corpus side did: a skipped row is reported, not raised."""
    db, _ = _seed(tmp_path, verdict=None)          # a row that will be skipped
    out = exemplar.export_approved(db)             # no exception
    assert out["written"] == [] and out["skipped"]
