"""The asset chain as one durable job record: prompt -> concept -> mesh -> asset -> build -> deploy -> ingame.

This is the vertical slice of the ``pipeline`` ledger step, and it is deliberately explicit about
which stages are real:

* ``asset``, ``build``, ``deploy`` and ``ingame`` call the same code the CLI calls (the
  ``.vmdl``/``.vmat`` writers, the server-owned build runs, ``deploys.push`` + ``deploys.verify``, and
  ``live.service.show_model`` over netcon), so the chain is proven exactly where it runs today.
* ``concept`` and ``mesh`` are recorded as **stubbed**. No text->3D generator is wired into this
  repo (``docs/HAMMERED.md``: the chain starts from an existing mesh), and a stage that silently
  "passes" without producing anything is worse than one that says so.
* ``ingame`` is **real only when it can be judged**: it needs a netcon-reachable target host running
  ``hermes_sandbox``. A succeeded model build also collects its compiled files
  (``live.asset_artifacts.collect_model_artifacts``), pushes them loose to that host, and then asks the
  running game to precache and spawn the model (`hs_model`), judging the readback
  ``[hs] model <path> spawned name=<path>`` plus a rising ``hs_ping`` counter. Without a target host, a
  successful build, or the spawngroup it is recorded **skipped** or **blocked** with the reason — never
  a fake pass.

``deploy`` only runs when the caller supplies a destination *and* the build succeeded; otherwise it
is recorded **skipped** with the reason (no destination, or nothing new compiled). Skipped is not
blocked and not a pass: the ledger says which precondition was missing instead of implying the
capability does not exist.

A verified ``ingame`` readback is **not** a visual verdict: the job closes ``inconclusive`` and its
``next`` says to look at the screen and then run ``s2docs pipeline verdict <job> pass|fail``. That
command (:func:`record_verdict`) is the one narrow transition this module owns besides the chain —
``inconclusive -> passed`` / ``-> failed`` on a verified spawn, writing a ``human-confirm`` evidence row
and a ``verdict`` stage event — because the look happens *after* the job closed, and re-running the
chain to claim it would spawn a second model nobody judged. A caller who already watched the run can
still pass ``verdict="pass"`` inline (the chain then closes ``passed``); ``failed`` is real for a build,
an artifact pull, a deploy or a readback that did not match, and no verdict can override one.

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
from s2docs.live import asset_artifacts, service

#: The chain, in order. This tuple is the contract: a job's timeline must carry one event per entry.
STAGES: tuple[str, ...] = ("prompt", "concept", "mesh", "asset", "build", "deploy", "ingame")

#: Stages this slice can execute. ``deploy`` and ``ingame`` execute only when their preconditions are
#: supplied (a destination, a netcon-reachable host with the sandbox loaded); otherwise they are
#: recorded skipped/blocked with the reason (see ``NO_DEST_REASON`` / ``BLOCKED_REASONS``).
REAL_STAGES: tuple[str, ...] = ("asset", "build", "deploy", "ingame")

STUB_REASON = (
    "no generator is wired for this stage yet: the chain starts from an existing mesh "
    "(docs/HAMMERED.md), so the stage is recorded as intent instead of pretending to produce output"
)

#: Why ``deploy`` is skipped when the caller gave no destination — a missing argument, not a block.
NO_DEST_REASON = (
    "no destination was given (--dest <remote root>), so nothing was copied to a host; the stage is "
    "skipped rather than blocked, because the deploy path itself works"
)

#: Why ``ingame`` is skipped when the caller gave no target host — a missing argument, not a block.
NO_INGAME_HOST_REASON = (
    "no in-game target host was given (--ingame-host <host id>), so nothing was spawned in a running "
    "game; a model can only be proven in-game on a host that is running CS2 with hermes_sandbox loaded"
)

#: The only close-out a mechanical readback earns on its own: someone has to look at the screen.
#: The job is CLOSED at this point, so the verdict cannot come from this run — it arrives later, as
#: `s2docs pipeline verdict <job> pass|fail` (:func:`record_verdict`), which is the one transition this
#: module owns besides the chain itself.
NEEDS_VISUAL_VERDICT = (
    "the model precached and spawned on the target host (readback verified), but a console line is not a "
    "visual verdict: look at the spawn in the running game, then record what you saw with "
    "`s2docs pipeline verdict <job> pass|fail --notes ...` (no re-run, no second spawn)"
)

#: Why ``ingame`` is blocked rather than skipped/attempted: a capability precondition is missing.
BLOCKED_REASONS: dict[str, str] = {
    "ingame": (
        "in-game proof needs a netcon-reachable host that is running CS2 with the hermes_sandbox spawngroup "
        "loaded: `hs_model` uses the sandbox's hs_tpl_prop template (a missing vpk makes spawn_group_load "
        "kick the client), so without it there is nothing to spawn onto. Nothing was launched here "
        "(docs/LIVE.md, measured 2026-10-27)"
    ),
}

#: Verdicts a caller may pass. Only ``pass`` closes the chain ``passed``.
VERDICTS: tuple[str, ...] = ("pass", "fail")

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
    ingame_host: str | None = None,
    ingame_model: str | None = None,
    ingame_clear: bool = False,
    verdict: str | None = None,
) -> m.Job:
    """Run the chain for one mesh and return the closed-out job.

    ``build_command`` is the host-side compile line; without it the build stage is recorded as
    *not attempted* rather than faked. ``dest`` is the remote root the compiled addon is copied to
    (a Windows/share path, ``D:/...`` — whatever the host's scp accepts): with it, and a successful
    build, the deploy stage pushes and MD5-verifies the addon content; without it the stage is
    recorded *skipped* and says no destination was given.

    ``ingame_host`` is the host running CS2 (netcon registered, ``hermes_sandbox`` loaded). With it, a
    succeeded build's compiled artifacts are collected to ``<data_dir>/artifacts/<build id>/<rel>``,
    pushed loose into that host's ``game/csgo`` tree (MD5 readback), and the game is asked to precache
    and spawn ``ingame_model`` (default ``models/<name>/<name>.vmdl``). The stage is ``verified`` only on
    the game's own ``[hs] model <path> spawned name=<path>`` line plus a rising ``hs_ping`` count.
    Without a target host the stage is *skipped*; with a host but no sandbox/no netcon it is *blocked*
    — neither is ever a pass.

    ``verdict`` is a visual verdict (``pass``/``fail``) the caller already made *while* the chain ran:
    ``pass`` closes the job ``passed`` inline. A verified readback with no verdict closes
    ``inconclusive``, because a console line is not a look at the screen — the job's ``next`` then points
    at ``s2docs pipeline verdict <job> pass|fail`` (:func:`record_verdict`), which answers the same
    question later without spawning anything again.
    """
    if verdict is not None and verdict not in VERDICTS:
        raise PipelineError(f"verdict must be one of {', '.join(VERDICTS)} (or omitted), got {verdict!r}")
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
    collected: asset_artifacts.Collected | None = None
    artifact_error: str | None = None
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
            build_data: dict = {"build": run.id, "status": run.status, "exit_code": run.exit_code,
                                "summary": run.summary, "log_path": run.log_path}
            level = "milestone" if run.status == "succeeded" else "error"
            message = f"build {run.status}"
            if run.status == "succeeded":
                # The compile host writes models/<name>/*.vmdl_c + materials/hammered/<name>/*.vmat_c; before
                # 2026-10-27 nothing collected them and `artifacts` stayed []. One MD5-checked pull later, the
                # build record carries the files a playing host needs.
                try:
                    collected = asset_artifacts.collect_model_artifacts(
                        db, host_id=host_id, addon=addon, name=name, run_id=run.id,
                    )
                except (service.LiveError, deploys.DeployError, hosts.HostError, OSError) as exc:
                    # A pull that did not match is not a build we can hand to a game: the stage is an error.
                    artifact_error = str(exc)
                    level = "error"
                    message = "build succeeded but its artifacts were not collected"
                    build_data["artifact_error"] = artifact_error
                else:
                    build_data["artifacts"] = len(collected.files)
                    build_data["artifact_root"] = str(collected.root)
                    message = f"build {run.status}, {len(collected.files)} artifact(s) collected"
            _stage(db, job.id, "build", level=level, message=message, data=build_data)
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

    # ---- ingame: real when a target host is given and the build produced artifacts
    ingame_status: str | None = None
    ingame_detail: str | None = None
    model_path = ingame_model or f"models/{name}/{name}.vmdl"
    ingame_data: dict = {"host_id": ingame_host, "model": model_path}
    if not ingame_host:
        _stage(db, job.id, "ingame", message="ingame skipped: no target host",
               data={"skipped": True, "reason": NO_INGAME_HOST_REASON})
    elif build_status != "succeeded" or collected is None:
        ingame_data.update({"skipped": True, "reason": (
            "the build did not succeed and its compiled artifacts are not on this machine, so there is "
            "nothing to spawn in a running game"
        )})
        _stage(db, job.id, "ingame", message="ingame skipped: no compiled artifacts",
               data=ingame_data)
    else:
        # The playing host loads loose files (an addon mount did not work): push the collected artifacts
        # to <CS2>/game/csgo at their own relative paths, MD5-read back, then ask the game to spawn it.
        try:
            loose, loose_failure = asset_artifacts.deploy_loose(
                db, collected, host_id=ingame_host, addon=addon, owner=f"live:pipeline:{job.id}",
            )
        except (deploys.DeployError, hosts.HostError, service.LiveError) as exc:
            loose, loose_failure = None, str(exc)
        if loose is not None:
            jobs.link(db, job.id, kind="deploy", ref_id=loose.id)
            ingame_data.update({"loose_deploy": loose.id, "loose_status": loose.status,
                                "files": len(loose.entries),
                                "verified": sum(1 for entry in loose.entries if entry.verified)})
        else:
            ingame_data["loose_status"] = "failed"
        loose_status = loose.status if loose is not None else "failed"
        if loose_failure or loose_status != "verified":
            ingame_status = "failed"
            ingame_detail = loose_failure or f"the loose artifact deploy is {loose_status}"
            ingame_data.update({"status": ingame_status, "error": ingame_detail})
            _stage(db, job.id, "ingame", level="error",
                   message="ingame failed: artifacts did not reach the target host", data=ingame_data)
        else:
            try:
                result = service.show_model(db, ingame_host, model_path, clear=ingame_clear)
            except (service.LiveError, hosts.HostError) as exc:
                # No netcon address for that host, or netcon went away: a missing precondition, not a pass.
                ingame_status = "blocked"
                ingame_detail = str(exc)
                ingame_data.update({"blocked": True, "status": ingame_status,
                                    "reason": BLOCKED_REASONS["ingame"], "detail": ingame_detail})
                _stage(db, job.id, "ingame", level="warn", message="ingame blocked", data=ingame_data)
            else:
                ingame_status = result["status"]
                ingame_detail = result["detail"]
                ingame_data.update({
                    "status": ingame_status, "detail": ingame_detail, "lines": result["lines"],
                    "pong_before": result["pong_before"], "pong_after": result["pong_after"],
                    "readback": result["readback"],
                })
                if verdict is not None:
                    ingame_data["verdict"] = verdict
                if ingame_status == "blocked":
                    ingame_data["blocked"] = True
                    ingame_data["reason"] = BLOCKED_REASONS["ingame"]
                    _stage(db, job.id, "ingame", level="warn", message="ingame blocked", data=ingame_data)
                else:
                    _stage(db, job.id, "ingame",
                           level="milestone" if ingame_status == "verified" else "error",
                           message=f"ingame {ingame_status}", data=ingame_data)

    # A deploy that ran and did not come back 'verified' is a failure, including the case where the
    # MD5 readback could not run: the chain must not close 'inconclusive' on bytes nobody confirmed.
    deploy_failed = deploy_status is not None and deploy_status != "verified"
    ingame_failed = ingame_status == "failed" or verdict == "fail"
    failed = (build_status in ("failed", "cancelled", "interrupted") or deploy_failed
              or artifact_error is not None or ingame_failed)
    # `passed` is the caller's verdict on a verified readback — never a console line on its own.
    passed = ingame_status == "verified" and verdict == "pass"
    if deploy_status is None:
        deploy_bit = "deploy skipped (no destination)" if not dest else "deploy skipped (no successful build)"
    else:
        deploy_bit = f"deploy {deploy_status}" + (f" ({deploy_id})" if deploy_id else "")
    if ingame_status is None:
        ingame_bit = ("ingame skipped (no target host)" if not ingame_host
                      else "ingame skipped (no compiled artifacts)")
    else:
        ingame_bit = f"ingame {ingame_status}" + (f": {ingame_detail}" if ingame_detail else "")
    learned_bits = [
        f"asset stage wrote {produced['vmdl']}" + (f" and {produced['vmat']}" if "vmat" in produced else ""),
        "concept/mesh have no generator (stubbed)",
        f"build {'not attempted' if build_status is None else build_status}"
        + (f" ({build_id})" if build_id else "")
        + (f", {len(collected.files)} artifact(s) collected" if collected else "")
        + (f", artifacts not collected: {artifact_error}" if artifact_error else ""),
        deploy_bit,
        ingame_bit,
    ]
    if ingame_status == "verified" and verdict is None:
        next_ = NEEDS_VISUAL_VERDICT
    elif passed:
        next_ = "Closed passed on the caller's visual verdict; wire concept/mesh generators to finish the chain."
    else:
        next_ = (
            "Pass --dest to copy a compiled addon to a host; pass --ingame-host (a host running CS2 with "
            "hermes_sandbox loaded) to prove the model in-game, then wire concept/mesh generators."
        )
    return jobs.finish(
        db, job.id,
        status="failed" if failed else ("passed" if passed else "inconclusive"),
        summary=_summary(
            what=(
                f"Ran the asset chain for {name} in addon {addon} from a prompt; "
                f"stages {', '.join(STAGES)} recorded on one job."
            ),
            why=(
                "Prove the chain where it exists (asset+build+deploy+ingame) and record stubs/blocks/skips "
                "honestly instead of a green status that hides them."
            ),
            learned="; ".join(learned_bits),
            next_=next_,
        ),
    )


# ------------------------------------------------------------------ the look at the screen

#: The stage a recorded verdict appends. It is deliberately NOT in :data:`STAGES`: the chain ran and
#: closed without it, and a timeline reads ``STAGES + (VERDICT_STAGE,)`` only for a job a human closed.
VERDICT_STAGE = "verdict"

#: What a verdict can close. ``skip`` exists on the confirm channel for a step that cannot be exercised;
#: here it would be a third way to say nothing was seen, and ``inconclusive`` already means that.
VERDICT_OUTCOMES: dict[str, str] = {"pass": "passed", "fail": "failed"}


def _ingame_event(job: m.Job) -> m.JobEvent | None:
    """The job's newest ``ingame`` stage event (the readback), or None when it has no such stage."""
    found = [e for e in job.events if e.phase == "ingame"]
    return found[-1] if found else None


def _recorded_verdict(job: m.Job) -> tuple[str | None, str | None]:
    """The verdict already on the job: ``(verdict, where)`` — the verdict stage first, then the readback.

    A verdict stage event is what :func:`record_verdict` appends; the fallback is the inline
    ``--verdict`` a caller passed while the chain ran, which ``run_asset_build_slice`` stores on the
    ``ingame`` event instead. Both count as "somebody already said this", so a repeat can be a no-op.
    """
    for event in reversed(job.events):
        if event.phase == VERDICT_STAGE and event.data.get("verdict"):
            return event.data["verdict"], VERDICT_STAGE
    ingame = _ingame_event(job)
    if ingame is not None and ingame.data.get("verdict"):
        return ingame.data["verdict"], "ingame"
    return None, None


def record_verdict(
    db,
    job_id: str,
    verdict: str,
    notes: str | None = None,
    *,
    source: str = "coordinator",
) -> m.Job:
    """Attach a human's visual verdict to an asset-chain job that already closed ``inconclusive``.

    The chain proves the model spawned (a console readback) and then stops: only a person looking at the
    screen can say it *looked right*, and that look happens after the job is closed. This is the one
    narrow transition that answers it without re-running anything —

    ``inconclusive -> passed`` (``verdict='pass'``) or ``-> failed`` (``verdict='fail'``).

    It is not a general reopen. Refused, with the reason:

    * a verdict outside ``pass|fail`` (:class:`PipelineError`);
    * a job with no ``ingame`` stage — it is not an asset-chain job at all;
    * an ``ingame`` stage that was **skipped** (no target host / no compiled artifacts) or **blocked**
      (no netcon, no sandbox): nobody spawned anything, so there is nothing anyone could have seen;
    * an ``ingame`` stage that did not verify: a ``pass`` can never override a readback that failed
      (that job stays ``failed``);
    * a job that is not closed (``queued``/``running`` — answer the step, don't close it here) or that
      closed ``error``/``cancelled``;
    * a second verdict that disagrees with the one already recorded (:class:`jobs.JobConflict`). The
      same verdict again is an idempotent no-op: the job comes back unchanged and no second evidence
      row or event is written, so a retried command cannot double-book the confirmation.

    The verdict is recorded durably, not just in the status: a ``human-confirm`` evidence row (through
    :func:`s2docs.evidence.add_and_publish`, so the confirm push files fire) anchored to the job, a
    ``milestone`` job event with ``phase='verdict'``, a link from the job to that row, and a rewritten
    close-out summary whose ``learned``/``next`` say who looked and what they saw.
    """
    from s2docs import evidence

    job = jobs.get(db, job_id)  # 404 on an unknown job
    if verdict not in VERDICTS:
        raise PipelineError(
            f"verdict must be one of {', '.join(VERDICTS)}, not {verdict!r}"
        )
    if source not in evidence.CONFIRM_SOURCES:
        raise PipelineError(
            f"source must be one of {', '.join(evidence.CONFIRM_SOURCES)}, not {source!r}"
        )
    ingame = _ingame_event(job)
    if ingame is None:
        raise jobs.JobConflict(
            f"job {job_id} has no ingame stage; it was not produced by the asset pipeline, so there is "
            "no spawn to look at (answer it with `s2docs job verdict` if it is a step-gated run)"
        )
    if ingame.data.get("skipped") or ingame.data.get("blocked") or ingame.data.get("status") in ("skipped", "blocked"):
        why = ingame.data.get("reason") or ingame.data.get("detail") or ingame.message
        raise jobs.JobConflict(
            f"job {job_id} ingame stage was {ingame.data.get('status') or 'skipped'}, not a spawn: "
            f"{why} — nothing was on screen for anyone to judge"
        )
    if ingame.data.get("status") != "verified" or ingame.level != "milestone":
        raise jobs.JobConflict(
            f"job {job_id} ingame readback is {ingame.data.get('status') or ingame.level}, not verified; "
            f"a {verdict} verdict cannot override a readback that did not match "
            f"({ingame.data.get('detail') or ingame.data.get('error') or ingame.message})"
        )
    already, where = _recorded_verdict(job)
    if job.status in jobs.ACTIVE_STATUSES:
        raise jobs.JobConflict(
            f"job {job_id} is {job.status}; a visual verdict closes a job that already ended — answer the "
            "running step with `s2docs job verdict`, or let the chain close it out first"
        )
    if job.status not in VERDICT_OUTCOMES.values() and job.status != "inconclusive":
        raise jobs.JobConflict(
            f"job {job_id} is {job.status}; only an inconclusive asset-chain job takes a visual verdict "
            "(a cancelled/error job stays as it is — that is `s2docs job restatus` territory)"
        )
    if job.status == VERDICT_OUTCOMES[verdict] and already == verdict:
        return job  # idempotent: the same verdict was already recorded, nothing new to say
    if job.status != "inconclusive":
        raise jobs.JobConflict(
            f"job {job_id} is already {job.status} (verdict {already!r} from {where}); refusing to "
            f"restate it as {verdict!r} — a contradictory verdict needs `s2docs job restatus` with a reason"
        )

    status = VERDICT_OUTCOMES[verdict]
    row = evidence.add_and_publish(
        db,
        kind="human-confirm",
        job_id=job_id,
        ref=job_id,
        data={
            "verdict": verdict,
            "notes": notes,
            "source": source,
            "job_id": job_id,
            "stage": "ingame",
            "ingame_event": ingame.id,
            "readback_status": ingame.data.get("status"),
            "host_id": ingame.data.get("host_id"),
            "model": ingame.data.get("model"),
        },
    )
    with db.writer() as conn, conn:
        cur = conn.execute(
            "UPDATE jobs SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND status = 'inconclusive'",
            (status, job_id),
        )
        changed = cur.rowcount
    if not changed:  # another writer closed it between the read and the update
        raise jobs.JobConflict(f"job {job_id} was closed by someone else while the verdict was recorded")
    jobs.link(db, job_id, kind="evidence", ref_id=row.id)
    jobs.event(
        db, job_id,
        level="milestone",
        phase=VERDICT_STAGE,
        message=f"visual verdict {verdict} ({source})"
        + (f": {notes}" if notes and notes.strip() else " (no note given)"),
        data={
            "verdict": verdict, "notes": notes, "source": source, "evidence_id": row.id,
            "from": "inconclusive", "to": status, "ingame_event": ingame.id,
            "host_id": ingame.data.get("host_id"), "model": ingame.data.get("model"),
        },
    )
    previous = job.summary.model_dump() if job.summary is not None else {}
    # Keep room for the new sentence: the verdict is the fact this rewrite exists to record, so the old
    # learned line is what gets shortened, never the verdict.
    before = previous.get("learned", "")[: SUMMARY_MAX - 250].rstrip(" ;,")
    sep = f"{before}; " if before else ""
    said = (notes or "").strip()[:120]
    seen = f"seen by a human ({source})" + (f": {said}" if said else ", no note given")
    if verdict == "pass":
        learned = (
            f"{sep}visual verdict pass — the spawn of {ingame.data.get('model')} "
            f"on {ingame.data.get('host_id')} looked right ({seen})"
        )
        next_ = (
            "Closed passed on a human look at the screen, not on the console readback alone; a second "
            "chain for the next asset is the way forward, and concept/mesh generators are still stubs."
        )
    else:
        learned = (
            f"{sep}visual verdict fail — the spawn of {ingame.data.get('model')} "
            f"on {ingame.data.get('host_id')} did NOT look right ({seen})"
        )
        next_ = (
            "Closed failed on a human look at the screen even though the readback verified: fix what was "
            "seen (mesh, material, scale, placement), then re-run the chain and look again."
        )
    return jobs.rewrite_summary(
        db, job_id,
        summary=_summary(
            what=previous.get("what", f"Ran the asset chain for job {job_id}."),
            why=previous.get("why", "Record what the chain proved and what a human saw."),
            learned=learned.lstrip("; "),
            next_=next_,
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
