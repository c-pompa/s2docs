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
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from s2docs import models as m
from s2docs.config import get_settings
from s2docs.db.connection import Database

HOST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DEFAULT_TTL_SEC = 1800
PROBE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "windows" / "probe_host.ps1"

# An ssh destination as the registry accepts it: an ssh_config alias, or [user@]host[:port].
# The first character of the user and of the host must be a letter, digit or '_', so the target can
# never start with '-': ssh and scp would otherwise read it as an option, and `-oProxyCommand=...` runs
# a command on THIS machine (NousResearch/hermes-agent#143393, review item 1). The charset leaves no
# room for whitespace, quotes or shell/ssh metacharacters. IPv6 literals are not accepted; give such a
# host an ssh_config alias. fullmatch + ASCII so a trailing newline or a Unicode letter cannot slip in.
_SSH_TARGET_RE = re.compile(
    r"(?:(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]{0,63})@)?"
    r"(?P<host>[A-Za-z0-9_][A-Za-z0-9._-]{0,252})"
    r"(?::(?P<port>[1-9][0-9]{0,4}))?",
    re.ASCII,
)
_SSH_TARGET_HELP = (
    "expected an ssh alias or [user@]host[:port] made of letters, digits, '.', '_' and '-' "
    "(not starting with '-'), e.g. 'rtx-pc' or 'pompa@192.0.2.172:22'"
)


class HostError(ValueError):
    pass


class HostLocked(HostError):
    pass


@dataclass(frozen=True)
class SshTarget:
    """A validated ssh destination: ``destination`` is ``[user@]host``; ``port`` is the optional port."""

    destination: str
    port: int | None = None


def parse_ssh_target(target: object) -> SshTarget:
    """Validate an ssh_target and split off its port. Raises HostError for anything option-like."""
    if not isinstance(target, str):
        raise HostError(f"invalid ssh_target {target!r}: {_SSH_TARGET_HELP}")
    match = _SSH_TARGET_RE.fullmatch(target)
    if match is None or (match["port"] is not None and int(match["port"]) > 65535):
        raise HostError(f"invalid ssh_target {target!r}: {_SSH_TARGET_HELP}")
    user, host, port = match["user"], match["host"], match["port"]
    return SshTarget(destination=f"{user}@{host}" if user else host, port=int(port) if port else None)


def _check_id(host_id: str) -> None:
    if not HOST_ID_RE.match(host_id):
        raise HostError(f"invalid host id {host_id!r}: lowercase [a-z0-9-], e.g. 'build-pc'")


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


#: Connection multiplexing (OpenSSH ControlMaster): every ssh/scp to one host rides ONE TCP session for
#: CONTROL_PERSIST_SEC after the last use. Measured 2026-10-27 on pomps-pc, whose Windows sshd drops new
#: connections for minutes after a burst: a loose deploy (1 mkdir + 6 scp + 1 hash pass, 8 fresh TCP
#: connections) failed from the 2nd file on in two consecutive runs (j-6304c124, j-9af97a72), and the
#: identical run with multiplexing went through (j-8ab87782). `%C` is OpenSSH's hash of host/port/user:
#: the socket path stays short (macOS caps unix sockets at 104 bytes) and never contains target text.
#: Set S2DOCS_SSH_MUX=0 to turn it off (e.g. a host whose sshd refuses session multiplexing).
CONTROL_PERSIST_SEC = 120
CONTROL_PATH = "~/.ssh/s2docs-cm-%C"


def _mux_options() -> list[str]:
    if os.environ.get("S2DOCS_SSH_MUX", "1").strip().lower() in {"0", "false", "no", "off"}:
        return []
    return ["-o", "ControlMaster=auto", "-o", f"ControlPath={CONTROL_PATH}",
            "-o", f"ControlPersist={CONTROL_PERSIST_SEC}"]


def _connection_options(host: m.Host, target: SshTarget, *, port_flag: str) -> list[str]:
    """The flags ssh and scp share: connect timeout, multiplexing, identity file, port (scp spells it -P)."""
    options = ["-o", "ConnectTimeout=15", *_mux_options()]
    if host.ssh_key:
        options += ["-i", host.ssh_key, "-o", "IdentitiesOnly=yes"]
    if target.port is not None:
        options += [port_flag, str(target.port)]
    return options


def ssh_argv(host: m.Host) -> list[str]:
    """Base ssh argv for a host: connection flags, then ``--``, then the destination.

    Start with this and append the remote command; the probe, build and deploy services all use it
    so a single place owns the ssh flags. The stored target is re-validated here, so a row written
    before registration validated it is refused at use time instead of reaching ssh. ``--`` ends
    option parsing right before the destination: nothing after it — the destination or the remote
    command — can be read as an option (OpenSSH otherwise re-parses the words after the host).
    The remote command is passed as one argv element; ssh hands it to the remote shell.
    """
    target = parse_ssh_target(host.ssh_target)
    return ["ssh", *_connection_options(host, target, port_flag="-p"), "--", target.destination]


POWERSHELL = "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand"
# cmd.exe (pomps-pc's login shell) cuts a command line at 8191 characters instead of refusing it.
CMD_EXE_MAX_LINE = 8191


def powershell_command(script: str) -> str:
    """The one remote command s2docs uses to run PowerShell on a host: ``-EncodedCommand`` (UTF-16LE base64).

    The script is parsed whole, and no quote or ``$`` in it can be re-split by the outer shell. Never send a
    script on stdin with ``-Command -``: that is parsed LINE BY LINE, so a ``try {}`` / ``finally {}`` split
    over two lines fails to parse while ssh still exits 0 (a CS2 launch task was registered but never started,
    live 2026-10-25). Callers should also print a success token and check for it: rc 0 alone proves nothing.
    """
    return f"{POWERSHELL} {base64.b64encode(script.encode('utf-16-le')).decode()}"


def powershell_argv(host: m.Host, script: str) -> list[str]:
    """``ssh_argv(host)`` plus :func:`powershell_command`; refuses a script cmd.exe would silently truncate."""
    command = powershell_command(script)
    if host.shell == "cmd" and len(command) > CMD_EXE_MAX_LINE - 200:  # leave room for ssh's own wrapping
        raise HostError(f"PowerShell script for {host.id} is {len(command)} characters encoded; cmd.exe cuts "
                        f"command lines at {CMD_EXE_MAX_LINE} - copy it over as a .ps1 file instead")
    return ssh_argv(host) + [command]


def scp_argv(host: m.Host, local: Path, remote: str, *, pull: bool = False) -> list[str]:
    """scp argv copying ``local`` to ``remote`` on the host (``pull=True``: ``remote`` down to ``local``),
    hardened like :func:`ssh_argv`. The ONLY place an scp argv is built (CI rule HPF-003).

    ``--`` precedes both operands; the ``host:path`` operand starts with the validated destination,
    so it can never start with '-'; and the local path is made absolute, so it can neither look like
    an option nor be mistaken for a ``host:path`` operand.
    """
    target = parse_ssh_target(host.ssh_target)
    operand = f"{target.destination}:{remote}"
    if operand.startswith("-"):  # unreachable for a validated destination; kept as the invariant
        raise HostError(f"refusing an scp operand that starts with '-': {operand!r}")
    local_path = str(Path(local).resolve())
    pair = [operand, local_path] if pull else [local_path, operand]
    return ["scp", *_connection_options(host, target, port_flag="-P"), "--", *pair]


def cs2_install_path(host: m.Host) -> str:
    """The host's CS2 install root, as ``s2docs host probe`` stores it: ``facts.probe.cs2.path``.

    The one reader of that fact (sandbox build/deploy and the CS2 launch both need it). An unprobed host
    is refused with the command that fixes it.
    """
    path = (((host.facts or {}).get("probe") or {}).get("cs2") or {}).get("path")
    if not isinstance(path, str) or not path.strip():
        raise HostError(f"{host.id} has no probed CS2 path (facts.probe.cs2.path); run `s2docs host probe {host.id}`")
    return path


def scp_pull_argv(host: m.Host, remote: str, local: Path) -> list[str]:
    """scp argv copying ``remote`` on the host *down* to ``local`` — :func:`scp_argv` in reverse.

    Same hardening as the push direction. The remote text here is a caller's string rather than a
    validated destination, so it is refused when it is empty, starts with '-' (scp would read it as an
    option) or carries a newline/NUL byte (a second remote command on the far side). ``--`` still ends
    option parsing before both operands, and the local path is made absolute so it cannot look like a
    ``host:path`` operand.
    """
    if not isinstance(remote, str) or not remote:
        raise HostError("refusing an empty remote path")
    if remote.startswith("-"):
        raise HostError(f"refusing a remote path that starts with '-': {remote!r}")
    if any(ch in remote for ch in "\r\n\x00"):
        raise HostError(f"refusing a remote path with a newline or NUL: {remote!r}")
    return scp_argv(host, local, remote, pull=True)


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
    with db.reader() as conn:
        rows = conn.execute("SELECT * FROM hosts ORDER BY id").fetchall()
        return [_load(conn, r["id"]) for r in rows]


def lock_summary(conn: sqlite3.Connection, host_id: str, owner: str) -> m.LockSummary | None:
    """The lock summary for a run *iff* `owner` still holds the host; else None.

    Used to enrich build/test-run list rows (the Activity page's lockStuckBadge reads `run.lock`).
    Takes an existing (read-only) connection so a list endpoint enriches N runs without N connects.
    """
    lock = conn.execute(_LOCK_SELECT, (host_id,)).fetchone()
    if lock is None or lock["owner"] != owner:
        return None
    phase = lock["phase"] or ""
    fresh = lock["age"] <= lock["ttl_sec"]
    stuck, reason = stuck_for(phase, lock["phase_age"], fresh=fresh)
    return m.LockSummary(
        owner=lock["owner"], phase=phase, phase_age_sec=lock["phase_age"], stuck=stuck, stuck_reason=reason
    )


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
    with db.reader() as conn:
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
    parse_ssh_target(ssh_target)  # refuse option-like / malformed targets before anything is stored
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
    with db.writer() as conn:
        # BEGIN IMMEDIATE takes the write lock before the SELECT, so two racing claims are serialized:
        # the loser re-reads the winner's row and gets HostLocked instead of a UNIQUE IntegrityError.
        conn.execute("BEGIN IMMEDIATE")
        try:
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
                    "INSERT INTO host_locks (host_id, owner, ttl_sec, phase, phase_since, note) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
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
            result = _load(conn, host_id).lock
            conn.commit()
            return result  # type: ignore[return-value]
        except BaseException:
            conn.rollback()
            raise


def heartbeat_host(
    db: Database, host_id: str, owner: str, *, phase: str | None = None, note: str | None = None
) -> m.HostLock:
    """Refresh your lock's heartbeat. Passing the same ``phase`` keeps phase_since (so a genuinely
    hung run still reads STUCK); passing a new ``phase`` resets it. Omit ``phase`` to leave it as-is."""
    _check_id(host_id)
    with db.writer() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            lock = conn.execute(_LOCK_SELECT, (host_id,)).fetchone()
            if lock is None:
                raise HostError(f"host {host_id} is not locked; claim it first")
            if lock["owner"] != owner:
                raise HostLocked(f"host {host_id} is locked by {lock['owner']!r}, not {owner!r}")
            prev_phase = lock["phase"] or ""
            new_phase, new_since = _resolve_phase(phase, prev_phase, lock["phase_since"], same_owner=True)
            # Owner-conditional: a former owner's heartbeat must never refresh the new owner's lock.
            cur = conn.execute(
                "UPDATE host_locks SET heartbeat_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
                "phase = ?, phase_since = ?, note = COALESCE(?, note) WHERE host_id = ? AND owner = ?",
                (new_phase, new_since, note, host_id, owner),
            )
            if cur.rowcount != 1:
                raise HostLocked(f"host {host_id} is no longer locked by {owner!r}")
            _record_phase_event(conn, host_id, owner, prev_phase, new_phase)
            result = _load(conn, host_id).lock
            conn.commit()
            return result  # type: ignore[return-value]
        except BaseException:
            conn.rollback()
            raise


def release_host(db: Database, host_id: str, owner: str, *, force: bool = False) -> str:
    _check_id(host_id)
    with db.writer() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            lock = conn.execute(_LOCK_SELECT, (host_id,)).fetchone()
            if lock is None:
                conn.rollback()
                return f"host {host_id} was not locked"
            if lock["owner"] != owner and not force:
                raise HostLocked(
                    f"host {host_id} is locked by {lock['owner']!r}, not {owner!r}; pass force=true to override"
                )
            if force:
                cur = conn.execute("DELETE FROM host_locks WHERE host_id = ?", (host_id,))
            else:
                # Owner-conditional: a former owner cannot delete the new owner's lock.
                cur = conn.execute("DELETE FROM host_locks WHERE host_id = ? AND owner = ?", (host_id, owner))
            if cur.rowcount != 1:
                raise HostLocked(
                    f"host {host_id} is locked by {lock['owner']!r}, not {owner!r}; pass force=true to override"
                )
            detail = f"released by {owner}" if owner != lock["owner"] else None
            _record_lock_event(conn, host_id, lock["owner"], "released", detail)
            conn.commit()
            return f"released {host_id} (was held by {lock['owner']})"
        except BaseException:
            conn.rollback()
            raise


def probe_host(db: Database, host_id: str, *, timeout: int = 90) -> m.Host:
    """Run scripts/windows/probe_host.ps1 on the host over SSH and store the facts."""
    host = get_host(db, host_id)
    if not PROBE_SCRIPT.exists():
        raise HostError(f"probe script missing at {PROBE_SCRIPT}")
    cmd = powershell_argv(host, PROBE_SCRIPT.read_bytes().decode("utf-8"))
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
