"""The `s2docs learning <verb>` CLI surface (learning-loop task T14, CLI half) + its exit contract.

Every verb here is only as good as the exit code a CI step or an agent branches on, so each test
asserts the int exit code *and* the reason behind it — on stderr or in the JSON, never a bare number.
The contract (docs/CLI_PLAN.md §2): 0 ok · 1 gate regression · 3 not found · 6 a declared source could
not be read OR the gate could not evaluate · 9 validation.

The load-bearing mapping: `learning gate` translates the gate module's own `2` (could-not-evaluate)
to **6**, NOT to this repo's `2`/EXIT_LOCKED — a "could not evaluate" must never read as "locked".

Seeding uses the shared builders the other learning tests use (a miniature cs2 index, an approved job,
a failed job), so the CLI is exercised against the same real fixtures as the modules it wires.
"""

from __future__ import annotations

import json

from s2docs.config import GAMES
from s2docs.ingest.loader import load_build
from s2docs.ingest.wiki import load_wiki
from s2docs.learning import learning_dir
from tests.conftest import write_tree, write_wiki
from tests.test_learning import _seed_job, db_for
from tests.test_learning_exemplar import _seed as _seed_exemplar
from tests.test_learning_export import _seed_approved
from tests.test_learning_review import _seed_failed

# --------------------------------------------------------------------- harness


def _run_cli(monkeypatch, tmp_path, argv):
    """Run `s2docs <argv>` against a per-test data dir; return the int exit code (test_jobs style)."""
    from s2docs import cli
    from s2docs.config import get_settings

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("S2DOCS_DB", str(tmp_path / "data" / "s2docs.db"))
    get_settings.cache_clear()
    return cli.main(argv)


def _seed_index(tmp_path):
    """A miniature cs2 index at the path the settings resolve, so `learning gate` has something to score."""
    db = db_for(tmp_path)
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "src"))
        load_wiki(conn, write_wiki(tmp_path / "wiki"))
    return db


def _write_golden(questions, thresholds=None):
    d = learning_dir()
    (d / "golden_questions.json").write_text(
        json.dumps({"frozen": True, "questions": questions}), encoding="utf-8")
    if thresholds is not None:
        (d / "thresholds.json").write_text(json.dumps(thresholds), encoding="utf-8")


# --------------------------------------------------------------------- digest


def test_cli_learning_digest_reports_path_signals_and_classes(tmp_path, monkeypatch, capsys):
    _seed_job(tmp_path, origin="human")

    assert _run_cli(monkeypatch, tmp_path, ["learning", "digest", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["report"].endswith(".md")
    assert out["signals"] >= 1
    assert isinstance(out["counts"], dict)


def test_cli_learning_digest_names_an_unreadable_source_and_exits_6(tmp_path, monkeypatch, capsys):
    """Rule 6 at the CLI boundary: a fresh data dir has no job-event journal, so the digest reports the
    unreadable source (in JSON and on stderr) and exits 6 — not a quiet '0 signals' week."""
    assert _run_cli(monkeypatch, tmp_path, ["learning", "digest", "--json"]) == 6
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert any("job-event log unreadable" in n for n in out["notes"])
    assert "error:" in captured.err


def test_cli_learning_harvest_missing_source_exits_6(tmp_path, monkeypatch, capsys):
    assert _run_cli(monkeypatch, tmp_path, ["learning", "harvest", "--json"]) == 6
    assert "job-event log" in capsys.readouterr().err


# ----------------------------------------------------------------------- gate


def test_cli_learning_gate_passes_and_self_test_confirms_it_can_fail(tmp_path, monkeypatch, capsys):
    _seed_index(tmp_path)
    _write_golden(
        [
            {"id": "g1", "kind": "retrieval", "query": "roundtime",
             "expect_kinds": ["convar"], "expect_key": "mp_roundtime"},
            {"id": "g3", "kind": "trap", "query": "how do I bake a cake in Hammer",
             "expect_no_hits": True},
        ],
        {"min_recall": 0.9, "min_questions": 2, "ks": [1, 3, 5], "max_regression": 0.0},
    )

    assert _run_cli(monkeypatch, tmp_path, ["learning", "gate"]) == 0
    assert "recall@5" in capsys.readouterr().out

    assert _run_cli(monkeypatch, tmp_path, ["learning", "gate", "--self-test"]) == 0
    assert "self-test" in capsys.readouterr().out


def test_cli_learning_gate_could_not_evaluate_is_6_not_2(tmp_path, monkeypatch, capsys):
    """The mapping that matters: the gate's own `2` (could not evaluate) becomes EXIT_PARTIAL (6), never
    EXIT_LOCKED (2). An agent branches on the code — 2 would make it retry a lock that is not there."""
    assert _run_cli(monkeypatch, tmp_path, ["learning", "gate"]) == 6
    err = capsys.readouterr().err
    assert "COULD NOT EVALUATE" in err
    assert "locked" not in err.lower()


# ------------------------------------------------------------------------ sft


def test_cli_learning_sft_requires_a_baseline_is_validation_9(tmp_path, monkeypatch, capsys):
    assert _run_cli(monkeypatch, tmp_path, ["learning", "sft", "--require-baseline"]) == 9
    assert "baseline" in capsys.readouterr().err


def test_cli_learning_sft_writes_the_dataset_and_reports_readiness(tmp_path, monkeypatch, capsys):
    _seed_approved(tmp_path, title="occluder wire")
    (learning_dir() / "eval_history.jsonl").write_text(json.dumps({"recall": 0.95}) + "\n", encoding="utf-8")

    assert _run_cli(monkeypatch, tmp_path, ["learning", "sft", "--require-baseline", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dataset"].endswith("sft_dataset.jsonl")
    assert out["report"]["eval_baseline_recall"] == 0.95


# --------------------------------------------------------------------- export


def test_cli_learning_export_surfaces_corpus_stats(tmp_path, monkeypatch, capsys):
    """lab4's load_corpus exits under 8 chunks, so `blocks/bytes/chunks_approx/lab4_ready` must be visible
    without opening the corpus file."""
    _seed_approved(tmp_path, title="occluder wire")
    _write_golden([{"id": "g1", "kind": "retrieval", "query": "roundtime",
                    "expect_kinds": ["convar"], "expect_key": "mp_roundtime"}])

    assert _run_cli(monkeypatch, tmp_path, ["learning", "export", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    stats = out["corpus_stats"]
    for key in ("blocks", "bytes", "chunks_approx", "lab4_ready"):
        assert key in stats, stats
    assert stats["blocks"] >= 1


def test_cli_learning_export_without_a_golden_set_is_6(tmp_path, monkeypatch, capsys):
    _seed_approved(tmp_path, title="occluder wire")
    assert _run_cli(monkeypatch, tmp_path, ["learning", "export"]) == 6
    err = capsys.readouterr().err
    assert "COULD NOT EVALUATE" in err and "golden" in err


# ------------------------------------------------------------------ exemplars


def test_cli_learning_exemplars_writes_and_reports_skips_with_reasons(tmp_path, monkeypatch, capsys):
    """A skipped row is the guard working, not an error: exit 0, with the reasons named."""
    _seed_exemplar(tmp_path, title="occluder wire", verdict="pass")
    _seed_exemplar(tmp_path, title="silent pass", verdict=None)

    assert _run_cli(monkeypatch, tmp_path, ["learning", "exemplars", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["written"]) == 1, out
    assert len(out["skipped"]) == 1, out
    assert "no human verdict" in " ".join(out["skipped"][0]["reasons"])


# ----------------------------------------------------------- review + promote


def test_cli_learning_review_lists_the_correction_queue(tmp_path, monkeypatch, capsys):
    _, job = _seed_failed(tmp_path, title="teleport a bot", purpose="teleport")

    assert _run_cli(monkeypatch, tmp_path, ["learning", "review", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    row = next(r for r in out["rows"] if r["job_id"] == job.id)
    assert row["failure_mode"] == "fail_verdict"
    assert row["promoted"] is False
    assert row["question"]


def test_cli_learning_promote_appends_then_a_duplicate_is_a_no_op(tmp_path, monkeypatch, capsys):
    _, job = _seed_failed(tmp_path, title="teleport a bot")

    argv = ["learning", "promote", "--job", job.id, "--expect", "s2docs:teleport", "--json"]
    assert _run_cli(monkeypatch, tmp_path, argv) == 0
    assert json.loads(capsys.readouterr().out)["appended"] is True

    # a double click must not double the golden set
    assert _run_cli(monkeypatch, tmp_path, argv) == 0
    assert json.loads(capsys.readouterr().out)["appended"] is False


def test_cli_learning_promote_unknown_job_is_not_found_3(tmp_path, monkeypatch, capsys):
    _seed_failed(tmp_path, title="teleport a bot")   # a real ledger, just not for this job id
    assert _run_cli(monkeypatch, tmp_path, ["learning", "promote", "--job", "nope", "--expect", "x"]) == 3
    assert "error:" in capsys.readouterr().err
