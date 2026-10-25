"""The asset chain as one durable job record: prompt -> concept -> mesh -> asset -> build -> deploy -> ingame.

This is the vertical slice of the ``pipeline`` ledger step, and it is deliberately explicit about
which stages are real:

* ``asset`` and ``build`` call the same code the CLI calls (the ``.vmdl``/``.vmat`` writers and the
  server-owned build runs), so the chain is proven exactly where it runs today.
* ``concept`` and ``mesh`` are recorded as **stubbed**. No text->3D generator is wired into this
  repo (``docs/HAMMERED.md``: the chain starts from an existing mesh), and a stage that silently
  "passes" without producing anything is worse than one that says so.
* ``deploy`` and ``ingame`` are recorded as **blocked**, with the measured reason, because the
  in-game step is genuinely blocked (``ent_create`` never renders a loose model in this client; the
  map-compile route needs a host with an active display adapter).

Every stage writes exactly one job event whose ``phase`` is the stage id, in stage order, so
``Job.current_step``/``jobs.get(...).phase`` and the timeline show the same chain. A produced
artifact is linked (``kind="build"``) rather than pasted into a message, so the job alone is enough
to reconstruct what happened.
"""
from __future__ import annotations

from pathlib import Path

from s2docs import builds, jobs
from s2docs import models as m
from s2docs.config import get_settings

#: The chain, in order. This tuple is the contract: a job's timeline must carry one event per entry.
STAGES: tuple[str, ...] = ("prompt", "concept", "mesh", "asset", "build", "deploy", "ingame")

#: Stages this slice actually executes.
REAL_STAGES: tuple[str, ...] = ("asset", "build")

STUB_REASON = (
    "no generator is wired for this stage yet: the chain starts from an existing mesh "
    "(docs/HAMMERED.md), so the stage is recorded as intent instead of pretending to produce output"
)

BLOCKED_REASONS: dict[str, str] = {
    "deploy": (
        "needs a compiled artifact plus a reachable target host; not attempted by this slice "
        "(pomps-pc sshd flaps for minutes at a time, desktop-pc has no display adapter for map compiles)"
    ),
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
) -> m.Job:
    """Run the chain for one mesh and return the closed-out job.

    ``build_command`` is the host-side compile line; without it the build stage is recorded as
    *not attempted* rather than faked. The returned job is terminal: ``passed`` never happens here,
    because ``deploy``/``ingame`` are blocked — ``inconclusive`` is the honest close-out.
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

    # ---- deploy / ingame: blocked, recorded as such ------------------------
    for stage in ("deploy", "ingame"):
        _stage(db, job.id, stage, message=f"{stage} blocked",
               data={"blocked": True, "reason": BLOCKED_REASONS[stage]})

    failed = build_status in ("failed", "cancelled", "interrupted")
    learned_bits = [
        f"asset stage wrote {produced['vmdl']}" + (f" and {produced['vmat']}" if "vmat" in produced else ""),
        "concept/mesh have no generator (stubbed)",
        f"build {'not attempted' if build_status is None else build_status}" + (f" ({build_id})" if build_id else ""),
        "deploy/ingame blocked",
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
                "Prove the chain where it exists (asset+build) and record stubs/blocks honestly "
                "instead of a green status that hides them."
            ),
            learned="; ".join(learned_bits),
            next_=(
                "Unblock ingame by compiling a map that references the model on a host with an "
                "active display adapter, then wire concept/mesh generators."
            ),
        ),
    )


# --------------------------------------------------------------------------- text -> cs_script

#: The script chain, in order. Same contract as STAGES: one event per entry, ``phase`` = the id.
SCRIPT_STAGES: tuple[str, ...] = ("prompt", "generate", "validate", "build", "deploy", "verdict")

SCRIPT_BLOCKED: dict[str, str] = {
    "build": (
        "the .js -> .vjs compile path is not established in this repo (the engine loads "
        "scripts/<name>.vjs); recorded instead of guessing a compiler invocation"
    ),
    "deploy": (
        "needs a reachable target host and a mounted addon; not attempted by this slice "
        "(pomps-pc sshd flaps for minutes at a time)"
    ),
    "verdict": (
        "an in-game verdict needs a live server plus the driver; blocked with the ingame stage "
        "(docs/HAMMERED.md 2026-10-24)"
    ),
}


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
) -> m.Job:
    """Run the text -> cs_script chain for one script and return the closed-out job.

    ``generate`` writes ``script_text`` when the caller supplies one (the LLM/caller is the
    generator) and otherwise the scaffold's own skeleton, so the stage never invents code it cannot
    attribute. ``validate`` type-checks through the same ``validate_cs_script`` the CLI uses; a
    missing TypeScript toolchain is *blocked*, not a pass and not a failure.
    """
    from s2docs.build.validate_script import ToolchainMissing, validate_cs_script
    from s2docs.build.workspace import addon_root, script_template
    from s2docs.db.repository import Repository

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
    try:
        with db.reader() as conn:
            result = validate_cs_script(Repository(conn), text, game=game, filename=f"{name}.js")
    except ToolchainMissing as exc:
        _stage(db, job.id, "validate", level="warn", message="validate blocked: no TypeScript toolchain",
               data={"blocked": True, "reason": str(exc)})
    else:
        if result.ok:
            validation = "ok"
            _stage(db, job.id, "validate", level="milestone", message="script type-checks",
                   data={"errors": result.errors, "warnings": result.warnings, "checker": result.checker})
        else:
            validation = "failed"
            _stage(
                db, job.id, "validate", level="error", message=f"script has {result.errors} error(s)",
                data={"errors": result.errors, "warnings": result.warnings,
                      "diagnostics": [d.model_dump() for d in result.diagnostics[:5]]},
            )

    # ---- build / deploy / verdict: blocked, recorded as such ---------------
    for stage in ("build", "deploy", "verdict"):
        _stage(db, job.id, stage, message=f"{stage} blocked",
               data={"blocked": True, "reason": SCRIPT_BLOCKED[stage]})

    learned = (
        f"generate wrote scripts/{name}.js ({len(text.encode('utf-8'))} B, "
        f"{'caller-supplied' if script_text is not None else 'scaffold skeleton'}); "
        f"validate {validation}; build/deploy/verdict blocked"
    )
    return jobs.finish(
        db, job.id,
        status="failed" if validation == "failed" else "inconclusive",
        summary=_summary(
            what=(
                f"Ran the cs_script chain for {name} in addon {addon}: "
                f"{', '.join(SCRIPT_STAGES)} recorded on one job."
            ),
            why=(
                "A text->script action kind needs a durable record of what was generated and whether "
                "it type-checks, not a shell transcript."
            ),
            learned=learned,
            next_=(
                "Establish the .js->.vjs compile path, then deploy the addon and capture an in-game "
                "verdict once a host with an active display adapter is reachable."
            ),
        ),
    )
