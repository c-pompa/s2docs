"""HTTP API routes. Thin layer: validate params, call Repository, return models."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from s2docs import builds as build_svc
from s2docs import deploys, evidence, hosts, test_runs
from s2docs import models as m
from s2docs.build.validate_cfg import validate_cfg
from s2docs.build.validate_script import validate_cs_script
from s2docs.config import GAMES
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


# ---------------------------------------------------------------- meta
@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
def list_projects(orch: Orch, status: Literal["active", "done", "abandoned"] | None = None):
    return orch.list(status)


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
def get_project(project_id: str, orch: Orch):
    return orch.get(project_id)


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


@router.get("/hosts", response_model=list[m.Host])
def list_hosts(db: Db):
    return hosts.list_hosts(db)


@router.get("/hosts/{host_id}", response_model=m.Host)
def get_host(host_id: str, db: Db):
    return hosts.get_host(db, host_id)


@router.put("/hosts/{host_id}", response_model=m.Host)
def register_host(host_id: str, body: HostIn, db: Db):
    return hosts.register_host(db, host_id, **body.model_dump())


@router.post("/hosts/{host_id}/probe", response_model=m.Host)
def probe_host(host_id: str, db: Db):
    return hosts.probe_host(db, host_id)


@router.post("/hosts/{host_id}/lock", response_model=m.HostLock)
def claim_host(host_id: str, body: LockIn, db: Db):
    return hosts.claim_host(db, host_id, body.owner, ttl_sec=body.ttl_sec, force=body.force)


@router.post("/hosts/{host_id}/heartbeat", response_model=m.HostLock)
def heartbeat_host(host_id: str, db: Db, owner: Annotated[str, Body(embed=True)]):
    return hosts.heartbeat_host(db, host_id, owner)


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
def list_builds(db: Db, host_id: str | None = None, active: bool = False):
    return build_svc.list_runs(db, host_id=host_id, active=active)


@router.get("/builds/{run_id}", response_model=m.BuildRun)
def get_build(run_id: str, db: Db):
    return build_svc.get_run(db, run_id)


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
    )


@router.get("/deploys", response_model=list[m.Deployment])
def list_deploys(db: Db, host_id: str | None = None):
    return deploys.list_deployments(db, host_id=host_id)


@router.get("/deploys/{deploy_id}", response_model=m.Deployment)
def get_deploy(deploy_id: str, db: Db):
    return deploys.get_deployment(db, deploy_id)


@router.post("/deploys/{deploy_id}/verify", response_model=m.Deployment)
def verify_deploy(deploy_id: str, db: Db):
    return deploys.verify(db, deploy_id)


# ---------------------------------------------------------------- evidence
class EvidenceIn(BaseModel):
    project_id: str
    step_id: str | None = None
    kind: m.EvidenceKind
    ref: str | None = None
    data: dict = Field(default_factory=dict)


@router.post("/evidence", response_model=m.Evidence, status_code=201)
def add_evidence(body: EvidenceIn, db: Db):
    return evidence.add(
        db, kind=body.kind, project_id=body.project_id, step_id=body.step_id, ref=body.ref, data=body.data
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
def list_test_runs(db: Db, state: m.TestRunState | None = None):
    return test_runs.list_runs(db, state=state)


@router.get("/test-runs/current", response_model=m.TestRun)
def current_test_run(db: Db):
    run = test_runs.current(db)
    if run is None:
        raise HTTPException(404, "no active test run")
    return run


@router.get("/test-runs/{run_id}", response_model=m.TestRun)
def get_test_run(run_id: str, db: Db):
    return test_runs.get(db, run_id)


@router.post("/test-runs/{run_id}/step", response_model=m.TestRun)
def step_test_run(run_id: str, body: TestStepIn, db: Db):
    return test_runs.step(db, run_id, body.text)


@router.post("/test-runs/{run_id}/finish", response_model=m.TestRun)
def finish_test_run(run_id: str, body: TestFinishIn, db: Db):
    return test_runs.finish(db, run_id, body.result, note=body.note)


@router.post("/test-runs/{run_id}/clear", response_model=m.TestRun)
def clear_test_run(run_id: str, db: Db):
    return test_runs.clear(db, run_id)


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
