"""The asset catalog: a promotion-gated view over the records the chain already keeps.

"what did I make, and can I prove it works" is answered by the `assets` table (migration 0015), but the
table is deliberately thin: every column that names another record — build, deploy, frame job — is a
*link*, and the bytes stay where the build/deploy/job put them. Promoting is therefore an act of
attestation, not a copy, and the gates below are what stop the catalog from becoming a catalog of lies
(docs/HAMMERED_ORCHESTRATION.md §2):

1. **No entry without a succeeded build.** `build_id` must name a build run that reached
   ``succeeded``; a queued/running/failed compile proves nothing about the asset.
2. **No ``model`` entry without an in-game frame.** A model must name the job it was seen in and the
   frame artifact that job stored, and that job's verdict must not be ``fail`` — and a job that never
   got a verdict is not a pass either (the same rule migration 0013 taught the ledger).
3. **Licence is recorded at promotion.** It is a required argument, never inferred from the build or
   the provider afterwards (HAMMERED.md §6: no Valve-derived asset may enter the catalog).
4. **Provenance is append-only.** :func:`retire` sets ``retired_at``; the row and every link survive.

`kind='action'` is a cs_script: its chain is prompt → draft → validate → compile (build) → deploy →
job, so the build and licence gates apply unchanged; the frame is optional because a script has no
mesh to look at (force one by passing both frame fields, which then validate exactly like a model's).

The gates are here, in the service layer, not in the routes: REST, MCP and any future caller go
through :func:`promote` and cannot skip a check by talking to a different surface.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3

from s2docs import builds, deploys, jobs, weburls
from s2docs import models as m
from s2docs.db.connection import Database
from s2docs.db.repository import NotFound

KINDS: tuple[str, ...] = m.ASSET_KINDS
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SLUG_MAX = 80
NAME_MAX = 200
TAG_MAX = 32
MAX_TAGS = 12
RETIRED_REASON_MAX = 500

# Job statuses that mean the run itself is not evidence, whatever a step verdict said.
_FAILED_JOB_STATUSES: tuple[str, ...] = ("failed", "error", "cancelled")


class AssetError(ValueError):
    """Base class for catalog failures. Subclasses map to specific HTTP statuses."""


class AssetValidationError(AssetError):
    """The promotion payload is missing or malformed (422, with the offending field names)."""

    def __init__(self, message: str, fields: list[str] | None = None):
        super().__init__(message)
        self.fields = fields or []


class AssetConflict(AssetError):
    """A linked record's state — or the slug — forbids this promotion (409)."""


# ------------------------------------------------------------------ helpers
def slugify(text: str) -> str:
    """Lowercase kebab-case slug: runs of anything non-alphanumeric become one '-'."""
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")[:SLUG_MAX].strip("-")


def _text(value: str | None) -> str | None:
    """Trim an optional string; a blank string is 'not given', not an empty value in the store."""
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def normalize_tags(tags: list[str] | None) -> list[str]:
    """Lowercase, trim, de-duplicate, order-preserving; rejects non-strings and oversized lists."""
    if not tags:
        return []
    if not isinstance(tags, list):
        raise AssetValidationError("tags must be a list of strings", ["tags"])
    out: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise AssetValidationError(f"tag {tag!r} is not a string", ["tags"])
        clean = tag.strip().lower()
        if not clean:
            raise AssetValidationError("a tag may not be blank", ["tags"])
        if len(clean) > TAG_MAX:
            raise AssetValidationError(f"tag {clean!r} is longer than {TAG_MAX} characters", ["tags"])
        if clean not in out:
            out.append(clean)
    if len(out) > MAX_TAGS:
        raise AssetValidationError(f"at most {MAX_TAGS} tags, got {len(out)}", ["tags"])
    return out


def job_verdict(job: m.Job) -> tuple[str | None, str | None]:
    """The job's verdict as (verdict, source); ``(None, None)`` when it never got one.

    A **verdict milestone** (``level='milestone'`` with ``data.verdict``) is a human's call recorded on
    the timeline — from chat, console, a key, the webapp or a coordinator relay. The newest one wins,
    because that is the job's final call (a late verdict supersedes an earlier one).

    When the timeline carries no verdict at all, the job's terminal status is the only evidence there
    is: ``passed`` reads as ``pass``, ``failed``/``error`` as ``fail``, and ``inconclusive`` (or a job
    still queued/running) as *no verdict* — a run that ended without one is not a pass.
    """
    last: m.JobEvent | None = None
    for event in job.events:
        if event.level == "milestone" and isinstance(event.data, dict) and event.data.get("verdict") in m.VERDICTS:
            last = event
    if last is not None:
        return str(last.data["verdict"]), (last.data.get("source") or None)
    if job.status == "passed":
        return "pass", "status"
    if job.status in ("failed", "error"):
        return "fail", "status"
    return None, None


def _frame_name(frame_artifact: str) -> str:
    """The artifact name from either the stored name or the REST path that serves it."""
    return frame_artifact.rstrip("/").rsplit("/", 1)[-1]


def _frame_event(job: m.Job, frame_artifact: str) -> m.JobEvent | None:
    """The job's screenshot event for this frame, matched on name or on the /api/artifacts URL."""
    wanted = _frame_name(frame_artifact)
    for event in job.events:
        if event.level != "screenshot" or not isinstance(event.data, dict):
            continue
        url = str(event.data.get("url") or "")
        if not url:
            continue
        if url.rsplit("/", 1)[-1] == wanted or url == frame_artifact:
            return event
    return None


def _frame_url(asset: m.Asset) -> str | None:
    """REST path serving the asset's frame bytes, derived from the link (never stored twice)."""
    if not asset.frame_job_id or not asset.frame_artifact:
        return None
    return f"/api/artifacts/{asset.frame_job_id}/{_frame_name(asset.frame_artifact)}"


def _row(row: sqlite3.Row) -> m.Asset:
    tags = json.loads(row["tags"]) if row["tags"] else []
    return m.Asset(
        id=row["id"],
        kind=row["kind"],
        name=row["name"],
        slug=row["slug"],
        prompt=row["prompt"],
        concept_artifact=row["concept_artifact"],
        mesh_sha256=row["mesh_sha256"],
        tris=row["tris"],
        vmdl_path=row["vmdl_path"],
        vmat_path=row["vmat_path"],
        build_id=row["build_id"],
        deploy_id=row["deploy_id"],
        frame_job_id=row["frame_job_id"],
        frame_artifact=row["frame_artifact"],
        licence=row["licence"],
        tags=tags if isinstance(tags, list) else [],
        created_by=row["created_by"],
        created_at=row["created_at"],
        retired_at=row["retired_at"],
        retired_reason=row["retired_reason"],
    )


# ---------------------------------------------------------------- promotion
def promote(
    db: Database,
    *,
    kind: str,
    name: str,
    build_id: str | None,
    licence: str | None,
    slug: str | None = None,
    prompt: str | None = None,
    concept_artifact: str | None = None,
    mesh_sha256: str | None = None,
    tris: int | None = None,
    vmdl_path: str | None = None,
    vmat_path: str | None = None,
    deploy_id: str | None = None,
    frame_job_id: str | None = None,
    frame_artifact: str | None = None,
    tags: list[str] | None = None,
    created_by: str | None = None,
) -> m.Asset:
    """Promote a chain into the catalog, enforcing every gate server-side.

    Raises :class:`AssetValidationError` (422) for a payload problem, :class:`AssetConflict` (409) when
    the referenced build/job state (or the slug) forbids promotion, and
    :class:`s2docs.db.repository.NotFound` (404) when a referenced record does not exist.
    """
    # ---- payload: everything that can be judged without touching another table
    fields: list[str] = []
    if kind not in KINDS:
        fields.append("kind")
    if not _text(name):
        fields.append("name")
    if not _text(licence):
        fields.append("licence")
    if not _text(build_id):
        fields.append("build_id")
    if fields:
        raise AssetValidationError(f"promotion is missing or invalid: {', '.join(fields)}", fields)

    if len(name.strip()) > NAME_MAX:
        raise AssetValidationError(f"name is longer than {NAME_MAX} characters", ["name"])
    clean_slug = slugify(_text(slug) or name)
    if not clean_slug or (slug and not SLUG_RE.match(str(slug).strip())):
        raise AssetValidationError(
            "slug must be lowercase kebab-case ([a-z0-9] words separated by '-'); "
            "pass one explicitly when the name has no ASCII letters",
            ["slug"],
        )
    clean_tags = normalize_tags(tags)
    clean_sha = _text(mesh_sha256)
    if clean_sha is not None:
        clean_sha = clean_sha.lower()
        if not SHA256_RE.match(clean_sha):
            raise AssetValidationError("mesh_sha256 must be 64 hex characters", ["mesh_sha256"])
    if tris is not None and (isinstance(tris, bool) or not isinstance(tris, int) or tris < 0):
        raise AssetValidationError("tris must be a non-negative integer", ["tris"])

    # ---- gate 1: the build must have succeeded
    clean_build = str(_text(build_id))
    run = builds.get_run(db, clean_build)  # NotFound -> 404
    if run.status != "succeeded":
        raise AssetConflict(
            f"build {run.id} is {run.status!r}; only a 'succeeded' build can be promoted "
            "(an uncompiled .vmdl is not an asset)"
        )

    # ---- optional deploy link: provenance, not a gate (its status is shown as it is)
    if _text(deploy_id):
        deploys.get_deployment(db, str(deploy_id).strip())  # NotFound -> 404

    # ---- gate 2: a model needs a frame from a job that did not fail
    job_id = _text(frame_job_id)
    frame = _text(frame_artifact)
    if kind == "model" and not job_id:
        raise AssetValidationError(
            "a model needs frame_job_id: the job whose in-game frame proves it renders", ["frame_job_id"]
        )
    if kind == "model" and not frame:
        raise AssetValidationError(
            "a model needs frame_artifact: the frame (screenshot) the job stored", ["frame_artifact"]
        )
    if bool(job_id) != bool(frame):
        missing = "frame_artifact" if job_id else "frame_job_id"
        raise AssetValidationError(f"frame_job_id and frame_artifact go together; {missing} is missing", [missing])

    if job_id and frame:
        job = jobs.get(db, job_id)  # NotFound -> 404
        if _frame_event(job, frame) is None:
            raise AssetValidationError(
                f"job {job.id} has no screenshot artifact {_frame_name(frame)!r}; "
                "a frame is only evidence when the job actually stored it",
                ["frame_artifact"],
            )
        if not jobs.artifact_path(job.id, _frame_name(frame)).is_file():
            raise AssetValidationError(
                f"frame artifact {_frame_name(frame)!r} is recorded on job {job.id} but not stored "
                "(a dead link proves nothing)",
                ["frame_artifact"],
            )
        if job.status in _FAILED_JOB_STATUSES:
            raise AssetConflict(f"job {job.id} is {job.status!r}; a failed run cannot attest an asset")
        verdict, _source = job_verdict(job)
        if verdict == "fail":
            raise AssetConflict(f"job {job.id}'s verdict is 'fail'; the frame does not prove the asset works")
        if verdict is None:
            raise AssetConflict(
                f"job {job.id} has no verdict yet (status {job.status!r}); "
                "a frame nobody judged is not evidence"
            )

    # ---- gate 3: licence is recorded, never inferred (already required above)

    # ---- gate 4: one slug, one asset (the row stays if retired, so the handle stays taken)
    with db.reader() as conn:
        existing = conn.execute("SELECT id, retired_at FROM assets WHERE slug = ?", (clean_slug,)).fetchone()
    if existing is not None:
        state = "retired" if existing["retired_at"] else "live"
        raise AssetConflict(f"slug {clean_slug!r} is already taken by {existing['id']} ({state})")

    aid = "a-" + secrets.token_hex(4)
    try:
        with db.writer() as conn, conn:
            conn.execute(
                "INSERT INTO assets (id, kind, name, slug, prompt, concept_artifact, mesh_sha256, tris, "
                "vmdl_path, vmat_path, build_id, deploy_id, frame_job_id, frame_artifact, licence, tags, "
                "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    aid,
                    kind,
                    name.strip(),
                    clean_slug,
                    _text(prompt),
                    _text(concept_artifact),
                    clean_sha,
                    tris,
                    _text(vmdl_path),
                    _text(vmat_path),
                    clean_build,
                    _text(deploy_id),
                    job_id,
                    _frame_name(frame) if frame else None,
                    str(_text(licence)),
                    json.dumps(clean_tags),
                    _text(created_by),
                ),
            )
    except sqlite3.IntegrityError as exc:  # a concurrent promote won the slug, or a link vanished
        raise AssetConflict(f"cannot promote {clean_slug!r}: {exc}") from exc
    return get(db, aid)


# -------------------------------------------------------------------- reads
def get(db: Database, asset_id: str) -> m.Asset:
    with db.reader() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise NotFound(f"no asset {asset_id!r}")
    return _row(row)


def resolve(db: Database, ref: str) -> m.Asset:
    """An asset by id, or by slug — the handle a page or another session links to."""
    try:
        return get(db, ref)
    except NotFound:
        with db.reader() as conn:
            row = conn.execute("SELECT id FROM assets WHERE slug = ?", (ref,)).fetchone()
        if row is None:
            raise NotFound(f"no asset {ref!r}") from None
        return get(db, row["id"])


def list_assets(
    db: Database,
    *,
    kind: str | None = None,
    tag: str | None = None,
    include_retired: bool = False,
) -> list[m.Asset]:
    """Assets newest first, optionally narrowed by kind and/or one tag.

    Retired assets are hidden unless ``include_retired`` is set — the provenance and the single-asset
    read still return them, so a retired asset never becomes unreachable evidence.
    """
    where: list[str] = []
    params: list[object] = []
    if kind is not None:
        if kind not in KINDS:
            raise AssetValidationError(f"kind must be one of {KINDS}, not {kind!r}", ["kind"])
        where.append("kind = ?")
        params.append(kind)
    if tag is not None:
        clean = tag.strip().lower()
        if not clean:
            raise AssetValidationError("tag filter may not be blank", ["tag"])
        where.append("EXISTS (SELECT 1 FROM json_each(assets.tags) WHERE json_each.value = ?)")
        params.append(clean)
    if not include_retired:
        where.append("retired_at IS NULL")
    sql = "SELECT * FROM assets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC"
    with db.reader() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row(r) for r in rows]


def provenance(db: Database, asset_id: str) -> m.AssetProvenance:
    """The asset plus the build, deploy and job it links, read from their own tables."""
    asset = resolve(db, asset_id)
    build = builds.get_run(db, asset.build_id)
    deploy = deploys.get_deployment(db, asset.deploy_id) if asset.deploy_id else None
    job = jobs.get(db, asset.frame_job_id) if asset.frame_job_id else None
    if build is not None:
        build.web_url = weburls.build_url(weburls.public_base_url(), build.id)
    if deploy is not None:
        deploy.web_url = weburls.deploy_url(weburls.public_base_url(), deploy.id)
    if job is not None:
        job.web_url = weburls.job_url(weburls.public_base_url(), job.id)
    return m.AssetProvenance(asset=asset, build=build, deploy=deploy, job=job, frame_url=_frame_url(asset))


# ------------------------------------------------------------------ retire
def retire(db: Database, asset_id: str, *, reason: str | None = None) -> m.Asset:
    """Soft-retire an asset: set ``retired_at`` (and the reason), keep the row and every link.

    Idempotent: retiring an already-retired asset returns it unchanged, so a retried call is never an
    error and can never un-retire anything. There is no hard delete — the catalog is the proof.
    """
    asset = resolve(db, asset_id)
    clean_reason = _text(reason)
    if clean_reason is not None and len(clean_reason) > RETIRED_REASON_MAX:
        raise AssetValidationError(
            f"reason is longer than {RETIRED_REASON_MAX} characters", ["reason"]
        )
    if asset.retired_at:
        return asset
    with db.writer() as conn, conn:
        conn.execute(
            "UPDATE assets SET retired_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), retired_reason = ? "
            "WHERE id = ? AND retired_at IS NULL",
            (clean_reason, asset_id),
        )
    return get(db, asset_id)
