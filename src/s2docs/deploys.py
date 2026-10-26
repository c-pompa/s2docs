"""Deployment registry: push files to a host and verify what actually landed.

The failure this guards against: two workers (or a worker and CI) deploying to the same host with
nothing checking the artifacts afterwards. A deployment is a recorded list of {path, size, md5} that
the *server* re-checks over SSH with `Get-FileHash`, so 'pushed' means 'the bytes on the host match',
not 'scp returned 0'.

Transport is a plain `scp` per file (the plan leaves the mechanism to the deploy service — §7);
`verify` reads hashes back with one PowerShell `Get-FileHash` pass over SSH. Both are a single,
documented invocation, faked in tests.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import sqlite3
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from s2docs import hosts, weburls
from s2docs import models as m
from s2docs.builds import md5_file
from s2docs.db.connection import Database
from s2docs.db.repository import NotFound

SSH_TIMEOUT_SEC = 120
_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class DeployError(ValueError):
    pass


# ------------------------------------------------------------------ helpers
def _remote_path(dest_root: str, rel: str) -> str:
    return dest_root.rstrip("/\\") + "/" + rel.replace("\\", "/").lstrip("/")


def _pwsh_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _hash_remote(host: m.Host, paths: list[str], *, timeout: int = SSH_TIMEOUT_SEC) -> dict[str, str]:
    """Run one PowerShell Get-FileHash pass over SSH. Returns {remote_path: md5|'missing'}."""
    if not paths:
        return {}
    targets = ",".join(_pwsh_literal(p) for p in paths)
    script = (
        f"$t = @({targets}); "
        "foreach ($p in $t) { "
        "if (Test-Path -LiteralPath $p) { "
        "$h = (Get-FileHash -Algorithm MD5 -LiteralPath $p).Hash; "
        'Write-Output ($h + "|" + $p) } '
        'else { Write-Output ("missing|" + $p) } }'
    )
    argv = hosts.powershell_argv(host, script)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DeployError(f"Get-FileHash on {host.id} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise DeployError(f"Get-FileHash on {host.id} failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}")
    out: dict[str, str] = {}
    for line in proc.stdout.replace("\r", "").splitlines():
        if "|" in line:
            digest, path = line.split("|", 1)
            out[path.strip()] = digest.strip().lower()
    return out


def _ensure_remote_dirs(host: m.Host, remotes: list[str], *, timeout: int = SSH_TIMEOUT_SEC) -> None:
    """Create every parent directory of ``remotes`` in one PowerShell pass (scp never creates them).

    ``[System.IO.Directory]::CreateDirectory`` is idempotent and takes the path literally (no wildcard
    expansion, unlike ``New-Item -Path``), so an existing tree is a no-op.
    """
    parents = sorted({r.rsplit("/", 1)[0] for r in remotes if "/" in r})
    if not parents:
        return
    targets = ",".join(_pwsh_literal(p) for p in parents)
    script = f"$t = @({targets}); foreach ($p in $t) {{ [void][System.IO.Directory]::CreateDirectory($p) }}"
    argv = hosts.powershell_argv(host, script)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DeployError(f"creating remote directories on {host.id} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise DeployError(f"creating remote directories on {host.id} failed (rc={proc.returncode}): "
                          f"{proc.stderr.strip()[:300]}")


def remote_md5(host: m.Host, paths: list[str]) -> dict[str, str]:
    """The MD5 each path has *on the host* (one Get-FileHash pass), or ``'missing'``.

    The readback half of a pull: a file scp brought back is only trusted when its local MD5 equals what
    the host says it has.
    """
    return _hash_remote(host, paths)


def _copy_file(host: m.Host, local: Path, remote: str, *, timeout: int = SSH_TIMEOUT_SEC) -> None:
    argv = hosts.scp_argv(host, local, remote)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DeployError(f"scp of {local.name} to {host.id} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise DeployError(
            f"scp of {local.name} to {host.id} failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}"
        )


def _row_to_deployment(row: sqlite3.Row) -> m.Deployment:
    return m.Deployment(
        id=row["id"],
        host_id=row["host_id"],
        addon=row["addon"],
        dest_root=row["dest_root"],
        entries=[m.DeployEntry(**e) for e in json.loads(row["entries"])],
        status=row["status"],
        owner=row["owner"],
        notes=row["notes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        web_url=weburls.deploy_url(weburls.public_base_url(), row["id"]),
    )


def get_deployment(db: Database, deploy_id: str) -> m.Deployment:
    with db.reader() as conn:
        row = conn.execute("SELECT * FROM deployments WHERE id = ?", (deploy_id,)).fetchone()
    if row is None:
        raise NotFound(f"no deployment {deploy_id!r}")
    return _row_to_deployment(row)


def list_deployments(db: Database, *, host_id: str | None = None) -> list[m.Deployment]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT * FROM deployments WHERE (? IS NULL OR host_id = ?) ORDER BY created_at DESC",
            (host_id, host_id),
        ).fetchall()
    return [_row_to_deployment(r) for r in rows]


def latest_for_host(db: Database, host_id: str) -> m.Deployment:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT * FROM deployments WHERE host_id = ? ORDER BY created_at DESC LIMIT 1", (host_id,)
        ).fetchone()
    if row is None:
        raise NotFound(f"no deployment recorded for host {host_id!r}")
    return _row_to_deployment(row)


# --------------------------------------------------------------------- push
def _normalize(files: Sequence[m.DeployFile | dict], dest_root: str) -> list[tuple[m.DeployFile, str, m.DeployEntry]]:
    """Resolve each file to (spec, remote_path, entry). Local files are hashed and sized here."""
    out = []
    for raw in files:
        spec = raw if isinstance(raw, m.DeployFile) else m.DeployFile(**raw)
        remote = _remote_path(dest_root, spec.path)
        entry = m.DeployEntry(path=spec.path, size=spec.size, md5=spec.md5, verified=False)
        if spec.local:
            local = Path(spec.local)
            if not local.is_file():
                raise DeployError(f"local file not found: {local}")
            entry.size = local.stat().st_size
            entry.md5 = md5_file(local)
        out.append((spec, remote, entry))
    return out


def _take_lease(db: Database, host: m.Host, host_id: str, owner: str | None, did: str) -> tuple[str, bool]:
    """Decide the effective host-lock owner for a deploy and take the lease when needed.

    Returns ``(effective_owner, took_lease)``. If ``owner`` already holds a *fresh* lock we reuse it and
    do not release it; if the host is free (or held only by a stale lock) we claim it as ``deploy:<id>``
    for the copy+verify window; a fresh lock held by anyone else is a hard conflict (HostLocked → 409).
    """
    lease_owner = f"deploy:{did}"
    lock = host.lock
    if lock is not None and lock.fresh:
        if owner is not None and lock.owner == owner:
            return owner, False
        raise hosts.HostLocked(
            f"host {host_id} is locked by {lock.owner!r} (heartbeat {lock.age_sec}s ago, ttl {lock.ttl_sec}s); "
            "wait for it to expire, ask the owner to release it, or deploy as that owner"
        )
    # free, or held only by a stale lock that may be taken over
    hosts.claim_host(db, host_id, lease_owner, phase="deploy")
    return lease_owner, True


def _release_lease(db: Database, host_id: str, owner: str) -> None:
    """Release the deploy lease iff it is still ours — never delete a lock someone else took over."""
    with contextlib.suppress(hosts.HostError):
        lock = hosts.get_host(db, host_id).lock
        if lock is not None and lock.owner == owner:
            hosts.release_host(db, host_id, owner)


def push(
    db: Database,
    host_id: str,
    dest_root: str,
    files: Sequence[m.DeployFile | dict],
    *,
    addon: str | None = None,
    parallel: int = 1,
    dry_run: bool = False,
    verify: bool = True,
    notes: str | None = None,
    owner: str | None = None,
) -> m.Deployment:
    """Copy files to the host per the manifest, then verify MD5s server-side over SSH.

    The push holds the host lease for the copy+verify window: a fresh lock held by another owner is a
    conflict (HostLocked → 409), a lock held by ``owner`` is reused, and a free host is claimed as
    ``deploy:<id>`` and released at the end (owner-conditional, even on failure).
    """
    if not files:
        raise DeployError("no files to deploy")
    if not dest_root.strip():
        raise DeployError("dest_root is required")
    host = hosts.get_host(db, host_id)  # raises HostError if unknown
    # A target stored before registration validated it must fail here, before the lease is taken
    # or a record written — not halfway through the copy loop.
    hosts.parse_ssh_target(host.ssh_target)
    resolved = _normalize(files, dest_root)

    did = "d-" + secrets.token_hex(4)
    if dry_run:
        now = "dry-run"
        return m.Deployment(
            id=did,
            host_id=host_id,
            addon=addon,
            dest_root=dest_root,
            entries=[e for _, _, e in resolved],
            status="pushed",
            owner=owner,
            notes="dry-run: nothing copied",
            created_at=now,
            updated_at=now,
            web_url=weburls.deploy_url(weburls.public_base_url(), did),
        )

    effective_owner, took_lease = _take_lease(db, host, host_id, owner, did)
    try:
        errors: list[str] = []
        _ensure_remote_dirs(host, [remote for _, remote, _ in resolved])

        def _copy(item: tuple[m.DeployFile, str, m.DeployEntry]) -> None:
            spec, remote, _ = item
            try:
                if not spec.local:
                    raise DeployError(f"{spec.path}: no local source to copy")
                _copy_file(host, Path(spec.local), remote)
            except DeployError as exc:
                errors.append(str(exc))

        items = resolved
        if parallel and parallel > 1 and len(items) > 1:
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                list(pool.map(_copy, items))
        else:
            for item in items:
                _copy(item)

        if errors:
            # Keep a record even on failure so `deploy list`/`verify` see what was attempted; a
            # partial batch returns the record (CLI exit 6), a total failure raises (exit 1).
            note = (f"{notes}\n" if notes else "") + "copy errors: " + "; ".join(errors[:25])
            deployment = _store(
                db, did, host_id, addon, dest_root, [e for _, _, e in resolved], "failed", note, effective_owner
            )
            if len(errors) == len(items):
                raise DeployError(f"deploy {did} to {host_id}: all copies failed:\n" + "\n".join(errors[:25]))
            return deployment

        entries = [e for _, _, e in resolved]
        status = "pushed"
        if verify:
            status = _verify_entries(host, dest_root, entries)
        deployment = _store(db, did, host_id, addon, dest_root, entries, status, notes, effective_owner)
        if status == "failed":
            raise DeployError(f"deploy {did} to {host_id}: MD5 verification failed for some files")
        return deployment
    finally:
        # Always release the lease the server took for this push — including on failure.
        if took_lease:
            _release_lease(db, host_id, effective_owner)


def _verify_entries(host: m.Host, dest_root: str, entries: list[m.DeployEntry]) -> str:
    hashes = _hash_remote(host, [_remote_path(dest_root, e.path) for e in entries])
    all_ok = True
    for entry in entries:
        remote = _remote_path(dest_root, entry.path)
        got = hashes.get(remote, "missing")
        ok = bool(entry.md5) and got == entry.md5
        entry.verified = ok
        all_ok = all_ok and ok
    return "verified" if all_ok else "failed"


def verify(db: Database, deploy_id: str) -> m.Deployment:
    """Re-check the recorded MD5s of a deployment over SSH and update its status."""
    deployment = get_deployment(db, deploy_id)
    host = hosts.get_host(db, deployment.host_id)
    status = _verify_entries(host, deployment.dest_root, deployment.entries)
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE deployments SET entries = ?, status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ?",
            (json.dumps([e.model_dump() for e in deployment.entries]), status, deploy_id),
        )
    return get_deployment(db, deploy_id)


def _store(
    db: Database,
    did: str,
    host_id: str,
    addon: str | None,
    dest_root: str,
    entries: list[m.DeployEntry],
    status: str,
    notes: str | None,
    owner: str | None = None,
) -> m.Deployment:
    with db.writer() as conn, conn:
        conn.execute(
            "INSERT INTO deployments (id, host_id, addon, dest_root, entries, status, notes, owner) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (did, host_id, addon, dest_root, json.dumps([e.model_dump() for e in entries]), status, notes, owner),
        )
    return get_deployment(db, did)
