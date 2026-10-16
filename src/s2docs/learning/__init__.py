"""Learning loop: turn recorded job outcomes into eval and training signal.

Deterministic and LLM-free by design — the same split as `review.py`: the server scopes and measures,
the agent layer reasons. Nothing in this package makes a network or model call, which is also what
makes an artifact inspectable: you can read which rule produced which row.

Two append-only sources, and nothing else:

    <data_dir>/job_events.jsonl   every timeline event (s2docs.jobs.publish_event)
    <data_dir>/s2docs.db          jobs, job_links, evidence, test_runs

Four artifacts, written into ``<data_dir>/learning/`` by `harvest.py`:

    sft_pairs.jsonl        approved successes -> fine-tune message pairs
    corrections.jsonl      failed/inconclusive runs -> golden-question candidates (need a human label)
    hard_negatives.jsonl   evidence a step was gated on and then contradicted
    gap_report.json        what the index/process could not ground -> what to fix next

and three the model side consumes, written by `export.py`:

    export/corpus.txt          behaviour text for the lab-4 distill corpus
    export/problems.json       Source 2 problems with a mechanically verifiable answer (lab-5 GRPO)
    export/golden_cases.jsonl  the frozen set in the labs' `{id,category,prompt,checker,expected}` shape

The rules, ported from ask-hittle's ``docs/LEARNING_LOOP.md`` (each one paid for by a bad artifact):

  1. Never infer intent from a proxy (answer length, event count). Read the recorded status/verdict.
  2. Default to real usage. Every reader scopes to ``origin='human'`` unless the caller passes
     ``origin=None``, which has to be justified in the calling code.
  3. Tag synthetic callers at intake — ``jobs.enqueue(origin='eval'|'probe'|'warmup'|'driver')``. An
     unknown value is rejected there, so a typo cannot silently become valid.
  4. One definition per concept. The helpers here are the only ones.
  5. Every number shown to a person ships its sample: how many rows, which origin, which window.
  6. A skip is not a pass: an unreadable source raises, it never yields an empty artifact.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from s2docs import jobs as jobsvc
from s2docs.config import get_settings
from s2docs.db.connection import Database

# Mirrors models.JOB_ORIGINS (the intake's closed set). Kept as a tuple here so the loop and its tests
# do not have to import the Pydantic models module to answer "is this a real origin".
ORIGINS: tuple[str, ...] = ("human", "agent", "driver", "eval", "probe", "warmup")
DEFAULT_ORIGIN = "agent"
# Traffic the loop will not learn from unless a caller explicitly asks for it. 'agent' (Hermes' own
# work) is treated as real usage; these four are synthetic by construction.
SYNTHETIC_ORIGINS: tuple[str, ...] = ("driver", "eval", "probe", "warmup")

__all__ = [
    "ORIGINS",
    "DEFAULT_ORIGIN",
    "SYNTHETIC_ORIGINS",
    "LearningSourceMissing",
    "learning_dir",
    "iso",
    "jobs_index",
    "events",
    "report",
]


class LearningSourceMissing(RuntimeError):
    """A declared input could not be read — raised instead of returning an empty artifact.

    Rule 6. A digest built on a silently empty source once reported "0 signals" as if the week had
    been quiet, which is worse than no digest: it reads as evidence of calm.
    """


def learning_dir() -> Path:
    """``<data_dir>/learning``, created on demand.

    Derived per call, never at import time: ``get_settings`` is lru_cached and the test fixture clears
    that cache, so a module-level constant would freeze the first data dir a process ever saw.
    """
    d = get_settings().data_dir / "learning"
    d.mkdir(parents=True, exist_ok=True)
    return d


def iso(when: float | None = None) -> str:
    """A timestamp in the same shape SQLite writes ('...Z'), so string comparison is date comparison."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when if when is not None else time.time()))


def _cutoff(since_days: int) -> str:
    """The ISO boundary "N days ago" — the window every reader takes."""
    return iso(time.time() - since_days * 86400)


def jobs_index(db: Database) -> dict[str, dict]:
    """Every job row by id — the join source that gives a timeline event its provenance."""
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT id, kind, title, target, origin, status, summary, worker, host_id, project_id,"
            " params, created_at, started_at, finished_at FROM jobs"
        ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def events(db: Database, *, origin: str | None = "human", since_days: int | None = None) -> list[dict]:
    """Timeline events joined to their job, scoped by the job's ``origin``.

    The JSONL is the append-only record — it survives a database rebuild, and it is what the driver
    writes as the run happens. A line that will not parse is a hard error: a half-written final line
    from a killed process must be visible, not silently dropped.

    An event whose job is gone (deleted, cascading its children) is skipped rather than raising: there
    is nothing to attribute it to, and it cannot inform a provenance-scoped artifact either way.
    """
    if origin is not None and origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS} or None, not {origin!r}")
    path = jobsvc.job_events_jsonl_path()
    if not path.exists():
        raise LearningSourceMissing(f"no job-event log at {path} — nothing to learn from yet")
    index = jobs_index(db)
    cutoff = _cutoff(since_days) if since_days else None
    out: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError as exc:
            raise LearningSourceMissing(f"{path}:{lineno} is not valid JSON: {exc}") from exc
        job = index.get(rec.get("job_id"))
        if job is None:
            continue
        if origin is not None and job["origin"] != origin:
            continue
        if cutoff and (job.get("created_at") or "") < cutoff:
            continue
        out.append({"origin": job["origin"], "job": job, **rec})
    out.sort(key=lambda e: (e.get("created_at") or "", e.get("id") or 0))
    return out


def report(db: Database, *, since_days: int | None = None) -> dict:
    """Readiness summary: counts by origin, so a thin sample is obvious before any artifact exists.

    Rule 5 — a number never travels without its sample, and the sample starts here.
    """
    index = jobs_index(db)
    by_origin: dict[str, int] = {}
    for job in index.values():
        by_origin[job["origin"]] = by_origin.get(job["origin"], 0) + 1
    evs = events(db, origin=None, since_days=since_days)
    return {
        "generated_at": iso(),
        "since_days": since_days,
        "jobs_by_origin": dict(sorted(by_origin.items())),
        "jobs_total": len(index),
        "events_total": len(evs),
        "real_usage_jobs": by_origin.get("human", 0),
        "synthetic_jobs": sum(by_origin.get(o, 0) for o in SYNTHETIC_ORIGINS),
        "learning_dir": str(learning_dir()),
    }
