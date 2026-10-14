"""Orchestration engine: run workflows as persistent projects that an MCP client drives step by step.

The server owns the plan and the quality gates; the client's model does the work:

    start_project ─▶ brief(next ready steps, with rendered prompt text + tool list)
         ▲                                │ agent works with the named tools
         └── update_step(done, notes) ◀───┘ server checks gate (e.g. re-typechecks every script)
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from string import Template

from s2docs import deploys, evidence, hosts
from s2docs import models as m
from s2docs.build import workspace as ws
from s2docs.build.validate_cfg import validate_cfg
from s2docs.build.validate_script import validate_cs_script
from s2docs.config import GAMES
from s2docs.db.connection import Database
from s2docs.db.repository import NotFound, Repository
from s2docs.orchestration.workflows import GATES, WORKFLOWS, StepSpec, WorkflowSpec
from s2docs.prompts import PromptError, render_prompt

STATUSES = ("pending", "in_progress", "done", "skipped", "blocked")
_SETTLED = ("done", "skipped")
# Deploy ids are 'd-' + 8 hex chars (deploys.push); artifacts carry them so the deploy_verified gate
# knows which deployment to check.
_DEPLOY_ID_RE = re.compile(r"^d-[0-9a-f]{8}$")


class OrchestrationError(ValueError):
    pass


class GateFailed(OrchestrationError):
    pass


def slug_addon(goal: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", goal.lower()).strip("_")[:40].strip("_")
    if not s or not s[0].isalpha():
        s = f"addon_{s}".strip("_")
    return s if ws.ADDON_NAME_RE.match(s) else "addon_project"


def workflow_info(w: WorkflowSpec) -> m.WorkflowInfo:
    return m.WorkflowInfo(
        id=w.id,
        title=w.title,
        description=w.description,
        games=list(w.games),
        needs_addon=w.needs_addon,
        inputs=[
            m.WorkflowInputInfo(name=i.name, description=i.description, required=i.required, default=i.default)
            for i in w.inputs
        ],
        steps=[f"{s.id}: {s.title}" for s in w.steps],
    )


class Orchestrator:
    def __init__(self, db: Database, workspace: Path):
        self.db = db
        self.workspace = workspace

    # --------------------------------------------------------------- start
    def start(
        self,
        workflow: str,
        goal: str,
        *,
        game: str = "cs2",
        addon: str | None = None,
        inputs: dict[str, str] | None = None,
        custom_steps: list[m.CustomStep] | None = None,
        title: str | None = None,
    ) -> m.ProjectBrief:
        if game not in GAMES:
            raise OrchestrationError(f"unknown game {game!r}")
        if not goal.strip():
            raise OrchestrationError("goal is required")
        if workflow == "custom":
            if not custom_steps:
                raise OrchestrationError("workflow 'custom' needs `steps`")
            spec = WorkflowSpec(
                id="custom",
                title=title or "Custom workflow",
                description=goal,
                games=(game,),
                steps=tuple(
                    StepSpec(
                        id=s.id,
                        title=s.title,
                        kind=s.kind,
                        instructions=s.instructions,
                        tools=tuple(s.tools),
                        depends_on=tuple(s.depends_on),
                        gate=s.gate,
                    )
                    for s in custom_steps
                ),
                needs_addon=bool(addon),
            )
            ids = [s.id for s in spec.steps]
            if len(ids) != len(set(ids)):
                raise OrchestrationError("custom step ids must be unique")
            for s in spec.steps:
                if not set(s.depends_on) <= set(ids):
                    raise OrchestrationError(f"step {s.id}: unknown dependency")
                if s.gate and s.gate not in GATES:
                    raise OrchestrationError(f"step {s.id}: unknown gate {s.gate!r}; known: {', '.join(GATES)}")
        else:
            spec = WORKFLOWS.get(workflow)  # type: ignore[assignment]
            if spec is None:
                raise OrchestrationError(f"unknown workflow {workflow!r}; known: {', '.join(WORKFLOWS)}, custom")
            if game not in spec.games:
                raise OrchestrationError(f"workflow {workflow} supports {', '.join(spec.games)}")

        values = {i.name: i.default or "" for i in spec.inputs}
        values.update({k: v for k, v in (inputs or {}).items() if v})
        missing = [i.name for i in spec.inputs if i.required and not values.get(i.name)]
        if missing:
            raise OrchestrationError(f"missing inputs: {missing}")
        if spec.needs_addon:
            addon = addon or slug_addon(goal)
            ws.addon_root(self.workspace, game, addon)  # validates the name
        values.update(goal=goal, game=game, game_name=GAMES[game].name, addon=addon or "")

        pid = "p-" + secrets.token_hex(4)
        # Available to step instructions as $project_id, so a workflow can tell the agent which owner
        # to claim a host as (e.g. owner 'project:<id>') and gates can re-derive it.
        values["project_id"] = pid
        with self.db.writer() as conn, conn:
            conn.execute(
                "INSERT INTO projects (id, workflow, title, goal, game, addon, inputs) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, spec.id, title or spec.title, goal, game, addon, json.dumps(values)),
            )
            for i, s in enumerate(spec.steps):
                prompt = None
                if s.prompt:
                    prompt = {
                        "name": s.prompt["name"],
                        "args": {k: Template(v).safe_substitute(values) for k, v in s.prompt["args"].items()},
                    }
                conn.execute(
                    "INSERT INTO project_steps (project_id, step_id, ordinal, title, kind, instructions, tools, "
                    "prompt, depends_on, gate, done_when) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        pid,
                        s.id,
                        i,
                        s.title,
                        s.kind,
                        Template(s.instructions).safe_substitute(values),
                        json.dumps(list(s.tools)),
                        json.dumps(prompt) if prompt else None,
                        json.dumps(list(s.depends_on)),
                        s.gate,
                        s.done_when,
                    ),
                )
            self._event(conn, pid, None, "started", f"workflow={spec.id} addon={addon}")
        return self.brief(pid)

    # ----------------------------------------------------------- read side
    def _load(self, pid: str) -> m.Project:
        with self.db.reader() as conn:
            p = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
            if p is None:
                raise NotFound(f"no project {pid!r}")
            steps = conn.execute("SELECT * FROM project_steps WHERE project_id = ? ORDER BY ordinal", (pid,)).fetchall()
            events = conn.execute(
                "SELECT step_id, event, detail, ts FROM project_events WHERE project_id = ? ORDER BY id DESC LIMIT 20",
                (pid,),
            ).fetchall()
            # Per-step start = the step's earliest event: the row itself only carries updated_at, so the
            # events table is where a step's duration (updated_at - started_at) comes from.
            firsts = {
                r["step_id"]: r["started"]
                for r in conn.execute(
                    "SELECT step_id, MIN(ts) AS started FROM project_events "
                    "WHERE project_id = ? AND step_id IS NOT NULL GROUP BY step_id",
                    (pid,),
                ).fetchall()
            }
        return m.Project(
            id=p["id"],
            workflow=p["workflow"],
            title=p["title"],
            goal=p["goal"],
            game=p["game"],
            addon=p["addon"],
            inputs=json.loads(p["inputs"]),
            status=p["status"],
            created_at=p["created_at"],
            updated_at=p["updated_at"],
            steps=[
                m.ProjectStep(
                    id=s["step_id"],
                    title=s["title"],
                    kind=s["kind"],
                    status=s["status"],
                    depends_on=json.loads(s["depends_on"]),
                    gate=s["gate"],
                    notes=s["notes"],
                    artifacts=json.loads(s["artifacts"]),
                    started_at=firsts.get(s["step_id"]),
                    updated_at=s["updated_at"],
                )
                for s in steps
            ],
            recent_events=[m.ProjectEvent(**dict(e)) for e in events],
        )

    def get(self, pid: str) -> m.Project:
        return self._load(pid)

    def list(self, status: str | None = None) -> list[m.ProjectSummary]:
        with self.db.reader() as conn:
            rows = conn.execute(
                """SELECT p.*, sum(s.status IN ('done', 'skipped')) AS settled, count(s.step_id) AS total
                   FROM projects p LEFT JOIN project_steps s ON s.project_id = p.id
                   WHERE (? IS NULL OR p.status = ?) GROUP BY p.id ORDER BY p.updated_at DESC""",
                (status, status),
            ).fetchall()
        return [
            m.ProjectSummary(
                id=r["id"],
                workflow=r["workflow"],
                title=r["title"],
                goal=r["goal"],
                game=r["game"],
                addon=r["addon"],
                status=r["status"],
                progress=f"{r['settled'] or 0}/{r['total']}",
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def brief(self, pid: str) -> m.ProjectBrief:
        project = self._load(pid)
        status = {s.id: s.status for s in project.steps}
        ready = [
            s
            for s in project.steps
            if s.status in ("pending", "in_progress") and all(status[d] in _SETTLED for d in s.depends_on)
        ]
        with self.db.reader() as conn:
            raw = {
                r["step_id"]: r
                for r in conn.execute("SELECT * FROM project_steps WHERE project_id = ?", (pid,)).fetchall()
            }
        briefs = []
        for s in ready:
            r = raw[s.id]
            prompt = json.loads(r["prompt"]) if r["prompt"] else None
            prompt_text = None
            if prompt:
                prompt_text = self.prompt_text(prompt["name"], prompt["args"])
            briefs.append(
                m.StepBrief(
                    step_id=s.id,
                    title=s.title,
                    kind=s.kind,
                    status=s.status,
                    instructions=r["instructions"],
                    tools=json.loads(r["tools"]),
                    prompt_name=prompt["name"] if prompt else None,
                    prompt_text=prompt_text,
                    gate=s.gate,
                    gate_description=GATES.get(s.gate or ""),
                    done_when=r["done_when"],
                )
            )
        settled = sum(1 for s in project.steps if s.status in _SETTLED)
        blocked = [s.id for s in project.steps if s.status == "blocked"]
        if project.status == "done":
            guidance = "Project complete. Summarize the result for the user with the addon path and README."
        elif briefs:
            guidance = (
                f"Work on the ready step(s) below for project {pid}. Mark one in_progress with update_step, do it "
                "using the listed tools (and prompt text, if any), then call update_step with status='done', notes "
                "summarizing what you did, and artifacts (file paths, findings). If you can't proceed, use status "
                "'blocked' with the reason. Independent ready steps may run in parallel."
            )
        else:
            guidance = (
                f"No step is ready. Blocked steps: {blocked}. Resolve them with the user or skip them (with reason)."
            )
        return m.ProjectBrief(
            project_id=pid,
            title=project.title,
            goal=project.goal,
            game=project.game,
            addon=project.addon,
            status=project.status,
            progress=f"{settled}/{len(project.steps)}",
            next_steps=briefs,
            blocked_steps=blocked,
            guidance=guidance,
        )

    def prompt_text(self, name: str, args: dict[str, str]) -> str:
        try:
            with self.db.reader() as conn:
                return render_prompt(Repository(conn), name, args)
        except PromptError as exc:
            raise OrchestrationError(str(exc)) from exc

    # ---------------------------------------------------------- write side
    def update(
        self,
        pid: str,
        step_id: str,
        status: str,
        *,
        notes: str | None = None,
        artifacts: list[str] | None = None,
    ) -> m.ProjectBrief:
        if status not in STATUSES:
            raise OrchestrationError(f"status must be one of {STATUSES}")
        project = self._load(pid)
        if project.status not in ("active", "done"):
            raise OrchestrationError(f"project {pid} is {project.status}")
        steps = {s.id: s for s in project.steps}
        step = steps.get(step_id)
        if step is None:
            raise OrchestrationError(f"no step {step_id!r}; steps: {', '.join(steps)}")

        merged_artifacts = [*step.artifacts, *(artifacts or [])]
        merged_notes = "\n\n".join(x for x in (step.notes, notes) if x) or None

        if project.status == "done":
            # A finished project accepts notes/artifacts-only updates ("ledger as you go"): the step's
            # status must not change (no reopening) and the gate is not re-run. A status-changing
            # update is still rejected.
            if status != step.status:
                raise OrchestrationError(
                    f"project {pid} is done; only notes-only updates are allowed "
                    f"(step {step_id!r} is {step.status!r}), not a change to {status!r}"
                )
            with self.db.writer() as conn, conn:
                conn.execute(
                    "UPDATE project_steps SET notes = ?, artifacts = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE project_id = ? AND step_id = ?",
                    (merged_notes, json.dumps(merged_artifacts), pid, step_id),
                )
                self._event(conn, pid, step_id, "note", notes)
            return self.brief(pid)

        if status in ("in_progress", "done"):
            waiting = [d for d in step.depends_on if steps[d].status not in _SETTLED]
            if waiting:
                raise OrchestrationError(f"step {step_id} depends on unfinished steps: {waiting}")
        if status in ("skipped", "blocked") and not notes:
            raise OrchestrationError(f"status {status} needs `notes` explaining why")
        if status == "done" and step.gate:
            self._check_gate(project, step_id, step.gate, merged_notes, merged_artifacts)

        with self.db.writer() as conn, conn:
            conn.execute(
                "UPDATE project_steps SET status = ?, notes = ?, artifacts = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE project_id = ? AND step_id = ?",
                (status, merged_notes, json.dumps(merged_artifacts), pid, step_id),
            )
            self._event(conn, pid, step_id, status, notes)
            left = conn.execute(
                "SELECT count(*) FROM project_steps WHERE project_id = ? AND status NOT IN ('done', 'skipped')",
                (pid,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE projects SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                ("done" if left == 0 else "active", pid),
            )
        return self.brief(pid)

    def abandon(self, pid: str, reason: str) -> m.Project:
        self._load(pid)
        with self.db.writer() as conn, conn:
            conn.execute("UPDATE projects SET status = 'abandoned' WHERE id = ?", (pid,))
            self._event(conn, pid, None, "abandoned", reason)
        return self._load(pid)

    # ---------------------------------------------------------------- gates
    def _check_gate(self, project: m.Project, step_id: str, gate: str, notes: str | None, artifacts: list[str]) -> None:
        if gate == "evidence":
            # Notes alone no longer pass: the step must have a real, non-note evidence row attached to
            # it (this is the gate that once let a step be marked done before its README existed).
            if evidence.count_for_step(self.db, project.id, step_id, exclude_note=True) < 1:
                raise GateFailed(
                    f"step {step_id} needs evidence: record at least one non-note evidence row for it with the "
                    "`add_evidence` tool (kinds: console_transcript, file_hash, pixel_diff, screenshot, other) "
                    "before marking it done — notes/artifacts alone no longer satisfy the evidence gate"
                )
            return
        if gate == "host_claimed":
            # The step is done only when this project actually holds the host lease. The owner
            # convention ('project:<id>') is what build/test leases must not collide with.
            host_id = (project.inputs or {}).get("host_id")
            if not host_id:
                raise GateFailed("gate host_claimed needs a `host_id` input on the project")
            owner = f"project:{project.id}"
            try:
                host = hosts.get_host(self.db, host_id)
            except hosts.HostError as exc:
                raise GateFailed(str(exc)) from exc
            lock = host.lock
            if lock is None:
                raise GateFailed(
                    f"host {host_id} is not locked: claim it with claim_host(host_id='{host_id}', "
                    f"owner='{owner}') before marking this step done"
                )
            if lock.owner != owner:
                raise GateFailed(
                    f"host {host_id} is locked by {lock.owner!r}, not this project ({owner!r}); "
                    f"re-claim it as owner '{owner}'"
                )
            return
        if gate == "deploy_verified":
            # Wire the gate to the deploy registry: a deployment to the project's host must be `verified`
            # (deploy_push re-checked the MD5s on the host). The deploy id is taken from the step's
            # artifacts when present, else the host's latest deployment.
            host_id = (project.inputs or {}).get("host_id")
            if not host_id:
                raise GateFailed("gate deploy_verified needs a `host_id` input on the project")
            deploy_id = next((a for a in artifacts if _DEPLOY_ID_RE.match(a)), None)
            try:
                deployment = (
                    deploys.get_deployment(self.db, deploy_id)
                    if deploy_id
                    else deploys.latest_for_host(self.db, host_id)
                )
            except NotFound as exc:
                raise GateFailed(
                    f"no deployment to verify for host {host_id}: push one with deploy_push and record its "
                    "id as an artifact of this step"
                ) from exc
            if deployment.status != "verified":
                raise GateFailed(
                    f"deployment {deployment.id} on {host_id} is {deployment.status!r}, not 'verified' — "
                    "re-run deploy_push (or deploy_verify) until the host MD5s match, then mark this done"
                )
            return
        if not project.addon:
            raise GateFailed(f"gate {gate} needs an addon on the project")
        try:
            addon = ws.describe(self.workspace, project.game, project.addon)
        except ws.WorkspaceError as exc:
            raise GateFailed(str(exc)) from exc
        if gate == "addon_exists":
            return
        if gate == "scripts_typecheck":
            # Only cs_script sources (scripts/) are checked against point_script.d.ts; other .js in the addon
            # (e.g. external tools) is not engine script.
            files = [f.path for f in addon.files if f.path.startswith("scripts/") and f.path.endswith(".js")]
            if not files:
                raise GateFailed("no scripts/*.js files in the addon to validate")
            problems = []
            with self.db.reader() as conn:
                repo = Repository(conn)
                for path in files:
                    code = ws.read_file(self.workspace, project.game, project.addon, path)
                    result = validate_cs_script(repo, code, game=project.game, filename=Path(path).name)
                    for d in result.diagnostics:
                        if d.severity == "error":
                            problems.append(f"{path}:{d.line}:{d.column} [{d.category}] {d.message}")
            if problems:
                raise GateFailed("scripts have errors:\n" + "\n".join(problems[:25]))
            return
        if gate == "cfgs_valid":
            # Console cfgs live under cfg/; other .cfg files (e.g. gamestate_integration VDF) aren't commands.
            files = [f.path for f in addon.files if f.path.startswith("cfg/") and f.path.endswith(".cfg")]
            if not files:
                raise GateFailed("no cfg/*.cfg files in the addon")
            problems = []
            with self.db.reader() as conn:
                repo = Repository(conn)
                for path in files:
                    text = ws.read_file(self.workspace, project.game, project.addon, path)
                    for d in validate_cfg(repo, text, game=project.game).diagnostics:
                        if d.severity == "error":
                            problems.append(f"{path}:{d.line} [{d.category}] {d.message}")
            if problems:
                raise GateFailed("configs have errors:\n" + "\n".join(problems[:25]))
            return
        raise GateFailed(f"unknown gate {gate!r}")

    @staticmethod
    def _event(conn, pid: str, step_id: str | None, event: str, detail: str | None) -> None:
        conn.execute(
            "INSERT INTO project_events (project_id, step_id, event, detail) VALUES (?, ?, ?, ?)",
            (pid, step_id, event, detail),
        )
