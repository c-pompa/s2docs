"""Pydantic models shared by the HTTP API, the MCP server and prompts."""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

SearchKind = Literal[
    "convar",
    "command",
    "entity",
    "schema_class",
    "schema_enum",
    "event",
    "script",
    "pulse",
    "example",
    "doc",
]
SEARCH_KINDS: tuple[str, ...] = SearchKind.__args__  # type: ignore[attr-defined]


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class GameInfo(BaseModel):
    id: str
    name: str
    app_id: int | None
    mod_dir: str | None
    current_build: int | None = None
    patch_version: str | None = None
    version_date: str | None = None
    script_api: str | None = None


class BuildInfo(BaseModel):
    build_number: int
    patch_version: str | None
    source_revision: str | None
    version_date: str | None
    source_commit: str | None
    ingested_at: str
    is_current: bool


class SearchHit(BaseModel):
    kind: str
    game: str
    key: str = Field(description="Lookup key for the detail endpoint/tool of this kind")
    name: str
    title: str
    snippet: str | None = None
    score: float


class ConVar(BaseModel):
    name: str
    kind: Literal["convar", "command"]
    default_value: str | None = None
    min_value: str | None = None
    max_value: str | None = None
    flags: list[str] = []
    description: str | None = None


Origin = Literal["runtime", "valve_fgd", "both"]


class EntityMember(BaseModel):
    kind: Literal["keyvalue", "input", "output"]
    name: str
    type: str | None = None
    label: str | None = Field(default=None, description="Hammer display label (Valve FGD)")
    default_value: str | None = Field(default=None, description="Hammer default (Valve FGD)")
    description: str | None = None
    notes: str | None = Field(default=None, description="Runtime dump notes: C++ field, PVAL types, Pulse node")
    component: str | None = None
    choices: list[tuple[str, str]] | None = None
    defined_in: str = Field(description="Entity class that declares this member")
    source: Origin = Field(
        default="runtime",
        description="runtime = registered by the game binary; valve_fgd = only in Valve's Hammer FGD "
        "(editor/compile-time key); both = present in each",
    )
    sort_key: tuple[int, int] = Field(default=(0, 0), exclude=True)


class EntitySummary(BaseModel):
    classname: str
    side: str
    class_type: str
    cpp_class: str | None = None
    description: str | None = None
    bases: list[str] = []
    tool_name: str | None = Field(default=None, description="Name in Hammer's entity tool")
    origin: Origin = "runtime"


class Entity(EntitySummary):
    tool_tip: str | None = None
    tool_group: str | None = None
    fgd_file: str | None = Field(default=None, description="Valve FGD file that defines the class")
    inheritance: list[str] = Field(default=[], description="Runtime ancestor chain, nearest first")
    fgd_inheritance: list[str] = Field(default=[], description="Valve FGD base classes, nearest first")
    members: list[EntityMember] = []


class Example(BaseModel):
    api: str
    name: str
    path: str
    origin: Literal["valve", "community"]
    description: str | None = None
    content: str | None = None


class Doc(BaseModel):
    key: str
    source: str
    title: str
    url: str
    game: str | None = None
    description: str | None = None
    license: str | None = None
    body: str | None = None


class SchemaField(BaseModel):
    name: str
    type: str | None = None
    value: str | None = Field(default=None, description="Enum value, or field default")
    metadata: list[str] = []
    defined_in: str


class SchemaClassSummary(BaseModel):
    name: str
    module: str
    kind: Literal["class", "enum"]
    bases: list[str] = []
    underlying_type: str | None = None
    field_count: int = 0


class SchemaClass(SchemaClassSummary):
    metadata: list[str] = []
    inheritance: list[str] = []
    derived: list[str] = Field(default=[], description="Direct subclasses")
    fields: list[SchemaField] = []


class GameEventField(BaseModel):
    name: str
    type: str
    description: str | None = None


class GameEvent(BaseModel):
    name: str
    source_file: str
    description: str | None = None
    fields: list[GameEventField] = []
    overrides: list[str] = Field(default=[], description="Lower-precedence files that also define this event")


class ScriptSymbol(BaseModel):
    api: str
    name: str
    qualified_name: str
    parent: str | None = None
    kind: str
    signature: str
    doc: str | None = None
    extends: str | None = None
    deprecated: bool = False


class ScriptType(ScriptSymbol):
    members: list[ScriptSymbol] = []
    inherited_from: list[str] = Field(default=[], description="Ancestor types whose members are also available")


class PulseBinding(BaseModel):
    id: int
    module: str
    library: str
    kind: str
    name: str
    signature: str
    friendly_name: str | None = None
    description: str | None = None
    metadata: dict = {}


class DiffEntry(BaseModel):
    name: str
    change: Literal["added", "removed", "changed"]
    before: str | None = None
    after: str | None = None


class BuildDiff(BaseModel):
    game: str
    from_build: int
    to_build: int
    sections: dict[str, list[DiffEntry]]


class Stats(BaseModel):
    schema_version: int
    games: list[GameInfo]
    counts: dict[str, dict[str, int]]


# ------------------------------------------------------------------ build tools
Severity = Literal["error", "warning", "info"]


class Diagnostic(BaseModel):
    line: int
    column: int = 1
    code: str
    severity: Severity
    category: str
    message: str
    hint: str | None = None


class ScriptValidation(BaseModel):
    ok: bool = Field(description="True when there are no errors (warnings/info allowed)")
    errors: int
    warnings: int
    diagnostics: list[Diagnostic]
    instance_methods_used: list[str] = []
    checker: str


class CfgValidation(BaseModel):
    ok: bool
    errors: int
    warnings: int
    commands: int
    diagnostics: list[Diagnostic]


class AddonFile(BaseModel):
    path: str
    size: int


class Addon(BaseModel):
    name: str
    game: str
    root: str = Field(description="Local content root of the addon")
    files: list[AddonFile] = []


# ---------------------------------------------------------------- orchestration
StepStatus = Literal["pending", "in_progress", "done", "skipped", "blocked"]


class WorkflowInputInfo(BaseModel):
    name: str
    description: str
    required: bool = False
    default: str | None = None


class WorkflowInfo(BaseModel):
    id: str
    title: str
    description: str
    games: list[str]
    needs_addon: bool
    inputs: list[WorkflowInputInfo] = []
    steps: list[str] = []


class CustomStep(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}$")
    title: str
    kind: Literal["research", "design", "build", "validate", "test", "document"] = "build"
    instructions: str
    tools: list[str] = []
    depends_on: list[str] = []
    gate: str | None = Field(default=None, description="addon_exists | scripts_typecheck | cfgs_valid | evidence")


class ProjectStep(BaseModel):
    id: str
    title: str
    kind: str
    status: StepStatus
    depends_on: list[str] = []
    gate: str | None = None
    notes: str | None = None
    artifacts: list[str] = []
    started_at: str | None = Field(
        default=None,
        description="When the step was first touched (earliest project event for it); NULL while pending. "
        "updated_at - started_at is the step's duration.",
    )
    updated_at: str


class ProjectEvent(BaseModel):
    step_id: str | None = None
    event: str
    detail: str | None = None
    ts: str


class ProjectSummary(BaseModel):
    id: str
    workflow: str
    title: str
    goal: str
    game: str
    addon: str | None = None
    status: Literal["active", "done", "abandoned"]
    progress: str
    created_at: str
    updated_at: str


class Project(BaseModel):
    id: str
    workflow: str
    title: str
    goal: str
    game: str
    addon: str | None = None
    inputs: dict[str, str] = {}
    status: Literal["active", "done", "abandoned"]
    created_at: str
    updated_at: str
    steps: list[ProjectStep] = []
    recent_events: list[ProjectEvent] = []


class StepBrief(BaseModel):
    step_id: str
    title: str
    kind: str
    status: StepStatus
    instructions: str
    tools: list[str] = []
    prompt_name: str | None = None
    prompt_text: str | None = Field(default=None, description="Rendered prompt for this step (grounded in live data)")
    gate: str | None = None
    gate_description: str | None = None
    done_when: str | None = None


class ProjectBrief(BaseModel):
    project_id: str
    title: str
    goal: str
    game: str
    addon: str | None = None
    status: str
    progress: str
    next_steps: list[StepBrief] = []
    blocked_steps: list[str] = []
    guidance: str


# --------------------------------------------------------------------- hosts
class HostLock(BaseModel):
    host_id: str
    owner: str
    acquired_at: str
    heartbeat_at: str
    ttl_sec: int
    fresh: bool = Field(description="heartbeat within ttl (False = stale, can be taken over)")
    age_sec: int = Field(description="seconds since the last heartbeat")
    phase: str = Field(
        default="", description="what the owner is doing ('', 'compile', 'test', ...); '' = not reported"
    )
    phase_since: str | None = Field(default=None, description="when the current phase started (NULL when no phase)")
    phase_age_sec: int | None = Field(default=None, description="seconds since the phase was last changed")
    note: str | None = Field(default=None, description="free text from the owner, e.g. the current step")
    stuck: bool = Field(
        default=False,
        description="fresh heartbeat but the phase has not moved past the server's threshold (liveness != progress)",
    )
    stuck_reason: str | None = Field(default=None, description="why the server marked this lock stuck")


class HostLockEvent(BaseModel):
    """One host-lock mutation (claim/takeover/release/phase change), for the Hosts page's lock history."""

    id: int
    host_id: str
    owner: str
    event: Literal["claimed", "released", "phase"]
    phase: str | None = None
    detail: str | None = None
    ts: str


class Host(BaseModel):
    id: str
    hostname: str | None = None
    ssh_target: str
    ssh_key: str | None = None
    shell: Literal["cmd", "powershell"] | None = None
    os: str | None = None
    facts: dict = Field(default_factory=dict, description="Probed facts (gpu, ram, node_path, cs2_path, ...)")
    probed_at: str | None = None
    notes: str | None = None
    lock: HostLock | None = None


class ServerInfo(BaseModel):
    """DB identity of a running server (REST /api/health and MCP server_info return the same shape)."""

    status: str = "ok"
    db_path: str
    data_dir: str
    pid: int
    started_at: str
    version: str


class LockSummary(BaseModel):
    """A run's view of the host lock it holds — enough for the Activity page to show STUCK/phase.

    Attached to a build/test-run *only when the lock's owner is that run* (build:<id> / test:<id>);
    a run whose lease was taken over by someone else carries no summary (the lock is not its own).
    """

    owner: str
    phase: str = ""
    phase_age_sec: int | None = None
    stuck: bool = False
    stuck_reason: str | None = None


# -------------------------------------------------------------------- builds
# 'interrupted' = the run was still queued/running when the server restarted and no live watcher
# owns it any more (an orphaned run finalized by reconciles at startup).
BuildStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
TERMINAL_BUILD_STATUSES: tuple[str, ...] = ("succeeded", "failed", "cancelled", "interrupted")


class BuildArtifact(BaseModel):
    path: str
    md5: str | None = None
    size: int | None = None


class MaterialGuard(BaseModel):
    """Structured material warnings from a build log (the 'referencing missing material' class).

    A rebuild whose meshes point at reserved/invalid material paths still *finishes* — the
    resourcecompiler reports 'referencing missing material' but exit code 0 — and the map error-renders
    as a red/black checkerboard. This makes those buried lines a first-class field.
    """

    clean: bool = True
    map_name: str | None = None
    counts: dict[str, int] = Field(default_factory=dict, description="Warnings per kind")
    paths: dict[str, list[str]] = Field(default_factory=dict, description="First few offending paths per kind")


class BuildRun(BaseModel):
    id: str
    host_id: str
    kind: str = "build"
    command: str
    status: BuildStatus
    owner: str = Field(description="Host-lock owner holding this run's lease, 'build:<id>'")
    handle: str | None = Field(default=None, description="Process handle (pid) when known")
    notes: str | None = None
    log_path: str | None = None
    exit_code: int | None = None
    summary: dict = Field(default_factory=dict, description="Parsed counts from the log summary line")
    warnings: MaterialGuard | None = Field(
        default=None, description="Material-guard findings; None when the log was clean or unscanned"
    )
    artifacts: list[BuildArtifact] = []
    lock: LockSummary | None = Field(
        default=None, description="The host lock this run holds (owner build:<id>), when it is still its own"
    )
    created_at: str
    updated_at: str


class BuildLogTail(BaseModel):
    run_id: str
    lines: list[str]
    encoding: str = Field(description="Log text encoding detected on read (utf-16 or utf-8)")
    size: int = Field(description="Log file size in bytes")
    truncated: bool = Field(default=False, description="True when only the tail was returned")
    warnings: MaterialGuard | None = Field(
        default=None, description="Material-guard findings over the whole log; None when clean"
    )


# --------------------------------------------------------------- deployments
DeployStatus = Literal["pushed", "verified", "failed"]


class DeployFile(BaseModel):
    path: str = Field(description="Destination path relative to the deployment root")
    local: str | None = Field(default=None, description="Local source file (hashed/sized on push)")
    md5: str | None = Field(default=None, description="Expected MD5 when no local file is available")
    size: int | None = None


class DeployEntry(BaseModel):
    path: str = Field(description="Path relative to the deployment root")
    size: int | None = None
    md5: str | None = Field(default=None, description="MD5 recorded at push time")
    verified: bool = Field(default=False, description="Last verify matched the recorded MD5")


class Deployment(BaseModel):
    id: str
    host_id: str
    addon: str | None = None
    dest_root: str
    entries: list[DeployEntry] = []
    status: DeployStatus
    notes: str | None = None
    created_at: str
    updated_at: str


# ------------------------------------------------------------------ evidence
EvidenceKind = Literal["console_transcript", "file_hash", "pixel_diff", "screenshot", "note", "other"]
EVIDENCE_KINDS: tuple[str, ...] = EvidenceKind.__args__  # type: ignore[attr-defined]


class Evidence(BaseModel):
    id: str
    project_id: str
    step_id: str | None = Field(default=None, description="Owning step; None = project-level evidence")
    kind: EvidenceKind
    ref: str | None = Field(default=None, description="Path / url / command the evidence points at")
    data: dict = Field(default_factory=dict, description="Structured detail (md5s, pixel diff pct, transcript excerpt)")
    created_at: str


# ----------------------------------------------------------------- test runs
TestRunState = Literal["idle", "running", "pass", "fail"]


class TestRun(BaseModel):
    """A server-side in-game test session, mirroring the iso-spectate TEST STATUS HUD strip fields."""

    id: str
    host_id: str | None = None
    project_id: str | None = None
    name: str
    step: str = ""
    state: TestRunState
    elapsed_sec: int = Field(default=0, description="Seconds since started_at (frozen at finished_at once terminal)")
    note: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    lock: LockSummary | None = Field(
        default=None, description="The host lock this run holds (owner test:<id>), when it is still its own"
    )
    created_at: str
    updated_at: str
