"""HTTP API routes. Thin layer: validate params, call Repository, return models."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from s2docs import builds as build_svc
from s2docs import deploys, evidence, hosts, jobs, test_runs, weburls
from s2docs import models as m
from s2docs.build.validate_cfg import validate_cfg
from s2docs.build.validate_script import validate_cs_script
from s2docs.config import GAMES, server_identity
from s2docs.db.connection import Database
from s2docs.db.repository import Repository
from s2docs.orchestration.engine import Orchestrator, workflow_info
from s2docs.orchestration.workflows import WORKFLOWS
from s2docs.prompts import PROMPTS, render_prompt

router = APIRouter(prefix="/api")

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_repo(db: Annotated[Database, Depends(get_db)]) -> Iterator[Repository]:
    with db.reader() as conn:
        yield Repository(conn)


def valid_game(game: Annotated[str, Path(description=f"One of: {', '.join(GAMES)}")]) -> str:
    if game not in GAMES:
        raise HTTPException(404, f"unknown game {game!r}; known: {', '.join(GAMES)}")
    return game


Repo = Annotated[Repository, Depends(get_repo)]
Game = Annotated[str, Depends(valid_game)]


def public_base(request: Request) -> str:
    """The web UI base URL for THIS request (S2DOCS_PUBLIC_URL, else scheme://host of the request)."""
    return weburls.public_base_url(request)


Base = Annotated[str, Depends(public_base)]


def _rebased(obj, base: str, fn):
    """Set `web_url` on a model (or every model in a list) with the request-derived base URL."""
    for it in obj if isinstance(obj, list) else [obj]:
        it.web_url = fn(base, it)
    return obj


# ---------------------------------------------------------------- meta
@router.get("/health", response_model=m.ServerInfo)
def health(request: Request):
    """DB identity: which store this process serves, and which process it is.

    Clients compare this against the MCP `server_info` tool before writing, so the split-database
    class of incident is detectable. `public_url` is the web UI base URL for this request."""
    info = m.ServerInfo(**server_identity())
    info.public_url = weburls.public_base_url(request)
    return info


@router.get("/stats", response_model=m.Stats)
def stats(repo: Repo):
    return repo.stats()


@router.get("/games", response_model=list[m.GameInfo])
def games(repo: Repo):
    return repo.games()


@router.get("/games/{game}/builds", response_model=list[m.BuildInfo])
def builds(game: Game, repo: Repo):
    return repo.builds(game)


@router.get("/search", response_model=m.Page[m.SearchHit])
def search(
    repo: Repo,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    game: str | None = None,
    kind: Annotated[list[str] | None, Query()] = None,
    limit: Limit = 20,
    offset: Offset = 0,
):
    if game is not None:
        valid_game(game)
    return repo.search(q, game=game, kinds=kind, limit=limit, offset=offset)


# ------------------------------------------------------------- convars
@router.get("/games/{game}/convars", response_model=m.Page[m.ConVar])
def list_convars(
    game: Game,
    repo: Repo,
    q: str | None = None,
    kind: Literal["convar", "command"] | None = None,
    flag: str | None = None,
    build: int | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return repo.list_convars(game, q=q, kind=kind, flag=flag, build=build, limit=limit, offset=offset)


@router.get("/games/{game}/convar-flags")
def convar_flags(game: Game, repo: Repo) -> list[dict[str, str | int]]:
    return [{"flag": f, "count": n} for f, n in repo.convar_flags(game)]


@router.get("/games/{game}/convars/{name}", response_model=m.ConVar)
def get_convar(game: Game, name: str, repo: Repo):
    return repo.get_convar(game, name)


# ------------------------------------------------------------ entities
@router.get("/games/{game}/entities", response_model=m.Page[m.EntitySummary])
def list_entities(
    game: Game,
    repo: Repo,
    q: str | None = None,
    side: Literal["server", "client"] | None = "server",
    io: Annotated[str | None, Query(description="Only entities declaring this input/output")] = None,
    include_base: bool = False,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return repo.list_entities(game, q=q, side=side, io=io, include_base=include_base, limit=limit, offset=offset)


@router.get("/games/{game}/entities/{side}/{classname}", response_model=m.Entity)
def get_entity(game: Game, side: Literal["server", "client"], classname: str, repo: Repo, inherited: bool = True):
    return repo.get_entity(game, classname, side=side, inherited=inherited)


# -------------------------------------------------------------- schema
@router.get("/games/{game}/schema", response_model=m.Page[m.SchemaClassSummary])
def list_schema(
    game: Game,
    repo: Repo,
    q: str | None = None,
    module: str | None = None,
    kind: Literal["class", "enum"] | None = None,
    field: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return repo.list_schema(game, q=q, module=module, kind=kind, field=field, limit=limit, offset=offset)


@router.get("/games/{game}/schema-modules")
def schema_modules(game: Game, repo: Repo) -> list[dict[str, str | int]]:
    return [{"module": mod, "count": n} for mod, n in repo.schema_modules(game)]


@router.get("/games/{game}/schema/{name}", response_model=m.SchemaClass)
def get_schema_class(game: Game, name: str, repo: Repo, module: str | None = None, inherited: bool = True):
    return repo.get_schema_class(game, name, module=module, inherited=inherited)


# -------------------------------------------------------------- events
@router.get("/games/{game}/events", response_model=m.Page[m.GameEvent])
def list_events(game: Game, repo: Repo, q: str | None = None, limit: Limit = 100, offset: Offset = 0):
    return repo.list_events(game, q=q, limit=limit, offset=offset)


@router.get("/games/{game}/events/{name}", response_model=m.GameEvent)
def get_event(game: Game, name: str, repo: Repo):
    return repo.get_event(game, name)


# ---------------------------------------------------------- script api
@router.get("/games/{game}/script-api/types", response_model=list[m.ScriptSymbol])
def script_types(game: Game, repo: Repo):
    return repo.list_script_types(game)


@router.get("/games/{game}/script-api/types/{name}", response_model=m.ScriptType)
def script_type(game: Game, name: str, repo: Repo, inherited: bool = True):
    return repo.get_script_type(game, name, inherited=inherited)


@router.get("/games/{game}/script-api/symbols/{symbol}", response_model=list[m.ScriptSymbol])
def script_symbols(game: Game, symbol: str, repo: Repo):
    return repo.find_script_symbols(game, symbol)


@router.get("/games/{game}/script-api/files/{filename}", response_class=PlainTextResponse)
def script_file(game: Game, filename: Literal["point_script.d.ts", "tsconfig.json"], repo: Repo):
    return PlainTextResponse(
        repo.raw_file(game, f"cs_script/{filename}"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------ examples
@router.get("/games/{game}/examples", response_model=list[m.Example])
def list_examples(game: Game, repo: Repo, api: str | None = None):
    return repo.list_examples(game, api=api)


@router.get("/games/{game}/examples/{name}", response_model=m.Example)
def get_example(game: Game, name: str, repo: Repo, api: str = "cs_script"):
    return repo.get_example(game, name, api=api)


# ---------------------------------------------------------------- docs
@router.get("/docs", response_model=m.Page[m.Doc])
def list_docs(repo: Repo, game: str | None = None, q: str | None = None, limit: Limit = 50, offset: Offset = 0):
    if game is not None:
        valid_game(game)
    return repo.list_docs(game=game, q=q, limit=limit, offset=offset)


@router.get("/doc", response_model=m.Doc)
def get_doc(repo: Repo, key: Annotated[str, Query(description="Doc key (from search) or URL")]):
    return repo.get_doc(key)


# --------------------------------------------------------------- pulse
@router.get("/games/{game}/pulse", response_model=m.Page[m.PulseBinding])
def list_pulse(
    game: Game,
    repo: Repo,
    q: str | None = None,
    library: str | None = None,
    kind: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return repo.list_pulse(game, q=q, library=library, kind=kind, limit=limit, offset=offset)


@router.get("/games/{game}/pulse/{binding_id}", response_model=m.PulseBinding)
def get_pulse(game: Game, binding_id: int, repo: Repo):
    return repo.get_pulse(game, binding_id)


# ---------------------------------------------------------------- diff
@router.get("/games/{game}/diff", response_model=m.BuildDiff)
def diff(
    game: Game,
    repo: Repo,
    from_build: Annotated[int | None, Query(alias="from")] = None,
    to_build: Annotated[int | None, Query(alias="to")] = None,
):
    return repo.diff_builds(game, from_build, to_build)


# ----------------------------------------------------------- validation
class ScriptIn(BaseModel):
    code: str = Field(max_length=2_000_000)
    game: str = "cs2"
    filename: str = "script.js"


class CfgIn(BaseModel):
    text: str = Field(max_length=2_000_000)
    game: str = "cs2"
    context: Literal["server", "client", "any"] = "server"


@router.post("/validate/cs-script", response_model=m.ScriptValidation)
def validate_script(body: ScriptIn, repo: Repo):
    valid_game(body.game)
    return validate_cs_script(repo, body.code, game=body.game, filename=body.filename)


@router.post("/validate/cfg", response_model=m.CfgValidation)
def validate_config(body: CfgIn, repo: Repo):
    valid_game(body.game)
    return validate_cfg(repo, body.text, game=body.game, context=body.context)


# -------------------------------------------------------- orchestration
def get_orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


Orch = Annotated[Orchestrator, Depends(get_orchestrator)]


class StartProjectIn(BaseModel):
    workflow: str
    goal: str = Field(min_length=3, max_length=2000)
    game: str = "cs2"
    addon: str | None = None
    inputs: dict[str, str] | None = None
    steps: list[m.CustomStep] | None = None
    title: str | None = None


class UpdateStepIn(BaseModel):
    status: Literal["in_progress", "done", "skipped", "blocked", "pending"]
    notes: str | None = Field(default=None, max_length=20000)
    artifacts: list[str] | None = None


@router.get("/workflows", response_model=list[m.WorkflowInfo])
def list_workflows():
    return [workflow_info(w) for w in WORKFLOWS.values()]


@router.get("/projects", response_model=list[m.ProjectSummary])
def list_projects(orch: Orch, base: Base, status: Literal["active", "done", "abandoned"] | None = None):
    return _rebased(orch.list(status), base, lambda b, r: weburls.project_url(b, r.game, r.id))


@router.post("/projects", response_model=m.ProjectBrief, status_code=201)
def start_project(body: StartProjectIn, orch: Orch):
    return orch.start(
        body.workflow,
        body.goal,
        game=body.game,
        addon=body.addon,
        inputs=body.inputs,
        custom_steps=body.steps,
        title=body.title,
    )


@router.get("/projects/{project_id}", response_model=m.Project)
def get_project(project_id: str, orch: Orch, base: Base):
    return _rebased(orch.get(project_id), base, lambda b, r: weburls.project_url(b, r.game, r.id))


@router.get("/projects/{project_id}/brief", response_model=m.ProjectBrief)
def project_brief(project_id: str, orch: Orch):
    return orch.brief(project_id)


@router.post("/projects/{project_id}/steps/{step_id}", response_model=m.ProjectBrief)
def update_step(project_id: str, step_id: str, body: UpdateStepIn, orch: Orch):
    return orch.update(project_id, step_id, body.status, notes=body.notes, artifacts=body.artifacts)


# --------------------------------------------------------------- hosts
def get_db_only(request: Request) -> Database:
    return request.app.state.db


Db = Annotated[Database, Depends(get_db_only)]


class HostIn(BaseModel):
    ssh_target: str
    hostname: str | None = None
    ssh_key: str | None = None
    shell: Literal["cmd", "powershell"] | None = None
    os: str | None = None
    notes: str | None = None


class LockIn(BaseModel):
    owner: str = Field(min_length=1, max_length=200)
    ttl_sec: int = Field(default=1800, ge=60)
    force: bool = False
    phase: str | None = Field(default=None, description="what the owner is doing, e.g. 'compile'")
    note: str | None = None


class HeartbeatIn(BaseModel):
    owner: str = Field(min_length=1, max_length=200)
    phase: str | None = Field(default=None, description="pass the same phase to keep it, a new one to reset the clock")
    note: str | None = None


@router.get("/hosts", response_model=list[m.Host])
def list_hosts(db: Db):
    return hosts.list_hosts(db)


@router.get("/hosts/{host_id}", response_model=m.Host)
def get_host(host_id: str, db: Db):
    return hosts.get_host(db, host_id)


@router.get("/hosts/{host_id}/locks", response_model=list[m.HostLockEvent])
def host_locks(host_id: str, db: Db, limit: Annotated[int, Query(ge=1, le=200)] = 20):
    """Recent claim/takeover/release events for a host (newest first) — the Hosts page's lock history."""
    hosts.get_host(db, host_id)  # 404 on an unknown host
    return hosts.lock_history(db, host_id, limit=limit)


@router.put("/hosts/{host_id}", response_model=m.Host)
def register_host(host_id: str, body: HostIn, db: Db):
    return hosts.register_host(db, host_id, **body.model_dump())


@router.post("/hosts/{host_id}/probe", response_model=m.Host)
def probe_host(host_id: str, db: Db):
    return hosts.probe_host(db, host_id)


@router.post("/hosts/{host_id}/lock", response_model=m.HostLock)
def claim_host(host_id: str, body: LockIn, db: Db):
    return hosts.claim_host(
        db, host_id, body.owner, ttl_sec=body.ttl_sec, force=body.force, phase=body.phase, note=body.note
    )


@router.post("/hosts/{host_id}/heartbeat", response_model=m.HostLock)
def heartbeat_host(host_id: str, body: HeartbeatIn, db: Db):
    return hosts.heartbeat_host(db, host_id, body.owner, phase=body.phase, note=body.note)


@router.delete("/hosts/{host_id}/lock")
def release_host(host_id: str, db: Db, owner: Annotated[str, Query()], force: Annotated[bool, Query()] = False):
    return {"result": hosts.release_host(db, host_id, owner, force=force)}


# ---------------------------------------------------------------- build runs
class BuildIn(BaseModel):
    command: str = Field(min_length=1, max_length=10000)
    kind: str = "build"
    log_path: str | None = None
    notes: str | None = None


class BuildWaitIn(BaseModel):
    timeout_sec: int = Field(default=3600, ge=1)


@router.post("/hosts/{host_id}/builds", response_model=m.BuildRun, status_code=201)
def start_build(host_id: str, body: BuildIn, db: Db):
    return build_svc.start(db, host_id, body.command, kind=body.kind, log_path=body.log_path, notes=body.notes)


@router.get("/builds", response_model=list[m.BuildRun])
def list_builds(db: Db, base: Base, host_id: str | None = None, active: bool = False):
    return _rebased(
        build_svc.list_runs(db, host_id=host_id, active=active), base, lambda b, r: weburls.build_url(b, r.id)
    )


@router.get("/builds/{run_id}", response_model=m.BuildRun)
def get_build(run_id: str, db: Db, base: Base):
    return _rebased(build_svc.get_run(db, run_id), base, lambda b, r: weburls.build_url(b, r.id))


@router.get("/builds/{run_id}/log", response_model=m.BuildLogTail)
def build_log(run_id: str, db: Db, lines: Annotated[int, Query(ge=1, le=5000)] = 200):
    return build_svc.log_tail(db, run_id, lines=lines)


@router.post("/builds/{run_id}/wait", response_model=m.BuildRun)
def build_wait(run_id: str, body: BuildWaitIn, db: Db):
    return build_svc.wait(db, run_id, timeout_sec=body.timeout_sec)


@router.post("/builds/{run_id}/cancel", response_model=m.BuildRun)
def build_cancel(run_id: str, db: Db):
    return build_svc.cancel(db, run_id)


# --------------------------------------------------------------- deployments
class DeployIn(BaseModel):
    host_id: str
    dest_root: str = Field(min_length=1)
    files: list[m.DeployFile]
    addon: str | None = None
    parallel: int = Field(default=1, ge=1, le=16)
    dry_run: bool = False
    verify: bool = True
    notes: str | None = None
    owner: str | None = Field(
        default=None,
        description="Your host-lock owner if you already hold the lease; a free host is claimed as "
        "'deploy:<id>' for the copy and released afterwards",
    )


@router.post("/deploys", response_model=m.Deployment, status_code=201)
def push_deploy(body: DeployIn, db: Db):
    return deploys.push(
        db,
        body.host_id,
        body.dest_root,
        body.files,
        addon=body.addon,
        parallel=body.parallel,
        dry_run=body.dry_run,
        verify=body.verify,
        notes=body.notes,
        owner=body.owner,
    )


@router.get("/deploys", response_model=list[m.Deployment])
def list_deploys(db: Db, base: Base, host_id: str | None = None):
    return _rebased(deploys.list_deployments(db, host_id=host_id), base, lambda b, r: weburls.deploy_url(b, r.id))


@router.get("/deploys/{deploy_id}", response_model=m.Deployment)
def get_deploy(deploy_id: str, db: Db, base: Base):
    return _rebased(deploys.get_deployment(db, deploy_id), base, lambda b, r: weburls.deploy_url(b, r.id))


@router.post("/deploys/{deploy_id}/verify", response_model=m.Deployment)
def verify_deploy(deploy_id: str, db: Db):
    return deploys.verify(db, deploy_id)


# ---------------------------------------------------------------- evidence
class EvidenceIn(BaseModel):
    project_id: str | None = Field(default=None, description="Owning project; omit when test_run_id identifies the run")
    test_run_id: str | None = Field(
        default=None, description="Run this evidence records; its project is used when project_id is omitted"
    )
    step_id: str | None = None
    kind: m.EvidenceKind
    ref: str | None = None
    data: dict = Field(default_factory=dict)


@router.post("/evidence", response_model=m.Evidence, status_code=201)
def add_evidence(body: EvidenceIn, db: Db):
    # The director puts the run inside `data`; accept a top-level `test_run_id` too. The store resolves
    # the project from it, so human-confirm evidence stores without the caller knowing a project.
    # add_and_publish is the ONE evidence path: a human-confirm row also fires the confirm push files
    # (data_dir/confirms.jsonl + latest_confirm.json) so a waiting agent can watch a local file, and
    # POST /jobs/{id}/verdict reuses it so a webapp verdict lands exactly the same way.
    test_run_id = body.test_run_id or body.data.get("test_run_id")
    return evidence.add_and_publish(
        db,
        kind=body.kind,
        project_id=body.project_id,
        step_id=body.step_id,
        ref=body.ref,
        data=body.data,
        test_run_id=test_run_id,
    )


@router.get("/evidence", response_model=list[m.Evidence])
def list_evidence(db: Db, project_id: str | None = None, step_id: str | None = None, kind: str | None = None):
    return evidence.list_evidence(db, project_id=project_id, step_id=step_id, kind=kind)


@router.get("/evidence/{evidence_id}", response_model=m.Evidence)
def get_evidence(evidence_id: str, db: Db):
    return evidence.get(db, evidence_id)


# --------------------------------------------------------------- test runs
class TestRunIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    host_id: str | None = None
    project_id: str | None = None


class TestStepIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class TestFinishIn(BaseModel):
    result: Literal["pass", "fail"]
    note: str | None = None


@router.post("/test-runs", response_model=m.TestRun, status_code=201)
def start_test_run(body: TestRunIn, db: Db):
    return test_runs.start(db, body.name, host_id=body.host_id, project_id=body.project_id)


@router.get("/test-runs", response_model=list[m.TestRun])
def list_test_runs(db: Db, base: Base, state: m.TestRunState | None = None, host_id: str | None = None):
    return _rebased(
        test_runs.list_runs(db, state=state, host_id=host_id), base, lambda b, r: weburls.test_run_url(b, r.id)
    )


@router.get("/test-runs/current", response_model=m.TestRun)
def current_test_run(db: Db, base: Base):
    run = test_runs.current(db)
    if run is None:
        raise HTTPException(404, "no active test run")
    return _rebased(run, base, lambda b, r: weburls.test_run_url(b, r.id))


@router.get("/test-runs/{run_id}", response_model=m.TestRun)
def get_test_run(run_id: str, db: Db, base: Base):
    return _rebased(test_runs.get(db, run_id), base, lambda b, r: weburls.test_run_url(b, r.id))


@router.post("/test-runs/{run_id}/step", response_model=m.TestRun)
def step_test_run(run_id: str, body: TestStepIn, db: Db):
    return test_runs.step(db, run_id, body.text)


@router.post("/test-runs/{run_id}/finish", response_model=m.TestRun)
def finish_test_run(run_id: str, body: TestFinishIn, db: Db):
    return test_runs.finish(db, run_id, body.result, note=body.note)


@router.post("/test-runs/{run_id}/clear", response_model=m.TestRun)
def clear_test_run(run_id: str, db: Db):
    return test_runs.clear(db, run_id)


# ------------------------------------------------------------------- jobs
class JobEnqueueIn(BaseModel):
    kind: str | None = Field(default=None, description="test | demo | final")
    title: str | None = Field(default=None, description="Human name (HUD strip / banner)")
    target: str | None = Field(default=None, description="What is under test: feature/test + branch@sha")
    purpose: str | None = Field(default=None, description="One sentence: why this run exists")
    expected: str | None = Field(default=None, description="Pass criterion (observable)")
    host_id: str | None = Field(default=None, description="Host to queue on; or use host_choice='ask'")
    host_choice: str | None = Field(default=None, description="'ask' = stays queued + unassigned")
    max_minutes: int | None = Field(default=None, description="Cap; >5 min needs a reason (test/demo)")
    reason: str | None = Field(default=None, description="Why the run may exceed the 5-minute default")
    needs_confirm: bool = Field(default=False, description="True = the human iso_confirm gate decides pass/fail")
    screenshots: str = Field(default="each_step", description="each_step | on_request | none")
    watching: bool = Field(default=False, description="The user is watching this run live")
    branch: str | None = None
    commit: str | None = None
    cleanup: list[str] | None = Field(default=None, description="Things that must be gone at the end")
    requested_by: str | None = Field(default=None, description="Coordinator session id")
    project_id: str | None = None


class JobAssignIn(BaseModel):
    host_id: str
    position: int | None = None


class JobStartIn(BaseModel):
    worker: str | None = Field(default=None, description="Subagent id / branch running it")


class JobEventIn(BaseModel):
    level: str
    message: str = ""
    phase: str | None = None
    data: dict | None = None


class JobLinkIn(BaseModel):
    kind: str
    ref_id: str


class JobVerdictIn(BaseModel):
    verdict: m.Verdict = Field(description="pass | fail | skip — the human's call on the current step")
    notes: str | None = Field(default=None, description="Free-text note recorded with the verdict")
    step: str | None = Field(default=None, description="Step id/phase the verdict answers (optional)")
    source: str = Field(default="web", description="chat | console | key | web | coordinator")


class JobScreenshotIn(BaseModel):
    note: str | None = None


class JobFinishIn(BaseModel):
    status: str
    summary: dict | None = None


class JobRestatusIn(BaseModel):
    status: str = Field(description="A terminal status: passed | failed | error | cancelled | inconclusive")
    reason: str | None = Field(
        default=None, description="Why the ledger is being corrected (required; recorded on the timeline)"
    )


class JobCancelIn(BaseModel):
    reason: str | None = None


@router.post("/jobs", response_model=m.Job, status_code=201)
def enqueue_job(body: JobEnqueueIn, db: Db):
    return jobs.enqueue(db, **body.model_dump())


@router.get("/jobs", response_model=list[m.Job])
def list_jobs(
    db: Db,
    base: Base,
    status: Annotated[str | None, Query(description="A status, or 'active' = queued+running")] = None,
    host_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return _rebased(
        jobs.list_jobs(db, status=status, host_id=host_id, limit=limit), base, lambda b, r: weburls.job_url(b, r.id)
    )


@router.get("/jobs/next", response_model=m.Job)
def next_job(db: Db, host_id: Annotated[str, Query(description="Host whose queue to read")]):
    """Head queued job for a host, only when that host's lease is free (409 + holder otherwise)."""
    return jobs.next_job(db, host_id)


@router.get("/jobs/{job_id}", response_model=m.Job)
def get_job(job_id: str, db: Db, base: Base):
    return _rebased(jobs.get(db, job_id), base, lambda b, r: weburls.job_url(b, r.id))


@router.get("/jobs/{job_id}/screenshots", response_model=list[m.JobScreenshot])
def job_screenshots(
    job_id: str,
    db: Db,
    base: Base,
    step: Annotated[str | None, Query(description="Filter by step id ('s4'); 'unassigned' = untagged shots")] = None,
    reason: Annotated[str | None, Query(description="ready | periodic | verdict | request | verify")] = None,
):
    """The job's screenshots tied to their step and verdict, with the fields a reviewer needs."""
    shots = jobs.screenshots(db, job_id, step=step, reason=reason)
    for s in shots:
        s.web_url = weburls.screenshot_url(base, job_id)
    return shots


@router.post("/jobs/{job_id}/assign", response_model=m.Job)
def assign_job(job_id: str, body: JobAssignIn, db: Db):
    return jobs.assign(db, job_id, body.host_id, position=body.position)


@router.post("/jobs/{job_id}/start", response_model=m.Job)
def start_job(job_id: str, body: JobStartIn, db: Db):
    return jobs.start(db, job_id, worker=body.worker)


@router.post("/jobs/{job_id}/event", response_model=m.JobEvent)
def job_event(job_id: str, body: JobEventIn, db: Db):
    return jobs.event(db, job_id, level=body.level, message=body.message, phase=body.phase, data=body.data)


@router.put("/jobs/{job_id}/artifacts/{name}", response_model=m.JobArtifact)
async def put_job_artifact(job_id: str, name: str, request: Request, db: Db):
    """Store a raw-body artifact (append-only, <= 25 MB, name `^[A-Za-z0-9._-]{1,120}$`).

    An image upload may carry the shot's event tag as ``?event=<json>`` (``{"message": str,
    "data": {...}}``); the server's single auto ``screenshot`` event then carries that tag, so a
    client that uploads tagged does NOT also POST its own event (one event per shot).
    """
    content = await request.body()
    raw_event = request.query_params.get("event")
    image_event = None
    if raw_event:
        try:
            image_event = json.loads(raw_event)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, detail="event query param must be JSON") from exc
        if not isinstance(image_event, dict):
            raise HTTPException(422, detail="event query param must be a JSON object")
    return jobs.save_artifact(db, job_id, name, content,
                              request.headers.get("content-type", ""), image_event=image_event)


@router.get("/artifacts/{job_id}/{name}")
def get_job_artifact(job_id: str, name: str, db: Db):
    data, ctype = jobs.read_artifact(db, job_id, name)
    headers = None
    if not ctype.startswith("image/"):
        # Non-image artifacts (logs, json, crash dumps, zips) are downloads, not inline views.
        headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    return Response(content=data, media_type=ctype, headers=headers)


@router.post("/jobs/{job_id}/link", response_model=m.Job)
def link_job(job_id: str, body: JobLinkIn, db: Db):
    return jobs.link(db, job_id, kind=body.kind, ref_id=body.ref_id)


@router.post("/jobs/{job_id}/verdict", response_model=m.Job)
def job_verdict(job_id: str, body: JobVerdictIn, db: Db):
    """Answer the running job's current step with a human verdict (the webapp's Pass/Fail/Skip buttons).

    Writes a `human-confirm` evidence row through the same path as `POST /api/evidence` (so the confirm
    push files fire) and appends a `milestone` event. 409 when the job is not running or links no test run.
    """
    return jobs.verdict(db, job_id, verdict=body.verdict, notes=body.notes, step=body.step, source=body.source)


@router.post("/jobs/{job_id}/screenshot-request", response_model=m.Job)
def request_job_screenshot(job_id: str, body: JobScreenshotIn, db: Db):
    return jobs.request_screenshot(db, job_id, note=body.note)


@router.post("/jobs/{job_id}/finish", response_model=m.Job)
def finish_job(job_id: str, body: JobFinishIn, db: Db):
    """Close out a queued/running job. `status='inconclusive'` = the run ended with no pass/fail verdict."""
    return jobs.finish(db, job_id, status=body.status, summary=body.summary)


@router.post("/jobs/{job_id}/restatus", response_model=m.Job)
def restatus_job(job_id: str, body: JobRestatusIn, db: Db):
    """Correct a FINISHED job's terminal status (terminal -> terminal). `reason` is mandatory and is
    recorded as a `milestone` timeline event, so the correction is auditable. 409 for a non-terminal
    job or a no-op change; 422 for a non-terminal target status or a missing reason."""
    return jobs.restatus(db, job_id, status=body.status, reason=body.reason)


@router.post("/jobs/{job_id}/cancel", response_model=m.Job)
def cancel_job(job_id: str, body: JobCancelIn, db: Db):
    return jobs.cancel(db, job_id, reason=body.reason)


# ------------------------------------------------------------- prompts
class PromptArgOut(BaseModel):
    name: str
    description: str
    required: bool


class PromptOut(BaseModel):
    name: str
    title: str
    description: str
    arguments: list[PromptArgOut]


class RenderedPrompt(BaseModel):
    name: str
    text: str


@router.get("/prompts", response_model=list[PromptOut])
def list_prompts():
    return [
        PromptOut(
            name=p.name,
            title=p.title,
            description=p.description,
            arguments=[PromptArgOut(name=a.name, description=a.description, required=a.required) for a in p.arguments],
        )
        for p in PROMPTS.values()
    ]


@router.post("/prompts/{name}/render", response_model=RenderedPrompt)
def render(name: str, repo: Repo, arguments: Annotated[dict[str, str], Body(embed=True)]):
    if name not in PROMPTS:
        raise HTTPException(404, f"unknown prompt {name!r}")
    return RenderedPrompt(name=name, text=render_prompt(repo, name, arguments))
