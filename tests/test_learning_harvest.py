"""Harvesters + the harvest driver (plan T4-T8).

One definition per concept: the seeding helpers here are the only ones this file uses, built on the
shared `db_for` / `_enqueue` from `tests.test_learning`. Every seed proves a *recorded* status or
verdict — nothing here infers intent from a count or a length (rule 1).
"""

from __future__ import annotations

import json

from s2docs import evidence, jobs, test_runs
from s2docs.learning import harvest, learning_dir
from tests.test_learning import _enqueue, db_for

SUMMED = {"what": "w", "why": "y", "learned": "l", "next": "n"}


# --------------------------------------------------------------------- helpers


def _run(db):
    """One test run to anchor verdict/evidence rows. No host -> no lease is claimed."""
    return test_runs.start(db, "iso_test")


def _verdict(db, job_id, verdict, *, step=None, notes=None, run=None, extra=None):
    """A human-confirm evidence row for a job, exactly as `jobs.verdict` would write one.

    `ref == job_id` is the anchor the harvesters read (one definition of "what did the human say").
    """
    run = run or _run(db)
    jobs.link(db, job_id, kind="test_run", ref_id=run.id)
    data = {"verdict": verdict, "source": "chat"}
    if step:
        data["step"] = step
    if notes:
        data["notes"] = notes
    if extra:
        data.update(extra)
    return evidence.add(db, kind="human-confirm", test_run_id=run.id, ref=job_id, data=data)


def _seed(tmp_path, *, origin="human", status="passed", target=None, title="occluder wire",
          verdict=None, step=None, summary=None):
    """One finished job. `verdict` (when given) attaches a human-confirm row; its `step` lives in the
    row's `data` (as `jobs.verdict` writes it), never inferred from anywhere else."""
    db = db_for(tmp_path)
    job = _enqueue(db, origin=origin, title=title, target=target or f"ws/{title}@abc123")
    jobs.start(db, job.id, worker="ws/learning")
    jobs.event(db, job.id, level="info", message="launch", data={"step": step or "s3"})
    if verdict:
        _verdict(db, job.id, verdict, step=step)
    jobs.finish(db, job.id, status=status, summary=summary or dict(SUMMED))
    return db, jobs.get(db, job.id)


def _seed_failed_step(tmp_path, *, step_id="s4", evidence_ref="content/iso_spectate/spectate.js",
                      verdict="fail", origin="human"):
    """A run that failed a step it gated on: a non-verdict evidence row (the thing the step checked),
    a fail verdict for that step, and a 'failed' close-out."""
    db = db_for(tmp_path)
    job = _enqueue(db, origin=origin, title="spectate", target="ws/spectate@abc123")
    jobs.start(db, job.id, worker="ws/learning")
    run = _run(db)
    jobs.link(db, job.id, kind="test_run", ref_id=run.id)
    ev = evidence.add(db, kind="file_hash", step_id=step_id, ref=evidence_ref, test_run_id=run.id,
                      data={"md5": "deadbeef"})
    jobs.link(db, job.id, kind="evidence", ref_id=ev.id)
    if verdict:
        _verdict(db, job.id, verdict, step=step_id, run=run)
    jobs.event(db, job.id, level="info", message="launch", data={"step": step_id})
    jobs.finish(db, job.id, status="failed", summary=dict(SUMMED))
    return db, jobs.get(db, job.id)


def _fake_search(by_term):
    return lambda term: by_term.get(term, [])


# ------------------------------------------------------------------ T4: sft_pairs


def test_sft_pairs_uses_only_passed_human_jobs_and_dedupes_by_target(tmp_path, monkeypatch):
    """Approved successes only, one example per distinct target: three runs of one target are one
    example, and driver traffic must not inflate the sample."""
    _seed(tmp_path, target="feat@aaa", origin="human", verdict="pass")
    _seed(tmp_path, target="feat@aaa", origin="human", verdict="pass")   # same target again
    _seed(tmp_path, target="other@bbb", origin="driver", verdict="pass")

    pairs = harvest.sft_pairs(db_for(tmp_path))
    assert len(pairs) == 1, "one distinct target, and driver traffic must not appear"
    assert pairs[0]["meta"]["target"] == "feat@aaa"
    assert pairs[0]["messages"][0]["role"] == "system"
    assert pairs[0]["messages"][1]["role"] == "user"
    assert pairs[0]["messages"][2]["role"] == "assistant"
    assert pairs[0]["messages"][2]["content"] == "<regenerate-at-run-time>"


def test_sft_pairs_skips_a_passed_run_with_a_failed_step_verdict(tmp_path, monkeypatch):
    """`passed` with a recorded `fail` is not an approval: the verdict is read, not the status alone."""
    _seed(tmp_path, status="passed", verdict="fail")
    assert harvest.sft_pairs(db_for(tmp_path)) == []


def test_sft_pairs_skips_a_run_no_human_ever_judged(tmp_path, monkeypatch):
    """`passed` with zero verdicts is exactly what `inconclusive` was invented for — unverified, not
    approved. It must not become a training example."""
    _seed(tmp_path, status="passed", verdict=None)
    assert harvest.sft_pairs(db_for(tmp_path)) == []


# ------------------------------------------------------------- T5: corrections


def test_corrections_collects_failures_and_never_labels_them(tmp_path, monkeypatch):
    """A failure is a queue entry, never a scored case: `needs_human_label` is always true."""
    _seed(tmp_path, status="failed", verdict="fail", origin="human",
          summary={"what": "s4 area presets", "why": "nothing moved", "learned": "l", "next": "fix"})
    _seed(tmp_path, status="passed", verdict="pass", origin="human")

    rows = harvest.corrections(db_for(tmp_path))
    assert len(rows) == 1
    assert rows[0]["failure_mode"] == "fail_verdict"
    assert rows[0]["needs_human_label"] is True
    assert rows[0]["question"]


def test_corrections_mark_an_unjudged_finish_as_inconclusive(tmp_path, monkeypatch):
    """An `inconclusive` close-out with no verdict is its own failure mode — not 'fail_verdict'."""
    _seed(tmp_path, status="inconclusive", verdict=None)
    rows = harvest.corrections(db_for(tmp_path))
    assert [r["failure_mode"] for r in rows] == ["inconclusive"]


# ---------------------------------------------------------- T6: hard_negatives


def test_hard_negatives_need_a_verdict_and_repeat(tmp_path, monkeypatch):
    """Evidence a step gated on, contradicted by a fail verdict in >=3 distinct jobs."""
    for _ in range(3):
        _seed_failed_step(tmp_path, step_id="s4", evidence_ref="content/iso_spectate/spectate.js")

    rows = harvest.hard_negatives(db_for(tmp_path))
    assert len(rows) == 1
    assert rows[0]["misses"] == 3
    assert rows[0]["misses_by_origin"] == {"human": 3}
    assert rows[0]["artifact"] == "spectate.js"


def test_hard_negatives_ignore_runs_with_no_verdict(tmp_path, monkeypatch):
    """No verdict means judging was impossible: counting those makes every artifact look like a
    distractor (the ask-hittle inflation defect)."""
    for _ in range(5):
        _seed_failed_step(tmp_path, step_id="s4", evidence_ref="x.js", verdict=None)
    assert harvest.hard_negatives(db_for(tmp_path)) == []


def test_hard_negatives_below_the_repeat_threshold_are_not_reported(tmp_path, monkeypatch):
    for _ in range(2):
        _seed_failed_step(tmp_path, step_id="s4", evidence_ref="x.js")
    assert harvest.hard_negatives(db_for(tmp_path)) == []


# ---------------------------------------------------------------- T7: gap_report


def test_gap_report_splits_a_retrieval_miss_from_a_true_gap(tmp_path, monkeypatch):
    _seed(tmp_path, status="inconclusive", verdict=None, title="teleport a bot",
          summary={"what": "cs_script teleport", "why": "no verdict", "learned": "l", "next": "n"})
    # overwrite the goal text so the subject terms are stable for the stub
    out = harvest.gap_report(db_for(tmp_path), origin=None,
                             search=_fake_search({"teleport": ["script:Teleport"]}))
    assert out["no_verdict_runs"], "an unanswered run is a process gap"
    assert out["likely_retrieval_misses"], "the index covers the subject -> retrieval, not corpus"
    assert out["true_gaps"] == []


def test_gap_report_reports_the_sample_with_every_list(tmp_path, monkeypatch):
    _seed(tmp_path, status="inconclusive", verdict=None)
    out = harvest.gap_report(db_for(tmp_path), origin=None, search=lambda q: [])
    for key in ("no_verdict_runs", "missing_evidence_steps", "blocked_goals", "empty_runs",
                "likely_retrieval_misses", "true_gaps"):
        assert key in out
    assert "sample" in out and "origin" in out["sample"]


# ------------------------------------------------------------ T8: harvest driver


def test_harvest_writes_all_four_artifacts_and_reports_the_sample(tmp_path, monkeypatch):
    _seed(tmp_path, verdict="pass")
    out = harvest.harvest(db_for(tmp_path))
    for name in ("sft_pairs.jsonl", "corrections.jsonl", "hard_negatives.jsonl", "gap_report.json"):
        assert (learning_dir() / name).exists(), name
    assert out["counts"]["sft_pairs"] == 1
    assert out["sample"]["origin"] == "human"
    data = json.loads((learning_dir() / "gap_report.json").read_text())
    assert data["sample"]["origin"] == "human"


def test_harvest_default_origin_is_human_and_writes_nothing_from_driver_runs(tmp_path, monkeypatch):
    _seed(tmp_path, origin="driver", verdict="pass")
    out = harvest.harvest(db_for(tmp_path))
    assert out["counts"]["sft_pairs"] == 0
    assert (learning_dir() / "sft_pairs.jsonl").read_text() == ""


# ------------------------------------------------------------- T13: learning events


def test_harvest_annotates_the_jobs_it_used_with_one_learning_event(tmp_path, monkeypatch):
    db, job = _seed(tmp_path, verdict="pass")
    harvest.harvest(db, annotate=True)
    evs = [e for e in jobs.get(db, job.id).events if e.level == "learning"]
    assert len(evs) == 1
    assert "sft_pairs" in json.dumps(evs[0].data)


def test_harvest_never_annotates_synthetic_origins(tmp_path, monkeypatch):
    """A driver run is synthetic: tagging it 'learning' would put synthetic traffic back in the
    human's view, which provenance was added to prevent."""
    db, job = _seed(tmp_path, origin="driver", verdict="pass")
    harvest.harvest(db, annotate=True, origin=None)
    assert [e for e in jobs.get(db, job.id).events if e.level == "learning"] == []


# ------------------------------------------------------------------- T8: CLI surface


def test_cli_learning_fresh_data_dir_exits_6_not_0(tmp_path, monkeypatch):
    """A skip is not a pass: with a migrated ledger but no job-event log yet, the CLI reports 6 (a
    source was unreadable), never a green run over an empty artifact."""
    from s2docs import cli
    from s2docs.config import get_settings
    from s2docs.db.connection import Database

    get_settings.cache_clear()
    with Database(get_settings().db_path).writer():
        pass  # the ledger exists (migrated); nothing has been recorded yet
    assert cli.main(["learning", "harvest", "--json"]) == cli.EXIT_PARTIAL
    assert cli.main(["learning", "report", "--json"]) == cli.EXIT_PARTIAL


def test_cli_learning_report_and_harvest_on_a_seeded_ledger(tmp_path, monkeypatch, capsys):
    from s2docs import cli
    from s2docs.config import get_settings

    get_settings.cache_clear()
    _seed(tmp_path, verdict="pass")

    assert cli.main(["learning", "report", "--json"]) == cli.EXIT_OK
    cap = json.loads(capsys.readouterr().out)
    assert cap["jobs_total"] == 1 and cap["real_usage_jobs"] == 1

    assert cli.main(["learning", "harvest", "--json"]) == cli.EXIT_OK
    cap = json.loads(capsys.readouterr().out)
    assert cap["counts"]["sft_pairs"] == 1
    assert cap["sample"]["origin"] == "human"
    assert (learning_dir() / "sft_pairs.jsonl").exists()

