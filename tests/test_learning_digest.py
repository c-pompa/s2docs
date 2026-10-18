"""The learning digest (plan T9): signals, a human-readable report, and lesson proposals.

Deterministic, offline, and — the load-bearing rule — honest about its own failures. The shape is
ported from the hermes coding loop's ``collect.py`` and its digest writer: collect outcomes, classify
them against a *closed* class set, write ``signals.jsonl`` + ``lessons.json``, and render a report
that names every source it could not read instead of quietly reporting zero (its
"Harness notes (a skip is not a pass)" section is why anyone trusts the rest of it).

Seeding helpers come from the shared ``tests.test_learning`` helpers; nothing here infers a class
from a proxy, it reads the recorded status (rule 1).
"""

from __future__ import annotations

import json
from pathlib import Path

from s2docs.learning import digest, harvest, learning_dir
from tests.test_learning import _seed_job, db_for


# ------------------------------------------------------------------- writes


def test_digest_writes_signals_report_and_lessons_with_evidence_refs(tmp_path: Path):
    """Collect -> classify -> three artifacts: ``signals.jsonl`` next to the other learning files,
    ``lessons.json`` for the writeback lane, and ``reports/digest-<date>.md`` a person can read."""
    db, _ = _seed_job(tmp_path, status="failed", title="spectate fail")

    out = digest.digest(db, since_days=30)

    signals_path = learning_dir() / "signals.jsonl"
    assert signals_path.exists()
    rows = [json.loads(line) for line in signals_path.read_text().splitlines() if line.strip()]
    assert rows and all({"class", "job_id", "origin", "evidence_ref"} <= set(r) for r in rows)

    report = Path(out["report"])
    assert report.exists() and report.name.startswith("digest-")
    text = report.read_text()
    assert "## Harness notes" in text
    assert "signals" in text

    lessons = json.loads(Path(out["lessons"]).read_text())
    # one signal does not clear the bar: a lesson needs two independent signals (rule: no lesson
    # from a single event).
    assert lessons["lessons"] == []
    assert out["counts"].get("failed_verdict") == 1


def test_digest_lists_artifacts_on_disk_with_their_row_counts(tmp_path: Path):
    """The report names the artifacts a person will act on, with counts — the digest is a work
    queue, not a summary: it points at the files the harvesters wrote."""
    db, _ = _seed_job(tmp_path, status="failed", title="spectate fail")
    harvest.harvest(db)                       # writes the four artifacts into learning_dir()

    text = Path(digest.digest(db)["report"]).read_text()

    for name in ("sft_pairs.jsonl", "corrections.jsonl", "hard_negatives.jsonl", "gap_report.json"):
        assert f"`{name}`" in text, name
    assert "corrections.jsonl` — 1 row(s)" in text
    assert "sft_pairs.jsonl` — 0 row(s)" in text


def test_digest_proposes_one_lesson_per_class_only_at_two_signals(tmp_path: Path):
    """Rule from the coding loop: one lesson per class per run, only with count >= 2 and a named
    ref. Two failures of the same class propose exactly one lesson, and it carries the refs."""
    _seed_job(tmp_path, status="failed", title="spectate fail")
    db, _ = _seed_job(tmp_path, status="failed", title="nav guard fail")

    out = digest.digest(db, since_days=30)

    lessons = json.loads(Path(out["lessons"]).read_text())["lessons"]
    failed = [l for l in lessons if l["class"] == "failed_verdict"]
    assert len(failed) == 1, "one lesson per class per run"
    assert failed[0]["count"] == 2
    assert failed[0]["refs"], "a lesson must name the evidence it came from"
    assert failed[0]["owner_hint"] == "cs2-test-orchestration"

    text = Path(out["report"]).read_text()
    assert "## Signals by class" in text
    assert "cs2-test-orchestration" in text, "the owning skill is shown with the class"


# ------------------------------------------------------- a skip is not a pass


def test_digest_reports_an_unreadable_source_instead_of_a_silent_zero(tmp_path: Path):
    """Rule 6, the one that makes the rest trustworthy. With no job-event log, the digest must NOT
    report '0 signals' as if the week were quiet — it names the source it could not read under
    'Harness notes (a skip is not a pass)'."""
    db = db_for(tmp_path)                     # a migrated ledger, but nothing was ever recorded

    out = digest.digest(db)

    assert out["signals"] == 0
    assert out["notes"], "an unreadable source must produce a note, never a silent zero"
    assert any("job-event log" in n for n in out["notes"]), out["notes"]

    text = Path(out["report"]).read_text()
    assert "## Harness notes (a skip is not a pass)" in text
    assert "job-event log" in text
    assert "every declared source was readable" not in text
