"""Job ledger + queue: one durable record per requested test / demo / final run.

A CS2 test run today is spread over test runs, host locks, evidence rows, project steps and chat, so
nothing answers "what was asked, what is queued, what is running where, what broke, show me the
screen, and what did we learn". A **job** is that one record: the intake (`params`), a timeline
(`job_events`), artifacts (screenshots, logs, bundles), links to the canonical records, and a
mandatory close-out summary.

Jobs are a **ledger + queue, not a lock.** They never claim the host lease — the canonical tools
(director run, `compile_map.py`, `test_start`) keep claiming it as today, otherwise they would 409.
A job links to those records instead. Queue order is per host: ``position`` ascending, then
``created_at``; ``next_job`` returns the head queued job for a host only when that host's lease is
free.

Every event also appends one line to ``<data_dir>/job_events.jsonl`` (advertised on ``GET
/api/health`` as ``job_events_jsonl``) so a coordinator can watch a local file instead of polling —
same push pattern as the human-confirm channel in :mod:`s2docs.confirms`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
from pathlib import Path

from s2docs import evidence, hosts, weburls
from s2docs import models as m
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.db.repository import NotFound

logger = logging.getLogger("s2docs")

KINDS: tuple[str, ...] = m.JOB_KINDS
STATUSES: tuple[str, ...] = m.JOB_STATUSES
ACTIVE_STATUSES: tuple[str, ...] = m.ACTIVE_JOB_STATUSES
TERMINAL_STATUSES: tuple[str, ...] = m.TERMINAL_JOB_STATUSES
LEVELS: tuple[str, ...] = m.JOB_EVENT_LEVELS
LINK_KINDS: tuple[str, ...] = m.JOB_LINK_KINDS
FINISH_STATUSES: tuple[str, ...] = ("passed", "failed", "error", "cancelled", "inconclusive")
VERDICTS: tuple[str, ...] = m.VERDICTS
CONFIRM_SOURCES: tuple[str, ...] = m.CONFIRM_SOURCES
DEFAULT_VERDICT_SOURCE = "web"

SUMMARY_FIELDS: tuple[str, ...] = ("what", "why", "learned", "next")
SUMMARY_MAX = 600
SCREENSHOT_MODES: tuple[str, ...] = ("each_step", "on_request", "none")
DEFAULT_MAX_MINUTES = 5  # the user rule: test/demo runs cap at 5 min unless a reason is given

MAX_ARTIFACT_BYTES = 25 * 1024 * 1024  # 25 MB
ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
ARTIFACT_CONTENT_TYPES: tuple[str, ...] = (
    "image/jpeg",
    "image/png",
    "image/jpg",
    "text/plain",
    "application/json",
    "application/zip",
    "application/octet-stream",  # crash dumps (.dmp/.mdmp) + Windows event logs (.evtx)
)
LOG_TAIL_MAX = 64 * 1024  # cap data.log_tail so a runaway log can't bloat a row

JOBS_JSONL_NAME = "job_events.jsonl"

_ARTIFACT_DIR_NAME = "artifacts"

# Content type to serve a stored artifact as, by extension (artifacts are files; no metadata table).
# Crash artifacts (.dmp/.mdmp/.evtx) have no dedicated type, so they are served as a binary download.
_CONTENT_TYPE_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".json": "application/json",
    ".zip": "application/zip",
    ".dmp": "application/octet-stream",
    ".mdmp": "application/octet-stream",
    ".evtx": "application/octet-stream",
}


class JobError(ValueError):
    """Base class for job failures. Subclasses map to specific HTTP statuses."""


class JobValidationError(JobError):
    """The intake/summary/event is missing or malformed. Carries the offending field names (422)."""

    def __init__(self, message: str, fields: list[str] | None = None):
        super().__init__(message)
        self.fields = fields or []


class JobConflict(JobError):
    """A state conflict: an illegal transition, an artifact that already exists, a held lease (409)."""


def _s(value: object) -> bool:
    return bool(value and str(value).strip())


# ---------------------------------------------------------------------- push


def job_events_jsonl_path() -> Path:
    """The append-only job-event journal: ``<data_dir>/job_events.jsonl``."""
    return get_settings().data_dir / JOBS_JSONL_NAME


def publish_event(event: m.JobEvent) -> None:
    """Append one event to the watchable jsonl, best-effort. Never raises (the DB row is already durable)."""
    line = json.dumps(event.model_dump(), separators=(",", ":")) + "\n"
    try:
        path = job_events_jsonl_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:  # side channel must not break the recording path
        logger.exception("job event push failed for job %s", event.job_id)


# ----------------------------------------------------------------------- rows


def _event(row: sqlite3.Row) -> m.JobEvent:
    return m.JobEvent(
        id=row["id"],
        job_id=row["job_id"],
        level=row["level"],
        phase=row["phase"],
        message=row["message"],
        data=json.loads(row["data"]),
        created_at=row["created_at"],
    )


def _artifact_url(job_id: str, name: str) -> str:
    return f"/api/artifacts/{job_id}/{name}"


def artifacts_dir(job_id: str) -> Path:
    """The per-job artifact directory (``<data_dir>/artifacts/<job_id>``). No path components from names."""
    return get_settings().data_dir / _ARTIFACT_DIR_NAME / job_id


def check_artifact_name(name: str) -> None:
    """Reject anything that is not a plain file name: regex-valid, and never '.'/'..' (path traversal)."""
    if not ARTIFACT_NAME_RE.match(name) or name in (".", ".."):
        raise JobValidationError(
            f"invalid artifact name {name!r}: ^[A-Za-z0-9._-]{{1,120}}$ with no path components", ["name"]
        )


def content_type_for(name: str) -> str | None:
    """The content type to serve a stored artifact as, by extension; None when the extension is unknown."""
    return _CONTENT_TYPE_BY_EXT.get(Path(name).suffix.lower())


def artifact_path(job_id: str, name: str) -> Path:
    check_artifact_name(name)
    base = artifacts_dir(job_id).resolve()
    path = (base / name).resolve()
    # Defense in depth: the resolved path must stay inside the job's own directory.
    if path != base and base not in path.parents:
        raise JobValidationError(f"invalid artifact name {name!r}", ["name"])
    return path


def _artifacts(job_id: str) -> list[m.JobArtifact]:
    base = artifacts_dir(job_id)
    if not base.is_dir():
        return []
    out: list[m.JobArtifact] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_file():
            continue
        data = entry.read_bytes()
        out.append(
            m.JobArtifact(
                name=entry.name,
                url=_artifact_url(job_id, entry.name),
                md5=hashlib.md5(data).hexdigest(),
                size=len(data),
            )
        )
    return out


def _job(row: sqlite3.Row) -> m.Job:
    return m.Job(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        target=row["target"],
        host_id=row["host_id"],
        status=row["status"],
        position=row["position"],
        params=json.loads(row["params"]),
        requested_by=row["requested_by"],
        worker=row["worker"],
        phase=row["phase"],
        project_id=row["project_id"],
        summary=json.loads(row["summary"]) if row["summary"] else None,
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        updated_at=row["updated_at"],
    )


def _current_step(events: list[m.JobEvent]) -> m.JobCurrentStep | None:
    """Derive the step awaiting a verdict from the timeline.

    A **step prompt** is an ``info`` event whose ``data`` carries ``prompt`` (the driver posts one per
    step with ``phase`` = the step id). ``current_step`` is the newest such prompt plus whether a
    **verdict milestone** (a ``milestone`` event carrying ``data.verdict``) landed *after* it — from any
    source (chat, console, web, coordinator), so a verdict typed in game answers the webapp card too.
    """
    prompt: m.JobEvent | None = None
    for e in events:
        if e.level == "info" and isinstance(e.data, dict) and "prompt" in e.data:
            prompt = e  # events are ordered by id ascending -> the last match is the newest
    if prompt is None:
        return None
    verdict_event: m.JobEvent | None = None
    for e in events:
        if (
            e.level == "milestone"
            and e.id > prompt.id
            and isinstance(e.data, dict)
            and e.data.get("verdict") in VERDICTS
        ):
            verdict_event = e
    data = prompt.data
    ans = None
    if verdict_event is not None:
        vd = verdict_event.data
        ans = m.StepVerdict(
            verdict=str(vd.get("verdict")),
            source=vd.get("source"),
            notes=vd.get("notes"),
            evidence_id=vd.get("evidence_id"),
            at=verdict_event.created_at,
        )
    return m.JobCurrentStep(
        phase=prompt.phase,
        prompt=data.get("prompt"),
        look_for=data.get("look_for"),
        step_index=data.get("step_index"),
        step_total=data.get("step_total"),
        respond_hint=data.get("respond_hint"),
        deadline_utc=data.get("deadline_utc"),
        asked_at=prompt.created_at,
        event_id=prompt.id,
        verdict=ans,
    )


def _enrich(conn: sqlite3.Connection, job: m.Job, *, with_events: bool = True) -> m.Job:
    if with_events:
        rows = conn.execute("SELECT * FROM job_events WHERE job_id = ? ORDER BY id", (job.id,)).fetchall()
        job.events = [_event(r) for r in rows]
        job.error_count = sum(1 for e in job.events if e.level == "error")
        job.crash_count = sum(1 for e in job.events if e.level == "crash")
        job.current_step = _current_step(job.events)
    else:
        counts = conn.execute(
            "SELECT level, count(*) AS n FROM job_events WHERE job_id = ? AND level IN ('error', 'crash') "
            "GROUP BY level",
            (job.id,),
        ).fetchall()
        by_level = {r["level"]: r["n"] for r in counts}
        job.error_count = by_level.get("error", 0)
        job.crash_count = by_level.get("crash", 0)
    job.links = [
        m.JobLink(kind=r["kind"], ref_id=r["ref_id"])
        for r in conn.execute("SELECT kind, ref_id FROM job_links WHERE job_id = ? ORDER BY kind, ref_id", (job.id,))
    ]
    job.artifacts = _artifacts(job.id)
    # The page URL in the web UI. Derived from the request in the REST layer (which overrides this);
    # here it is the setting / LAN fallback, so MCP and CLI callers get a usable URL for free.
    job.web_url = weburls.job_url(weburls.public_base_url(), job.id)
    return job


def get(db: Database, job_id: str) -> m.Job:
    with db.reader() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFound(f"no job {job_id!r}")
        return _enrich(conn, _job(row))


def list_jobs(db: Database, *, status: str | None = None, host_id: str | None = None, limit: int = 100) -> list[m.Job]:
    """Jobs newest first. ``status='active'`` means queued + running (the queue board's filter)."""
    where: list[str] = []
    params: list[object] = []
    if status == "active":
        where.append("status IN ('queued', 'running')")
    elif status is not None:
        if status not in STATUSES:
            raise JobValidationError(f"status must be one of {STATUSES} or 'active', not {status!r}", ["status"])
        where.append("status = ?")
        params.append(status)
    if host_id is not None:
        where.append("host_id = ?")
        params.append(host_id)
    sql = "SELECT * FROM jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with db.reader() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_enrich(conn, _job(r), with_events=False) for r in rows]


# ------------------------------------------------------------------- lifecycle


def enqueue(
    db: Database,
    *,
    kind: str | None,
    title: str | None,
    target: str | None,
    purpose: str | None = None,
    expected: str | None = None,
    host_id: str | None = None,
    host_choice: str | None = None,
    max_minutes: int | None = None,
    reason: str | None = None,
    needs_confirm: bool = False,
    screenshots: str = "each_step",
    watching: bool = False,
    branch: str | None = None,
    commit: str | None = None,
    cleanup: list[str] | None = None,
    requested_by: str | None = None,
    project_id: str | None = None,
) -> m.Job:
    """Validate the intake and store a queued job. The intake is the gate: every missing required field
    is reported in one 422 with a field list, so an agent fixes them in one pass."""
    fields: list[str] = []
    if kind not in KINDS:
        fields.append("kind")
    if not _s(title):
        fields.append("title")
    if not _s(target):
        fields.append("target")
    if not _s(purpose):
        fields.append("purpose")
    if not _s(expected):
        fields.append("expected")
    if not host_id and host_choice != "ask":
        fields.append("host_id")  # needs host_id or host_choice='ask'
    if screenshots not in SCREENSHOT_MODES:
        fields.append("screenshots")
    if max_minutes is None:
        max_minutes = DEFAULT_MAX_MINUTES
    if kind in ("test", "demo") and max_minutes > DEFAULT_MAX_MINUTES and not _s(reason):
        fields.append("reason")  # more than the 5-minute default needs a reason
    if host_id:
        try:
            hosts.get_host(db, host_id)
        except hosts.HostError as exc:
            raise JobValidationError(f"unknown host {host_id!r}: {exc}", ["host_id"]) from exc
    if project_id:
        with db.reader() as conn:
            if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise JobValidationError(f"unknown project {project_id!r}", ["project_id"])
    if fields:
        raise JobValidationError(f"intake is missing or invalid: {', '.join(fields)}", fields)

    params = {
        "purpose": purpose,
        "expected": expected,
        "max_minutes": max_minutes,
        "needs_confirm": bool(needs_confirm),
        "screenshots": screenshots,
        "watching": bool(watching),
        "branch": branch,
        "commit": commit,
        "cleanup": list(cleanup or []),
        "reason": reason,
        "host_choice": host_choice,
        "pending_shot": False,
    }
    jid = "j-" + secrets.token_hex(4)
    with db.writer() as conn, conn:
        conn.execute(
            "INSERT INTO jobs (id, kind, title, target, host_id, status, params, requested_by, project_id) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
            (jid, kind, title, target, host_id, json.dumps(params), requested_by, project_id),
        )
    return get(db, jid)


def assign(db: Database, job_id: str, host_id: str, *, position: int | None = None) -> m.Job:
    """Set (or move) a queued job's host and queue order."""
    job = get(db, job_id)
    hosts.get_host(db, host_id)  # raises HostError (400) on an unknown host
    if job.status != "queued":
        raise JobConflict(f"job {job_id} is {job.status}; only a queued job can be (re)assigned")
    with db.writer() as conn, conn:
        if position is None:
            conn.execute(
                "UPDATE jobs SET host_id = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (host_id, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET host_id = ?, position = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                "WHERE id = ?",
                (host_id, position, job_id),
            )
    return get(db, job_id)


def start(db: Database, job_id: str, *, worker: str | None) -> m.Job:
    """queued -> running, recording the worker and started_at."""
    if not _s(worker):
        raise JobValidationError("worker is required (the subagent id / branch running this job)", ["worker"])
    job = get(db, job_id)
    if job.status != "queued":
        raise JobConflict(f"job {job_id} is {job.status}; only a queued job can start")
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', worker = ?, started_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ? AND status = 'queued'",
            (worker, job_id),
        )
    return get(db, job_id)


def _insert_event(
    db: Database,
    job_id: str,
    level: str,
    phase: str | None,
    message: str,
    data: dict | None = None,
) -> m.JobEvent:
    payload = dict(data or {})
    tail = payload.get("log_tail")
    if isinstance(tail, str):
        encoded = tail.encode("utf-8")
        if len(encoded) > LOG_TAIL_MAX:
            payload["log_tail"] = encoded[-LOG_TAIL_MAX:].decode("utf-8", "ignore")
    with db.writer() as conn, conn:
        cur = conn.execute(
            "INSERT INTO job_events (job_id, level, phase, message, data) VALUES (?, ?, ?, ?, ?)",
            (job_id, level, phase, message, json.dumps(payload)),
        )
        if phase:
            conn.execute("UPDATE jobs SET phase = ? WHERE id = ?", (phase, job_id))
        conn.execute("UPDATE jobs SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?", (job_id,))
        row = conn.execute("SELECT * FROM job_events WHERE id = ?", (cur.lastrowid,)).fetchone()
    event = _event(row)
    publish_event(event)
    return event


def event(
    db: Database,
    job_id: str,
    *,
    level: str,
    message: str,
    phase: str | None = None,
    data: dict | None = None,
) -> m.JobEvent:
    """Append one timeline event. A ``phase`` updates the job's current phase; error/crash require a
    message and SHOULD carry ``data.log_tail`` (capped at 64 KB)."""
    get(db, job_id)  # 404 on an unknown job
    if level not in LEVELS:
        raise JobValidationError(f"level must be one of {LEVELS}, not {level!r}", ["level"])
    if not _s(message):
        raise JobValidationError("message is required", ["message"])
    return _insert_event(db, job_id, level, phase, message, data)


def _set_pending_shot(db: Database, job_id: str, pending: bool, note: str | None = None) -> None:
    job = get(db, job_id)
    params = dict(job.params)
    params["pending_shot"] = pending
    if pending:
        params["pending_shot_note"] = note
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE jobs SET params = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (json.dumps(params), job_id),
        )


def request_screenshot(db: Database, job_id: str, *, note: str | None = None) -> m.Job:
    """Ask the runner for a screenshot: an ``info`` event plus ``params.pending_shot=true``. The runner
    answers by uploading an artifact (any upload clears the flag)."""
    get(db, job_id)
    message = "screenshot requested" + (f": {note}" if _s(note) else "")
    _insert_event(db, job_id, "info", None, message, {"note": note} if note else {})
    _set_pending_shot(db, job_id, True, note)
    return get(db, job_id)


def save_artifact(db: Database, job_id: str, name: str, content: bytes, content_type: str) -> m.JobArtifact:
    """Store a raw-body artifact (append-only: no overwrite). Images auto-add a ``screenshot`` event."""
    get(db, job_id)  # 404 on an unknown job
    check_artifact_name(name)
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype not in ARTIFACT_CONTENT_TYPES:
        raise JobValidationError(
            f"unsupported content type {content_type!r}; allowed: {', '.join(ARTIFACT_CONTENT_TYPES)}",
            ["content_type"],
        )
    if len(content) > MAX_ARTIFACT_BYTES:
        raise JobValidationError(
            f"artifact is too large ({len(content)} > {MAX_ARTIFACT_BYTES} bytes/25 MB)", ["content"]
        )
    path = artifact_path(job_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "xb") as fh:  # exclusive create: never overwrite (409)
            fh.write(content)
    except FileExistsError as exc:
        raise JobConflict(f"artifact {name!r} already exists for job {job_id} (append-only, no overwrite)") from exc
    md5 = hashlib.md5(content).hexdigest()
    if ctype.startswith("image/"):
        _insert_event(
            db,
            job_id,
            "screenshot",
            None,
            f"screenshot: {name}",
            {"url": _artifact_url(job_id, name), "md5": md5, "bytes": len(content)},
        )
    if get(db, job_id).params.get("pending_shot"):
        _set_pending_shot(db, job_id, False)
    return m.JobArtifact(name=name, url=_artifact_url(job_id, name), md5=md5, size=len(content))


def read_artifact(db: Database, job_id: str, name: str) -> tuple[bytes, str]:
    """Return (bytes, content_type) for one stored artifact; 404 when the job or the file is unknown."""
    get(db, job_id)
    path = artifact_path(job_id, name)
    if not path.is_file():
        raise NotFound(f"no artifact {name!r} for job {job_id}")
    ctype = content_type_for(path.name) or "application/octet-stream"
    return path.read_bytes(), ctype


def screenshots(
    db: Database,
    job_id: str,
    *,
    step: str | None = None,
    reason: str | None = None,
) -> list[m.JobScreenshot]:
    """The job's screenshots, derived from its ``screenshot`` events (newest events keep their own seq).

    Ties each shot to its step and (for a verdict shot) the verdict it was captured alongside. Older
    events carry only ``{url, md5, bytes}`` — their step is None ('unassigned') and they get a
    positional ``seq`` by time. ``step``/``reason`` filter the result; ``step='unassigned'`` selects
    the older, untagged shots.
    """
    job = get(db, job_id)  # 404 on an unknown job
    if reason is not None and reason not in m.SCREENSHOT_REASONS:
        raise JobValidationError(f"reason must be one of {m.SCREENSHOT_REASONS}, not {reason!r}", ["reason"])
    base = weburls.public_base_url()
    out: list[m.JobScreenshot] = []
    auto = 0
    for e in job.events:
        if e.level != "screenshot":
            continue
        d = e.data if isinstance(e.data, dict) else {}
        url = d.get("url")
        if not url:
            continue
        auto += 1
        raw_seq = d.get("seq")
        seq = int(raw_seq) if isinstance(raw_seq, (int, float)) and not isinstance(raw_seq, bool) else auto
        raw_reason = d.get("reason")
        shot_reason = raw_reason if raw_reason in m.SCREENSHOT_REASONS else None
        vd = d.get("verdict") if isinstance(d.get("verdict"), dict) else None
        shot_verdict = None
        if vd and vd.get("verdict"):
            shot_verdict = m.ScreenshotVerdict(
                verdict=str(vd["verdict"]),
                source=vd.get("source"),
                confirm_id=vd.get("confirm_id"),
                late=bool(vd.get("late")),
            )
        out.append(
            m.JobScreenshot(
                seq=seq,
                step=d.get("step") or e.phase or None,
                step_index=d.get("step_index"),
                step_total=d.get("step_total"),
                reason=shot_reason,
                url=url,
                web_url=weburls.screenshot_url(base, job.id),
                name=str(url).rsplit("/", 1)[-1],
                md5=d.get("md5"),
                bytes=d.get("bytes"),
                verdict=shot_verdict,
                full_res=d.get("full_res"),
                width=d.get("width"),
                height=d.get("height"),
                created_at=e.created_at,
            )
        )
    if step is not None:
        out = [s for s in out if (s.step is None if step == "unassigned" else s.step == step)]
    if reason is not None:
        out = [s for s in out if s.reason == reason]
    return out


def link(db: Database, job_id: str, *, kind: str, ref_id: str) -> m.Job:
    """Attach a canonical record (test run / build / deploy / evidence / project) to the job."""
    get(db, job_id)
    if kind not in LINK_KINDS:
        raise JobValidationError(f"kind must be one of {LINK_KINDS}, not {kind!r}", ["kind"])
    if not _s(ref_id):
        raise JobValidationError("ref_id is required", ["ref_id"])
    with db.writer() as conn, conn:
        conn.execute("INSERT OR IGNORE INTO job_links (job_id, kind, ref_id) VALUES (?, ?, ?)", (job_id, kind, ref_id))
    return get(db, job_id)


def _latest_test_run_link(db: Database, job_id: str) -> str | None:
    """The most recently linked test run for a job (rowid DESC), or None when the job links none."""
    with db.reader() as conn:
        row = conn.execute(
            "SELECT ref_id FROM job_links WHERE job_id = ? AND kind = 'test_run' ORDER BY rowid DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    return row["ref_id"] if row else None


def verdict(
    db: Database,
    job_id: str,
    *,
    verdict: str,
    notes: str | None = None,
    step: str | None = None,
    source: str = DEFAULT_VERDICT_SOURCE,
) -> m.Job:
    """Answer the running job's step from the webapp (or a coordinator relaying a chat verdict).

    Records a ``human-confirm`` evidence row (through the same path as ``POST /api/evidence``, so the
    confirm push files fire) and appends a ``milestone`` timeline event. A job that is not running is a
    conflict, and so is one with no linked test run — a verdict needs a run to anchor its evidence row.
    """
    job = get(db, job_id)  # 404 on an unknown job
    if verdict not in VERDICTS:
        raise JobValidationError(f"verdict must be one of {VERDICTS}, not {verdict!r}", ["verdict"])
    if source not in CONFIRM_SOURCES:
        raise JobValidationError(f"source must be one of {CONFIRM_SOURCES}, not {source!r}", ["source"])
    if job.status != "running":
        raise JobConflict(f"job {job_id} is {job.status}; a verdict is only accepted while the job is running")
    test_run_id = _latest_test_run_link(db, job_id)
    if test_run_id is None:
        raise JobConflict(
            f"job {job_id} has no linked test run; link one (kind='test_run') before posting a verdict"
        )
    # The evidence row is the confirm-channel record for this verdict: `ref` names the job so a reader
    # can trace the confirm back to the page it came from, and a note from the page itself (source 'web')
    # is tagged "via job page" — the job page is a distinct confirm source beside chat/console/key/
    # coordinator, and a waiting agent can tell where a verdict came from. The milestone below keeps the
    # raw note; test_run_id anchors the row to the job's newest linked run.
    page_note = notes
    if source == "web":
        page_note = f"{notes} via job page" if notes else "via job page"
    row = evidence.add_and_publish(
        db,
        kind="human-confirm",
        test_run_id=test_run_id,
        ref=job_id,
        data={"verdict": verdict, "notes": page_note, "step": step, "source": source, "job_id": job_id},
    )
    _insert_event(
        db,
        job_id,
        "milestone",
        None,
        f"verdict {verdict} ({source}): {notes or ''}",
        {"verdict": verdict, "notes": notes, "step": step, "source": source, "evidence_id": row.id},
    )
    return get(db, job_id)


def next_job(db: Database, host_id: str) -> m.Job:
    """The head queued job for a host, only when that host's lease is free.

    404 when the queue for the host is empty; 409 (with the holder) when a fresh lock blocks it. A job
    never claims the lease itself — this just refuses to hand out work for a host someone else owns.
    """
    host = hosts.get_host(db, host_id)  # HostError (400) on an unknown host
    if host.lock is not None and host.lock.fresh:
        raise JobConflict(
            f"host {host_id} is locked by {host.lock.owner!r} (heartbeat {host.lock.age_sec}s ago); "
            "the queue is not free to advance"
        )
    with db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE host_id = ? AND status = 'queued' "
            "ORDER BY position ASC, created_at ASC, id ASC LIMIT 1",
            (host_id,),
        ).fetchone()
    if row is None:
        raise NotFound(f"no queued job for host {host_id}")
    return get(db, row["id"])


def finish(db: Database, job_id: str, *, status: str, summary: dict | None) -> m.Job:
    """Close out a running/queued job. Gate: all four summary fields non-empty, each <= 600 chars.

    ``status='inconclusive'`` is the honest close-out for a run that ended without a pass/fail verdict
    (a step-gated demo the user stopped answering). It carries the same summary gate as any other
    finish status.
    """
    job = get(db, job_id)
    if status not in FINISH_STATUSES:
        raise JobValidationError(f"status must be one of {FINISH_STATUSES}, not {status!r}", ["status"])
    if job.status not in ACTIVE_STATUSES:
        raise JobConflict(f"job {job_id} is {job.status}; only a queued/running job can finish")
    fields: list[str] = []
    cleaned: dict[str, str] = {}
    for field in SUMMARY_FIELDS:
        value = (summary or {}).get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > SUMMARY_MAX:
            fields.append(field)
        else:
            cleaned[field] = value.strip()
    if fields:
        raise JobValidationError(
            f"summary must have all four fields (what, why, learned, next), each 1-{SUMMARY_MAX} chars; "
            f"bad: {', '.join(fields)}",
            fields,
        )
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE jobs SET status = ?, summary = ?, finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ? AND status IN ('queued', 'running')",
            (status, json.dumps(cleaned), job_id),
        )
    return get(db, job_id)


def cancel(db: Database, job_id: str, *, reason: str | None = None) -> m.Job:
    """queued/running -> cancelled. The reason is recorded as a timeline event."""
    job = get(db, job_id)
    if job.status not in ACTIVE_STATUSES:
        raise JobConflict(f"job {job_id} is {job.status}; only a queued/running job can be cancelled")
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ? AND status IN ('queued', 'running')",
            (job_id,),
        )
    if _s(reason):
        _insert_event(db, job_id, "info", None, f"cancelled: {reason}", {"reason": reason})
    return get(db, job_id)


def restatus(db: Database, job_id: str, *, status: str, reason: str | None = None) -> m.Job:
    """Correct a FINISHED job's terminal status (terminal -> terminal) with a mandatory reason.

    The ledger's own fix-up path, so a mis-recorded close-out (a step-gated demo that finished
    'passed' with pass=0/fail=0/no-verdict=1) can be corrected through the API instead of by hand.
    Rules: the target must be a terminal status and must differ from the current one; the job must
    already be terminal (a queued/running job finishes or is cancelled — restatus never resurrects a
    queue entry); and the reason is required, recorded as a ``milestone`` timeline event so the
    correction is auditable. ``finished_at`` is left alone: the run ended when it ended, this only
    re-states the verdict.
    """
    job = get(db, job_id)  # 404 on an unknown job
    if status not in TERMINAL_STATUSES:
        raise JobValidationError(f"status must be one of {TERMINAL_STATUSES}, not {status!r}", ["status"])
    if job.status not in TERMINAL_STATUSES:
        raise JobConflict(
            f"job {job_id} is {job.status}; only a finished job can be restated "
            "(finish or cancel it first — restatus never reopens a job)"
        )
    if job.status == status:
        raise JobConflict(f"job {job_id} is already {status}; restatus must change the status")
    if not _s(reason):
        raise JobValidationError("reason is required (the correction is recorded on the timeline)", ["reason"])
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE jobs SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND status = ?",
            (status, job_id, job.status),
        )
    _insert_event(
        db,
        job_id,
        "milestone",
        None,
        f"restatused {job.status} -> {status}: {reason}",
        {"restatus": {"from": job.status, "to": status, "reason": reason}},
    )
    return get(db, job_id)
