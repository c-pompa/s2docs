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
    name: str = Field(description="Fully-qualified member name, e.g. 'C_CSPlayerPawn.m_hActiveMinimapVolume'")
    change: Literal["added", "removed", "changed"]
    before: str | None = None
    after: str | None = None
    group: str | None = Field(
        default=None,
        description="Parent key when the section diffs members (class/entity/event/library); None for flat sections",
    )


class BuildDiff(BaseModel):
    game: str
    from_build: int
    to_build: int
    sections: dict[str, list[DiffEntry]]
    totals: dict[str, int] = Field(
        default_factory=dict,
        description="Full change count per section, before the per-section `limit` truncation",
    )
    counts: dict[str, dict[str, int]] = Field(
        default_factory=dict,
        description=(
            "Per-section change-type breakdown `{section: {'added': n, 'removed': n, 'changed': n}}`, "
            "counted BEFORE the per-section `limit` cap — exact even when `sections` is truncated. "
            "`totals[section]` is the sum of that section's three counts."
        ),
    )
    limit: int = Field(default=500, description="Row cap applied to each section")


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
    web_url: str | None = Field(
        default=None, description="Absolute URL of this project's page in the web UI (Orchestration)"
    )


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
    web_url: str | None = Field(
        default=None, description="Absolute URL of this project's page in the web UI (Orchestration)"
    )


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
    auth_required: bool = Field(
        default=False, description="True when S2DOCS_AUTH_TOKEN is set (mutating /api and /mcp need a token)"
    )
    public_url: str = Field(
        default="", description="Base URL of the web UI (S2DOCS_PUBLIC_URL, else LAN fallback)"
    )
    confirms_jsonl: str = Field(
        default="", description="Append-only confirm journal a waiting agent watches (data_dir/confirms.jsonl)"
    )
    latest_confirm: str = Field(
        default="", description="Atomically-rewritten newest human confirm (data_dir/latest_confirm.json)"
    )
    job_events_jsonl: str = Field(
        default="", description="Append-only job-event journal a waiting agent watches (data_dir/job_events.jsonl)"
    )


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
    web_url: str | None = Field(
        default=None, description="Absolute URL of this run's page in the web UI (Activity page)"
    )


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
    owner: str | None = Field(
        default=None,
        description="host-lock owner effective for this push: 'deploy:<id>' when the server took the "
        "lease itself, else the caller's owner whose lease was reused",
    )
    notes: str | None = None
    created_at: str
    updated_at: str
    web_url: str | None = Field(
        default=None, description="Absolute URL of this deployment's page in the web UI (Activity page)"
    )


# ------------------------------------------------------------------ evidence
EvidenceKind = Literal["console_transcript", "file_hash", "pixel_diff", "screenshot", "note", "other", "human-confirm"]
EVIDENCE_KINDS: tuple[str, ...] = EvidenceKind.__args__  # type: ignore[attr-defined]

# A human verdict on a step. `pass`/`fail` are the original two; `skip` was added so a demo driver can
# move past a step that cannot be exercised (a parallel director worker sends it).
Verdict = Literal["pass", "fail", "skip"]
VERDICTS: tuple[str, ...] = Verdict.__args__  # type: ignore[attr-defined]

# Where a verdict came from: in-game chat (`!pass NOTE`, the director posts source='chat'), the game
# console, a key bind, the webapp, or the coordinator relaying a verdict the user typed in Hermes chat
# ('coordinator'). 'director' is the tag the in-game confirm gate's own producer already used.
ConfirmSource = Literal["chat", "console", "key", "web", "coordinator", "director"]
CONFIRM_SOURCES: tuple[str, ...] = ConfirmSource.__args__  # type: ignore[attr-defined]


class Evidence(BaseModel):
    id: str
    project_id: str | None = Field(
        default=None,
        description="Owning project; None when the row is anchored to a test run with no project",
    )
    step_id: str | None = Field(default=None, description="Owning step; None = project-level evidence")
    kind: EvidenceKind
    ref: str | None = Field(default=None, description="Path / url / command the evidence points at")
    data: dict = Field(default_factory=dict, description="Structured detail (md5s, pixel diff pct, transcript excerpt)")
    test_run_id: str | None = Field(
        default=None, description="Test run this evidence records (a human-confirm verdict, a transcript, ...)"
    )
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
    web_url: str | None = Field(
        default=None, description="Absolute URL of this run's page in the web UI (Activity page)"
    )


# --------------------------------------------------------------------- jobs
# A job is the one record for a requested run: kind, target, host, intake, a timeline, artifacts and a
# mandatory close-out summary. It is a ledger + queue, never a host lock (see docs/TEST_JOBS.md).
JobKind = Literal["test", "demo", "final"]
JOB_KINDS: tuple[str, ...] = JobKind.__args__  # type: ignore[attr-defined]

# 'inconclusive' is terminal and means "the run ended without a pass/fail verdict" — a step-gated demo
# the user stopped answering, or a driver with no verdict to report. It is NOT a pass: a run with
# pass=0/fail=0/no-verdict=1 closes out inconclusive so the ledger never claims a verdict nobody made.
JobStatus = Literal["queued", "running", "passed", "failed", "error", "cancelled", "inconclusive"]
JOB_STATUSES: tuple[str, ...] = JobStatus.__args__  # type: ignore[attr-defined]
ACTIVE_JOB_STATUSES: tuple[str, ...] = ("queued", "running")
TERMINAL_JOB_STATUSES: tuple[str, ...] = ("passed", "failed", "error", "cancelled", "inconclusive")

# Who asked for the run (migration 0014). The learning loop scopes to this: 'human' is a person
# running or watching a demo, 'driver' the verify-demo driver, 'eval' a golden-question suite,
# 'probe' ad-hoc diagnostics and 'warmup' a post-deploy smoke check — those three are synthetic
# traffic and must never inflate a learning artifact. 'agent' is Hermes' own work and is treated as
# real usage. Recorded at intake, validated there, never inferred later.
JobOrigin = Literal["human", "agent", "driver", "eval", "probe", "warmup"]
JOB_ORIGINS: tuple[str, ...] = JobOrigin.__args__  # type: ignore[attr-defined]
DEFAULT_JOB_ORIGIN = "agent"

JobEventLevel = Literal["info", "warn", "error", "crash", "screenshot", "milestone", "learning"]
JOB_EVENT_LEVELS: tuple[str, ...] = JobEventLevel.__args__  # type: ignore[attr-defined]

JobLinkKind = Literal["test_run", "build", "deploy", "evidence", "project"]
JOB_LINK_KINDS: tuple[str, ...] = JobLinkKind.__args__  # type: ignore[attr-defined]


class JobSummary(BaseModel):
    """The mandatory close-out summary: what was asked, why, what we learned, what is next."""

    what: str
    why: str
    learned: str
    next: str


class JobEvent(BaseModel):
    id: int
    job_id: str
    level: JobEventLevel
    phase: str | None = None
    message: str
    data: dict = Field(default_factory=dict, description="JSON detail: log_tail, exit_code, artifact url, md5, ...")
    created_at: str


class JobLink(BaseModel):
    kind: JobLinkKind
    ref_id: str


class JobArtifact(BaseModel):
    name: str
    url: str = Field(description="REST path that serves the bytes, e.g. '/api/artifacts/j-1a2b3c4d/hud.png'")
    md5: str
    size: int


class StepVerdict(BaseModel):
    """The verdict that answered a step prompt: which way, from where, and the evidence row it wrote."""

    verdict: str
    source: str | None = None
    notes: str | None = None
    evidence_id: str | None = Field(default=None, description="The human-confirm evidence row this verdict wrote")
    at: str = Field(description="When the verdict milestone event landed (created_at)")


class JobCurrentStep(BaseModel):
    """The step the runner is waiting on: the newest 'info' prompt event (+phase) and whether it is answered.

    A step prompt is an ``info`` event whose ``data`` carries ``prompt``; the driver posts one per step
    with ``phase`` = the step id and ``data {prompt, look_for, step_index, step_total, respond_hint,
    deadline_utc}``. ``verdict`` is set once a *verdict* milestone event (a milestone carrying
    ``data.verdict``, from any source) is newer than the prompt.
    """

    phase: str | None = None
    prompt: str | None = None
    look_for: str | None = None
    step_index: int | None = None
    step_total: int | None = None
    respond_hint: str | None = None
    deadline_utc: str | None = Field(default=None, description="ISO-8601 UTC deadline for the answer")
    asked_at: str = Field(description="When the prompt event landed (created_at)")
    event_id: int = Field(description="The prompt event's id (verdict must be newer than this)")
    verdict: StepVerdict | None = Field(default=None, description="Set once a verdict arrived, from any source")


class Job(BaseModel):
    id: str
    kind: JobKind
    title: str
    target: str
    host_id: str | None = Field(default=None, description="NULL = not yet assigned (host_choice='ask')")
    status: JobStatus
    origin: JobOrigin = Field(
        default="agent",
        description="Who asked for this run (human | agent | driver | eval | probe | warmup) — "
        "scopes the learning loop",
    )
    position: int = 100
    params: dict = Field(default_factory=dict, description="Intake: purpose, expected, caps, flags, pending_shot, ...")
    requested_by: str | None = None
    worker: str | None = None
    phase: str = ""
    project_id: str | None = None
    summary: JobSummary | None = Field(default=None, description="Present once the job is closed out")
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str
    error_count: int = Field(default=0, description="error-level events (the queue board flags these)")
    crash_count: int = Field(default=0, description="crash-level events (the queue board flags these)")
    current_step: JobCurrentStep | None = Field(
        default=None, description="The step awaiting a verdict (newest prompt event + whether it is answered)"
    )
    events: list[JobEvent] = []
    links: list[JobLink] = []
    artifacts: list[JobArtifact] = []
    web_url: str | None = Field(
        default=None, description="Absolute URL of this job's page in the web UI"
    )


# A screenshot tied to a step / verdict, derived from the job's `screenshot` events. The demo driver
# posts those events with data {url, md5, bytes, step, step_index, step_total, reason, seq, verdict,
# full_res, width, height}; older events carry only {url, md5, bytes} and read under 'unassigned'.
ScreenshotReason = Literal["ready", "periodic", "verdict", "request", "verify"]
SCREENSHOT_REASONS: tuple[str, ...] = ScreenshotReason.__args__  # type: ignore[attr-defined]


class ScreenshotVerdict(BaseModel):
    """The verdict a screenshot was captured alongside (a verdict-reason shot)."""

    verdict: str
    source: str | None = None
    confirm_id: str | None = Field(default=None, description="The human-confirm evidence id to match on")
    late: bool = False


class JobScreenshot(BaseModel):
    """One screenshot of a job, tied to its step and (optionally) its verdict."""

    seq: int = Field(description="1-based capture order within the job")
    step: str | None = Field(default=None, description="Step id/phase ('s4'); None = unassigned (older event)")
    step_index: int | None = None
    step_total: int | None = None
    reason: ScreenshotReason | None = Field(default=None, description="Why it was taken; None on older events")
    url: str = Field(description="REST path serving the bytes ('/api/artifacts/<job>/<name>')")
    web_url: str = Field(description="Absolute page URL for this shot (the job page)")
    name: str = Field(description="Artifact file name")
    md5: str | None = None
    bytes: int | None = None
    verdict: ScreenshotVerdict | None = Field(default=None, description="Verdict captured with this shot")
    full_res: bool | None = Field(default=None, description="True when the capture is full resolution")
    width: int | None = None
    height: int | None = None
    created_at: str


# -------------------------------------------------------------- asset catalog
# The catalog is a *view with a promotion step* over builds, deployments and jobs (migration 0015):
# every column that names another record is a link, not a copy, so the bytes and the evidence stay
# owned by the service that made them. `retired_at` is a soft delete: the row and its links survive,
# and the default list hides it.
AssetKind = Literal["model", "action"]
ASSET_KINDS: tuple[str, ...] = AssetKind.__args__  # type: ignore[attr-defined]


class Asset(BaseModel):
    id: str
    kind: AssetKind
    name: str
    slug: str = Field(description="Stable lowercase kebab-case handle, unique across the catalog")
    prompt: str | None = Field(default=None, description="The prompt this asset came from")
    concept_artifact: str | None = Field(default=None, description="Concept image artifact (path or URL)")
    mesh_sha256: str | None = Field(default=None, description="GLB content hash (64 hex)")
    tris: int | None = Field(default=None, description="Triangle count of the source mesh")
    vmdl_path: str | None = None
    vmat_path: str | None = None
    build_id: str = Field(description="The build run that compiled it (promotion requires 'succeeded')")
    deploy_id: str | None = Field(default=None, description="The deployment that shipped these bytes")
    frame_job_id: str | None = Field(default=None, description="The job whose in-game frame proves it renders")
    frame_artifact: str | None = Field(default=None, description="Frame artifact name inside that job")
    licence: str = Field(description="Licence recorded at promotion; never inferred later")
    tags: list[str] = []
    created_by: str | None = None
    created_at: str
    retired_at: str | None = Field(default=None, description="Soft retire: set once, never cleared")
    retired_reason: str | None = None


class AssetProvenance(BaseModel):
    """The records an asset points at, read from their own tables at request time.

    Nothing here is a copy of the asset row: `build` / `deploy` / `job` are the canonical records, so
    a stale catalog cannot invent a compile, a push or a verdict that did not happen.
    """

    asset: Asset
    build: BuildRun | None = None
    deploy: Deployment | None = None
    job: Job | None = None
    frame_url: str | None = Field(
        default=None, description="REST path serving the frame bytes ('/api/artifacts/<job>/<name>')"
    )


LiveStatus = Literal["verified", "sent", "failed", "refused"]


class LiveAction(BaseModel):
    id: str
    host_id: str
    source: str
    text: str | None = None
    intents: dict = Field(description="The validated IntentEnvelope")
    lines: list[str] = []
    status: LiveStatus
    detail: str | None = None
    readback: str | None = Field(default=None, description="Console tail the readback check was judged on")
    request_id: str | None = None
    created_at: str


class LiveRequest(BaseModel):
    id: str
    host_id: str
    player: str | None = None
    text: str
    state: Literal["pending", "done", "failed", "ignored"]
    note: str | None = None
    action_id: str | None = None
    created_at: str


class LiveHostStatus(BaseModel):
    host_id: str
    netcon: str = Field(description="host:port tried")
    reachable: bool
    map: str | None = None
    spawngroups: list[str] = []
    sandbox: bool = Field(description="hermes_sandbox answered hs_ping")
    listening: bool = Field(default=False, description="An s2docs chat listener holds netcon open")
    error: str | None = None
