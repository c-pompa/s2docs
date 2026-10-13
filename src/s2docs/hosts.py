"""Host registry + server-side host locks.

The registry answers "what is this machine" (shell, node path, GPU, Steam account, paths)
so agent briefs stop restating machine trivia; probe_host refreshes those facts over SSH
(scripts/windows/probe_host.ps1). host_locks is the single source of truth for "who is
driving this host right now": one lock per host with owner (workstream/session id),
heartbeat and TTL. A lock whose heartbeat is older than its TTL is stale and can be taken
over — that covers crashed or interrupted workers (the old hand-maintained
C:\\iso_build\\iso_host.lock on the hosts did not).

A lock also carries a **phase** (what the owner is doing: 'compile', 'test', ...) and the time it
started. A fresh heartbeat with a phase that has not moved past a threshold reads **STUCK** — the
server computes it, clients cannot fake it. This is the 2026-10-13 lesson: a worker heartbeat its
lock faithfully for 95 minutes while its compile sat hung, and the dashboard looked healthy because
liveness (a fresh heartbeat) is not progress. Thresholds come from the server: S2DOCS_STUCK_SEC
(default 1800s) and the per-phase overrides in S2DOCS_STUCK_PHASES (e.g. {'compile': 7200}). A lock
with no phase (an old client that never sends one) is never STUCK — it just reports 'no phase'.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from s2docs import models as m
from s2docs.config import get_settings
from s2docs.db.connection import Database

HOST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DEFAULT_TTL_SEC = 1800
PROBE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "windows" / "probe_host.ps1"


class HostError(ValueError):
    pass


class HostLocked(HostError):
    pass


def _check_id(host_id: str) -> None:
    if not HOST_ID_RE.match(host_id):
        raise HostError(f"invalid host id {host_id!r}: lowercase [a-z0-9-], e.g. 'desktop-pc'")


def _now() -> str:
    """UTC timestamp in the same format the SQL defaults use (so phase_since sorts with them)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def stuck_threshold(phase: str) -> int:
    """Seconds a fresh lock may hold `phase` before the server calls it stuck (per-phase override wins)."""
    settings = get_settings()
    return settings.stuck_phases.get(phase, settings.stuck_sec)


def stuck_for(phase: str, phase_age_sec: int | None, *, fresh: bool) -> tuple[bool, str | None]:
    """Server-side stuck verdict: a *fresh* lock whose phase has not moved past its threshold.

    Fail-soft: no phase reported (old client) or a stale heartbeat (which the lock already exposes
    as stale) is never STUCK.
    """
    if not fresh or not phase or phase_age_sec is None:
        return False, None
    threshold = stuck_threshold(phase)
    if phase_age_sec > threshold:
        return True, f"phase {phase!r} unchanged for {phase_age_sec}s (threshold {threshold}s)"
    return False, None


def _row_to_host(row: sqlite3.Row, lock: sqlite3.Row | None) -> m.Host:
    lock_model = None
    if lock is not None:
        age = lock["age"]
        fresh = age <= lock["ttl_sec"]
        phase = lock["phase"] or ""
        phase_age = lock["phase_age"]
        stuck, reason = stuck_for(phase, phase_age, fresh=fresh)
        lock_model = m.HostLock(
            host_id=lock["host_id"],
            owner=lock["owner"],
            acquired_at=lock["acquired_at"],
            heartbeat_at=lock["heartbeat_at"],
            ttl_sec=lock["ttl_sec"],
            fresh=fresh,
            age_sec=age,
            phase=phase,
            phase_since=lock["phase_since"],
            phase_age_sec=phase_age,
            note=lock["note"],
            stuck=stuck,
            stuck_reason=reason,
        )
    return m.Host(
        id=row["id"],
        hostname=row["hostname"],
        ssh_target=row["ssh_target"],
        ssh_key=row["ssh_key"],
        shell=row["shell"],
        os=row["os"],
        facts=json.loads(row["facts"]),
        probed_at=row["probed_at"],
        notes=row["notes"],
        lock=lock_model,
    )


_LOCK_SELECT = """
    SELECT *,
           CAST(strftime('%s', 'now') AS INTEGER) - CAST(strftime('%s', heartbeat_at) AS INTEGER) AS age,
           CASE WHEN phase_since IS NULL THEN NULL
                ELSE CAST(strftime('%s', 'now') AS INTEGER) - CAST(strftime('%s', phase_since) AS INTEGER)
           END AS phase_age
    FROM host_locks WHERE host_id = ?
"""


def _load(conn: sqlite3.Connection, host_id: str) -> m.Host:
    row = conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()
    if row is None:
        raise HostError(f"unknown host {host_id!r}; register it first")
    lock = conn.execute(_LOCK_SELECT, (host_id,)).fetchone()
    return _row_to_host(row, lock)


def ssh_argv(host: m.Host) -> list[str]:
    """Base ssh argv for a host (connection timeout + identity file when set).

    Start with this and append the remote command; the build and deploy services use it so a
    single place owns the ssh flags. The remote command is passed as one argv element — ssh
    joins the remaining arguments and hands them to the remote shell.
    """
    argv = ["ssh", "-o", "ConnectTimeout=15"]
    if host.ssh_key:
        argv += ["-i", host.ssh_key, "-o", "IdentitiesOnly=yes"]
    argv.append(host.ssh_target)
    return argv


def _record_lock_event(
    conn: sqlite3.Connection, host_id: str, owner: str, event: str, detail: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO host_lock_events (host_id, owner, event, detail) VALUES (?, ?, ?, ?)",
        (host_id, owner, event, detail),
    )


def _record_phase_event(conn: sqlite3.Connection, host_id: str, owner: str, old_phase: str, new_phase: str) -> None:
    """Append a phase transition to the audit trail (only when the phase actually moved)."""
    if old_phase == new_phase:
        return
    detail = f"phase {old_phase!r} -> {new_phase!r}" if old_phase else f"phase -> {new_phase!r}"
    conn.execute(
        "INSERT INTO host_lock_events (host_id, owner, event, phase, detail) VALUES (?, ?, 'phase', ?, ?)",
        (host_id, owner, new_phase or None, detail),
    )


def _resolve_phase(
    requested: str | None, previous: str, prev_since: str | None, *, same_owner: bool
) -> tuple[str, str | None]:
    """Decide the (phase, phase_since) a lock mutation should store.

    - requested is None (the client did not report a phase): keep the previous phase when the same
      owner is refreshing, otherwise start clean (a new owner has no phase yet).
    - requested is '' (explicitly cleared) or whitespace: no phase.
    - requested changed, or the owner changed: phase_since resets to now.
    - requested equals the previous phase for the same owner: phase_since is *kept* (a faithful
      heartbeat must not hide a phase that has not moved — that is the whole point of stuck-ness).
    """
    if requested is None:
        return (previous, prev_since) if same_owner else ("", None)
    phase = requested.strip()
    if not phase:
        return "", None
    if same_owner and phase == previous and prev_since:
        return phase, prev_since
    return phase, _now()


def list_hosts(db: Database) -> list[m.Host]:
    with db.writer() as conn:
        rows = conn.execute("SELECT * FROM hosts ORDER BY id").fetchall()
        return [_load(conn, r["id"]) for r in rows]


def lock_history(db: Database, host_id: str, *, limit: int = 20) -> list[m.HostLockEvent]:
    """Recent lock mutations for a host (newest first) — the Hosts page's lock history."""
    _check_id(host_id)
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT * FROM host_lock_events WHERE host_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (host_id, limit),
        ).fetchall()
    return [m.HostLockEvent(**dict(r)) for r in rows]


def get_host(db: Database, host_id: str) -> m.Host:
    _check_id(host_id)
    with db.writer() as conn:
        return _load(conn, host_id)


def register_host(
    db: Database,
    host_id: str,
    ssh_target: str,
    *,
    hostname: str | None = None,
    ssh_key: str | None = None,
    shell: str | None = None,
    os: str | None = None,
    notes: str | None = None,
) -> m.Host:
    _check_id(host_id)
    if shell is not None and shell not in ("cmd", "powershell"):
        raise HostError("shell must be 'cmd' or 'powershell'")
    with db.writer() as conn, conn:
        conn.execute(
            """
            INSERT INTO hosts (id, hostname, ssh_target, ssh_key, shell, os, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                hostname   = COALESCE(excluded.hostname, hostname),
                ssh_target = excluded.ssh_target,
                ssh_key    = COALESCE(excluded.ssh_key, ssh_key),
                shell      = COALESCE(excluded.shell, shell),
                os         = COALESCE(excluded.os, os),
                notes      = COALESCE(excluded.notes, notes),
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (host_id, hostname, ssh_target, ssh_key, shell, os, notes),
        )
        return _load(conn, host_id)


def claim_host(
    db: Database,
    host_id: str,
    owner: str,
    *,
    ttl_sec: int = DEFAULT_TTL_SEC,
    force: bool = False,
    phase: str | None = None,
    note: str | None = None,
) -> m.HostLock:
    """Take (or refresh) the host lock. ``phase``/``note`` are optional: a phase change resets
    phase_since; omitting them on a same-owner refresh keeps the phase already reported."""
    _check_id(host_id)
    if not owner.strip():
        raise HostError("owner is required (workstream/session id, e.g. 'ws/occluder-wire' or 'p-1a2b3c4d')")
    if ttl_sec < 60:
        raise HostError("ttl_sec must be >= 60")
    with db.writer() as conn, conn:
        _load(conn, host_id)  # raises if unknown
        lock = conn.execute(_LOCK_SELECT, (host_id,)).fetchone()
        if lock is not None and lock["owner"] != owner and lock["age"] <= lock["ttl_sec"] and not force:
            raise HostLocked(
                f"host {host_id} is locked by {lock['owner']!r} "
                f"(heartbeat {lock['age']}s ago, ttl {lock['ttl_sec']}s); "
                "heartbeat it, wait for expiry, or pass force=true to take over"
            )
        detail = None
        if lock is not None and lock["owner"] != owner:
            how = "forced takeover" if lock["age"] <= lock["ttl_sec"] else "took over stale lock"
            detail = f"{how} from {lock['owner']!r} (heartbeat {lock['age']}s ago)"
        same_owner = lock is not None and lock["owner"] == owner
        prev_phase = (lock["phase"] or "") if lock is not None else ""
        prev_since = lock["phase_since"] if lock is not None else None
        new_phase, new_since = _resolve_phase(phase, prev_phase, prev_since, same_owner=same_owner)
        if lock is None:
            conn.execute(
                "INSERT INTO host_locks (host_id, owner, ttl_sec, phase, phase_since, note) VALUES (?, ?, ?, ?, ?, ?)",
                (host_id, owner, ttl_sec, new_phase, new_since, note),
            )
        else:
            conn.execute(
                "UPDATE host_locks SET owner = ?, acquired_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
                "heartbeat_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ttl_sec = ?, phase = ?, "
                "phase_since = ?, note = COALESCE(?, note) WHERE host_id = ?",
                (owner, ttl_sec, new_phase, new_since, note, host_id),
            )
        _record_lock_event(conn, host_id, owner, "claimed", detail)
        if lock is not None:
            _record_phase_event(conn, host_id, owner, prev_phase, new_phase)
        return _load(conn, host_id).lock  # type: ignore[return-value]


def heartbeat_host(
    db: Database, host_id: str, owner: str, *, phase: str | None = None, note: str | None = None
) -> m.HostLock:
    """Refresh your lock's heartbeat. Passing the same ``phase`` keeps phase_since (so a genuinely
    hung run still reads STUCK); passing a new ``phase`` resets it. Omit ``phase`` to leave it as-is."""
    _check_id(host_id)
    with db.writer() as conn, conn:
        lock = conn.execute(_LOCK_SELECT, (host_id,)).fetchone()
        if lock is None:
            raise HostError(f"host {host_id} is not locked; claim it first")
        if lock["owner"] != owner:
            raise HostLocked(f"host {host_id} is locked by {lock['owner']!r}, not {owner!r}")
        prev_phase = lock["phase"] or ""
        new_phase, new_since = _resolve_phase(phase, prev_phase, lock["phase_since"], same_owner=True)
        conn.execute(
            "UPDATE host_locks SET heartbeat_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "phase = ?, phase_since = ?, note = COALESCE(?, note) WHERE host_id = ?",
            (new_phase, new_since, note, host_id),
        )
        _record_phase_event(conn, host_id, owner, prev_phase, new_phase)
        return _load(conn, host_id).lock  # type: ignore[return-value]


def release_host(db: Database, host_id: str, owner: str, *, force: bool = False) -> str:
    _check_id(host_id)
    with db.writer() as conn, conn:
        lock = conn.execute(_LOCK_SELECT, (host_id,)).fetchone()
        if lock is None:
            return f"host {host_id} was not locked"
        if lock["owner"] != owner and not force:
            raise HostLocked(
                f"host {host_id} is locked by {lock['owner']!r}, not {owner!r}; pass force=true to override"
            )
        conn.execute("DELETE FROM host_locks WHERE host_id = ?", (host_id,))
        detail = f"released by {owner}" if owner != lock["owner"] else None
        _record_lock_event(conn, host_id, lock["owner"], "released", detail)
        return f"released {host_id} (was held by {lock['owner']})"


def probe_host(db: Database, host_id: str, *, timeout: int = 90) -> m.Host:
    """Run scripts/windows/probe_host.ps1 on the host over SSH and store the facts."""
    host = get_host(db, host_id)
    if not PROBE_SCRIPT.exists():
        raise HostError(f"probe script missing at {PROBE_SCRIPT}")
    encoded = base64.b64encode(PROBE_SCRIPT.read_bytes().decode("utf-8").encode("utf-16-le")).decode()
    cmd = ["ssh", "-o", "ConnectTimeout=15"]
    if host.ssh_key:
        cmd += ["-i", host.ssh_key, "-o", "IdentitiesOnly=yes"]
    cmd += [host.ssh_target, f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HostError(f"probe of {host_id} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise HostError(f"probe of {host_id} failed (rc={proc.returncode}): {proc.stderr.strip()[:400]}")
    payload = next((ln for ln in proc.stdout.replace("\r", "").splitlines() if ln.startswith("{")), None)
    if payload is None:
        raise HostError(f"probe of {host_id} returned no JSON payload; output: {proc.stdout[:300]!r}")
    facts = json.loads(payload)
    merged = {**host.facts, "probe": facts}
    hostname = facts.get("host") or host.hostname
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE hosts SET facts = ?, hostname = ?, probed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (json.dumps(merged), hostname, host_id),
        )
        return _load(conn, host_id)
