"""The asset chain as one durable job record: prompt -> concept -> mesh -> asset -> build -> deploy -> ingame.

This is the vertical slice of the ``pipeline`` ledger step, and it is deliberately explicit about
which stages are real:

* ``asset``, ``build`` and ``deploy`` call the same code the CLI calls (the ``.vmdl``/``.vmat``
  writers, the server-owned build runs, and ``deploys.push`` + ``deploys.verify``), so the chain is
  proven exactly where it runs today.
* ``concept`` and ``mesh`` are recorded as **stubbed**. No text->3D generator is wired into this
  repo (``docs/HAMMERED.md``: the chain starts from an existing mesh), and a stage that silently
  "passes" without producing anything is worse than one that says so.
* ``ingame`` is recorded as **blocked**, with the measured reason (``ent_create`` never renders a
  loose model in this client; the map-compile route needs a host with an active display adapter).

``deploy`` only runs when the caller supplies a destination *and* the build succeeded; otherwise it
is recorded **skipped** with the reason (no destination, or nothing new compiled). Skipped is not
blocked and not a pass: the ledger says which precondition was missing instead of implying the
capability does not exist.

Every stage writes exactly one job event whose ``phase`` is the stage id, in stage order, so
``Job.current_step``/``jobs.get(...).phase`` and the timeline show the same chain. A produced
artifact is linked (``kind="build"``/``kind="deploy"``) rather than pasted into a message, so the job
alone is enough to reconstruct what happened.

The same contract holds for the cs_script chain (``run_script_slice``): ``generate`` and ``validate``
always run, ``build`` compiles the ``.js`` on a host and pulls the ``.vjs_c`` back
(:mod:`s2docs.build.script_compile`) when ``compile=True``, ``deploy`` copies it loose to a named host,
and only ``verdict`` is still blocked (running a cs_script needs a map whose point_script references it).
"""
from __future__ import annotations

from pathlib import Path

from s2docs import builds, deploys, hosts, jobs
from s2docs import models as m
from s2docs.config import get_settings

#: The chain, in order. This tuple is the contract: a job's timeline must carry one event per entry.
STAGES: tuple[str, ...] = ("prompt", "concept", "mesh", "asset", "build", "deploy", "ingame")

#: Stages this slice can execute. ``deploy`` executes only when a destination is supplied and the
#: build succeeded; otherwise it is recorded skipped with the reason (see ``NO_DEST_REASON``).
REAL_STAGES: tuple[str, ...] = ("asset", "build", "deploy")

STUB_REASON = (
    "no generator is wired for this stage yet: the chain starts from an existing mesh "
    "(docs/HAMMERED.md), so the stage is recorded as intent instead of pretending to produce output"
)

#: Why ``deploy`` is skipped when the caller gave no destination — a missing argument, not a block.
NO_DEST_REASON = (
    "no destination was given (--dest <remote root>), so nothing was copied to a host; the stage is "
    "skipped rather than blocked, because the deploy path itself works"
)

BLOCKED_REASONS: dict[str, str] = {
    "ingame": (
        "in-game proof is blocked: ent_create never renders a loose model in this client, and the "
        "map-compile route needs a host with an active display adapter (docs/HAMMERED.md 2026-10-24)"
    ),
}

SUMMARY_MAX = 600


class PipelineError(RuntimeError):
    """The chain could not run (as opposed to a stage that ran and failed)."""


def _stage(db, job_id: str, stage: str, *, message: str, level: str = "info", data: dict | None = None):
    """One event per stage, always carrying ``phase=stage`` so the timeline is the chain."""
    return jobs.event(db, job_id, level=level, message=message, phase=stage, data=data or {})


def _finish_build(db, run: m.BuildRun, *, timeout_sec: int, poll_sec: float) -> m.BuildRun:
    """Wait on the run through the server's own waiter (no sleep loop here); a timeout is the run's state."""
    try:
        return builds.wait(db, run.id, timeout_sec=timeout_sec, poll_sec=poll_sec)
    except builds.BuildTimeout:
        return builds.get_run(db, run.id)


def _deploy_files(workspace: Path, game: str, addon: str) -> list[m.DeployFile]:
    """The addon's files as deploy specs — the manifest ``s2docs deploy push <addon>`` builds.

    Built through ``workspace.describe`` rather than a second directory walk so the chain ships the
    same set of paths a manual push would: one definition of "the addon's content", not two.
    """
    from s2docs.build.workspace import describe

    info = describe(workspace, game, addon)
    root = Path(info.root)
    return [m.DeployFile(path=f.path, local=str(root / f.path)) for f in info.files]


def _deploy_addon(
    db, *, host_id: str, dest_root: str, addon: str, files: list[m.DeployFile]
) -> tuple[m.Deployment | None, str | None]:
    """Push the addon's content to ``dest_root`` and verify the readback: ``(deployment, failure)``.

    This is the deploy service the CLI wraps (``deploy push`` is ``deploys.push``, ``deploy verify``
    is ``deploys.verify``), reached through its public functions so the push takes the host lease and
    records the {path, size, md5} manifest exactly as a manual push does.

    ``push`` runs with ``verify=False`` and the readback is a separate ``deploys.verify`` call on
    purpose: ``push(verify=True)`` raises *after* storing a failed record, so the deployment id —
    the thing the job has to link — would only survive inside the exception text. Splitting the two
    keeps one id for every outcome at the same cost of a single ``Get-FileHash`` pass.
    """
    try:
        deployment = deploys.push(db, host_id, dest_root, files, addon=addon, verify=False)
    except (deploys.DeployError, hosts.HostError) as exc:  # bad target, lock conflict, all copies failed
        return None, str(exc)
    if deployment.status == "failed":  # partial copy: the record carries the per-file errors
        return deployment, "some files did not copy; see the deployment record for the per-file errors"
    try:
        deployment = deploys.verify(db, deployment.id)
    except deploys.DeployError as exc:  # readback could not run: no verdict, but the id still stands
        return deployment, f"MD5 readback could not run: {exc}"
    return deployment, None


def _summary(*, what: str, why: str, learned: str, next_: str) -> dict[str, str]:
    fields = {"what": what, "why": why, "learned": learned, "next": next_}
    return {k: v[:SUMMARY_MAX] for k, v in fields.items()}


def run_asset_build_slice(
    db,
    *,
    prompt: str,
    addon: str,
    name: str,
    mesh: str,
    host_id: str,
    material_color: str | Path | None = None,
    game: str = "cs2",
    kind: str = "test",
    title: str | None = None,
    project_id: str | None = None,
    origin: str = "agent",
    build_command: str | None = None,
    build_timeout_sec: int = 300,
    poll_sec: float = 2.0,
    expected: str | None = None,
    dest: str | None = None,
) -> m.Job:
    """Run the chain for one mesh and return the closed-out job.

    ``build_command`` is the host-side compile line; without it the build stage is recorded as
    *not attempted* rather than faked. ``dest`` is the remote root the compiled addon is copied to
    (a Windows/share path, ``D:/...`` — whatever the host's scp accepts): with it, and a successful
    build, the deploy stage pushes and MD5-verifies the addon content; without it the stage is
    recorded *skipped* and says no destination was given. The returned job is terminal: ``passed``
    never happens here, because ``ingame`` is blocked — ``inconclusive`` is the honest close-out, and
    ``failed`` when the build or the deploy actually failed.
    """
    from s2docs.asset.material import write_vmat
    from s2docs.asset.model import render_model_doc, write_model_doc
    from s2docs.build.workspace import addon_root

    job = jobs.enqueue(
        db,
        kind=kind,
        title=title or f"{addon}: {name} asset chain",
        target=addon,
        purpose=prompt,
        expected=expected or f"a compiled {name} asset, plus a job timeline that records every stage",
        host_id=host_id,
        project_id=project_id,
        origin=origin,
    )
    jobs.start(db, job.id, worker="pipeline-slice")

    _stage(db, job.id, "prompt", level="milestone", message="prompt recorded", data={"prompt": prompt})
    for stage in ("concept", "mesh"):
        _stage(db, job.id, stage, message=f"{stage} stubbed", data={"stubbed": True, "reason": STUB_REASON})

    # ---- asset: real -------------------------------------------------------
    workspace = get_settings().workspace_dir
    root = addon_root(workspace, game, addon)
    if not root.is_dir():
        _stage(
            db, job.id, "asset", level="error",
            message=f"addon {addon!r} is not in the workspace",
            data={"workspace": str(workspace), "hint": f"run `s2docs scaffold {addon}` first"},
        )
        return jobs.finish(db, job.id, status="error", summary=_summary(
            what=(
                f"Chain for {name} in addon {addon} stopped at the asset stage: "
                "the addon is not in the workspace."
            ),
            why="The asset stage writes into the addon's content tree; without the addon there is nothing to write.",
            learned=(
                f"workspace={workspace} has no addon {addon!r}; "
                "`s2docs scaffold` is the gate before any asset write."
            ),
            next_=f"Run `s2docs scaffold {addon}`, then re-run the chain.",
        ))

    doc = render_model_doc(name, mesh)
    vmdl = write_model_doc(root / "models" / name / f"{name}.vmdl", doc, overwrite=True)
    produced = {"vmdl": vmdl.relative_to(root).as_posix()}
    if material_color is not None:
        vmat = write_vmat(root, name, Path(material_color), force=True)
        produced["vmat"] = vmat.relative_to(root).as_posix()
    _stage(db, job.id, "asset", level="milestone", message="asset documents written", data=produced)

    # ---- build: real when a command is supplied ----------------------------
    build_status: str | None = None
    build_id: str | None = None
    if build_command:
        try:
            run = builds.start(db, host_id, build_command, kind="compile", timeout_sec=build_timeout_sec)
        except Exception as exc:  # host lock, ssh, validation — recorded, never swallowed
            _stage(db, job.id, "build", level="warn", message="build could not start",
                   data={"error": str(exc), "host_id": host_id})
        else:
            build_id = run.id
            jobs.link(db, job.id, kind="build", ref_id=run.id)
            run = _finish_build(db, run, timeout_sec=build_timeout_sec, poll_sec=poll_sec)
            build_status = run.status
            _stage(
                db, job.id, "build",
                level="milestone" if run.status == "succeeded" else "error",
                message=f"build {run.status}",
                data={"build": run.id, "status": run.status, "exit_code": run.exit_code,
                      "summary": run.summary, "log_path": run.log_path},
            )
    else:
        _stage(db, job.id, "build", message="build not attempted",
               data={"skipped": True, "reason": "no build_command supplied"})

    # ---- deploy: real when a destination is given and the build succeeded ---
    deploy_id: str | None = None
    deploy_status: str | None = None
    if not dest:
        _stage(db, job.id, "deploy", message="deploy skipped: no destination",
               data={"skipped": True, "reason": NO_DEST_REASON})
    elif build_status != "succeeded":
        _stage(
            db, job.id, "deploy", message="deploy skipped: no successful build",
            data={
                "skipped": True,
                "reason": (
                    f"build {'was not attempted' if build_status is None else build_status}; "
                    "nothing new was compiled, so the addon content was not copied"
                ),
            },
        )
    else:
        deployment, failure = _deploy_addon(
            db, host_id=host_id, dest_root=dest, addon=addon,
            files=_deploy_files(workspace, game, addon),
        )
        if deployment is not None:
            deploy_id, deploy_status = deployment.id, deployment.status
            # The artifact is linked, not pasted into the message — same as the build above.
            jobs.link(db, job.id, kind="deploy", ref_id=deployment.id)
        else:
            deploy_status = "failed"
        data: dict = {"host_id": host_id, "dest_root": dest, "deploy": deploy_id, "status": deploy_status}
        if deployment is not None:
            data["files"] = len(deployment.entries)
            data["verified"] = sum(1 for entry in deployment.entries if entry.verified)
        if failure:
            data["error"] = failure
        _stage(
            db, job.id, "deploy",
            level="milestone" if deploy_status == "verified" else "error",
            message=f"deploy {deploy_status}", data=data,
        )

    # ---- ingame: still blocked, recorded as such ---------------------------
    _stage(db, job.id, "ingame", message="ingame blocked",
           data={"blocked": True, "reason": BLOCKED_REASONS["ingame"]})

    # A deploy that ran and did not come back 'verified' is a failure, including the case where the
    # MD5 readback could not run: the chain must not close 'inconclusive' on bytes nobody confirmed.
    deploy_failed = deploy_status is not None and deploy_status != "verified"
    failed = build_status in ("failed", "cancelled", "interrupted") or deploy_failed
    if deploy_status is None:
        deploy_bit = "deploy skipped (no destination)" if not dest else "deploy skipped (no successful build)"
    else:
        deploy_bit = f"deploy {deploy_status}" + (f" ({deploy_id})" if deploy_id else "")
    learned_bits = [
        f"asset stage wrote {produced['vmdl']}" + (f" and {produced['vmat']}" if "vmat" in produced else ""),
        "concept/mesh have no generator (stubbed)",
        f"build {'not attempted' if build_status is None else build_status}" + (f" ({build_id})" if build_id else ""),
        deploy_bit,
        "ingame blocked",
    ]
    return jobs.finish(
        db, job.id,
        status="failed" if failed else "inconclusive",
        summary=_summary(
            what=(
                f"Ran the asset chain for {name} in addon {addon} from a prompt; "
                f"stages {', '.join(STAGES)} recorded on one job."
            ),
            why=(
                "Prove the chain where it exists (asset+build+deploy) and record stubs/blocks/skips "
                "honestly instead of a green status that hides them."
            ),
            learned="; ".join(learned_bits),
            next_=(
                "Pass --dest to copy a compiled addon to a host; unblock ingame by compiling a map "
                "that references the model on a host with an active display adapter, then wire "
                "concept/mesh generators."
            ),
        ),
    )


# --------------------------------------------------------------------------- text -> cs_script

#: The script chain, in order. Same contract as STAGES: one event per entry, ``phase`` = the id.
SCRIPT_STAGES: tuple[str, ...] = ("prompt", "generate", "validate", "build", "deploy", "verdict")

#: Stages that still cannot run, with the measured reason. ``build``/``deploy`` are real now (they are
#: skipped, not blocked, when the caller does not ask for them) — only the verdict needs something this
#: repo does not have yet.
SCRIPT_BLOCKED: dict[str, str] = {
    "verdict": (
        "running a cs_script needs a map whose point_script references it: a runtime ent_create of "
        "point_script fails with 'Failed to load cs_script' (2026-10-27), so the verdict needs a map "
        "compiled with scripts/<name>.vjs on its point_script — a later step"
    ),
}

#: ``build`` without ``compile=True`` is a missing argument, not a block.
COMPILE_NOT_REQUESTED = (
    "no compile was requested (pass compile=True / --compile): the .js is written and type-checked, "
    "but nothing was pushed to a host, so build is skipped rather than blocked"
)

#: ``deploy`` without a target host is a missing argument too.
NO_SCRIPT_DEST_REASON = (
    "no deploy host was given (--deploy-host <host id>), so the .vjs_c was not copied loose to any "
    "host; the stage is skipped rather than blocked, because the deploy path itself works"
)


def run_script_slice(
    db,
    *,
    prompt: str,
    addon: str,
    name: str,
    host_id: str,
    script_text: str | None = None,
    game: str = "cs2",
    kind: str = "test",
    title: str | None = None,
    project_id: str | None = None,
    origin: str = "agent",
    expected: str | None = None,
    compile: bool = False,
    deploy_host_id: str | None = None,
    build_timeout_sec: int = 120,
    poll_sec: float = 1.0,
) -> m.Job:
    """Run the text -> cs_script chain for one script and return the closed-out job.

    ``generate`` writes ``script_text`` when the caller supplies one (the LLM/caller is the
    generator) and otherwise the scaffold's own skeleton, so the stage never invents code it cannot
    attribute. ``validate`` type-checks through the same ``validate_cs_script`` the CLI uses; a
    missing TypeScript toolchain is *blocked*, not a pass and not a failure.

    ``compile=True`` makes ``build`` real: the ``.js`` is pushed to ``host_id`` and one
    ``resourcecompiler`` run compiles it to ``.vjs_c`` (:mod:`s2docs.build.script_compile`), which is
    pulled back MD5-checked and attached to the run. It never runs when ``validate`` failed — a script
    that does not type-check is not compiled. ``deploy_host_id`` makes ``deploy`` real: the pulled
    ``.vjs_c`` is copied LOOSE to that host's ``<CS2>/game/csgo/scripts/<name>.vjs_c``. ``verdict``
    stays blocked: it needs a map whose point_script references the script.
    """
    from s2docs.build.validate_script import ToolchainMissing, validate_cs_script
    from s2docs.build.workspace import addon_root, script_template
    from s2docs.db.repository import NotFound, Repository

    job = jobs.enqueue(
        db,
        kind=kind,
        title=title or f"{addon}: {name} cs_script chain",
        target=addon,
        purpose=prompt,
        expected=expected or f"a type-checked cs_script ({name}.js) plus the job timeline for every stage",
        host_id=host_id,
        project_id=project_id,
        origin=origin,
    )
    jobs.start(db, job.id, worker="pipeline-slice")
    _stage(db, job.id, "prompt", level="milestone", message="prompt recorded", data={"prompt": prompt})

    # ---- generate ----------------------------------------------------------
    workspace = get_settings().workspace_dir
    root = addon_root(workspace, game, addon)
    if not root.is_dir():
        _stage(
            db, job.id, "generate", level="error",
            message=f"addon {addon!r} is not in the workspace",
            data={"workspace": str(workspace), "hint": f"run `s2docs scaffold {addon}` first"},
        )
        return jobs.finish(db, job.id, status="error", summary=_summary(
            what=f"cs_script chain for {name} stopped at generate: addon {addon} is not in the workspace.",
            why="The script is written into the addon's content tree, so the addon is the gate.",
            learned=f"workspace={workspace} has no addon {addon!r}.",
            next_=f"Run `s2docs scaffold {addon}`, then re-run the chain.",
        ))

    text = script_text if script_text is not None else script_template(name)
    target = root / "scripts" / f"{name}.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _stage(
        db, job.id, "generate", level="milestone", message="script written",
        data={"path": target.relative_to(root).as_posix(), "bytes": len(text.encode("utf-8")),
              "source": "supplied" if script_text is not None else "scaffold skeleton"},
    )

    # ---- validate ----------------------------------------------------------
    validation = "blocked"
    validation_errors = 0
    try:
        with db.reader() as conn:
            result = validate_cs_script(Repository(conn), text, game=game, filename=f"{name}.js")
    except ToolchainMissing as exc:
        _stage(db, job.id, "validate", level="warn", message="validate blocked: no TypeScript toolchain",
               data={"blocked": True, "reason": str(exc)})
    except NotFound as exc:
        # No ingested cs_script API in this data dir: there is nothing to type-check against. That is a
        # blocked validation (like a missing tsc), not a crash and not a pass.
        _stage(db, job.id, "validate", level="warn", message="validate blocked: no ingested script API",
               data={"blocked": True, "reason": str(exc)})
    else:
        if result.ok:
            validation = "ok"
            _stage(db, job.id, "validate", level="milestone", message="script type-checks",
                   data={"errors": result.errors, "warnings": result.warnings, "checker": result.checker})
        else:
            validation = "failed"
            validation_errors = result.errors
            _stage(
                db, job.id, "validate", level="error", message=f"script has {result.errors} error(s)",
                data={"errors": result.errors, "warnings": result.warnings,
                      "diagnostics": [d.model_dump() for d in result.diagnostics[:5]]},
            )

    # ---- build: real when compile is requested and validate did not fail ----
    from s2docs.build import script_compile

    build_status: str | None = None
    build_id: str | None = None
    build_reason: str | None = None
    compiled = None
    if not compile:
        build_reason = COMPILE_NOT_REQUESTED
        _stage(db, job.id, "build", message="build skipped: no compile requested",
               data={"skipped": True, "reason": build_reason})
    elif validation == "failed":
        build_reason = (
            f"validate reported {validation_errors} error(s), so nothing was compiled: "
            "a script that does not type-check is not shipped as a .vjs_c"
        )
        _stage(db, job.id, "build", message="build skipped: validate failed",
               data={"skipped": True, "reason": build_reason})
    else:
        try:
            compiled = script_compile.compile_js(
                db, host_id, addon=addon, name=name, source=target,
                timeout_sec=build_timeout_sec, poll_sec=poll_sec, owner=f"job:{job.id}",
            )
        except script_compile.ScriptCompileError as exc:  # refused input, failed run, stale artifact
            build_status = "failed"
            build_reason = str(exc)
            run_id = getattr(exc, "run_id", None)
            if run_id:
                build_id = run_id
                jobs.link(db, job.id, kind="build", ref_id=run_id)
            _stage(db, job.id, "build", level="error", message="build failed", data={
                "build": build_id, "status": build_status, "host_id": host_id, "error": build_reason,
                "reason": build_reason,
            })
        else:
            build_id, build_status = compiled.run.id, compiled.run.status
            jobs.link(db, job.id, kind="build", ref_id=compiled.run.id)
            _stage(
                db, job.id, "build", level="milestone", message=f"compiled {name}.vjs_c",
                data={
                    "build": build_id, "status": build_status, "host_id": host_id,
                    "artifact": compiled.path.name, "md5": compiled.md5, "size": compiled.size,
                    "remote": compiled.remote, "exit_code": compiled.run.exit_code,
                    "log_path": compiled.run.log_path,
                },
            )

    # ---- deploy: real when a target host is given and the build produced a .vjs_c ----
    deploy_id: str | None = None
    deploy_status: str | None = None
    if build_status != "succeeded" or compiled is None:
        _stage(
            db, job.id, "deploy", message="deploy skipped: no compiled .vjs_c",
            data={
                "skipped": True,
                "reason": (
                    (build_reason or "the build did not produce a .vjs_c")
                    + "; nothing was copied to a host"
                ),
            },
        )
    elif not deploy_host_id:
        _stage(db, job.id, "deploy", message="deploy skipped: no deploy host",
               data={"skipped": True, "reason": NO_SCRIPT_DEST_REASON})
    else:
        try:
            deployment = script_compile.deploy_loose(
                db, deploy_host_id, addon=addon, name=name, compiled=compiled.path,
                owner=f"job:{job.id}",
            )
        except script_compile.ScriptCompileError as exc:
            deploy_status = "failed"
            _stage(db, job.id, "deploy", level="error", message="deploy failed",
                   data={"host_id": deploy_host_id, "status": deploy_status, "error": str(exc)})
        else:
            deploy_id, deploy_status = deployment.id, deployment.status
            jobs.link(db, job.id, kind="deploy", ref_id=deployment.id)
            _stage(
                db, job.id, "deploy",
                level="milestone" if deploy_status == "verified" else "error",
                message=f"deploy {deploy_status}",
                data={
                    "host_id": deploy_host_id, "deploy": deploy_id, "status": deploy_status,
                    "path": f"scripts/{name}.vjs_c", "md5": compiled.md5, "size": compiled.size,
                },
            )

    # ---- verdict: still blocked, with the measured reason -------------------
    _stage(db, job.id, "verdict", message="verdict blocked",
           data={"blocked": True, "reason": SCRIPT_BLOCKED["verdict"]})

    learned_bits = [
        f"generate wrote scripts/{name}.js ({len(text.encode('utf-8'))} B, "
        f"{'caller-supplied' if script_text is not None else 'scaffold skeleton'})",
        f"validate {validation}",
        ("build skipped (no compile requested)" if not compile
         else f"build {build_status or 'skipped'}" + (f" ({build_id})" if build_id else "")),
        ("deploy skipped" if deploy_status is None else f"deploy {deploy_status}"
         + (f" ({deploy_id})" if deploy_id else "")),
        "verdict blocked",
    ]
    failed = validation == "failed" or build_status == "failed" or (
        deploy_status is not None and deploy_status != "verified"
    )
    return jobs.finish(
        db, job.id,
        status="failed" if failed else "inconclusive",
        summary=_summary(
            what=(
                f"Ran the cs_script chain for {name} in addon {addon}: "
                f"{', '.join(SCRIPT_STAGES)} recorded on one job."
            ),
            why=(
                "A text->script action kind needs a durable record of what was generated, whether it "
                "type-checks, and — when a host was named — which .vjs_c that host actually compiled."
            ),
            learned="; ".join(learned_bits),
            next_=(
                "Compile a map whose point_script references scripts/"
                f"{name}.vjs on desktop-pc (vmap compiles need its display adapter) and capture an "
                "in-game verdict there."
            ),
        ),
    )
