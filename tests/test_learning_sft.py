"""SFT prep (learning-loop task T12): build the dataset + train config, and gate the training.

Two gates, both asserted here: the readiness floor (`MIN_EXAMPLES = 500` — below it few-shot prompting
beats fine-tuning, so `report()` says so instead of implying a target) and `require_baseline=True`,
which refuses without a recorded eval run. The train config must name the *real* target, not a
placeholder.
"""

from __future__ import annotations

import json
from pathlib import Path

from s2docs import evidence, jobs, test_runs
from s2docs.learning import gate, learning_dir, sft
from tests.test_learning import _enqueue, db_for

SUMMARY = {"what": "the wire is occluded", "why": "the wall hides it",
           "learned": "occluders need a trigger volume", "next": "reuse the volume"}


# --------------------------------------------------------------------- helpers


def _run(db):
    return test_runs.start(db, "iso_test")


def _verdict(db, job_id, verdict, *, step="s3", run=None):
    run = run or _run(db)
    jobs.link(db, job_id, kind="test_run", ref_id=run.id)
    return evidence.add(db, kind="human-confirm", test_run_id=run.id, ref=job_id,
                        data={"verdict": verdict, "source": "chat", "step": step})


def _seed_approved(tmp_path, *, title="occluder wire", target=None, verdict="pass"):
    db = db_for(tmp_path)
    job = _enqueue(db, origin="human", title=title, target=target or f"ws/{title}@abc123")
    jobs.start(db, job.id, worker="ws/learning")
    jobs.event(db, job.id, level="info", message="launch", data={"step": "s3"})
    if verdict is not None:
        _verdict(db, job.id, verdict)
    jobs.finish(db, job.id, status="passed", summary=dict(SUMMARY))
    return db, jobs.get(db, job.id)


def _record_baseline():
    """A recorded eval run, so `gate.last_good_recall()` is not None."""
    (learning_dir() / "eval_history.jsonl").write_text(
        json.dumps({"ts": 0.0, "n": 3, "questions_total": 3, "recall": 1.0}) + "\n",
        encoding="utf-8")


# ------------------------------------------------------------------- the dataset


def test_sft_prep_writes_the_dataset_and_a_real_train_config(tmp_path, monkeypatch):
    """`prep` writes the dataset and a config that names the real fleet target — no placeholder."""
    _seed_approved(tmp_path)
    db = db_for(tmp_path)

    out = sft.prep(db)
    assert out["examples"] == 1
    assert Path(out["dataset"]).exists()

    cfg = json.loads(Path(out["config"]).read_text())
    assert cfg["base_model"] == "Qwen/Qwen3-0.6B"
    assert cfg["framework"] == "cpompa-ai-cloud lab4 (LoRA distill) -> lab5 (GRPO)"
    assert cfg["method"] == "lora" and cfg["lora_rank"] == 16 and cfg["learning_rate"] == 1e-4
    assert "lab4_distill.distill" in cfg["command"]
    assert "corpus.txt" in cfg["dataset"]
    assert "gate" in cfg and cfg["eval"].startswith("uv run s2docs learning gate")
    assert cfg["handoff_doc"] == "docs/learning/TRAINING_HANDOFF.md"


def test_sft_report_is_not_ready_below_the_minimum_and_says_why(tmp_path, monkeypatch):
    """Below 500 examples the honest answer is 'use few-shot prompting' — a readiness check, not a goal."""
    _seed_approved(tmp_path)
    rep = sft.report(db_for(tmp_path))
    assert rep["ready_to_train"] is False
    assert rep["min_examples"] == sft.MIN_EXAMPLES == 500
    assert "few-shot" in rep["honest_note"]


# ------------------------------------------------------------------- the gates


def test_sft_refuses_to_prepare_without_an_eval_baseline(tmp_path, monkeypatch):
    """No fine-tune without a recorded eval run: without a baseline there is no way to show it helped."""
    _seed_approved(tmp_path)
    assert gate.last_good_recall() is None
    try:
        sft.prep(db_for(tmp_path), require_baseline=True)
    except sft.SftGateError as exc:
        assert "baseline" in str(exc)
    else:  # pragma: no cover - the assertion is that it raised
        raise AssertionError("prep(require_baseline=True) did not raise without a baseline")


def test_sft_prepares_with_a_baseline_recorded(tmp_path, monkeypatch):
    """With a recorded gate run, `require_baseline=True` is satisfied."""
    _seed_approved(tmp_path)
    _record_baseline()
    out = sft.prep(db_for(tmp_path), require_baseline=True)
    assert out["examples"] == 1
    assert sft.report(db_for(tmp_path))["eval_baseline_recall"] == 1.0
