"""Export the loop's artifacts in the shapes the cpompa-ai-cloud training labs consume.

    (called by the CLI as)  uv run s2docs learning export

Reads only local artifacts and the job ledger; writes only into ``<data_dir>/learning/export/``.
Nothing is uploaded and no lab is invoked — the handoff is files plus
``docs/learning/TRAINING_HANDOFF.md``.

Three files, three consumers (see the handoff doc for the full contract):

  * ``corpus.txt`` — plain-text **behaviour** for lab4's distill (``lab/lab4_distill/distill.py``, a
    drop-in replacement for its ``corpus.txt``). lab4's ``load_corpus`` chunks it at ``seq_len`` (256)
    and holds the **last 5 chunks out as val**, so it needs **≥ 8 full chunks of ~1 KB each** to run at
    all. The file opens with a line stating how many job blocks and approximate bytes it produced, so
    an operator can tell at a glance whether it is even runnable.
  * ``problems.json`` — verifiable Source 2 problems for lab5's GRPO reward (``lab/lab5_grpo``).
  * ``golden_cases.jsonl`` — the frozen set in the labs' ``{id,category,prompt,checker,expected}`` shape
    (``scripts/eval/golden_set.jsonl``).

The corpus is behaviour, never the indexed API facts: the facts already live in the index and stay
there (ask-hittle: "SFT for tone/format only. Facts stay in RAG"). A distill corpus of API facts would
teach the model to answer from memory instead of calling ``search_docs`` — the opposite of a tool-using
specialist.

Deterministic and offline: no network, no model call.
"""

from __future__ import annotations

import json
from pathlib import Path

from s2docs.db.connection import Database
from s2docs.learning import harvest, jobs_index, learning_dir, reward

__all__ = ["corpus", "problems", "cases", "everything"]

# Approximate size of one lab4 chunk at seq_len 256 (~4 chars/token), used only for the header note.
_CHUNK_BYTES = 1024
_VAL_CHUNKS = 5
_MIN_CHUNKS = 8


def _dir() -> Path:
    d = learning_dir() / "export"
    d.mkdir(parents=True, exist_ok=True)
    return d


def corpus(db: Database, *, origin: str = "human", since_days: int | None = None) -> Path:
    """Plain-text behaviour corpus for lab4 — one ``## job <id> — <title>`` block per approved job.

    Each block carries the request, what it proved, what was learned and what is next — the human's
    own close-out words. Deliberately NOT the indexed API facts (those stay in RAG) and NOT a raw
    transcript dump (trajectory distillation beats raw-log prompting).

    The file is PURE blocks — no manifest or count line — because every byte of it is training text and
    a header would be a large fraction of a small corpus. lab4 chunks this at ``seq_len`` (256) and
    holds the last 5 chunks out as val, so it needs >= 8 full chunks of ~1 KB *or it exits*, so
    readiness is reported by ``corpus_stats()`` (and by ``everything()``) where an operator reads it
    before a training run rather than from a failed one.
    """
    from s2docs.learning import _cutoff

    cutoff = _cutoff(since_days) if since_days else None
    blocks: list[str] = []
    for job_id, job in sorted(jobs_index(db).items()):
        if origin is not None and job["origin"] != origin:
            continue
        if cutoff and (job.get("created_at") or "") < cutoff:
            continue
        if not harvest._approved(job, harvest._verdicts(db, job_id)):
            continue
        s = harvest._summary(job)
        blocks.append("\n".join([
            f"## job {job_id} — {job['title']}",
            f"request: {harvest._goal_text(job)}",
            f"proved: {s.get('why', '')}",
            f"learned: {s.get('learned', '')}",
            f"next: {s.get('next', '')}",
            "",
        ]))
    path = _dir() / "corpus.txt"
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def corpus_stats(path: str | Path) -> dict:
    """Blocks, bytes and the approximate chunk count of a written corpus.

    The readiness number for whoever runs the training: lab4's ``load_corpus`` chunks at ``seq_len``
    (256) and exits when it cannot find ``_MIN_CHUNKS`` full chunks, so a corpus too small to train on
    should be obvious from the export instead of from a failed run. Reported here rather than inside
    ``corpus.txt``, because that file is training text.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    n_bytes = len(text.encode("utf-8"))
    chunks = max(1, n_bytes // _CHUNK_BYTES)
    return {"blocks": text.count("## job"), "bytes": n_bytes, "chunks_approx": chunks,
            "min_chunks": _MIN_CHUNKS, "val_chunks": _VAL_CHUNKS,
            "lab4_ready": chunks >= _MIN_CHUNKS}


def problems(db: Database, *, origin: str = "human") -> Path:
    """Verifiable Source 2 problems for lab5's GRPO reward.

    Each problem carries the checker that decides it (``s2docs_script``/``s2docs_cfg``/``s2docs_lookup``
    /``exact`` — see ``reward.py``), mapped from the approved run's evidence by
    ``reward.checker_for``. A problem with no mechanical checker is **dropped**, never emitted with a
    guessed checker: an unverifiable reward is a preference pair in disguise.
    """
    out: list[dict] = []
    for job_id, job in sorted(jobs_index(db).items()):
        if origin is not None and job["origin"] != origin:
            continue
        if not harvest._approved(job, harvest._verdicts(db, job_id)):
            continue
        goal = harvest._goal_text(job)
        for row in harvest._evidence_for_job(db, job_id):
            check = reward.checker_for(row)
            if check is None:
                continue                      # no mechanical checker -> not trainable here
            out.append({
                "problem": goal,
                "answer": check.expected,
                "checker": check.name,
                "job_id": job_id,
                "evidence_ref": row["id"],
            })
    path = _dir() / "problems.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return path


def cases(db: Database) -> Path:
    """The frozen golden set in cpompa's ``{id, category, prompt, checker, expected}`` shape.

    Derived from ``gate.questions_file()`` (the one definition of the golden set — rule 4). The
    ``checker`` is translated to the lab's vocabulary (``exact`` for a lookup key, ``exact_empty`` for
    a trap that must return nothing) and the id namespace is left alone, so a case can be traced back
    here.
    """
    from s2docs.learning import gate as gate_mod

    lines = []
    for q in gate_mod.questions_file():
        lines.append(json.dumps({
            "id": q["id"],
            "category": q.get("category", q.get("kind", "retrieval")),
            "prompt": q["query"],
            "checker": "exact_empty" if q.get("kind") == "trap" else "exact",
            "expected": q.get("expect_key", ""),
        }))
    path = _dir() / "golden_cases.jsonl"
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def everything(db: Database, *, origin: str = "human", since_days: int | None = None) -> dict:
    """Run all three exports and return their paths, plus the corpus readiness numbers."""
    corpus_path = corpus(db, origin=origin, since_days=since_days)
    return {
        "corpus": str(corpus_path),
        "corpus_stats": corpus_stats(corpus_path),
        "problems": str(problems(db, origin=origin)),
        "cases": str(cases(db)),
        "dir": str(_dir()),
    }
