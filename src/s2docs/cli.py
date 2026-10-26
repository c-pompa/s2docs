"""Command line: `s2docs sync | load | serve | mcp | stats | prompt`."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from s2docs.config import GAMES, get_game, get_settings
from s2docs.db.connection import Database

# Stable exit-code contract (docs/CLI_PLAN.md §2). 2 = locked matches the Phase 1 host commands.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOCKED = 2
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_TOOLCHAIN = 5
EXIT_PARTIAL = 6
EXIT_TIMEOUT = 7
EXIT_MATERIALS = 8  # build finished but the material guard flagged warnings (strict mode)
EXIT_VALIDATION = 9  # request failed validation, e.g. the `s2docs job` intake/summary gate

# `s2docs job` shares the global contract above: 3 = not found, 4 = conflict; intake/summary
# validation failures are EXIT_VALIDATION (9). An agent can always read one meaning per code.

_DURATION_RE = re.compile(r"^(\d+)([smhd]?)$")
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}

# Kept as literals so `cli.py`'s hot path never imports the Pydantic models module.
_EVIDENCE_KINDS = ("console_transcript", "file_hash", "pixel_diff", "screenshot", "note", "other")
_TEST_STATES = ("idle", "running", "pass", "fail")
# `job finish` accepts these; `job restatus` only a terminal one. Literals, mirroring models.JOB_STATUSES
# (the service validates too, so a drift here is a 9, never a bad row).
_JOB_FINISH_STATUSES = ("passed", "failed", "error", "cancelled", "inconclusive")
_JOB_TERMINAL_STATUSES = ("passed", "failed", "error", "cancelled", "inconclusive")
# Who asked for the run (mirrors models.JOB_ORIGINS; migration 0014). The learning loop reads the job
# ledger by origin, so a synthetic caller must tag itself here or it inflates a training artifact.
_JOB_ORIGINS = ("human", "agent", "driver", "eval", "probe", "warmup")


def _parse_duration(value: str) -> int:
    """Parse '1h', '90m', '300s' or a bare number of seconds into seconds."""
    match = _DURATION_RE.match(str(value).strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(f"invalid duration {value!r}; use e.g. 1h, 90m, 300s")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


def _fail(message: str, code: int) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _warn_data_dir() -> None:
    """Loud WARNING (stderr) when S2DOCS_DATA_DIR is unset — the data dir has a *fixed* default.

    With the variable unset the data dir is the source checkout's ``./data`` (or a per-user directory
    for an installed copy) — never the cwd. A server and an MCP client then agree on one store; the
    warning still names the resolved path so a split-database misconfiguration is impossible to miss.
    Stderr only: a stdio MCP client speaks JSON-RPC on stdout.
    """
    if os.environ.get("S2DOCS_DATA_DIR"):
        return
    print(
        f"WARNING: S2DOCS_DATA_DIR is not set — the data dir defaults to {get_settings().data_dir}. "
        "Set S2DOCS_DATA_DIR so this server and its MCP clients share one store.",
        file=sys.stderr,
    )


def _emit(args: argparse.Namespace, payload: dict, human: list[str]) -> int:
    """--json → one object on stdout (schema s2docs/v1); otherwise the human lines. Logs go to stderr."""
    if getattr(args, "json", False):
        print(json.dumps({"schema": "s2docs/v1", **payload}))
    else:
        for line in human:
            print(line)
    return EXIT_OK


def _cmd_sync(args: argparse.Namespace) -> int:
    from s2docs.ingest.loader import load_build
    from s2docs.ingest.sources import sync_source

    settings = get_settings()
    db = Database(settings.db_path)
    games = [] if args.docs_only else ([args.game] if args.game else list(GAMES))
    rc = 0
    for gid in games:
        game = get_game(gid)
        try:
            root = settings.sources_dir / gid
            if not args.no_fetch:
                root = sync_source(game, settings.sources_dir)
            with db.writer() as conn:
                stats = load_build(conn, game, root, keep_builds=args.keep)
            verb = "replaced" if stats.replaced else "loaded"
            print(f"{gid}: {verb} build {stats.build_number}: {json.dumps(stats.counts)}")
        except Exception as exc:  # report and continue with the next game
            logging.getLogger("s2docs").exception("sync failed for %s", gid)
            print(f"{gid}: FAILED: {exc}", file=sys.stderr)
            rc = 1
    if not args.skip_docs:
        from s2docs.ingest.wiki import load_wiki, sync_wiki_source

        try:
            root = settings.sources_dir / "source2wiki"
            if not args.no_fetch or not root.exists():
                root = sync_wiki_source(settings.sources_dir)
            with db.writer() as conn:
                counts = load_wiki(conn, root)
            print(f"source2.wiki: loaded {json.dumps(counts)}")
        except Exception as exc:
            logging.getLogger("s2docs").exception("docs sync failed")
            print(f"source2.wiki: FAILED: {exc}", file=sys.stderr)
            rc = 1
    return rc


def _cmd_load(args: argparse.Namespace) -> int:
    """Load from an existing local checkout (e.g. a full GameTracking clone)."""
    from s2docs.ingest.loader import load_build

    db = Database(get_settings().db_path)
    with db.writer() as conn:
        stats = load_build(conn, get_game(args.game), Path(args.path).expanduser().resolve(), keep_builds=args.keep)
    print(f"{args.game}: build {stats.build_number}: {json.dumps(stats.counts)}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from s2docs.api.app import create_app

    _warn_data_dir()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from s2docs.mcp_server import create_mcp_server

    _warn_data_dir()
    server = create_mcp_server(Database(get_settings().db_path))
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
    return 0


def _cmd_stats(_: argparse.Namespace) -> int:
    from s2docs.db.repository import Repository

    with Database(get_settings().db_path).reader() as conn:
        print(Repository(conn).stats().model_dump_json(indent=2))
    return 0


def _cmd_surfaces(args: argparse.Namespace) -> int:
    """`s2docs surfaces`: which operations exist, and which surface (CLI, API, MCP) exposes each.

    Read-only and database-free: it prints the declarative table in `s2docs.surfaces` plus whatever
    that module's verifier found against the parser, the FastAPI app and the MCP tool registry.
    `--gaps` prints only the disagreements and exits 1 when there is one, so CI can gate on it; the
    same verdict is the `surfaces_list` MCP tool, for a session that only speaks MCP.
    """
    from s2docs import surfaces

    mismatches = surfaces.verify()
    if args.gaps:
        if getattr(args, "json", False):
            print(json.dumps({"schema": "s2docs/v1", "gaps": [m.as_dict() for m in mismatches]}))
        elif mismatches:
            for m in mismatches:
                print(f"{m.operation:24} {m.surface:5} {m.kind:10} {m.detail}")
            print(f"{len(mismatches)} gap(s): the declared table and the code disagree")
        else:
            print("no gaps: every declared surface exists and no undeclared surface exposes an operation")
        return EXIT_ERROR if mismatches else EXIT_OK
    if getattr(args, "json", False):
        print(json.dumps({
            "schema": "s2docs/v1",
            "operations": [op.as_dict() for op in surfaces.OPERATIONS],
            "mismatches": [m.as_dict() for m in mismatches],
        }))
        return EXIT_OK
    width = max(len(op.id) for op in surfaces.OPERATIONS) + 2
    print(f"{'operation':{width}} {'surfaces':18} {'cli':26} {'api':44} mcp")
    for op in surfaces.OPERATIONS:
        exposed = ",".join(s for s in surfaces.SURFACES if s in op.surfaces)
        api = f"{op.api[0]} {op.api[1]}" if op.api else "-"
        print(f"{op.id:{width}} {exposed:18} {' '.join(op.cli) or '-':26} {api:44} {op.mcp or '-'}")
    print(f"\n{len(surfaces.OPERATIONS)} operations, {len(mismatches)} mismatch(es); `s2docs surfaces --gaps`")
    return EXIT_OK


def _cmd_review(args: argparse.Namespace) -> int:
    """Scope a branch diff into per-area reviewer briefs (read-only git client)."""
    from s2docs import review

    try:
        specs = review.parse_area_specs(args.area or [])
        scope = review.build_scope(repo=args.repo or os.getcwd(), base=args.base, branch=args.branch, area_specs=specs)
    except review.ReviewError as exc:
        return _fail(str(exc), EXIT_ERROR)
    if getattr(args, "json", False):
        print(json.dumps({"schema": "s2docs/v1", **scope.as_dict()}))
    else:
        print(review.render_markdown(scope))
    return EXIT_OK


def _cmd_prompt(args: argparse.Namespace) -> int:
    from s2docs.db.repository import Repository
    from s2docs.prompts import render_prompt

    kv = dict(a.split("=", 1) for a in args.arg)
    with Database(get_settings().db_path).reader() as conn:
        print(render_prompt(Repository(conn), args.name, kv))
    return 0


def _print_diags(result) -> int:
    for d in result.diagnostics:
        print(f"  {d.line}:{d.column} {d.severity:7} {d.category:22} {d.message}")
        if d.hint:
            print(f"      hint: {d.hint}")
    print(f"{'OK' if result.ok else 'FAILED'}: {result.errors} error(s), {result.warnings} warning(s)")
    return 0 if result.ok else 1


def _cmd_validate_script(args: argparse.Namespace) -> int:
    from s2docs.build.validate_script import validate_cs_script
    from s2docs.db.repository import Repository

    path = Path(args.file)
    with Database(get_settings().db_path).reader() as conn:
        result = validate_cs_script(
            Repository(conn), path.read_text(encoding="utf-8"), game=args.game, filename=path.name
        )
    print(result.checker)
    return _print_diags(result)


def _cmd_validate_cfg(args: argparse.Namespace) -> int:
    from s2docs.build.validate_cfg import validate_cfg
    from s2docs.db.repository import Repository

    with Database(get_settings().db_path).reader() as conn:
        result = validate_cfg(
            Repository(conn), Path(args.file).read_text(encoding="utf-8"), game=args.game, context=args.context
        )
    print(f"{result.commands} command(s) checked")
    return _print_diags(result)


def _cmd_scaffold(args: argparse.Namespace) -> int:
    from s2docs.build.workspace import scaffold
    from s2docs.db.repository import Repository

    settings = get_settings()
    with Database(settings.db_path).reader() as conn:
        addon = scaffold(settings.workspace_dir, Repository(conn), game=args.game, name=args.name)
    print(addon.root)
    for f in addon.files:
        print(f"  {f.path} ({f.size} B)")
    return 0


def _cmd_asset(args: argparse.Namespace) -> int:
    """Write a Source 2 model document (`.vmdl`) for a mesh inside an addon's content tree.

    The document is generated from the shape a real CS2 document uses and is compiled for real by
    `resourcecompiler` (see docs/HAMMERED.md); this command only authors it.
    """
    from s2docs.asset.model import ModelDocError, render_model_doc, write_model_doc
    from s2docs.build.workspace import addon_root

    settings = get_settings()
    root = addon_root(settings.workspace_dir, args.game, args.addon)
    if not root.is_dir():
        return _fail(
            f"addon {args.addon!r} does not exist in {settings.workspace_dir}; "
            f"run `s2docs scaffold {args.addon}` first",
            3,
        )
    if args.kind == "material":
        return _asset_material(args, root)
    if not args.mesh:
        return _fail("--mesh is required for a model (s2docs asset [model] --mesh models/<n>/<n>.fbx)", 2)
    target = root / "models" / args.name / f"{args.name}.vmdl"
    try:
        doc = render_model_doc(
            args.name,
            args.mesh,
            translation=tuple(args.translate) if args.translate else None,
            collision=args.collision_surface,
            collision_mesh=args.collision_mesh,
        )
        write_model_doc(target, doc, overwrite=args.force)
    except ModelDocError as exc:
        return _fail(str(exc), 2)
    rel = target.relative_to(root).as_posix()
    payload = {
        "path": str(target),
        "addon": args.addon,
        "mesh": args.mesh,
        "collision_surface": args.collision_surface,
        "collision_mesh": args.collision_mesh or (args.mesh if args.collision_surface else None),
        "bytes": len(doc.encode()),
    }
    lines = [
        f"wrote {rel} ({payload['bytes']} B)",
        f"  mesh reference: {args.mesh} (addon-relative)",
    ]
    if args.collision_surface:
        # The hull is selected by name inside the mesh file: PhysicsHullFile.name = <name>_collision.
        lines.append(
            f"  physics: PhysicsHullFile on {payload['collision_mesh']}, surface_prop "
            f"{args.collision_surface!r}, mesh name {args.name}_collision"
        )
    else:
        lines.append("  no physics node (pass --collision-surface <surfaceproperties name> to add one)")
    lines.append(f"  compile it on a host: resourcecompiler -i <addon content>/{rel} -nop4")
    _emit(args, payload, lines)
    return 0


def _emit_chain(args: argparse.Namespace, job) -> int:
    """Render a chain job: the stages with their disposition, then the close-out's next step."""
    lines = [f"{job.id} {job.status} (phase {job.phase})"]
    for event in job.events:
        if not event.phase:
            continue
        if event.data.get("stubbed"):
            mark = "stub"
        elif event.data.get("blocked"):
            mark = "blocked"
        elif event.data.get("skipped"):
            mark = "skipped"
        else:
            mark = event.level
        lines.append(f"  {event.phase:<8} {mark:<8} {event.message}")
    if job.summary is not None:
        lines.append(f"  next: {job.summary.next}")
    _emit(args, job.model_dump(mode="json"), lines)
    return EXIT_ERROR if job.status == "error" else EXIT_OK


def _cmd_pipeline(args: argparse.Namespace) -> int:
    """`s2docs pipeline run`: the chain as one job — asset+build real, concept/mesh stubbed, deploy/ingame blocked."""
    from s2docs import pipeline

    db = Database(get_settings().db_path)
    job = pipeline.run_asset_build_slice(
        db,
        prompt=args.prompt,
        addon=args.addon,
        name=args.name,
        mesh=args.mesh,
        host_id=args.host,
        material_color=args.color,
        project_id=args.project,
        build_command=args.build_command,
        build_timeout_sec=args.build_timeout,
    )
    return _emit_chain(args, job)


def _cmd_pipeline_script(args: argparse.Namespace) -> int:
    """`s2docs pipeline script`: text -> cs_script as one job — generate+validate real, the rest blocked."""
    from s2docs import pipeline

    text = Path(args.script).read_text(encoding="utf-8") if args.script else None
    db = Database(get_settings().db_path)
    job = pipeline.run_script_slice(
        db,
        prompt=args.prompt,
        addon=args.addon,
        name=args.name,
        host_id=args.host,
        script_text=text,
        project_id=args.project,
    )
    return _emit_chain(args, job)


def _asset_material(args: argparse.Namespace, root) -> int:
    """`s2docs asset material`: write materials/hammered/<name>/<name>.vmat + the PNGs beside it.

    The colour PNG's baked ``<name>_normal.png`` / ``<name>_ao.png`` siblings are picked up from the
    same folder automatically: the bridge writes all three into one ``.fbm`` dir, so the one command a
    user runs after a bake is this one, with ``--color`` pointing at the colour map.
    """
    from pathlib import Path

    from s2docs.asset.material import (
        BAKED_MAP_VMAT_KEY,
        DEFAULT_SHADER,
        MaterialError,
        baked_texture_path,
        color_texture_path,
        find_baked_maps,
        material_path,
        write_vmat,
    )

    if not args.color:
        return _fail("--color <baked colour .png> is required for a material", 2)
    shader = args.shader or DEFAULT_SHADER
    try:
        target = write_vmat(root, args.name, Path(args.color), shader=shader, force=args.force)
    except MaterialError as exc:
        return _fail(str(exc), 2)
    rel = material_path(args.name)
    baked = {kind: baked_texture_path(args.name, kind) for kind in find_baked_maps(Path(args.color))}
    textures = {"TextureColor": color_texture_path(args.name),
                **{BAKED_MAP_VMAT_KEY[kind]: path for kind, path in baked.items()}}
    payload = {"path": str(target), "addon": args.addon, "material": rel, "shader": shader,
               "textures": textures, "bytes": target.stat().st_size}
    _emit(args, payload, [
        f"wrote {rel} ({payload['bytes']} B, shader {shader})",
        *(f"  {key}: {path}" for key, path in textures.items()),
        f"  the FBX material must be named exactly {rel!r} (glb_to_fbx_uv.py --material-path)",
    ])
    return 0


def _cmd_host(args: argparse.Namespace) -> int:
    from s2docs import hosts

    db = Database(get_settings().db_path)

    def _stuck_marker(host_list) -> None:
        """A human-visible STUCK line on stderr (JSON stays clean on stdout)."""
        for h in host_list:
            lock = h.lock
            if lock is not None and lock.stuck:
                print(f"STUCK: {h.id} held by {lock.owner!r} — {lock.stuck_reason}", file=sys.stderr)

    try:
        if args.host_cmd == "list":
            hs = hosts.list_hosts(db)
            out = [h.model_dump() for h in hs]
            _stuck_marker(hs)
        elif args.host_cmd == "show":
            h = hosts.get_host(db, args.host_id)
            out = h.model_dump()
            _stuck_marker([h])
        elif args.host_cmd == "register":
            out = hosts.register_host(
                db,
                args.host_id,
                args.ssh_target,
                hostname=args.hostname,
                ssh_key=args.ssh_key,
                shell=args.shell,
                os=args.os,
                notes=args.notes,
            ).model_dump()
        elif args.host_cmd == "probe":
            out = hosts.probe_host(db, args.host_id).model_dump()
        elif args.host_cmd == "lock":
            out = hosts.claim_host(
                db, args.host_id, args.owner, ttl_sec=args.ttl, force=args.force, phase=args.phase, note=args.note
            ).model_dump()
        elif args.host_cmd == "heartbeat":
            out = hosts.heartbeat_host(db, args.host_id, args.owner, phase=args.phase, note=args.note).model_dump()
        elif args.host_cmd == "unlock":
            out = {"result": hosts.release_host(db, args.host_id, args.owner, force=args.force)}
        else:  # pragma: no cover - argparse enforces the subcommand
            raise hosts.HostError(f"unknown host command {args.host_cmd!r}")
    except hosts.HostLocked as exc:
        print(f"LOCKED: {exc}", file=sys.stderr)
        return 2
    except hosts.HostError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


def _human_run(run) -> list[str]:
    from s2docs import builds

    line = f"{run.id}  {run.host_id}  {run.status}  {run.kind}  exit={run.exit_code}"
    out = [line]
    if run.summary:
        shown = {k: v for k, v in run.summary.items() if k != "material_guard"}
        out.append("  summary: " + ", ".join(f"{k}={v}" for k, v in shown.items()))
    if builds.has_material_warnings(run):
        counts = run.warnings.counts
        out.append("  MATERIAL WARNINGS: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v))
        for kind, offenders in run.warnings.paths.items():
            for path in offenders:
                out.append(f"    {kind}: {path}")
    if run.handle:
        out.append(f"  handle: {run.handle}")
    if run.log_path:
        out.append(f"  log: {run.log_path}")
    if run.notes:
        out.append(f"  notes: {run.notes}")
    return out


def _emit_run(args: argparse.Namespace, run) -> int:
    """Emit a run, then apply the --strict-materials gate: a 'succeeded'-but-warned run exits non-zero.

    The resourcecompiler's own semantics keep such a run 'succeeded'; strict mode is the CI opt-in
    that refuses to treat it as clean (the red/black-checkerboard incident class).
    """
    from s2docs import builds

    rc = _emit(args, {"run": run.model_dump()}, _human_run(run))
    if getattr(args, "strict_materials", False) and builds.has_material_warnings(run):
        counts = ", ".join(f"{k}={v}" for k, v in run.warnings.counts.items() if v)
        return _fail(f"material guard: {counts} (a clean rebuild needs 0 missing/reserved materials)", EXIT_MATERIALS)
    return rc


def _cmd_build(args: argparse.Namespace) -> int:
    from s2docs import builds, hosts
    from s2docs.db.repository import NotFound

    db = Database(get_settings().db_path)
    try:
        if args.build_cmd == "start":
            note_parts = [f"target={args.target}"]
            if args.map:
                note_parts.append(f"map={args.map}")
            run = builds.start(
                db,
                args.host,
                args.command,
                kind=args.kind,
                log_path=args.log,
                timeout_sec=args.timeout,
                notes=" ".join(note_parts),
            )
            if args.wait:
                run = builds.wait(db, run.id, timeout_sec=args.timeout)
            return _emit_run(args, run)
        if args.build_cmd == "status":
            run = builds.get_run(db, args.run_id)
            return _emit_run(args, run)
        if args.build_cmd == "log":
            if args.follow:
                return _follow_log(db, args.run_id)
            tail = builds.log_tail(db, args.run_id, lines=args.lines)
            if args.json:
                print(json.dumps({"schema": "s2docs/v1", "log": tail.model_dump()}))
                return EXIT_OK
            for line in tail.lines:
                print(line)
            print(f"# encoding={tail.encoding} size={tail.size} truncated={tail.truncated}", file=sys.stderr)
            if tail.warnings is not None:
                counts = ", ".join(f"{k}={v}" for k, v in tail.warnings.counts.items() if v)
                print(f"# MATERIAL WARNINGS: {counts}", file=sys.stderr)
                for kind, offenders in tail.warnings.paths.items():
                    for path in offenders:
                        print(f"#   {kind}: {path}", file=sys.stderr)
            return EXIT_OK
        if args.build_cmd == "wait":
            run = builds.wait(db, args.run_id, timeout_sec=args.timeout)
            return _emit_run(args, run)
        if args.build_cmd == "cancel":
            run = builds.cancel(db, args.run_id)
            return _emit(args, {"run": run.model_dump()}, _human_run(run))
        if args.build_cmd == "list":
            runs = builds.list_runs(db, host_id=args.host, active=args.active)
            human = [f"{r.id}  {r.host_id}  {r.status:9}  {r.kind}  {r.summary}" for r in runs] or ["no runs"]
            return _emit(args, {"runs": [r.model_dump() for r in runs]}, human)
    except hosts.HostLocked as exc:
        return _fail(f"LOCKED: {exc}", EXIT_LOCKED)
    except NotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except builds.BuildTimeout as exc:
        return _fail(str(exc), EXIT_TIMEOUT)
    except (builds.BuildError, hosts.HostError) as exc:
        return _fail(str(exc), EXIT_ERROR)
    return _fail(f"unknown build command {args.build_cmd!r}", EXIT_ERROR)


def _follow_log(db, run_id: str) -> int:
    """Stream new log lines until the run reaches a terminal status."""
    from s2docs import builds

    seen = 0
    while True:
        run = builds.get_run(db, run_id)
        tail = builds.log_tail(db, run_id, lines=1_000_000)
        for line in tail.lines[seen:]:
            print(line, flush=True)
        seen = len(tail.lines)
        if run.status in ("succeeded", "failed", "cancelled"):
            return EXIT_OK if run.status == "succeeded" else EXIT_ERROR
        time.sleep(1.0)


def _manifest_files(path: str) -> list:
    from s2docs import models as m

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    base = Path(path).parent
    raw = data.get("files", data.get("entries", []))
    files = []
    for item in raw:
        spec = m.DeployFile(**item)
        if spec.local and not Path(spec.local).is_absolute():
            spec.local = str((base / spec.local).resolve())
        files.append(spec)
    return files


def _addon_manifest(args: argparse.Namespace) -> list:
    from s2docs import models as m
    from s2docs.build.workspace import describe

    addon = describe(get_settings().workspace_dir, args.game, args.addon)
    root = Path(addon.root)
    return [m.DeployFile(path=f.path, local=str(root / f.path)) for f in addon.files]


def _human_deploy(deploy) -> list[str]:
    out = [f"{deploy.id}  {deploy.host_id}  {deploy.status}  {len(deploy.entries)} file(s)"]
    for entry in deploy.entries:
        out.append(f"  {'ok ' if entry.verified else '   '}{entry.path}  {entry.md5 or ''}")
    if deploy.notes:
        out.append(f"  notes: {deploy.notes}")
    return out


def _cmd_deploy(args: argparse.Namespace) -> int:
    from s2docs import deploys, hosts
    from s2docs.db.repository import NotFound

    db = Database(get_settings().db_path)
    try:
        if args.deploy_cmd == "push":
            if bool(args.addon) == bool(args.manifest):
                return _fail("pass exactly one of <addon> or --manifest", EXIT_ERROR)
            if not args.dest:
                return _fail("--dest (remote root) is required", EXIT_ERROR)
            files = _manifest_files(args.manifest) if args.manifest else _addon_manifest(args)
            deploy = deploys.push(
                db,
                args.host,
                args.dest,
                files,
                addon=args.addon,
                parallel=args.parallel,
                dry_run=args.dry_run,
                verify=not args.no_verify,
                owner=args.owner,
            )
            _emit(args, {"deployment": deploy.model_dump()}, _human_deploy(deploy))
            return EXIT_PARTIAL if deploy.status == "failed" else EXIT_OK
        if args.deploy_cmd == "verify":
            if args.deploy_id:
                deploy = deploys.verify(db, args.deploy_id)
            elif args.host:
                deploy = deploys.verify(db, deploys.latest_for_host(db, args.host).id)
            else:
                return _fail("pass a <deploy-id> or --host", EXIT_ERROR)
            _emit(args, {"deployment": deploy.model_dump()}, _human_deploy(deploy))
            return EXIT_PARTIAL if deploy.status == "failed" else EXIT_OK
        if args.deploy_cmd == "list":
            deploys_list = deploys.list_deployments(db, host_id=args.host)
            human = [f"{d.id}  {d.host_id}  {d.status:8}  {len(d.entries)} file(s)" for d in deploys_list]
            return _emit(args, {"deployments": [d.model_dump() for d in deploys_list]}, human or ["no deployments"])
    except NotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except hosts.HostError as exc:
        return _fail(str(exc), EXIT_ERROR)
    except deploys.DeployError as exc:
        return _fail(str(exc), EXIT_PARTIAL)
    return _fail(f"unknown deploy command {args.deploy_cmd!r}", EXIT_ERROR)


def _human_evidence(ev) -> list[str]:
    out = [f"{ev.id}  {ev.kind}  project={ev.project_id}  step={ev.step_id or '-'}"]
    if ev.ref:
        out.append(f"  ref: {ev.ref}")
    if ev.data:
        out.append(f"  data: {json.dumps(ev.data)}")
    out.append(f"  created: {ev.created_at}")
    return out


def _cmd_evidence(args: argparse.Namespace) -> int:
    from s2docs import evidence
    from s2docs.db.repository import NotFound

    db = Database(get_settings().db_path)
    try:
        if args.evidence_cmd == "add":
            kind = args.kind or ("note" if args.note else None)
            if kind is None:
                return _fail("pass --kind or --note", EXIT_ERROR)
            data = json.loads(args.data) if args.data else {}
            if args.note:
                data = {**data, "text": args.note}
            ev = evidence.add(db, kind=kind, project_id=args.project, step_id=args.step, ref=args.ref, data=data)
            return _emit(args, {"evidence": ev.model_dump()}, _human_evidence(ev))
        if args.evidence_cmd == "list":
            rows = evidence.list_evidence(db, project_id=args.project, step_id=args.step, kind=args.kind)
            human = [f"{e.id}  {e.kind:18}  {e.step_id or '-':16}  {e.ref or ''}" for e in rows]
            return _emit(args, {"evidence": [e.model_dump() for e in rows]}, human or ["no evidence"])
        if args.evidence_cmd == "show":
            ev = evidence.get(db, args.evidence_id)
            return _emit(args, {"evidence": ev.model_dump()}, _human_evidence(ev))
    except NotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except (evidence.EvidenceError, json.JSONDecodeError) as exc:
        return _fail(str(exc), EXIT_ERROR)
    return _fail(f"unknown evidence command {args.evidence_cmd!r}", EXIT_ERROR)


def _human_learning_report(out: dict) -> list[str]:
    return [f"jobs: {out['jobs_total']}  by origin: {json.dumps(out['jobs_by_origin'])}",
            f"real usage: {out['real_usage_jobs']}  synthetic: {out['synthetic_jobs']}",
            f"events: {out['events_total']}  learning dir: {out['learning_dir']}"]


def _map_gate_exit(rc: int) -> int:
    """Translate the eval gate's OWN exit codes into this repo's stable contract.

    ``s2docs/learning/gate.py`` uses 0 pass · 1 regression · 2 could-not-evaluate (its ``main`` and
    ``self_test`` return those). In THIS repo, ``2`` is EXIT_LOCKED (a host lease). So a
    could-not-evaluate MUST be remapped to EXIT_PARTIAL (6): an agent branches on the code, and 2
    would make it retry a lock that does not exist instead of fixing the missing/short golden set.
    gate ``1`` (regression) is a domain failure and stays EXIT_ERROR (1). Documented in
    docs/CLI_PLAN.md §2.
    """
    return {0: EXIT_OK, 1: EXIT_ERROR, 2: EXIT_PARTIAL}.get(rc, EXIT_ERROR)


def _human_learning_digest(out: dict) -> list[str]:
    """Report path, signal count and counts by class, plus the harness notes (rule 6: name the source)."""
    lines = [f"report: {out['report']}", f"signals: {out['signals']}"]
    classes = [f"  {klass}: {n}" for klass, n in sorted(out["counts"].items(), key=lambda kv: (-kv[1], kv[0]))]
    lines += classes or ["  (no signals in the window)"]
    lines += [f"  note: {n}" for n in out["notes"]]
    return lines


def _cmd_learning(args: argparse.Namespace) -> int:
    """`s2docs learning`: the loop's read/measure/prepare surface over the job ledger.

    Exit 6 (EXIT_PARTIAL) when a declared source could not be read (an empty artifact is worse than a
    named skip) OR when the eval gate could not evaluate. Exit 3 for an unknown job on `promote`.
    Exit 9 when SFT prep refuses for want of a baseline. `promote` treats a duplicate as EXIT_OK — a
    double click is not an error.
    """
    from s2docs.learning import LearningSourceMissing
    from s2docs.learning import harvest as H
    from s2docs.learning import report as L
    from s2docs.learning.gate import GateError
    from s2docs.learning.rewrite_eval import RewriteEvalError
    from s2docs.learning.sft import SftGateError

    db = Database(get_settings().db_path)
    try:
        if args.learning_cmd == "report":
            out = L(db, since_days=args.since_days)
            return _emit(args, out, _human_learning_report(out))
        if args.learning_cmd == "harvest":
            origin = None if args.origin == "all" else args.origin
            out = H.harvest(db, origin=origin, since_days=args.since_days,
                            search=lambda q: H.search_terms(db, q))
            human = [f"{k}: {v}" for k, v in out["counts"].items()] or ["nothing harvested"]
            return _emit(args, out, human)
        if args.learning_cmd == "digest":
            from s2docs.learning import digest as D

            out = D.digest(db, since_days=args.since_days)
            if getattr(args, "json", False):
                print(json.dumps({"schema": "s2docs/v1", **out}))
            else:
                for line in _human_learning_digest(out):
                    print(line)
            # The digest never raises for an unreadable source (that is its design) — it reports it in
            # `notes`. At the CLI boundary a reported skip is not a pass: exit 6 and name it on stderr.
            if out["notes"]:
                return _fail("declared source unreadable: " + "; ".join(out["notes"]), EXIT_PARTIAL)
            return EXIT_OK
        if args.learning_cmd == "gate":
            from s2docs.learning import gate as G

            gate_argv = (["--self-test"] if args.self_test else []) + (["-v"] if args.learning_verbose else [])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):           # gate.main prints human metrics to stdout
                rc = G.main(gate_argv)
            mapped = _map_gate_exit(rc)                     # gate 2 (could not evaluate) -> 6, never 2
            lines = buf.getvalue().strip().splitlines()
            if getattr(args, "json", False):
                print(json.dumps({"schema": "s2docs/v1",
                                  "gate": {"exit": rc, "exit_code": mapped, "output": lines}}))
            else:
                for line in lines:
                    print(line)
            return mapped
        if args.learning_cmd == "sft":
            from s2docs.learning import sft as S

            out = S.prep(db, require_baseline=args.require_baseline, since_days=args.since_days)
            rep = S.report(db)
            human = [
                f"examples: {out['examples']}  -> {out['dataset']}",
                f"config: {out['config']}",
                f"ready_to_train: {rep['ready_to_train']} (min {rep['min_examples']}, "
                f"target {rep['target_examples']})  eval baseline recall: {rep['eval_baseline_recall']}",
                rep["honest_note"],
            ]
            return _emit(args, {**out, "report": rep}, human)
        if args.learning_cmd == "export":
            from s2docs.learning import export as X

            out = X.everything(db, since_days=args.since_days)
            st = out["corpus_stats"]
            human = [
                f"corpus: {out['corpus']}  blocks={st['blocks']} bytes={st['bytes']} "
                f"chunks~{st['chunks_approx']} lab4_ready={st['lab4_ready']} "
                f"(needs {st['min_chunks']}, +{st['val_chunks']} val)",
                f"problems: {out['problems']}",
                f"cases: {out['cases']}",
                f"dir: {out['dir']}",
            ]
            return _emit(args, out, human)
        if args.learning_cmd == "exemplars":
            from s2docs.learning import exemplar as E

            out = E.export_approved(db, since_days=args.since_days)
            human = [f"written: {len(out['written'])}  dir: {out['dir']}"]
            human += [f"  {p}" for p in out["written"]]
            # A skipped row is the guard working, not an error: report it, exit 0.
            human += [f"  skipped {r['job_id']}: {'; '.join(r['reasons'])}" for r in out["skipped"]]
            if not out["written"] and not out["skipped"]:
                human.append("  (no approved confirmations in the window)")
            return _emit(args, out, human)
        if args.learning_cmd == "review":
            from s2docs.learning import review as R

            rows = R.queue(db, since_days=args.since_days)
            human = [
                f"{r['job_id']}  {r['failure_mode']:12}  promoted={r['promoted']}  "
                f"{' '.join((r.get('question') or '').split())[:80]}"
                for r in rows
            ]
            return _emit(args, {"rows": rows}, human or ["no corrections in the window"])
        if args.learning_cmd == "promote":
            from s2docs.learning import review as R

            out = R.promote(db, args.job, expected=args.expect, query=args.query, checker=args.checker)
            if out.get("appended"):
                human = [f"appended: {out['id']}", f"query (the gate will search with): {out['query']!r}",
                         f"expected: {out['expect_key']}"]
            else:
                human = [f"not appended ({out['reason']}): job {out['job_id']} is already a golden case"]
            return _emit(args, out, human)          # a duplicate is EXIT_OK — a double click is not an error
        if args.learning_cmd == "rewrite":
            from s2docs.learning import rewrite as RW

            r = RW.rewrite(args.text)
            human = [
                f"query: {r.query}",
                f"game: {r.game or 'undetermined'}",
                f"dropped: {' '.join(r.dropped) or '(none)'}",
            ] + [f"  note: {n}" for n in r.notes]
            return _emit(args, {"rewrite": {"query": r.query, "game": r.game,
                                            "dropped": list(r.dropped), "notes": list(r.notes)}}, human)
        if args.learning_cmd == "rewrite-eval":
            from s2docs.learning import rewrite_eval as RE

            spec = getattr(args, "rewriter", None)
            # Lazy: the import path is only resolved when --rewriter is given. A bad path (or a
            # callable that raises) surfaces as RewriteEvalError -> EXIT_PARTIAL (6), never a traceback.
            rewriter = RE.load_rewriter(spec) if spec else None
            out = RE.evaluate(rewriter=rewriter)             # RE.main's own 0/1/2 contract, mapped below
            mapped = _map_gate_exit(out["code"])             # could-not-evaluate (2) -> EXIT_PARTIAL (6)
            if getattr(args, "json", False):
                print(json.dumps({"schema": "s2docs/v1", "rewrite_eval": out}))
            else:
                for line in RE.report_lines(out):
                    print(line)
            if out["reason"]:
                print(("COULD NOT EVALUATE" if out["code"] == 2 else "REWRITE EVAL FAILED")
                      + f": {out['reason']}", file=sys.stderr)
            return mapped
    except LearningSourceMissing as exc:
        return _fail(str(exc), EXIT_PARTIAL)  # 6: a source was unreadable — a skip is not a pass
    except KeyError as exc:                    # review.promote on a job with no correction row
        return _fail(str(exc), EXIT_NOT_FOUND)
    except SftGateError as exc:
        return _fail(str(exc), EXIT_VALIDATION)  # 9: no eval baseline — refuse to prep a fine-tune
    except GateError as exc:                    # export.cases(): the golden set is a declared source
        return _fail(f"COULD NOT EVALUATE: {exc}", EXIT_PARTIAL)
    except RewriteEvalError as exc:             # rewrite-eval: the live index was unreadable
        return _fail(f"COULD NOT EVALUATE: {exc}", EXIT_PARTIAL)
    return _fail(f"unknown learning command {args.learning_cmd!r}", EXIT_ERROR)


def _human_test(run) -> list[str]:
    out = [f"{run.id}  {run.name}  {run.state}  step={run.step or '-'}  elapsed={run.elapsed_sec}s"]
    if run.host_id:
        out.append(f"  host: {run.host_id}")
    if run.project_id:
        out.append(f"  project: {run.project_id}")
    if run.note:
        out.append(f"  note: {run.note}")
    return out


def _cmd_test(args: argparse.Namespace) -> int:
    from s2docs import hosts, test_runs
    from s2docs.db.repository import NotFound

    db = Database(get_settings().db_path)
    try:
        if args.test_cmd == "start":
            run = test_runs.start(db, args.name, host_id=args.host, project_id=args.project)
            return _emit(args, {"test_run": run.model_dump()}, _human_test(run))
        if args.test_cmd == "step":
            run = test_runs.step(db, args.run_id, args.text)
            return _emit(args, {"test_run": run.model_dump()}, _human_test(run))
        if args.test_cmd == "finish":
            run = test_runs.finish(db, args.run_id, args.result, note=args.note)
            return _emit(args, {"test_run": run.model_dump()}, _human_test(run))
        if args.test_cmd == "clear":
            run = test_runs.clear(db, args.run_id)
            return _emit(args, {"test_run": run.model_dump()}, _human_test(run))
        if args.test_cmd == "status":
            run = test_runs.get(db, args.run_id) if args.run_id else test_runs.current(db)
            if run is None:
                return _fail("no active test run", EXIT_NOT_FOUND)
            return _emit(args, {"test_run": run.model_dump()}, _human_test(run))
        if args.test_cmd == "list":
            runs = test_runs.list_runs(db, state=args.state)
            human = [f"{r.id}  {r.name}  {r.state:8}  step={r.step or '-'}  elapsed={r.elapsed_sec}s" for r in runs]
            return _emit(args, {"test_runs": [r.model_dump() for r in runs]}, human or ["no test runs"])
    except NotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except hosts.HostLocked as exc:
        return _fail(f"LOCKED: {exc}", EXIT_LOCKED)
    except (test_runs.TestRunError, hosts.HostError) as exc:
        return _fail(str(exc), EXIT_ERROR)
    return _fail(f"unknown test command {args.test_cmd!r}", EXIT_ERROR)


def _human_job(job) -> list[str]:
    out = [f"{job.id}  {job.kind:5}  {job.status:12}  pos={job.position}  {job.title}"]
    if getattr(job, "web_url", None):
        out.append(f"  VIEW: {job.web_url}")
    out.append(f"  target: {job.target}")
    if job.host_id:
        out.append(f"  host: {job.host_id}")
    if job.worker:
        out.append(f"  worker: {job.worker}")
    if job.phase:
        out.append(f"  phase: {job.phase}")
    if job.error_count or job.crash_count:
        out.append(f"  errors={job.error_count} crashes={job.crash_count}")
    p = job.params or {}
    out.append(f"  purpose: {p.get('purpose', '')}")
    out.append(f"  expected: {p.get('expected', '')}")
    out.append(
        f"  max_minutes: {p.get('max_minutes')}  screenshots: {p.get('screenshots')}  confirm: {p.get('needs_confirm')}"
    )
    if p.get("pending_shot"):
        out.append("  pending_shot: true")
    out.append(f"  events: {len(job.events)}  links: {len(job.links)}  artifacts: {len(job.artifacts)}")
    if job.summary:
        for key in ("what", "why", "learned", "next"):
            out.append(f"  {key}: {getattr(job.summary, key, '')}")
    return out


def _tail_lines(path: str, count: int = 200) -> str:
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-count:])


def _cmd_job(args: argparse.Namespace) -> int:
    from s2docs import jobs
    from s2docs.db.repository import NotFound

    db = Database(get_settings().db_path)
    try:
        if args.job_cmd == "enqueue":
            job = jobs.enqueue(
                db,
                kind=args.kind,
                title=args.title,
                target=args.target,
                purpose=args.purpose,
                expected=args.expected,
                host_id=args.host,
                host_choice="ask" if args.ask_host else None,
                max_minutes=args.max_minutes,
                reason=args.reason,
                needs_confirm=args.confirm,
                screenshots=args.screenshots,
                watching=args.watching,
                branch=args.branch,
                commit=args.commit,
                requested_by=args.requested_by,
                origin=args.origin,
            )
            return _emit(args, {"job": job.model_dump()}, _human_job(job))
        if args.job_cmd == "list":
            rows = jobs.list_jobs(db, status="active" if args.active else None, host_id=args.host)
            human = [
                f"{j.id}  {j.kind:5}  {j.status:12}  pos={j.position}  {j.host_id or '-'}  {j.title}" for j in rows
            ]
            return _emit(args, {"jobs": [j.model_dump() for j in rows]}, human or ["no jobs"])
        if args.job_cmd == "show":
            job = jobs.get(db, args.job_id)
            return _emit(args, {"job": job.model_dump()}, _human_job(job))
        if args.job_cmd == "next":
            job = jobs.next_job(db, args.host)
            return _emit(args, {"job": job.model_dump()}, _human_job(job))
        if args.job_cmd == "start":
            job = jobs.start(db, args.job_id, worker=args.worker)
            return _emit(args, {"job": job.model_dump()}, _human_job(job))
        if args.job_cmd == "event":
            data = json.loads(args.data) if args.data else {}
            if args.log_file:
                data = {**data, "log_tail": _tail_lines(args.log_file, 200)}
            ev = jobs.event(db, args.job_id, level=args.level, message=args.message, phase=args.phase, data=data)
            return _emit(args, {"event": ev.model_dump()}, [f"{ev.id}  {ev.level}  {ev.phase or '-'}  {ev.message}"])
        if args.job_cmd == "attach":
            path = Path(args.file).expanduser()
            name = args.name or path.name
            ctype = jobs.content_type_for(name)
            if ctype is None:
                return _fail(
                    f"unsupported file type {path.suffix!r}; use png/jpg/txt/log/json/zip/dmp/mdmp/evtx", EXIT_ERROR
                )
            art = jobs.save_artifact(db, args.job_id, name, path.read_bytes(), ctype)
            return _emit(args, {"artifact": art.model_dump()}, [f"{art.name}  {art.size} B  md5={art.md5}  {art.url}"])
        if args.job_cmd == "shot-request":
            job = jobs.request_screenshot(db, args.job_id, note=args.note)
            return _emit(args, {"job": job.model_dump()}, _human_job(job))
        if args.job_cmd == "finish":
            summary = {"what": args.what, "why": args.why, "learned": args.learned, "next": args.next_field}
            job = jobs.finish(db, args.job_id, status=args.status, summary=summary)
            return _emit(args, {"job": job.model_dump()}, _human_job(job))
        if args.job_cmd == "verdict":
            # The coordinator relaying a verdict the user typed in the Hermes chat: the CLI defaults its
            # source to 'coordinator' (the webapp posts 'web'). The service validates verdict/source (9).
            job = jobs.verdict(
                db,
                args.job_id,
                verdict=args.verdict,
                notes=args.notes,
                step=args.step,
                source=args.source,
            )
            return _emit(args, {"job": job.model_dump()}, _human_job(job))
        if args.job_cmd == "cancel":
            job = jobs.cancel(db, args.job_id, reason=args.reason)
            return _emit(args, {"job": job.model_dump()}, _human_job(job))
        if args.job_cmd == "restatus":
            job = jobs.restatus(db, args.job_id, status=args.status, reason=args.reason)
            return _emit(args, {"job": job.model_dump()}, _human_job(job))
        if args.job_cmd == "screenshots":
            shots = jobs.screenshots(db, args.job_id, step=args.step, reason=args.reason)
            human = [f"{len(shots)} screenshot(s)"] + [
                f"  #{s.seq}  {s.reason or '-'}  {s.step or 'unassigned'}  {s.name}  {s.web_url}" for s in shots
            ]
            return _emit(
                args,
                {"job_id": args.job_id, "screenshots": [s.model_dump() for s in shots]},
                human if shots else ["no screenshots"],
            )
        if args.job_cmd == "watch":
            return _watch_job(db, args.job_id)
    except NotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)
    except jobs.JobConflict as exc:
        return _fail(str(exc), EXIT_CONFLICT)
    except jobs.JobValidationError as exc:
        return _fail(str(exc), EXIT_VALIDATION)
    except (jobs.JobError, ValueError, OSError) as exc:
        return _fail(str(exc), EXIT_ERROR)
    return _fail(f"unknown job command {args.job_cmd!r}", EXIT_ERROR)


def _watch_job(db, job_id: str) -> int:
    """Tail a job's events until it reaches a terminal status (passed -> 0, otherwise 1)."""
    from s2docs import jobs

    seen = 0
    while True:
        job = jobs.get(db, job_id)
        for ev in job.events[seen:]:
            print(f"{ev.created_at}  {ev.level:10}  {ev.phase or '-':12}  {ev.message}", flush=True)
        seen = len(job.events)
        if job.status in jobs.TERMINAL_STATUSES:
            print(f"# job {job.id} -> {job.status}")
            return EXIT_OK if job.status == "passed" else EXIT_ERROR
        time.sleep(1.0)


def _cmd_live(args: argparse.Namespace) -> int:
    """`s2docs live ...`: probe a host's CS2 over netcon, record where netcon listens, run an envelope."""
    from pydantic import ValidationError

    from s2docs.db.repository import NotFound
    from s2docs.live import capabilities, service
    from s2docs.live.intent import IntentEnvelope

    db = Database(get_settings().db_path)
    try:
        if args.live_cmd == "status":
            st = service.status(db, args.host)
            human = [f"{st.host_id}  {st.netcon}  reachable={st.reachable}  sandbox={st.sandbox}  map={st.map or '-'}"]
            if st.error:
                human.append(f"  error: {st.error}")
            return _emit(args, {"status": st.model_dump()}, human)
        if args.live_cmd == "set-netcon":
            addr = service.set_netcon(db, args.host, args.addr)
            return _emit(args, {"host_id": args.host, "netcon_addr": addr}, [f"{args.host} -> {addr}"])
        if args.live_cmd == "act":
            env = IntentEnvelope.model_validate_json(args.envelope)
            act = service.act(db, args.host, env, source="cli", text=args.text)
            return _emit(args, {"action": act.model_dump()},
                         [f"{act.id}  {act.status}  {act.detail or ''}"] + [f"  > {line}" for line in act.lines])
        if args.live_cmd == "actions":
            rows = service.list_actions(db, args.host, limit=args.limit)
            human = [f"{a.id}  {a.created_at}  {a.host_id}  {a.source:6}  {a.status:8}  {a.detail or ''}"
                     for a in rows]
            return _emit(args, {"actions": [a.model_dump() for a in rows]}, human or ["no live actions"])
        if args.live_cmd == "caps":
            report = service.capability_report(db, args.host)
            human = [f"cs2_build: {report['cs2_build'] or 'unknown'}"]
            if report.get("error"):
                human.append(f"  error: {report['error']}")
            human += [f"  {name:20} {row['status']:7}  {row['what']}" for name, row in report["capabilities"].items()]
            return _emit(args, report, human)
        if args.live_cmd == "cap-record":
            capabilities.record(db, args.build, args.name, args.status, args.evidence)
            return _emit(args, {"cs2_build": args.build, "name": args.name, "status": args.status},
                         [f"{args.build}  {args.name} -> {args.status}"])
    except service.LiveError as exc:
        return _fail(str(exc), EXIT_CONFLICT)  # 4: the host cannot take an action now (no netcon, leased)
    except ValidationError as exc:
        return _fail(f"invalid envelope: {exc}", EXIT_VALIDATION)  # 9
    except NotFound as exc:
        return _fail(str(exc), EXIT_NOT_FOUND)  # 3
    return _fail(f"unknown live command {args.live_cmd!r}", EXIT_ERROR)


def build_parser() -> argparse.ArgumentParser:
    """Build the whole command parser.

    Kept separate from `main()` so the command tree is introspectable: `s2docs.surfaces` walks the
    returned parser to prove that a declared CLI handle really is a command — and that a capability
    it calls API/MCP-only has not quietly grown a CLI door.
    """
    parser = argparse.ArgumentParser(prog="s2docs", description="Source 2 docs index, API and MCP server")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable output: one JSON object on stdout (schema s2docs/v1); logs stay on stderr",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="Fetch GameTracking data and load it into SQLite")
    p.add_argument("--game", choices=list(GAMES))
    p.add_argument("--no-fetch", action="store_true", help="Reuse the existing checkout under data/sources")
    p.add_argument("--keep", type=int, default=5, help="Builds to keep per game (for diffs)")
    p.add_argument("--skip-docs", action="store_true", help="Don't refresh community docs (source2.wiki)")
    p.add_argument("--docs-only", action="store_true", help="Only refresh community docs")
    p.set_defaults(fn=_cmd_sync)

    p = sub.add_parser("load", help="Load a local GameTracking checkout")
    p.add_argument("game", choices=list(GAMES))
    p.add_argument("path")
    p.add_argument("--keep", type=int, default=5)
    p.set_defaults(fn=_cmd_load)

    p = sub.add_parser("serve", help="Run the web UI + JSON API + MCP (/mcp)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=_cmd_serve)

    p = sub.add_parser("mcp", help="Run the MCP server standalone")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    p.set_defaults(fn=_cmd_mcp)

    sub.add_parser("stats", help="Show indexed counts").set_defaults(fn=_cmd_stats)

    p = sub.add_parser("surfaces", help="Which named operations exist, and where (CLI/API/MCP) each is exposed")
    p.add_argument("--gaps", action="store_true",
                   help="Only disagreements between the declared table and the code; exits 1 when any")
    # SUPPRESS so `s2docs --json surfaces` (the global flag) is not reset to False by this default.
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Machine-readable output")
    p.set_defaults(fn=_cmd_surfaces)

    p = sub.add_parser("validate-script", help="Type-check a cs_script .js against point_script.d.ts")
    p.add_argument("file")
    p.add_argument("--game", default="cs2", choices=list(GAMES))
    p.set_defaults(fn=_cmd_validate_script)

    p = sub.add_parser("validate-cfg", help="Check a .cfg against indexed convars/commands")
    p.add_argument("file")
    p.add_argument("--game", default="cs2", choices=list(GAMES))
    p.add_argument("--context", default="server", choices=["server", "client", "any"])
    p.set_defaults(fn=_cmd_validate_cfg)

    p = sub.add_parser("scaffold", help="Create a new addon in the local workspace")
    p.add_argument("name")
    p.add_argument("--game", default="cs2", choices=["cs2"])
    p.set_defaults(fn=_cmd_scaffold)

    p = sub.add_parser("asset", help="Author Source 2 asset documents (model .vmdl, material .vmat) in an addon")
    p.add_argument("kind", nargs="?", choices=["model", "material"], default="model",
                   help="What to write (default: model)")
    p.add_argument("--addon", required=True, help="Addon name in the local workspace")
    p.add_argument("--name", required=True, help="Model/material name (addon-safe: [a-z][a-z0-9_]+)")
    p.add_argument("--mesh", help="model: addon-relative mesh path, e.g. models/probe/probe.dmx")
    p.add_argument("--translate", nargs=3, type=float, metavar=("X", "Y", "Z"),
                   help="model: optional ModelModifier_Translate for the whole model")
    p.add_argument("--collision-surface", metavar="SURFACEPROP",
                   help="model: a surfaceproperties name (rock, metal, metal_barrel) — adds the "
                        "PhysicsHullFile node with that surface_prop; omit for no physics node")
    p.add_argument("--collision-mesh", metavar="PATH",
                   help="model: addon-relative mesh holding the collision (default: the render mesh). "
                        "The hull is selected inside it by the name <name>_collision; requires "
                        "--collision-surface")
    p.add_argument("--color", help="material: the baked colour .png to copy beside the .vmat "
                                   "(its _normal.png/_ao.png siblings are wired automatically)")
    p.add_argument("--shader", help="material: an evidenced shader (default csgo_vertexlitgeneric.vfx)")
    p.add_argument("--force", action="store_true", help="Replace an existing .vmdl / .vmat + texture")
    p.add_argument("--game", default="cs2", choices=["cs2"])
    p.set_defaults(fn=_cmd_asset)

    p = sub.add_parser(
        "pipeline",
        help="Run the asset chain as one job (prompt->concept->mesh->asset->build->deploy->ingame)",
    )
    pc = p.add_subparsers(dest="pipeline_cmd", required=True)
    q = pc.add_parser("run", help="Run the chain for one mesh and close the job out")
    q.add_argument("--prompt", required=True, help="What was asked for; recorded on the job")
    q.add_argument("--addon", required=True, help="Addon name in the local workspace")
    q.add_argument("--name", required=True, help="Asset name (addon-safe: [a-z][a-z0-9_]+)")
    q.add_argument("--mesh", required=True, help="Addon-relative mesh path, e.g. models/probe/probe.fbx")
    q.add_argument("--host", required=True, help="Build host id (see `s2docs host list`)")
    q.add_argument("--color", help="baked colour .png (its _normal/_ao siblings wire automatically)")
    q.add_argument("--build-command", help="host-side compile line; omit to record the build as not attempted")
    q.add_argument("--build-timeout", type=int, default=300, help="seconds to wait for the build")
    q.add_argument("--project", help="ledger project id to attach the chain to")
    q.set_defaults(fn=_cmd_pipeline)

    s = pc.add_parser("script", help="Run the text->cs_script chain for one script")
    s.add_argument("--prompt", required=True, help="What the script should do; recorded on the job")
    s.add_argument("--addon", required=True, help="Addon name in the local workspace")
    s.add_argument("--name", required=True, help="Script name (addon-safe: [a-z][a-z0-9_]+)")
    s.add_argument("--host", required=True, help="Build host id (see `s2docs host list`)")
    s.add_argument("--script", help="an existing .js to use instead of the scaffold skeleton")
    s.add_argument("--project", help="ledger project id to attach the chain to")
    s.set_defaults(fn=_cmd_pipeline_script)

    p = sub.add_parser("prompt", help="Render a prompt template, e.g. prompt cs2_script task='...'")
    p.add_argument("name")
    p.add_argument("arg", nargs="*", help="key=value")
    p.set_defaults(fn=_cmd_prompt)

    p = sub.add_parser("review", help="Scope a branch diff into per-area reviewer briefs (read-only git client)")
    p.add_argument("branch", help="Branch to review, e.g. ws/my-feature")
    p.add_argument("--repo", help="Repository path (default: cwd)")
    p.add_argument("--base", default="main", help="Base branch to diff against (default: main)")
    p.add_argument(
        "--area",
        action="append",
        metavar="NAME:GLOB",
        help="Assign files matching GLOB to area NAME (repeatable; last match wins)",
    )
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON instead of markdown")
    p.set_defaults(fn=_cmd_review)

    p = sub.add_parser("host", help="Test/build host registry and locks (JSON output)")
    hc = p.add_subparsers(dest="host_cmd", required=True)
    hc.add_parser("list", help="List hosts with lock state")
    q = hc.add_parser("show", help="Show one host")
    q.add_argument("host_id")
    q = hc.add_parser("register", help="Register/update a host")
    q.add_argument("host_id")
    q.add_argument("ssh_target", help="ssh alias or [user@]host[:port]; never starts with '-'")
    q.add_argument("--hostname")
    q.add_argument("--ssh-key")
    q.add_argument("--shell", choices=["cmd", "powershell"])
    q.add_argument("--os")
    q.add_argument("--notes")
    q = hc.add_parser("probe", help="SSH-probe a host (GPU, RAM, processes, CS2) and store the facts")
    q.add_argument("host_id")
    for name, helptext in (("lock", "Claim a host"), ("heartbeat", "Refresh your lock"), ("unlock", "Release a lock")):
        q = hc.add_parser(name, help=helptext)
        q.add_argument("host_id")
        q.add_argument("owner", help="Workstream/session id, e.g. ws/occluder-wire")
        if name == "lock":
            q.add_argument("--ttl", type=int, default=1800)
        if name in ("lock", "heartbeat"):
            q.add_argument("--phase", help="What you are doing, e.g. 'compile' (drives stuck detection)")
            q.add_argument("--note", help="Free text for the dashboard")
        if name != "heartbeat":
            q.add_argument("--force", action="store_true")
    p.set_defaults(fn=_cmd_host)

    p = sub.add_parser("build", help="Server-owned build runs on a host (JSON output, --json contract)")
    bc = p.add_subparsers(dest="build_cmd", required=True)
    q = bc.add_parser("start", help="Start a run: acquires the host lease and runs the command over SSH")
    q.add_argument("target", help="Addon/target label recorded on the run")
    q.add_argument("--host", required=True, help="Host id (see `host list`)")
    q.add_argument("--command", required=True, help="Shell command to run on the host over SSH")
    q.add_argument("--kind", default="build", help="Free label, e.g. 'resourcecompiler'")
    q.add_argument("--log", help="Server-local path for the captured log")
    q.add_argument("--map", help="Map name (recorded in the run notes)")
    q.add_argument("--wait", action="store_true", help="Block until the run finishes")
    q.add_argument("--timeout", type=_parse_duration, default=3600, help="Run/lease timeout, e.g. 1h (default)")
    q.add_argument(
        "--strict-materials",
        action="store_true",
        help="Exit 8 when the material guard flags missing/reserved materials (a 'succeeded' run is not clean)",
    )
    q = bc.add_parser("status", help="Show one run's record")
    q.add_argument("run_id")
    q.add_argument("--strict-materials", action="store_true", help="Exit 8 when the material guard flagged warnings")
    q = bc.add_parser("log", help="Tail a run's log (decodes UTF-16)")
    q.add_argument("run_id")
    q.add_argument("--lines", type=int, default=200)
    q.add_argument("--follow", action="store_true", help="Stream new lines until the run finishes")
    q = bc.add_parser("wait", help="Block until a run finishes (server-side; no sleep loop)")
    q.add_argument("run_id")
    q.add_argument("--timeout", type=_parse_duration, default=3600)
    q.add_argument("--strict-materials", action="store_true", help="Exit 8 when the material guard flagged warnings")
    q = bc.add_parser("cancel", help="Cancel a run and release its host lease")
    q.add_argument("run_id")
    q = bc.add_parser("list", help="List runs (newest first)")
    q.add_argument("--host")
    q.add_argument("--active", action="store_true", help="Only queued/running runs")
    p.set_defaults(fn=_cmd_build)

    p = sub.add_parser("deploy", help="Deployment registry: push to a host and verify MD5s")
    dc = p.add_subparsers(dest="deploy_cmd", required=True)
    q = dc.add_parser("push", help="Copy files to a host per a manifest, then verify MD5s server-side")
    q.add_argument("addon", nargs="?", help="Addon in the local workspace (or use --manifest)")
    q.add_argument("--host", required=True)
    q.add_argument("--manifest", help="Manifest JSON {dest_root, files:[{path, local}]}")
    q.add_argument("--dest", help="Remote destination root")
    q.add_argument("--game", default="cs2", choices=list(GAMES))
    q.add_argument("--parallel", type=int, default=1, help="Concurrent transfers")
    q.add_argument("--dry-run", action="store_true", help="Resolve entries without copying")
    q.add_argument("--no-verify", action="store_true", help="Skip the post-copy MD5 check")
    q.add_argument(
        "--owner",
        help="Your host-lock owner if you already hold the lease (a free host is claimed as deploy:<id>)",
    )
    q = dc.add_parser("verify", help="Re-check a deployment's MD5s on the host")
    q.add_argument("deploy_id", nargs="?", help="Deploy id (or use --host for the latest)")
    q.add_argument("--host")
    q = dc.add_parser("list", help="List deployments (newest first)")
    q.add_argument("--host")
    p.set_defaults(fn=_cmd_deploy)

    p = sub.add_parser("evidence", help="Evidence store: what the orchestration `evidence` gate checks")
    ev = p.add_subparsers(dest="evidence_cmd", required=True)
    q = ev.add_parser("add", help="Record one evidence row for a project step")
    q.add_argument("--project", required=True, help="Project id")
    q.add_argument("--step", help="Step id the evidence proves")
    q.add_argument(
        "--kind",
        choices=list(_EVIDENCE_KINDS),
        help="Evidence kind (notes alone don't satisfy the gate)",
    )
    q.add_argument("--ref", help="Path / url / command the evidence points at")
    q.add_argument("--data", help="Structured detail as a JSON object, e.g. '{\"pixel_diff_pct\": 1.2}'")
    q.add_argument("--note", help="Shortcut: record a note (kind=note) with this text")
    q = ev.add_parser("list", help="List evidence rows (newest first)")
    q.add_argument("--project")
    q.add_argument("--step")
    q.add_argument("--kind", choices=list(_EVIDENCE_KINDS))
    q = ev.add_parser("show", help="Show one evidence row")
    q.add_argument("evidence_id")
    p.set_defaults(fn=_cmd_evidence)

    p = sub.add_parser("learning", help="Learning loop: harvest job outcomes into eval/training signal")
    lc = p.add_subparsers(dest="learning_cmd", required=True)
    q = lc.add_parser("report", help="Readiness: counts by origin, artifacts on disk")
    q.add_argument("--since-days", type=int, default=None)
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    q = lc.add_parser("harvest", help="Write the four artifacts from the job ledger")
    q.add_argument("--since-days", type=int, default=None)
    q.add_argument("--origin", default="human", choices=[*_JOB_ORIGINS, "all"])
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    q = lc.add_parser("digest", help="Classify outcomes into signals, a report and lesson proposals")
    q.add_argument("--since-days", type=int, default=30)
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    q = lc.add_parser("gate", help="Score the golden-question eval (exit 6 when it cannot evaluate)")
    q.add_argument("--self-test", action="store_true",
                   help="Falsify the gate itself: a planted impossible expectation must fail it")
    q.add_argument("-v", "--verbose", action="store_true", dest="learning_verbose",
                   help="Print per-question pass/fail detail")
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    q = lc.add_parser("sft", help="Build the SFT dataset + train config (trains nothing)")
    q.add_argument("--require-baseline", action="store_true",
                   help="Refuse to prepare without a recorded eval baseline (exit 9)")
    q.add_argument("--since-days", type=int, default=None)
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    q = lc.add_parser("export", help="Write the lab4/lab5 export artifacts (corpus, problems, cases)")
    q.add_argument("--since-days", type=int, default=None)
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    q = lc.add_parser("exemplars", help="One exemplar doc per approved confirmation (skips reported)")
    q.add_argument("--since-days", type=int, default=None)
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    q = lc.add_parser("review", help="The human correction queue: job id, failure mode, question, promoted")
    q.add_argument("--since-days", type=int, default=None)
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    q = lc.add_parser("promote", help="File a human's known answer as a frozen golden case")
    q.add_argument("--job", required=True, help="Job id of the correction being answered")
    q.add_argument("--expect", required=True, help="The expected key/keyword the run should have produced")
    q.add_argument("--query", help="What the gate will search with (default: the derived subject of the "
                                   "run's goal — not the whole brief, which nothing can match)")
    q.add_argument("--checker", default="exact", help="Checker the labs use for this case (default: exact)")
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    q = lc.add_parser("rewrite", help="Deterministically rewrite prose into search keywords (+ game)")
    q.add_argument("text", help="A user's question, e.g. 'how do I make a breakable prop'")
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    q = lc.add_parser("rewrite-eval",
                      help="Measure the rewriter over the frozen eval set (exit 6 when it cannot evaluate)")
    q.add_argument("--json", action="store_true", help="Machine-readable output")
    q.add_argument("--rewriter", metavar="MODULE:FUNCTION",
                   help="Score a supplied rewriter -- any callable f(text)->Rewrite, e.g. a trained "
                        "model (dotted import path, 'pkg.module:function') -- as a FOURTH 'model' "
                        "condition alongside baseline/rewrite/oracle. ACCEPTANCE RULE: with "
                        "--rewriter the exit code is decided on the model's score, and the floor is "
                        "the deterministic rewriter's recall@5 (0.40) -- a model must BEAT the "
                        "deterministic rewriter to be adopted; equal is not beating it. Without "
                        "--rewriter the gate is decided on the deterministic condition, exactly as "
                        "before. A path that cannot be imported, or a callable that raises, exits 6 "
                        "(could not evaluate) -- never a pass, never a traceback.")
    p.set_defaults(fn=_cmd_learning)

    p = sub.add_parser("test", help="In-game test-run records (mirror of the TEST STATUS HUD strip)")
    tc = p.add_subparsers(dest="test_cmd", required=True)
    q = tc.add_parser("start", help="Start a test session (claims the host lease if --host is given)")
    q.add_argument("--name", required=True, help="Label shown on the HUD, e.g. 'iso_test'")
    q.add_argument("--host", help="Host to claim for the session")
    q.add_argument("--project", help="Project this session belongs to")
    q = tc.add_parser("step", help="Set the running session's current step")
    q.add_argument("run_id")
    q.add_argument("text", help="Step label, e.g. 'wall-occlusion probe'")
    q = tc.add_parser("finish", help="Finish a running session pass/fail (releases the host lease)")
    q.add_argument("run_id")
    q.add_argument("--result", choices=["pass", "fail"], default="pass")
    q.add_argument("--note")
    q = tc.add_parser("clear", help="Dismiss a session (`iso_test off`)")
    q.add_argument("run_id")
    q = tc.add_parser("status", help="Show a run, or the current active run")
    q.add_argument("run_id", nargs="?")
    q = tc.add_parser("list", help="List test runs (newest first)")
    q.add_argument("--state", choices=list(_TEST_STATES))
    p.set_defaults(fn=_cmd_test)

    p = sub.add_parser("job", help="Job ledger + queue: one record per requested run")
    jc = p.add_subparsers(dest="job_cmd", required=True)
    q = jc.add_parser("enqueue", help="Enqueue a job (intake is validated)")
    q.add_argument("--kind", required=True, choices=["test", "demo", "final"])
    q.add_argument("--title", required=True)
    q.add_argument("--target", required=True, help="What is under test: feature/test + branch@sha")
    q.add_argument("--purpose", required=True, help="One sentence: why this run exists")
    q.add_argument("--expected", required=True, help="Pass criterion (observable)")
    q.add_argument("--host", help="Host to queue on (or use --ask-host)")
    q.add_argument("--ask-host", action="store_true", help="Leave unassigned until assign")
    q.add_argument("--max-minutes", type=int, help="Cap; test/demo default 5, larger needs --reason")
    q.add_argument("--reason", help="Why the run may exceed the 5-minute default")
    q.add_argument("--confirm", action="store_true", help="The human iso_confirm gate decides pass/fail")
    q.add_argument("--screenshots", default="each_step", choices=["each_step", "on_request", "none"])
    q.add_argument("--watching", action="store_true", help="The user is watching this run live")
    q.add_argument(
        "--origin",
        choices=_JOB_ORIGINS,
        default=None,
        help=(
            "Who asked for this run (default: agent). The learning loop reads the ledger by origin: "
            "tag tooling/driver/eval/probe runs so they cannot be mistaken for a person's."
        ),
    )
    q.add_argument("--branch")
    q.add_argument("--commit")
    q.add_argument("--requested-by", help="Coordinator session id")
    q = jc.add_parser("list", help="List jobs (newest first)")
    q.add_argument("--active", action="store_true", help="Only queued + running")
    q.add_argument("--host")
    q = jc.add_parser("show", help="Show one job with its timeline, links and artifacts")
    q.add_argument("job_id")
    q = jc.add_parser("next", help="Head queued job for a host, when that host's lease is free")
    q.add_argument("--host", required=True)
    q = jc.add_parser("start", help="queued -> running")
    q.add_argument("job_id")
    q.add_argument("--worker", required=True, help="Subagent id / branch running it")
    q = jc.add_parser("event", help="Append a timeline event")
    q.add_argument("job_id")
    q.add_argument(
        "--level", required=True, choices=["info", "warn", "error", "crash", "screenshot", "milestone", "learning"]
    )
    q.add_argument("--message", required=True)
    q.add_argument("--phase", help="Sets the job's current phase")
    q.add_argument("--data", help="JSON detail, e.g. '{\"exit_code\": 1}'")
    q.add_argument("--log-file", help="Attach the tail (200 lines) of this file as data.log_tail")
    q = jc.add_parser("attach", help="Copy a local file into the job's artifacts (append-only)")
    q.add_argument("job_id")
    q.add_argument("file")
    q.add_argument("--name", help="Stored name (default: the file's basename)")
    q = jc.add_parser("shot-request", help="Ask the runner for a screenshot (sets params.pending_shot)")
    q.add_argument("job_id")
    q.add_argument("--note", help="What to capture")
    q = jc.add_parser("finish", help="Close out a job (all four summary fields required)")
    q.add_argument("job_id")
    q.add_argument("--status", required=True, choices=list(_JOB_FINISH_STATUSES))
    q.add_argument("--what", required=True)
    q.add_argument("--why", required=True)
    q.add_argument("--learned", required=True)
    q.add_argument("--next", dest="next_field", required=True, help="What is next")
    q = jc.add_parser("verdict", help="Answer the running job's step: pass | fail | skip")
    q.add_argument("job_id")
    q.add_argument("--verdict", required=True, help="pass | fail | skip (validated in the service -> exit 9)")
    q.add_argument("--notes", help="Free-text note recorded with the verdict")
    q.add_argument("--step", help="Step id/phase the verdict answers (optional)")
    q.add_argument("--source", default="coordinator", help="chat | console | key | web | coordinator")
    q = jc.add_parser("cancel", help="queued/running -> cancelled")
    q.add_argument("job_id")
    q.add_argument("--reason")
    q = jc.add_parser("restatus", help="Correct a FINISHED job's status (terminal -> terminal; reason required)")
    q.add_argument("job_id")
    q.add_argument(
        "--status",
        required=True,
        help=" | ".join(_JOB_TERMINAL_STATUSES) + " (validated in the service -> exit 9)",
    )
    q.add_argument("--reason", required=True, help="Why the ledger is being corrected (recorded on the timeline)")
    q = jc.add_parser("screenshots", help="List a job's screenshots tied to their step and verdict")
    q.add_argument("job_id")
    q.add_argument("--step", help="Filter by step id ('s4'); 'unassigned' = untagged shots")
    q.add_argument("--reason", help="ready | periodic | verdict | request | verify")
    q = jc.add_parser("watch", help="Tail a job's events until it ends")
    q.add_argument("job_id")
    p.set_defaults(fn=_cmd_job)

    p = sub.add_parser("live", help="Live actions on a running CS2: probe netcon, run a validated request")
    lc = p.add_subparsers(dest="live_cmd", required=True)
    q = lc.add_parser("status", help="Probe the host's CS2 over netcon (map, spawngroups, sandbox)")
    q.add_argument("--host", required=True)
    q = lc.add_parser("set-netcon", help="Record where the host's CS2 -netconport listens")
    q.add_argument("--host", required=True)
    q.add_argument("--addr", required=True, help="IP-or-name:port, e.g. 10.88.20.235:2121")
    q = lc.add_parser("act", help="Run an IntentEnvelope on the host and judge it by readback")
    q.add_argument("--host", required=True)
    q.add_argument("--envelope", required=True, help='JSON: {"intents": [{"action": "bots", "team_size": 5}]}')
    q.add_argument("--text", help="The user's own words, verbatim")
    q = lc.add_parser("actions", help="Recent live actions (newest first)")
    q.add_argument("--host")
    q.add_argument("--limit", type=int, default=20)
    q = lc.add_parser("caps", help="Which experimental capabilities were measured on the host's CS2 build")
    q.add_argument("--host", required=True)
    q = lc.add_parser("cap-record", help="Record one spike result for one CS2 build")
    q.add_argument("--build", required=True, help="The build key netcon `status` prints, e.g. 1.41.9.2/14192")
    q.add_argument("--name", required=True, help="Capability name (see `live caps`)")
    q.add_argument("--status", required=True, choices=["pass", "fail"])
    q.add_argument("--evidence", required=True, help="The readback/answer/artifact that proves it")
    p.set_defaults(fn=_cmd_live)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # stdout is the MCP stdio channel: logs must go to stderr.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
