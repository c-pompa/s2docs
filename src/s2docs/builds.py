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
- **Who owns the run.** `start` launches the command in its own session (POSIX) or process group
  (Windows) and records its pid, so the run survives the death of whatever process launched it — the
  CLI, `s2docs mcp`, or `s2docs serve`. The run record in SQLite is the authority: readers in *other*
  processes see the same state, and a `cancel` from any process flips the record, which the watcher
  notices and acts on. The module-level `_RUNNERS` map is only a fast path for a cancel issued in the
  same process; it is never evidence that a run is alive. A process that starts up later calls
  `recover`, which asks the OS about every still-'running' run's pid instead of guessing from its own
  memory (`reconcile_orphans` is the old, this-process-only rule and is superseded by it).
- **UTF-16 logs.** resourcecompiler writes its log as UTF-16 (with or without a BOM). `_read_log`
  detects the encoding instead of assuming UTF-8 — naive probes broke the log twice.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import IO, Any

from s2docs import hosts, weburls
from s2docs import models as m
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.db.repository import NotFound

DEFAULT_TIMEOUT_SEC = 3600
DEFAULT_POLL_SEC = 1.0
DEFAULT_TAIL_LINES = 200

# '<count> <word>' pairs, e.g. "4123 compiled, 2 failed" — last occurrence per word wins.
_SUMMARY_RE = re.compile(r"(\d[\d,]*)\s+(compiled|failed|errors?|warnings?)\b", re.IGNORECASE)

# ------------------------------------------------------------- material guard
# The 2026-10-12 de_dust2_iso incident: a decompiled+rebuilt map error-rendered (red/black
# checkerboard) because its meshes referenced the map's own nodraw physics-surface materials at
# RESERVED, content-illegal paths (maps/<map>/_vrf/...), which never compile into a rebuilt vpk.
# The resourcecompiler reported 'referencing missing material' but still exited 0, and the lines
# were buried in an 8 MB UTF-16 log. These patterns surface them instead of leaving a pixel scan
# as the only detector.
_MISSING_MATERIAL_RE = re.compile(r"referencing missing material", re.IGNORECASE)
_FAILED_COMPILE_RE = re.compile(r"\bfailed to compile\b", re.IGNORECASE)
# Valve's own reserved subtree; assets under it are never packed into a rebuilt map.
_RESERVED_VRF_RE = re.compile(r"[A-Za-z0-9_./\\-]*_vrf/[A-Za-z0-9_./\\-]+", re.IGNORECASE)
# maps/<name>/<...>: legal only when <name> is the build's own map; another map's subtree is reserved.
_MAPS_SUBTREE_RE = re.compile(r"maps/(?P<name>[A-Za-z0-9_]+)/[A-Za-z0-9_./\\-]+", re.IGNORECASE)
# A path-like token ending in an asset extension, used to pull the offender out of a message line.
_ASSET_PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{2,5}")
# 'map=<name>' as recorded in the CLI run notes (build start --map ...).
_MAP_NOTE_RE = re.compile(r"\bmap=(?P<name>[A-Za-z0-9_-]+)", re.IGNORECASE)
# Keep the warning payload small: counts are exact, but only the first few paths are listed.
MAX_GUARD_PATHS = 5
_MATERIAL_KINDS = ("missing_material", "failed_compile", "reserved_path")

# run id → live process + its open log handle, for cancels issued in this process. Deliberately not a
# liveness registry: a detached run outlives this dict, which is why `recover` asks the OS instead.
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


def map_name_from_notes(notes: str | None, command: str | None = None) -> str | None:
    """Best-effort own-map name for a run: the 'map=<name>' note first, then the command text."""
    for text in (notes, command):
        if text:
            match = _MAP_NOTE_RE.search(text)
            if match:
                return match.group("name")
    return None


def scan_materials(text: str, *, map_name: str | None = None) -> dict:
    """Scan a (decoded) build log for the material-failure class; empty-clean dict when none found.

    Returns ``{"clean": True, "map": <name>}`` when the log is clean, else a dict with per-kind
    ``counts`` and the first few offending ``paths``. ``map_name`` is the build's own map: a
    ``maps/<name>/`` reference to a *different* map is reserved, as is anything under ``_vrf/``.
    """
    counts: dict[str, int] = {kind: 0 for kind in _MATERIAL_KINDS}
    paths: dict[str, list[str]] = {kind: [] for kind in _MATERIAL_KINDS}
    own = map_name.lower() if map_name else None

    def _add(kind: str, path: str | None) -> None:
        counts[kind] += 1
        if path and path not in paths[kind] and len(paths[kind]) < MAX_GUARD_PATHS:
            paths[kind].append(path)

    for line in text.splitlines():
        if _MISSING_MATERIAL_RE.search(line):
            found = _ASSET_PATH_RE.search(line)
            _add("missing_material", found.group(0) if found else None)
        if _FAILED_COMPILE_RE.search(line):
            found = _ASSET_PATH_RE.search(line)
            _add("failed_compile", found.group(0) if found else None)
        reserved: set[str] = {match.group(0) for match in _RESERVED_VRF_RE.finditer(line)}
        for match in _MAPS_SUBTREE_RE.finditer(line):
            if own is None or match.group("name").lower() != own:
                reserved.add(match.group(0))
        for token in sorted(reserved):
            _add("reserved_path", token)

    if not any(counts.values()):
        return {"clean": True, "map": map_name}
    return {"clean": False, "map": map_name, "counts": counts, "paths": paths}


def _guard_from_summary(summary: dict) -> m.MaterialGuard | None:
    data = summary.get("material_guard")
    return m.MaterialGuard(**data) if data else None


def has_material_warnings(run: m.BuildRun) -> bool:
    """True when the run's log recorded material warnings (missing/reserved materials).

    A run can be 'succeeded' and still carry these; callers that must not ship such an artifact
    (CI, `--strict-materials`) gate on this instead of the run status.
    """
    return run.warnings is not None and not run.warnings.clean


def _row_to_run(row: sqlite3.Row) -> m.BuildRun:
    summary = json.loads(row["summary"])
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
        summary=summary,
        warnings=_guard_from_summary(summary),
        artifacts=[m.BuildArtifact(**a) for a in json.loads(row["artifacts"])],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        web_url=weburls.build_url(weburls.public_base_url(), row["id"]),
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
        runs = [_row_to_run(r) for r in rows]
        # Attach the host lock this run still holds (owner build:<id>) so the Activity page can show
        # STUCK/phase; a run whose lease was taken over carries no summary.
        for run in runs:
            run.lock = hosts.lock_summary(conn, run.host_id, run.owner)
        return runs


def _status_of(db: Database, run_id: str) -> str | None:
    with db.reader() as conn:
        row = conn.execute("SELECT status FROM build_runs WHERE id = ?", (run_id,)).fetchone()
    return row["status"] if row else None


# ---------------------------------------------------------- process ownership
# CREATE_NEW_PROCESS_GROUP only exists on Windows; the literal is its documented value (0x00000200),
# kept so the Windows launch path is expressible (and testable) from any platform.
WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def _detach_kwargs() -> dict[str, Any]:
    """Popen flags that make the child independent of the process that launches it.

    The server's lifetime must not decide a build's fate. A detached child gets its own session
    (POSIX) or process group (Windows), so a restart, a Ctrl-C or a closed terminal leaves a running
    compile alone; the pid is persisted next to the run, so `recover` can find it again afterwards.
    """
    if os.name == "nt":
        return {"creationflags": WINDOWS_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _windows_pid_alive(pid: int) -> bool:
    """Best-effort Windows probe: `tasklist` filtered to the pid, matched on its PID field.

    `os.kill(pid, 0)` is *not* an existence probe on Windows — it calls TerminateProcess, i.e. it
    would kill the very run being asked about. tasklist reports no start time and cannot tell a
    recycled pid from the original one; that is accepted rather than pulling in psutil for one probe.
    If the probe itself fails we answer "alive": a broken check must never interrupt a running compile.
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return any(len(row) > 1 and row[1].strip() == str(pid) for row in csv.reader(io.StringIO(out.stdout)))


def pid_alive(pid: int) -> bool:
    """True when `pid` is a live process.

    POSIX: `os.kill(pid, 0)` is a pure existence probe — EPERM means the pid exists and belongs to
    another user, which still counts as alive. Windows: see `_windows_pid_alive` (best-effort).
    Unknown platforms answer True: refusing to invent a death is always safer than interrupting a run.

    A recycled pid reads as alive; that is the one false-positive this accepts. It costs a late
    recovery (and `build cancel` is the escape hatch), whereas the opposite mistake would kill a
    compile someone is waiting on.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:  # ESRCH (gone) or EINVAL (not a signalable pid): not a run of ours either way
        return False
    return True


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
    lease_renew_sec: float | None = None,
    notes: str | None = None,
    poll_sec: float = DEFAULT_POLL_SEC,
) -> m.BuildRun:
    """Claim the host lease server-side, launch `command` on the host over SSH, return the run.

    The host lease (owner 'build:<run-id>') is acquired here, not by the caller, so a second actor
    gets `hosts.HostLocked` instead of a corrupted compile. It is released when the run reaches a
    terminal status (or immediately if the launch itself fails).

    The lease carries the run's ``kind`` as its initial **phase**, and the watcher renews the
    heartbeat while the process runs (``lease_renew_sec``, default ttl/3): a legitimately running
    build therefore does not lose its host to a spurious TTL expiry. Because the phase does not move
    on its own, a hung compile still reads STUCK server-side (a fresh heartbeat is not progress).
    If another owner does take the host, the run is finalized 'cancelled' with a clear note instead
    of driving a host it no longer owns.
    """
    if not command.strip():
        raise BuildError("command is required")
    host = hosts.get_host(db, host_id)  # raises HostError if unknown
    # Built (and the stored target re-validated) before the lease or the run record exist, so an
    # unsafe target fails cleanly instead of leaving a 'running' row behind.
    argv = hosts.ssh_argv(host) + [command]
    rid = "b-" + secrets.token_hex(4)
    owner = f"build:{rid}"
    phase = kind.strip() or None  # e.g. 'compile' — settable per-run via kind, allowlisted per phase

    # One active run per host: a stale/expired lease must not let a second compile start on the same
    # host while the first run's record is still 'running' (an orphaned run has no live watcher).
    active = list_runs(db, host_id=host_id, active=True)
    if active:
        raise BuildError(
            f"host {host_id} already has a running build run {active[0].id!r} "
            f"(status {active[0].status!r}); cancel it or wait for it to finish"
        )

    # Confine the log to the data dir: an unauthenticated LAN POST must not be able to truncate any
    # file the server can write. A client-supplied path outside the data dir is rejected; otherwise
    # the path is derived server-side.
    settings = get_settings()
    if log_path is None:
        log_dir = settings.data_dir / "builds"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{rid}.log"
    else:
        resolved = Path(log_path).expanduser().resolve()
        data_dir = settings.data_dir.resolve()
        if resolved != data_dir and not resolved.is_relative_to(data_dir):
            raise BuildError(
                f"log_path {resolved} is outside the data dir {data_dir}; derive it server-side instead"
            )
        log_path = resolved
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    hosts.claim_host(db, host_id, owner, ttl_sec=ttl_sec, phase=phase)  # raises HostLocked on contention
    with db.writer() as conn, conn:
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner, log_path, notes) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
            (rid, host_id, kind, command, owner, str(log_path), notes),
        )

    log_fh = log_path.open("wb")
    try:
        proc = subprocess.Popen(
            argv,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,  # a detached child must not inherit (and block on) our stdin
            **_detach_kwargs(),
        )
    except OSError as exc:
        log_fh.close()
        _finalize(db, rid, host_id, owner, "failed", None, notes=f"launch failed: {exc}")
        raise BuildError(f"failed to launch command on {host_id}: {exc}") from exc

    with _RUNNERS_LOCK:
        _RUNNERS[rid] = (proc, log_fh)
    # The pid is what lets a *later* process find this run again: `recover` asks the OS whether it is
    # still alive instead of trusting this process's memory of it.
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE build_runs SET handle = ?, pid = ? WHERE id = ?",
            (str(proc.pid), proc.pid, rid),
        )
    threading.Thread(
        target=_watch,
        args=(db, rid, host_id, owner, proc, log_fh, timeout_sec, poll_sec),
        kwargs={"ttl_sec": ttl_sec, "lease_renew_sec": lease_renew_sec, "phase": phase},
        daemon=True,
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
    *,
    ttl_sec: int = hosts.DEFAULT_TTL_SEC,
    lease_renew_sec: float | None = None,
    phase: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout_sec
    renew_interval = lease_renew_sec if lease_renew_sec is not None else max(1.0, ttl_sec / 3)
    last_renew = time.monotonic()
    timed_out = False
    lease_lost: str | None = None
    while proc.poll() is None:
        status = _status_of(db, rid)
        if status == "cancelled":
            break
        now = time.monotonic()
        if now > deadline:
            timed_out = True
            _terminate(proc)
            break
        if now - last_renew >= renew_interval:
            lease_lost = _renew_lease(db, host_id, owner, phase)
            last_renew = now
            if lease_lost:
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
    if lease_lost:
        # Explicit expiry behaviour: never keep driving a host another owner took. Finalize without
        # touching their lock.
        final = "cancelled"
        extra = f"host lease lost: {lease_lost}"
        _finalize(db, rid, host_id, owner, final, rc, notes=extra, release_lease=False)
        return
    _finalize(db, rid, host_id, owner, final, rc, notes=extra)


def _renew_lease(db: Database, host_id: str, owner: str, phase: str | None) -> str | None:
    """Server-side liveness for a running build: the run keeps its own lease alive. Returns a message
    when the lease is gone (taken over or released), else None. Same-phase heartbeats keep phase_since,
    so a phase that never moves still reads STUCK."""
    try:
        hosts.heartbeat_host(db, host_id, owner, phase=phase)
        return None
    except hosts.HostError as exc:
        return str(exc)


def _finalize(
    db: Database,
    rid: str,
    host_id: str,
    owner: str,
    status: str,
    exit_code: int | None,
    *,
    notes: str | None = None,
    release_lease: bool = True,
) -> None:
    run = get_run(db, rid)
    summary: dict = {}
    if run.log_path and Path(run.log_path).exists():
        text, _ = _read_log(Path(run.log_path))
        summary = parse_summary(text)
        guard = scan_materials(text, map_name=map_name_from_notes(run.notes, run.command))
        if not guard["clean"]:
            # A material warning is not a failed run (the resourcecompiler exits 0), so status stays
            # 'succeeded' — but the summary now carries the warnings for build_status/build_log/CLI.
            summary["material_guard"] = guard
    merged_notes = "\n".join(x for x in (run.notes, notes) if x) or None
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE build_runs SET status = ?, exit_code = ?, summary = ?, notes = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (status, exit_code, json.dumps(summary), merged_notes, rid),
        )
    if release_lease:
        _release(db, host_id, owner)


def _release(db: Database, host_id: str, owner: str) -> None:
    """Free the lease iff it is still ours — releasing clears the lock (and its phase). Never deletes
    another owner's lock: on a takeover the run is finalized without touching the new owner's lease."""
    with contextlib.suppress(hosts.HostError):
        lock = hosts.get_host(db, host_id).lock
        if lock is not None and lock.owner == owner:
            hosts.release_host(db, host_id, owner)


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
    guard = scan_materials(text, map_name=map_name_from_notes(run.notes, run.command))
    all_lines = text.splitlines()
    truncated = len(all_lines) > lines
    tail = all_lines[-lines:] if lines > 0 else all_lines
    return m.BuildLogTail(
        run_id=run_id,
        lines=tail,
        encoding=encoding,
        size=path.stat().st_size,
        truncated=truncated,
        warnings=None if guard["clean"] else m.MaterialGuard(**guard),
    )


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
    _release(db, run.host_id, run.owner)  # the run is terminal: free its own lease now
    return get_run(db, run_id)


def recover(db: Database) -> list[str]:
    """Finalize build runs whose owning process is gone, so a restart leaves no stale 'running' row.

    Why the OS and not `_RUNNERS`: a run is launched detached, so it can still be compiling happily
    while the process that started it is long gone (or while a *different* process — the CLI, the MCP
    server — owns it). The old rule, "not in this process's `_RUNNERS`, therefore dead", would
    interrupt a live 38-minute compile. So the question is put to the kernel: is the recorded pid
    still a live process?

    Safe to call repeatedly: only 'queued'/'running' rows are considered and 'interrupted' is
    terminal, so a second pass changes nothing and appends nothing. Never interrupts a live run: an
    alive pid, an unprobeable pid, or a run registered in this process all leave the row untouched.
    The log is never deleted or moved — the note names the pid that vanished and keeps ``log_path``
    readable, so the tail of a dead compile can still be inspected.

    Returns the ids that were interrupted (empty when there was nothing to do). Called at startup by
    `s2docs serve` and `s2docs mcp`, by the periodic reaper, and by `s2docs build recover`.
    """
    return _recover(db, trust_live_pid=True)


def reconcile_orphans(db: Database) -> list[str]:
    """Legacy pid-blind sweep: any run without a live watcher in *this* process is interrupted.

    Only correct for processes that know every run was launched by them — i.e. never after a restart,
    when a detached run legitimately outlives its launcher. Kept for the in-process repair case; new
    code must call `recover`.
    """
    return _recover(db, trust_live_pid=False)


def _recover(db: Database, *, trust_live_pid: bool) -> list[str]:
    with _RUNNERS_LOCK:
        live = set(_RUNNERS)
    interrupted: list[str] = []
    with db.writer() as conn, conn:
        rows = conn.execute(
            "SELECT id, host_id, owner, pid, handle, log_path FROM build_runs "
            "WHERE status IN ('queued', 'running')"
        ).fetchall()
        for r in rows:
            if r["id"] in live:
                continue
            pid = _recorded_pid(r)
            if trust_live_pid and pid is not None and pid_alive(pid):
                continue
            _interrupt(conn, r["id"], r["host_id"], r["owner"], _interrupt_note(pid, r["log_path"]))
            interrupted.append(r["id"])
    return interrupted


def _recorded_pid(row: sqlite3.Row) -> int | None:
    """The owning pid: the typed `pid` column, else a numeric `handle` (rows from before 0017, whose
    handle was always written as ``str(proc.pid)``); None when the row never recorded one."""
    if row["pid"] is not None:
        return int(row["pid"])
    handle = (row["handle"] or "").strip()
    return int(handle) if handle.isdigit() else None


def _interrupt_note(pid: int | None, log_path: str | None) -> str:
    """The reason recorded on an interrupted run: which process vanished, and where its log still is."""
    if pid is None:
        return "interrupted: no pid was recorded, so the process that owned this run cannot be checked"
    note = f"interrupted: the process that owned this run (pid {pid}) disappeared"
    if log_path:
        note += f"; its log is kept at {log_path}"
    return note


def _interrupt(conn: sqlite3.Connection, rid: str, host_id: str, owner: str, note: str) -> None:
    """Mark a run 'interrupted' (terminal) with the reason, and free its lease iff it is still ours."""
    conn.execute(
        "UPDATE build_runs SET status = 'interrupted', "
        "notes = CASE WHEN notes IS NULL OR notes = '' THEN ? ELSE notes || char(10) || ? END, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
        (note, note, rid),
    )
    lock = conn.execute("SELECT owner FROM host_locks WHERE host_id = ?", (host_id,)).fetchone()
    if lock is not None and lock["owner"] == owner:
        conn.execute("DELETE FROM host_locks WHERE host_id = ? AND owner = ?", (host_id, owner))
        conn.execute(
            "INSERT INTO host_lock_events (host_id, owner, event, detail) VALUES (?, ?, 'released', ?)",
            (host_id, owner, "released: interrupted run no longer owns it"),
        )


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
