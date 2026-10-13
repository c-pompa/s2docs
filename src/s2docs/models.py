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
