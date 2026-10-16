"""Approved human confirmations -> exemplar docs a retrieval index can ingest.

    (called by the CLI as)  uv run s2docs learning exemplar

One markdown doc per approved confirmation, written into ``<data_dir>/learning/exemplars/`` with the
provenance in a frontmatter block. Ported from hitl-v1's ``app/exemplar.py``, where each rule was paid
for by a bad corpus doc:

  * **The guard is the point.** An exemplar is the retrieval source for "what does a good run of this
    look like", so a confirmation whose job recorded an unmet gate, a missing artifact or a late /
    superseded verdict is *not* an exemplar — it only looked approved, and one wrong lesson in the
    corpus poisons the rest. Bad rows are SKIPPED and reported in ``skipped``, never exported.
  * **Non-fatal.** The export must never raise for a bad row: approval must not fail because the
    corpus side did. Every failure is a report, not an exception.

Deterministic and offline — a file write, no network, no model call.
"""

from __future__ import annotations

from s2docs.db.connection import Database
from s2docs.learning import _cutoff, harvest, iso, jobs_index, learning_dir

__all__ = ["export_approved"]

# One slug per title so a human can read the directory; the job id keeps it unique.
_SLUG_MAX = 60


def _verdict_flag(v: dict, key: str):
    """Read a flag off a verdict row. `_verdicts` merges the evidence `data` up to the top level, so a
    flag lives on the row itself — but this tolerates a nested `data` block too, so a future change to
    the row shape cannot silently disable the guard."""
    if v.get(key) is not None:
        return v.get(key)
    data = v.get("data")
    if isinstance(data, dict):
        return data.get(key)
    return None


def _suspicious(job: dict, verdicts: list[dict]) -> list[str]:
    """Reasons this confirmation must not become an exemplar. Empty list = clean."""
    reasons: list[str] = []
    if job["status"] != "passed":
        reasons.append(f"status is {job['status']}")
    if not verdicts:
        reasons.append("no human verdict recorded")
    for v in verdicts:
        if v.get("verdict") != "pass":
            reasons.append(f"verdict {v.get('verdict')} on step {v.get('step_id')}")
        if _verdict_flag(v, "late"):
            reasons.append("late verdict — attributed after the window closed")
        gate = _verdict_flag(v, "gate_unmet")
        if gate:
            reasons.append(f"gate unmet: {gate}")
        if _verdict_flag(v, "artifact_missing"):
            reasons.append("a required artifact was missing")
    return reasons


def _slug(title: str | None, job_id: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (title or job_id).lower()).strip("-")
    return (slug or "run")[:_SLUG_MAX].strip("-")


def _doc(job: dict, job_id: str, verdicts: list[dict]) -> str:
    s = harvest._summary(job)
    steps = ", ".join(str(v.get("step_id") or v.get("step") or "-") for v in verdicts)
    lines = [
        "---",
        f"job_id: {job_id}",
        f"target: {job['target']}",
        f"kind: {job['kind']}",
        f"origin: {job['origin']}",
        "verdict: pass",
        f"steps: {steps}",
        f"generated_at: {iso()}",
        "---",
        "",
        f"# Passing run: {job['title']}",
        "",
        f"**What was asked:** {s.get('what', '')}",
        "",
        f"**What it proved:** {s.get('why', '')}",
        "",
        f"**What was learned:** {s.get('learned', '')}",
        "",
        f"**Next:** {s.get('next', '')}",
        "",
        "## Human verdicts",
        "",
    ]
    lines += [
        f"- step `{v.get('step_id') or v.get('step') or '-'}` → **{v.get('verdict')}**"
        f"{(' — ' + v['notes']) if v.get('notes') else ''}"
        for v in verdicts
    ]
    return "\n".join(lines) + "\n"


def export_approved(db: Database, *, origin: str = "human",
                    since_days: int | None = None) -> dict:
    """One doc per approved job into ``<data_dir>/learning/exemplars/``.

    Returns ``{"written": [paths], "skipped": [{"job_id", "reasons"}], "dir": <dir>}``. Never raises
    for a bad row — it reports it, because a corpus export must not fail the run that produced it.
    """
    out_dir = learning_dir() / "exemplars"
    out_dir.mkdir(parents=True, exist_ok=True)
    cutoff = _cutoff(since_days) if since_days else None
    written: list[str] = []
    skipped: list[dict] = []
    for job_id, job in sorted(jobs_index(db).items()):
        if origin is not None and job["origin"] != origin:
            continue
        if cutoff and (job.get("created_at") or "") < cutoff:
            continue
        try:
            verdicts = harvest._verdicts(db, job_id)
            reasons = _suspicious(job, verdicts)
            if reasons:
                skipped.append({"job_id": job_id, "reasons": reasons})
                continue
            path = out_dir / f"exemplar-{job_id}-{_slug(job.get('title'), job_id)}.md"
            path.write_text(_doc(job, job_id, verdicts), encoding="utf-8")
            written.append(str(path))
        except Exception as exc:  # noqa: BLE001 — a bad row is reported, never raised
            skipped.append({"job_id": job_id, "reasons": [f"export failed: {exc}"]})
    return {"written": written, "skipped": skipped, "dir": str(out_dir)}
