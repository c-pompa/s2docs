"""Server-owned build runs.

A build run is one remote compile/command on a host. The point of the record is that the *server*
owns the run, not the client: the process, its status, its exit code and its log live behind a run
id, so an agent or a CI step re-attaches with `build status <id>` instead of babysitting a 50-minute
compile with a hand-rolled sleep-poll loop (the failure this plan was written to kill).

Lifecycle:
  start ──▶ claim host lease ('build:<run-id>') ──▶ launch over SSH ──▶ record status=running
              │                                          │
              │                                  ssh output → local log file
              ▼                                          ▼
  release lease ◀── terminal status (succeeded/failed/cancelled) ◀── watcher thread

Design notes (the plan left these to the build service):
- **Process launch** is a plain, documented `ssh <host> <command>` (see `hosts.ssh_argv`). The
  caller supplies the command; making it detach/hide on the host ("hidden-launch") is the caller's
  concern, so nothing here hardcodes a scheduler. The server holds the ssh for the run's duration
  and tees its output to `log_path`.
- **Who owns the run.** `start` runs the process in the calling process (the long-lived `s2docs
  serve`, or a CLI that stays alive, e.g. with `--wait`). The run record is in SQLite, so readers
  in *other* processes see the same state; a `cancel` from any process flips the record to
  `cancelled`, which the watcher notices and acts on.
- **UTF-16 logs.** resourcecompiler writes its log as UTF-16 (with or without a BOM). `_read_log`
  detects the encoding instead of assuming UTF-8 — naive probes broke the log twice.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import IO

from s2docs import hosts
from s2docs import models as m
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.db.repository import NotFound

DEFAULT_TIMEOUT_SEC = 3600
DEFAULT_POLL_SEC = 1.0
DEFAULT_TAIL_LINES = 200

# '<count> <word>' pairs, e.g. "4123 compiled, 2 failed" — last occurrence per word wins.
_SUMMARY_RE = re.compile(r"(\d[\d,]*)\s+(compiled|failed|errors?|warnings?)\b", re.IGNORECASE)

# run id → live process + its open log handle, for cancels issued in this process.
_RUNNERS: dict[str, tuple[subprocess.Popen, IO[bytes]]] = {}
_RUNNERS_LOCK = threading.Lock()


class BuildError(ValueError):
    pass


class BuildTimeout(BuildError):
    pass


# ------------------------------------------------------------------ helpers
def parse_summary(text: str) -> dict[str, int]:
    """Pull the compile counts out of a log's summary line(s): {compiled, failed, errors, ...}.

    Heuristic on purpose: resourcecompiler formats vary by version, so we match any
    '<int> <word>' where the word is one of the known counters and keep the last value seen
    (the summary is at the end of the log).
    """
    out: dict[str, int] = {}
    for match in _SUMMARY_RE.finditer(text):
        key = match.group(2).lower()
        key = {"error": "errors", "warning": "warnings"}.get(key, key)
        out[key] = int(match.group(1).replace(",", ""))
    return out


def _read_log(path: Path) -> tuple[str, str]:
    """Return (text, encoding). Detects UTF-16 (BOM or NUL-heavy) — resourcecompiler's format."""
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace"), "utf-16"
    if b"\x00" in data[:64]:  # UTF-16 without BOM (little-endian on our hosts)
        return data.decode("utf-16-le", errors="replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("utf-16-le", errors="replace"), "utf-16"


def _row_to_run(row: sqlite3.Row) -> m.BuildRun:
    return m.BuildRun(
        id=row["id"],
        host_id=row["host_id"],
        kind=row["kind"],
        command=row["command"],
        status=row["status"],
        owner=row["owner"],
        handle=row["handle"],
        notes=row["notes"],
        log_path=row["log_path"],
        exit_code=row["exit_code"],
        summary=json.loads(row["summary"]),
        artifacts=[m.BuildArtifact(**a) for a in json.loads(row["artifacts"])],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_run(db: Database, run_id: str) -> m.BuildRun:
    with db.reader() as conn:
        row = conn.execute("SELECT * FROM build_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise NotFound(f"no build run {run_id!r}")
    return _row_to_run(row)


def list_runs(db: Database, *, host_id: str | None = None, active: bool = False) -> list[m.BuildRun]:
    sql = "SELECT * FROM build_runs WHERE (? IS NULL OR host_id = ?)"
    if active:
        sql += " AND status IN ('queued', 'running')"
    sql += " ORDER BY created_at DESC"
    with db.reader() as conn:
        rows = conn.execute(sql, (host_id, host_id)).fetchall()
    return [_row_to_run(r) for r in rows]


def _status_of(db: Database, run_id: str) -> str | None:
    with db.reader() as conn:
        row = conn.execute("SELECT status FROM build_runs WHERE id = ?", (run_id,)).fetchone()
    return row["status"] if row else None


# -------------------------------------------------------------------- start
def start(
    db: Database,
    host_id: str,
    command: str,
    *,
    kind: str = "build",
    log_path: str | Path | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    ttl_sec: int = hosts.DEFAULT_TTL_SEC,
    notes: str | None = None,
    poll_sec: float = DEFAULT_POLL_SEC,
) -> m.BuildRun:
    """Claim the host lease server-side, launch `command` on the host over SSH, return the run.

    The host lease (owner 'build:<run-id>') is acquired here, not by the caller, so a second actor
    gets `hosts.HostLocked` instead of a corrupted compile. It is released when the run reaches a
    terminal status (or immediately if the launch itself fails).
    """
    if not command.strip():
        raise BuildError("command is required")
    host = hosts.get_host(db, host_id)  # raises HostError if unknown
    rid = "b-" + secrets.token_hex(4)
    owner = f"build:{rid}"

    if log_path is None:
        log_dir = get_settings().data_dir / "builds"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{rid}.log"
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    hosts.claim_host(db, host_id, owner, ttl_sec=ttl_sec)  # raises HostLocked on contention
    with db.writer() as conn, conn:
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner, log_path, notes) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
            (rid, host_id, kind, command, owner, str(log_path), notes),
        )

    argv = hosts.ssh_argv(host) + [command]
    log_fh = log_path.open("wb")
    try:
        kwargs: dict = {"stdout": log_fh, "stderr": subprocess.STDOUT}
        if os.name == "posix":
            kwargs["start_new_session"] = True  # detach from the server's process group
        proc = subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        log_fh.close()
        _finalize(db, rid, host_id, owner, "failed", None, notes=f"launch failed: {exc}")
        raise BuildError(f"failed to launch command on {host_id}: {exc}") from exc

    with _RUNNERS_LOCK:
        _RUNNERS[rid] = (proc, log_fh)
    with db.writer() as conn, conn:
        conn.execute("UPDATE build_runs SET handle = ? WHERE id = ?", (str(proc.pid), rid))
    threading.Thread(
        target=_watch, args=(db, rid, host_id, owner, proc, log_fh, timeout_sec, poll_sec), daemon=True
    ).start()
    return get_run(db, rid)


def _watch(
    db: Database,
    rid: str,
    host_id: str,
    owner: str,
    proc: subprocess.Popen,
    log_fh: IO[bytes],
    timeout_sec: int,
    poll_sec: float,
) -> None:
    deadline = time.monotonic() + timeout_sec
    timed_out = False
    while proc.poll() is None:
        status = _status_of(db, rid)
        if status == "cancelled":
            break
        if time.monotonic() > deadline:
            timed_out = True
            _terminate(proc)
            break
        time.sleep(poll_sec)
    rc = proc.wait()
    log_fh.close()
    with _RUNNERS_LOCK:
        _RUNNERS.pop(rid, None)
    current = _status_of(db, rid)
    final = "succeeded"
    if rc != 0:
        final = "failed"
    if current == "cancelled" or timed_out:
        final = "cancelled"
    extra = f"timed out after {timeout_sec}s" if timed_out else None
    _finalize(db, rid, host_id, owner, final, rc, notes=extra)


def _finalize(
    db: Database, rid: str, host_id: str, owner: str, status: str, exit_code: int | None, *, notes: str | None = None
) -> None:
    run = get_run(db, rid)
    summary: dict = {}
    if run.log_path and Path(run.log_path).exists():
        text, _ = _read_log(Path(run.log_path))
        summary = parse_summary(text)
    merged_notes = "\n".join(x for x in (run.notes, notes) if x) or None
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE build_runs SET status = ?, exit_code = ?, summary = ?, notes = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (status, exit_code, json.dumps(summary), merged_notes, rid),
        )
    _release(db, host_id, owner)


def _release(db: Database, host_id: str, owner: str) -> None:
    # host or lock already gone → nothing to release
    with contextlib.suppress(hosts.HostError):
        hosts.release_host(db, host_id, owner, force=True)


def _terminate(proc: subprocess.Popen) -> None:
    with contextlib.suppress(OSError):
        proc.terminate()


# --------------------------------------------------------------- operations
def log_tail(db: Database, run_id: str, *, lines: int = DEFAULT_TAIL_LINES) -> m.BuildLogTail:
    """Return the last `lines` lines of the run's log, decoded from UTF-16 when needed."""
    run = get_run(db, run_id)
    if not run.log_path or not Path(run.log_path).exists():
        return m.BuildLogTail(run_id=run_id, lines=[], encoding="utf-8", size=0, truncated=False)
    path = Path(run.log_path)
    text, encoding = _read_log(path)
    all_lines = text.splitlines()
    truncated = len(all_lines) > lines
    tail = all_lines[-lines:] if lines > 0 else all_lines
    return m.BuildLogTail(run_id=run_id, lines=tail, encoding=encoding, size=path.stat().st_size, truncated=truncated)


def wait(db: Database, run_id: str, *, timeout_sec: float = DEFAULT_TIMEOUT_SEC, poll_sec: float = DEFAULT_POLL_SEC):
    """Block (server-side) until the run reaches a terminal status, or raise BuildTimeout."""
    deadline = time.monotonic() + timeout_sec
    while True:
        run = get_run(db, run_id)
        if run.status in m.TERMINAL_BUILD_STATUSES:
            return run
        if time.monotonic() > deadline:
            raise BuildTimeout(f"build run {run_id} still {run.status} after {timeout_sec}s")
        time.sleep(poll_sec)


def cancel(db: Database, run_id: str) -> m.BuildRun:
    """Mark a run cancelled and release its host lease; terminate the process if we own it here."""
    run = get_run(db, run_id)
    if run.status in m.TERMINAL_BUILD_STATUSES:
        return run
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE build_runs SET status = 'cancelled', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ?",
            (run_id,),
        )
    with _RUNNERS_LOCK:
        entry = _RUNNERS.get(run_id)
    if entry is not None:
        _terminate(entry[0])  # the watcher notices the cancelled record and finalizes
    _release(db, run.host_id, run.owner)  # the run is terminal: free the lease now, whoever owns it
    return get_run(db, run_id)


def record_artifacts(db: Database, run_id: str, paths: list[str]) -> m.BuildRun:
    """Attach artifacts (path + md5 + size) to a run. Local files are hashed; remote paths keep md5=None.

    Remote artifact hashing over SSH is the deploy service's job (it already verifies MD5s), so a
    path that is not present locally is recorded by name only.
    """
    run = get_run(db, run_id)
    artifacts: list[m.BuildArtifact] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            artifacts.append(m.BuildArtifact(path=raw, md5=md5_file(path), size=path.stat().st_size))
        else:
            artifacts.append(m.BuildArtifact(path=raw))
    merged = [*run.artifacts, *(a for a in artifacts if a not in run.artifacts)]
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE build_runs SET artifacts = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (json.dumps([a.model_dump() for a in merged]), run_id),
        )
    return get_run(db, run_id)


def md5_file(path: Path) -> str:
    """MD5 of a local file (integrity check for artifacts/deploys, not a security boundary)."""
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
