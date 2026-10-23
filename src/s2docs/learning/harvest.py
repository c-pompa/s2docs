"""The four harvesters: turn recorded outcomes into the loop's artifacts.

Deterministic and offline. Each harvester **returns** the rows it would write, so a caller (or a
test) can inspect the artifact before anything touches disk; `harvest()` is the only function in this
module that writes, and every `write_*` helper is a one-liner so the "what" and the "where" stay apart.

The rules (see `s2docs.learning`'s docstring) are enforced here, not documented here:

  1. A verdict is read from the recorded `human-confirm` row (`_verdicts`) — never inferred from a
     count, a length or an event.
  2. Provenance defaults to `origin='human'`. A harvester reads a synthetic run only when the caller
     passes the origin explicitly, and every row records the origin it came from.
  4. `_verdicts`, `_summary`, `_goal_text` and `_evidence_for_job` are the only definitions of their
     concept in the tree.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from s2docs.db.connection import Database
from s2docs.learning import SYNTHETIC_ORIGINS, _cutoff, jobs_index, learning_dir
from s2docs.learning import events as _events

__all__ = [
    "HARD_NEG_MIN_COUNT",
    "SYSTEM_STYLE",
    "sft_pairs",
    "corrections",
    "hard_negatives",
    "gap_report",
    "search_terms",
    "harvest",
    "annotate_jobs",
    "write_sft_pairs",
    "write_corrections",
    "write_hard_negatives",
    "write_gap_report",
]

# A hard negative must fail more than once: one miss is noise, three is a pattern.
HARD_NEG_MIN_COUNT = 3

SYSTEM_STYLE = (
    "You are the Source 2 docs assistant for CS2 and Deadlock. Answer only from the indexed API "
    "surface (convars, entities, schema, game events, Pulse, cs_script). Cite the identifiers you "
    "used, and say plainly when the index does not cover the question."
)

# The timeline levels that carry a run's own account of itself; a job with none of these has nothing
# for the SFT harvester to be an example of.
_EVENT_LEVELS = ("milestone", "info", "error", "crash", "warn", "learning")


# ------------------------------------------------------------------- primitives


def _verdicts(db: Database, job_id: str) -> list[dict]:
    """The human-confirm verdict rows that answer a job's steps (oldest first).

    A verdict row is anchored to the job by ``ref == job_id`` — exactly what `jobs.verdict` writes.
    Reading them here, and only here, is rule 4: one definition of "what did the human say".
    """
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT id, step_id, data, created_at FROM evidence "
            "WHERE kind = 'human-confirm' AND ref = ? ORDER BY created_at, id",
            (job_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            data = json.loads(r["data"])
        except ValueError:
            data = {}
        out.append({"id": r["id"], "step_id": r["step_id"], "created_at": r["created_at"], **data})
    return out


def _summary(job: dict) -> dict:
    """A job's close-out summary, or {} when it is absent/corrupt. A corrupt summary is a job that
    cannot be learned from — not a crash."""
    raw = job.get("summary")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _params(job: dict) -> dict:
    raw = job.get("params")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return {}


def _goal_text(job: dict) -> str:
    """What the run was asked to do, in the words of the intake. `params.purpose`/`params.expected`
    are the request; `title`/`target` are its identity."""
    params = _params(job)
    parts = [job.get("title") or "", job.get("target") or ""]
    if params.get("purpose"):
        parts.append(f"Purpose: {params['purpose']}")
    if params.get("expected"):
        parts.append(f"Expected: {params['expected']}")
    return "\n".join(p for p in parts if p)


def _approved(job: dict, verdicts: list[dict]) -> bool:
    """A job worth imitating: it passed, a human said pass on every step they saw, and the close-out
    carries real words in `learned`.

    `passed` with zero verdicts is exactly the state `inconclusive` was invented for — unverified, not
    approved (rule 1: read the verdict, never the status alone).
    """
    if job["status"] != "passed" or not verdicts:
        return False
    if any(v.get("verdict") != "pass" for v in verdicts):
        return False
    return bool((_summary(job).get("learned") or "").strip())


# ------------------------------------------------------- T4: sft_pairs (approved)


def sft_pairs(db: Database, *, origin: str | None = "human",
              since_days: int | None = None) -> list[dict]:
    """Approved successes -> one training example per *distinct target*.

    Deduplicated on purpose: three runs of `nav-guard@abc123` are one example, and counting three
    flatters the sample against the ~500-example gate. The newest run wins — it was made against the
    current tree.

    The assistant turn is a placeholder. The target text is produced by re-running the job in the
    frozen environment, never by copying an old transcript: a stale answer teaches a stale API.
    """
    jobs_with_events = {
        e["job_id"]: e["job"] for e in _events(db, origin=origin, since_days=since_days)
        if e.get("level") in _EVENT_LEVELS
    }
    latest: dict[str, tuple[str, dict, list[dict]]] = {}
    for job_id, job in jobs_with_events.items():
        verdicts = _verdicts(db, job_id)
        if not _approved(job, verdicts):
            continue
        key = job["target"]
        stamp = job.get("finished_at") or job.get("created_at") or ""
        prev = latest.get(key)
        if prev is None or stamp >= prev[0]:
            latest[key] = (stamp, job, verdicts)

    out = []
    for target, (_, job, verdicts) in sorted(latest.items()):
        s = _summary(job)
        out.append({
            "messages": [
                {"role": "system", "content": SYSTEM_STYLE},
                {"role": "user", "content": _goal_text(job)},
                {"role": "assistant", "content": "<regenerate-at-run-time>"},
            ],
            "meta": {
                "origin": "human-approval",
                "job_id": job["id"],
                "origin_kind": job["origin"],
                "target": target,
                "kind": job["kind"],
                "verdicts": len(verdicts),
                "learned": s.get("learned"),
                "regenerate": ("re-run the job in the frozen environment and record the transcript; "
                               "do not copy the old one"),
            },
        })
    out.sort(key=lambda p: p["meta"]["job_id"])
    return out


def write_sft_pairs(rows: list[dict]) -> Path:
    path = learning_dir() / "sft_pairs.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# -------------------------------------------- T5: corrections (a queue, not a dataset)

_FAILED_STATUSES = ("failed", "error", "cancelled", "inconclusive")


def corrections(db: Database, *, origin: str | None = None,
                since_days: int | None = None) -> list[dict]:
    """Runs the system got wrong -> golden-question candidates.

    `origin=None` by default on purpose: a failure is worth learning from whoever asked for it, and
    the row records who did (`origin`) so a consumer can filter instead of guess. These are the
    questions production actually failed; if a human can say what the right outcome was, they belong
    in the golden set. Until then `needs_human_label` keeps them out of scoring — an unlabeled
    correction scored as if it were a case is how an eval set starts lying.
    """
    index = jobs_index(db)
    cutoff = _cutoff(since_days) if since_days else None
    out = []
    for job_id, job in sorted(index.items()):
        if origin is not None and job["origin"] != origin:
            continue
        if cutoff and (job.get("created_at") or "") < cutoff:
            continue
        if job["status"] not in _FAILED_STATUSES:
            continue
        verdicts = _verdicts(db, job_id)
        bad = [v for v in verdicts if v.get("verdict") == "fail"]
        mode = ("fail_verdict" if bad else
                "inconclusive" if job["status"] == "inconclusive" else job["status"])
        out.append({
            "question": _goal_text(job) or job["title"],
            "job_id": job_id,
            "target": job["target"],
            "origin": job["origin"],
            "status": job["status"],
            "failure_mode": mode,
            "failed_steps": [v.get("step_id") or v.get("step") for v in bad],
            "notes": [v.get("notes") for v in bad if v.get("notes")],
            "summary": _summary(job),
            "evidence_refs": [v["id"] for v in verdicts],
            "needs_human_label": True,
        })
    return out


def write_corrections(rows: list[dict]) -> Path:
    path = learning_dir() / "corrections.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# ---------------------------------------------- T6: hard_negatives (contradicted evidence)


def hard_negatives(db: Database, *, origin: str | None = None, since_days: int | None = None,
                   min_count: int = HARD_NEG_MIN_COUNT) -> list[dict]:
    """Evidence that was produced for a step and then contradicted by the human verdict.

    Only rows where a verdict *existed* count. A run with no verdict proves nothing about the evidence,
    and counting those made every artifact in a driver run look like a distractor — the defect that
    inflated ask-hittle's list to 163 rows over 58 real ones.

    Identity is `(step_id, basename(ref))`: the same file cited by path from two roots is the same
    artifact, so a path-prefix difference cannot split one negative into two. Provenance is kept per
    record (`misses_by_origin`) so a consumer that wants only human evidence can filter.
    """
    index = jobs_index(db)
    cutoff = _cutoff(since_days) if since_days else None
    seen: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"misses": 0, "origins": {}, "jobs": []}
    )
    for job_id, job in index.items():
        if origin is not None and job["origin"] != origin:
            continue
        if cutoff and (job.get("created_at") or "") < cutoff:
            continue
        verdicts = _verdicts(db, job_id)
        if not verdicts:
            continue                                  # nothing could have been contradicted
        failed_steps = {v.get("step_id") or v.get("step") for v in verdicts
                        if v.get("verdict") == "fail"}
        if not failed_steps:
            continue
        for row in _evidence_for_job(db, job_id):
            if row["step_id"] not in failed_steps:
                continue
            key = (row["step_id"], _basename(row.get("ref")))
            rec = seen[key]
            rec["misses"] += 1
            rec["origins"][job["origin"]] = rec["origins"].get(job["origin"], 0) + 1
            if job_id not in rec["jobs"]:
                rec["jobs"].append(job_id)
    out = []
    for (step_id, name), rec in seen.items():
        if rec["misses"] < min_count:
            continue
        out.append({
            "artifact": name,
            "step_id": step_id,
            "misses": rec["misses"],
            "misses_by_origin": dict(sorted(rec["origins"].items())),
            "jobs": rec["jobs"][:10],
            "note": ("evidence this step was gated on, in runs the human failed — the checker or the "
                     "artifact is wrong, not the corpus"),
        })
    out.sort(key=lambda x: -x["misses"])
    return out


def _basename(ref: str | None) -> str:
    from pathlib import PurePosixPath
    return PurePosixPath((ref or "").replace("\\", "/")).name or "(no ref)"


def _evidence_for_job(db: Database, job_id: str) -> list[dict]:
    """The evidence rows a job produced or its steps were judged on.

    Reached two ways, both deterministic: rows explicitly linked to the job
    (`job_links.kind='evidence'`) and rows anchored to the job's linked test runs. A row that
    arrives by neither path is not this job's evidence.
    """
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT e.id, e.step_id, e.kind, e.ref, e.data, e.created_at FROM evidence e "
            "JOIN job_links l ON l.kind = 'evidence' AND l.ref_id = e.id AND l.job_id = ? "
            "UNION "
            "SELECT e.id, e.step_id, e.kind, e.ref, e.data, e.created_at FROM evidence e "
            "JOIN job_links l ON l.kind = 'test_run' AND l.job_id = ? AND e.test_run_id = l.ref_id",
            (job_id, job_id),
        ).fetchall()
    return [{"id": r["id"], "step_id": r["step_id"], "kind": r["kind"], "ref": r["ref"],
             "data": json.loads(r["data"]), "created_at": r["created_at"]} for r in rows]


def write_hard_negatives(rows: list[dict]) -> Path:
    path = learning_dir() / "hard_negatives.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# ------------------------------------ T7: gap_report (retrieval miss vs. a true gap)


def gap_report(db: Database, *, origin: str | None = None, since_days: int | None = None,
               search=None, max_queries: int = 20) -> dict:
    """What could not be grounded, split by what actually went wrong.

    Reads recorded status only — never a proxy. ask-hittle's gap report was ~5x inflated by calling
    any short answer a refusal; the fix was to read `outcome`, and the same discipline applies here.

    `search` is injected so the same function serves the CLI (real `Repository.search`) and a unit
    test (a stub). When it returns hits for a failed goal's subject, that is a **retrieval miss** —
    the index covers the topic and the run still failed — reported separately from a **true gap**,
    which is the only list that justifies ingesting more.
    """
    index = jobs_index(db)
    cutoff = _cutoff(since_days) if since_days else None
    buckets: dict[str, list[dict]] = {"no_verdict_runs": [], "missing_evidence_steps": [],
                                      "blocked_goals": [], "empty_runs": []}
    for job_id, job in sorted(index.items()):
        if origin is not None and job["origin"] != origin:
            continue
        if cutoff and (job.get("created_at") or "") < cutoff:
            continue
        verdicts = _verdicts(db, job_id)
        entry = {"job_id": job_id, "goal": _goal_text(job) or job["title"],
                 "origin": job["origin"], "status": job["status"]}
        if job["status"] == "inconclusive" or (job["status"] == "passed" and not verdicts):
            buckets["no_verdict_runs"].append(entry)
        elif job["status"] in ("failed", "error"):
            failed = [v for v in verdicts if v.get("verdict") == "fail"]
            if failed:
                buckets["missing_evidence_steps"].append(
                    {**entry, "steps": [v.get("step_id") or v.get("step") for v in failed]})
            else:
                buckets["empty_runs"].append(entry)
        elif job["status"] == "cancelled":
            buckets["blocked_goals"].append({**entry, "reason": _params(job).get("reason")})

    misses, true_gaps = [], []
    if search is not None:
        for bucket in ("no_verdict_runs", "missing_evidence_steps", "empty_runs"):
            for entry in buckets[bucket][:max_queries]:
                hits, term = [], None
                for term in _terms(entry["goal"])[:4]:
                    hits = search(term)
                    if hits:
                        break
                if hits:
                    misses.append({**entry, "term": term,
                                   "in_index": [getattr(h, "key", str(h)) for h in hits[:3]],
                                   "note": "the index covers this subject — suspect retrieval, "
                                           "the prompt, or the checker before ingesting more"})
                else:
                    true_gaps.append(entry)
    return {
        **buckets,
        "likely_retrieval_misses": misses,
        "true_gaps": true_gaps,
        "generated_at": _cutoff(0),
        "sample": {
            "origin": origin or "all",
            "since_days": since_days,
            "jobs": len(index),
            "real_usage_jobs": sum(1 for j in index.values() if j["origin"] == "human"),
        },
        "note": ("no gaps over a thin sample means 'not enough usage', not 'complete coverage' — read "
                 "the sample block first"),
    }


def _terms(text: str) -> list[str]:
    """Subject terms from a goal: 3+ character words, longest first, identifiers preserved."""
    import re
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")
    seen: list[str] = []
    for w in sorted(words, key=len, reverse=True):
        if w.lower() not in {s.lower() for s in seen}:
            seen.append(w)
    return seen


def write_gap_report(report: dict) -> Path:
    path = learning_dir() / "gap_report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def search_terms(db: Database, term: str, *, limit: int = 3) -> list:
    """The one production search entry point for the gap report (mirrors MCP's `search_docs`)."""
    from s2docs.db.repository import Repository
    with db.reader() as conn:
        return list(Repository(conn).search(term, limit=limit).items)


# ------------------------------------------------------- T8/T13: the harvest driver


def _job_ids_from(pairs, corr, negs, gaps) -> list[str]:
    ids: set[str] = set()
    for p in pairs:
        ids.add(p["meta"]["job_id"])
    for row in corr:
        ids.add(row["job_id"])
    for row in negs:
        ids.update(row["jobs"])
    for bucket in ("no_verdict_runs", "missing_evidence_steps", "blocked_goals", "empty_runs",
                   "likely_retrieval_misses", "true_gaps"):
        for entry in gaps.get(bucket, []):
            ids.add(entry["job_id"])
    return sorted(ids)


def annotate_jobs(db: Database, counts: dict, *, jobs: list[str], artifacts: dict) -> int:
    """Append one `learning` event per job this harvest used, so the job page shows it.

    Only jobs whose origin is real usage are annotated: tagging a driver/eval/probe/warmup run with
    "learning" would put synthetic traffic back in the human's view, which is what provenance was
    added to prevent.
    """
    from s2docs import jobs as jobsvc
    n = 0
    for job_id in jobs:
        try:
            job = jobsvc.get(db, job_id)
        except Exception:  # noqa: BLE001 — a vanished job is not a harvest failure
            continue
        if job.origin in SYNTHETIC_ORIGINS:
            continue
        jobsvc.event(db, job_id, level="learning",
                     message=f"harvested: {', '.join(f'{k}={v}' for k, v in counts.items())}",
                     data={"counts": counts, "artifacts": artifacts})
        n += 1
    return n


def harvest(db: Database, *, origin: str | None = "human", since_days: int | None = None,
            search=None, annotate: bool = False) -> dict:
    """Run all four harvesters, write their artifacts, and report the sample behind each count.

    Never narrows `origin` on its own: the default is real usage, and a caller that wants everything
    passes `origin=None` and says why (rule 2).
    """
    pairs = sft_pairs(db, origin=origin, since_days=since_days)
    corr = corrections(db, origin=origin, since_days=since_days)
    negs = hard_negatives(db, origin=origin, since_days=since_days)
    gaps = gap_report(db, origin=origin, since_days=since_days, search=search)
    paths = {
        "sft_pairs": write_sft_pairs(pairs),
        "corrections": write_corrections(corr),
        "hard_negatives": write_hard_negatives(negs),
        "gap_report": write_gap_report(gaps),
    }
    counts = {"sft_pairs": len(pairs), "corrections": len(corr), "hard_negatives": len(negs),
              "gaps": len(gaps["true_gaps"]) + len(gaps["likely_retrieval_misses"])}
    artifacts = {k: str(v) for k, v in paths.items()}
    out = {
        "counts": counts,
        "sample": {"origin": origin or "all", "since_days": since_days},
        "artifacts": artifacts,
        "generated_at": _cutoff(0),
    }
    if annotate:
        out["annotated"] = annotate_jobs(db, counts, jobs=_job_ids_from(pairs, corr, negs, gaps),
                                         artifacts=artifacts)
    return out
