// Source 2 Docs: a small vanilla-JS SPA over the JSON API. Hash routes: #/<game>/<section>/...
// All dynamic text goes through esc() before it reaches innerHTML.

const $ = (sel) => document.querySelector(sel);
const view = $("#view");
const state = { games: [], game: localStorage.getItem("s2docs.game") || "cs2", activityActive: false, jobsActive: false };

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const enc = encodeURIComponent;
const snippet = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<mark>$1</mark>");
const badge = (t, cls = "") => `<span class="badge ${cls}">${esc(t)}</span>`;
const firstLine = (s) => (s || "").split("\n")[0];
// Valve FGD text contains <br> line breaks; everything else stays escaped.
const rich = (s) => esc(s).replace(/&lt;br\s*\/?&gt;/gi, "<br>");
const stripTags = (s) => String(s).replace(/<[^>]+>/g, " ");

// ---- Hosts & Activity helpers (ops/visibility pages) ----------------------
// Run-time helpers, kept in one contiguous block so a headless test can lift them and check the
// rendered durations (tests/test_web_ui.py extracts between the two duration-helpers markers).
// duration-helpers:start
const parseTs = (iso) => {
  const s = String(iso);
  return Date.parse(/[Zz]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z");
};
// The single humanizer every elapsed/duration value goes through.
const human = (s) => {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
};
// A record is 'live' while its work is still going: a run queued/running, a test session running, a
// project still active, a step in progress. Only a live record ticks to now; a settled one is frozen.
const LIVE_STATES = new Set(["queued", "running", "active", "in_progress"]);
const isLive = (r) => LIVE_STATES.has(r.status) || LIVE_STATES.has(r.state);
// Seconds a record has been going (live) or took (settled): started_at|created_at -> finished_at|updated_at.
// null when the record carries no usable timestamps (e.g. a dry-run deploy's 'dry-run' placeholder).
const runSeconds = (r) => {
  const start = parseTs(r.started_at || r.created_at);
  const end = isLive(r) ? Date.now() : parseTs(r.finished_at || r.updated_at);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return Math.max(0, Math.floor((end - start) / 1000));
};
// The one duration renderer every row uses: live while running, final once finished.
const runTime = (r) => human(runSeconds(r));
// duration-helpers:end
const secsAgo = (iso) => {
  if (!iso) return null;
  const t = parseTs(iso);
  return Number.isNaN(t) ? null : Math.max(0, Math.floor((Date.now() - t) / 1000));
};
// ---- Central (America/Chicago) absolute timestamps -------------------------
// The default clock for every timestamp the UI renders. One user is in America/Chicago and wants
// to read wall-clock time, not a relative age. Intl carries the DST rules, so the same formatter
// prints the standard offset in winter and the daylight offset in summer; the label is the zone's
// real abbreviation for that date — 'CST' in standard time, 'CDT' in daylight time.
// central-time-helpers:start
const CT_ZONE = "America/Chicago";
const CT_DAY = new Intl.DateTimeFormat("en-US", { timeZone: CT_ZONE, month: "short", day: "numeric" });
const CT_CLOCK = new Intl.DateTimeFormat("en-US", {
  timeZone: CT_ZONE, hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true,
});
const CT_NOW = new Intl.DateTimeFormat("en-US", { timeZone: CT_ZONE, hour: "numeric", minute: "2-digit", hour12: true });
// The zone's real abbreviation for one instant: 'CST' in standard time, 'CDT' in daylight time.
// Intl resolves the DST switch per date, so a November timestamp reads CST even in a CDT year.
// Falls back to the generic 'CT' only when Intl cannot name the zone (no timeZoneName support).
const CT_TZN = (() => {
  try {
    return new Intl.DateTimeFormat("en-US", { timeZone: CT_ZONE, timeZoneName: "short" });
  } catch (e) {
    return null;
  }
})();
const ctAbbr = (d) => {
  if (CT_TZN) {
    const p = CT_TZN.formatToParts(d).find((x) => x.type === "timeZoneName");
    if (p && p.value) return p.value;
  }
  return "CT";
};
// Absolute Central wall-clock for one instant: 'Oct 13, 4:37:21 PM CDT'.
const ctAbs = (d) => `${CT_DAY.format(d)}, ${CT_CLOCK.format(d)} ${ctAbbr(d)}`;
// The single timestamp string: '<Central> (<relative>)', e.g. 'Oct 13, 4:37:21 PM CDT (5m ago)'.
// null when unparseable (the caller keeps its own fallback). The relative age is the shared secsAgo().
const ctStamp = (iso) => {
  const t = parseTs(iso);
  if (Number.isNaN(t)) return null;
  return `${ctAbs(new Date(t))} (${human(secsAgo(iso))} ago)`;
};
// The renderer every timestamp cell uses: absolute Central inline, the raw ISO in a tooltip.
// A null/empty value reads '—'; an unparseable literal (e.g. a dry-run deploy's 'dry-run') is kept
// escaped rather than swallowed, so the cell never throws and never invents a time.
const fmtTime = (iso) => {
  if (iso == null || iso === "") return "—";
  const s = ctStamp(iso);
  return s == null ? esc(String(iso)) : `<span title="${esc(String(iso))}">${esc(s)}</span>`;
};
// 'Server now: 4:37 PM CDT' — the sanity check that makes a relative age trustworthy.
const nowCT = () => { const d = new Date(); return `${CT_NOW.format(d)} ${ctAbbr(d)}`; };
const nowLine = () => `<p class="muted">Server now: <strong>${nowCT()}</strong> · all times shown in Central time (America/Chicago, CST/CDT).</p>`;
// central-time-helpers:end
// Shared status colours across build/test/deploy/lock states.
const STATE_CLASS = {
  succeeded: "kind", verified: "kind", pass: "kind", running: "kind", claimed: "kind",
  queued: "", idle: "", pushed: "", released: "", failed: "warn", cancelled: "warn", fail: "warn", stale: "warn",
};
const stateBadge = (s) => badge(s, STATE_CLASS[s] ?? "");
const summaryText = (s) => Object.entries(s || {}).map(([k, v]) => `${v} ${k}`).join(", ") || "—";
// A lock's 'stuck' flag (added by the lock-phase work) is rendered when present and tolerated when not.
const lockStuckBadge = (lock) => (lock && lock.stuck ? badge("STUCK", "warn") : "");
// Material-guard findings on a build run (missing/reserved materials) — the run can be 'succeeded' and still carry them.
const materialWarning = (r) => {
  const w = r && r.warnings;
  if (!w || w.clean) return "";
  const n = Object.values(w.counts || {}).reduce((a, b) => a + Number(b || 0), 0);
  return badge(`⚠ ${n} material${n === 1 ? "" : "s"}`, "warn");
};
// A run's phase/notes line (build start --notes, or a finalize note like 'timed out after 360s').
const runPhase = (r) => firstLine(r && r.notes ? r.notes : "");
const gpuOf = (h) => {
  const gpus = h.facts?.probe?.gpu || h.facts?.gpu || [];
  return gpus.map((g) => `${g.name || "gpu"}${g.util_pct != null ? ` (${g.util_pct}%)` : ""}`).join(", ") || "—";
};
const lockCell = (lock) => {
  if (!lock) return `<span class="muted">free</span>`;
  // A fresh heartbeat that is not STUCK reads healthy; the SERVER decides stuck-ness from the phase age.
  const phase = lock.phase
    ? ` · phase ${esc(lock.phase)}${lock.phase_age_sec != null ? ` ${human(lock.phase_age_sec)}` : ""}`
    : " · no phase reported";
  const state = lock.stuck ? "STUCK" : lock.fresh ? "fresh" : "STALE";
  return `${badge(lock.owner, lock.fresh && !lock.stuck ? "kind" : "warn")}${lockStuckBadge(lock)}<div class="muted">${state} · heartbeat ${fmtTime(lock.heartbeat_at)} · ttl ${human(lock.ttl_sec)}${phase}${
    lock.stuck && lock.stuck_reason ? `<br>${esc(lock.stuck_reason)}` : ""
  }</div>`;
};

// A project's recent_events as a newest-first Timeline (the engine orders by id DESC). Each row
// renders through the one Central-time formatter, so milestones read as wall-clock, not just 'ago'.
const timelineRows = (evs) =>
  evs && evs.length
    ? `<ul class="timeline">${evs
        .map(
          (e) =>
            `<li>${badge(e.event)} <span class="muted">${fmtTime(e.ts)}</span>${
              e.step_id ? ` <span class="mono muted">${esc(e.step_id)}</span>` : ""
            }${e.detail ? `<div class="muted">${esc(e.detail)}</div>` : ""}</li>`,
        )
        .join("")}</ul>`
    : `<p class="muted">No events yet.</p>`;

const lockRows = (evs) =>
  evs.length
    ? `<table><tr><th>When</th><th>Event</th><th>Owner</th><th>Detail</th></tr>${evs
        .map(
          (e) =>
            `<tr><td class="muted">${fmtTime(e.ts)}</td><td>${stateBadge(e.event)}</td><td class="mono">${esc(e.owner)}</td>` +
            `<td class="muted">${esc(e.detail || "")}</td></tr>`,
        )
        .join("")}</table>`
    : `<p class="muted">No lock history.</p>`;

const buildRows = (runs) =>
  runs.length
    ? `<table><tr><th>Run</th><th>Host</th><th>Status</th><th>Started</th><th>Elapsed</th><th>Summary</th></tr>${runs
        .map(
          (r) =>
            `<tr><td class="mono">${esc(r.id)}<div class="muted">${esc(r.kind)}</div></td>` +
            `<td>${link(["hosts", r.host_id], r.host_id)}</td>` +
            `<td>${stateBadge(r.status)}${lockStuckBadge(r.lock)}${r.exit_code != null ? `<div class="muted mono">exit ${r.exit_code}</div>` : ""}</td>` +
            `<td class="muted">${fmtTime(r.started_at || r.created_at)}</td>` +
            `<td class="muted">${runTime(r)}${isLive(r) ? `<div class="muted">running</div>` : ""}</td><td class="muted">${esc(summaryText(r.summary))}${materialWarning(r)}${
              runPhase(r) ? `<div class="muted">${esc(runPhase(r))}</div>` : ""
            }</td></tr>`,
        )
        .join("")}</table>`
    : `<p class="muted">No build runs.</p>`;

// Deployments: created_at -> updated_at (a push is synchronous, so the duration is the copy+verify time).
const deployRows = (ds) =>
  ds.length
    ? `<table><tr><th>Deploy</th><th>Host</th><th>Status</th><th>Elapsed</th><th>Files</th><th>When</th></tr>${ds
        .map((d) => {
          const total = d.entries.length;
          const verified = d.entries.filter((e) => e.verified).length;
          return (
            `<tr><td class="mono">${esc(d.id)}${d.addon ? `<div class="muted">${esc(d.addon)}</div>` : ""}</td>` +
            `<td>${link(["hosts", d.host_id], d.host_id)}</td><td>${stateBadge(d.status)}</td>` +
            `<td class="muted">${runTime(d)}</td>` +
            `<td class="muted"><strong>${verified}</strong>/${total} verified</td>` +
            `<td class="muted">${fmtTime(d.created_at)}</td></tr>`
          );
        })
        .join("")}</table>`
    : `<p class="muted">No deployments.</p>`;

// The test-run fields mirror the in-game TEST STATUS HUD strip: name / step / state / elapsed.
// elapsed_sec is the server's own value (started_at -> finished_at, or now while running), so a
// running session's row ticks with the poll and a finished one stays frozen.
const testRows = (ts) =>
  ts.length
    ? `<table><tr><th>Name</th><th>Step</th><th>State</th><th>Started</th><th>Elapsed</th><th>Host</th></tr>${ts
        .map(
          (t) =>
            `<tr><td class="mono">${esc(t.name)}</td><td>${esc(t.step || "—")}</td><td>${stateBadge(t.state)}${lockStuckBadge(t.lock)}</td>` +
            `<td class="muted">${fmtTime(t.started_at || t.created_at)}</td>` +
            `<td class="mono">${human(t.elapsed_sec)}${t.state === "running" ? `<div class="muted">running</div>` : ""}</td><td>${t.host_id ? link(["hosts", t.host_id], t.host_id) : "—"}</td></tr>`,
        )
        .join("")}</table>`
    : `<p class="muted">No test runs.</p>`;

// ---- Jobs helpers --------------------------------------------------------
// A job is the one record for a requested test/demo/final run. The queue board groups by host (running
// on top, then queued by position); the job page shows the intake, timeline, screenshots, links and the
// close-out summary. Events at level error/crash expand their log tail in a <pre>.
const JOB_LEVEL_CLASS = { error: "warn", crash: "warn", milestone: "kind", learning: "kind", screenshot: "kind" };
// One class per job status. 'inconclusive' (the run ended with no pass/fail verdict) gets its OWN
// class: it must never read as the amber 'passed'/'running' or the red 'failed'/'error' — a dashed,
// muted badge says "no call was made" at a glance in the queue, the finished table and the timeline.
const JOB_STATUS_CLASS = { passed: "kind", running: "kind", failed: "warn", error: "warn", inconclusive: "inconclusive" };
const jobStatusBadge = (s) => badge(s, JOB_STATUS_CLASS[s] ?? "");
const jobFlags = (j) =>
  `${j.error_count ? badge(`${j.error_count} error${j.error_count === 1 ? "" : "s"}`, "warn") : ""}` +
  `${j.crash_count ? badge(`${j.crash_count} crash${j.crash_count === 1 ? "" : "es"}`, "warn") : ""}`;
const jobHeadline = (j) =>
  `${jobStatusBadge(j.status)}${badge(j.kind)}${jobFlags(j)} <strong>${esc(j.title)}</strong> ` +
  `<span class="mono muted">${esc(j.target)}</span>`;
const jobMeta = (j) => {
  const bits = [];
  if (j.host_id) bits.push(link(["hosts", j.host_id], j.host_id));
  if (j.worker) bits.push(badge("worker: " + j.worker));
  if (j.phase) bits.push(`phase ${badge(j.phase)}`);
  bits.push(`elapsed ${runTime(j)}`);
  if (j.params && j.params.pending_shot) bits.push(badge("screenshot requested", "kind"));
  return bits.join(" · ");
};
const jobCard = (j) =>
  `<div class="card" style="margin-bottom:8px">${link(["jobs", j.id], j.id)} ${jobHeadline(j)}` +
  `<div class="muted">${jobMeta(j)}</div></div>`;
const jobTable = (jobs) =>
  `<table><tr><th>Job</th><th>Kind</th><th>Status</th><th>Host</th><th>Duration</th><th>Finished</th></tr>${jobs
    .map(
      (j) =>
        `<tr><td>${link(["jobs", j.id], j.id)}<div class="muted">${esc(j.title)}</div></td>` +
        `<td>${badge(j.kind)}</td><td>${jobStatusBadge(j.status)}${jobFlags(j)}</td>` +
        `<td>${j.host_id ? link(["hosts", j.host_id], j.host_id) : "—"}</td>` +
        `<td class="muted">${runTime(j)}</td><td class="muted">${fmtTime(j.finished_at || j.updated_at)}</td></tr>`,
    )
    .join("")}</table>`;

function jobBoard(jobs, hosts) {
  const isActive = (j) => j.status === "running" || j.status === "queued";
  const active = jobs.filter(isActive);
  const done = jobs.filter((j) => !isActive(j));
  // One column per registered host (even empty), plus any host that only appears on a job, plus
  // "(unassigned)". Running first, then queued by position (ties broken by created_at).
  const columns = new Map(hosts.map((h) => [h.id, []]));
  active.forEach((j) => {
    const key = j.host_id || "(unassigned)";
    if (!columns.has(key)) columns.set(key, []);
    columns.get(key).push(j);
  });
  const byOrder = (a, b) => a.position - b.position || (a.created_at < b.created_at ? -1 : 1);
  const cols = [...columns.entries()]
    .map(([hid, list]) => {
      const running = list.filter((j) => j.status === "running").sort(byOrder);
      const queued = list.filter((j) => j.status === "queued").sort(byOrder);
      const label = hid === "(unassigned)" ? `<strong>${esc(hid)}</strong>` : link(["hosts", hid], hid);
      const body = [...running, ...queued].map(jobCard).join("") || `<p class="muted">No queued jobs.</p>`;
      return `<div class="dag-col"><h3>${label} <span class="muted">${running.length} running · ${queued.length} queued</span></h3>${body}</div>`;
    })
    .join("");
  return `${nowLine()}<h2>Queue <span class="muted">per host — running on top, then queued by position</span></h2>
    <div class="dag">${cols}</div>
    <h2>Finished</h2>${done.length ? jobTable(done) : `<p class="muted">No finished jobs yet.</p>`}`;
}

const jobTimeline = (events) =>
  `<ul class="timeline">${events
    .map((e) => {
      const tail =
        e.data && e.data.log_tail
          ? `<details open><summary class="muted">log tail</summary><pre>${esc(e.data.log_tail)}</pre></details>`
          : "";
      // A verdict milestone carries which way it went and the source it came from — surface both inline
      // next to the level badge so each step's verdict is readable in the timeline.
      const vd = e.data && e.data.verdict
        ? ` ${badge(e.data.verdict, e.data.verdict === "pass" ? "kind" : e.data.verdict === "fail" ? "warn" : "")}` +
          `${e.data.source ? ` ${badge("via " + e.data.source)}` : ""}`
        : "";
      return `<li${e.phase ? ` data-vx-step="${esc(e.phase)}"` : ""}>${badge(e.level, JOB_LEVEL_CLASS[e.level] || "")}${vd} <span class="muted">${fmtTime(e.created_at)}</span>` +
        `${e.phase ? ` <span class="mono muted">${esc(e.phase)}</span>` : ""}<div>${esc(e.message)}</div>${tail}</li>`;
    })
    .join("")}</ul>`;

// ---- Job verification model: the stepper + the NOW TESTING card -------------
// The driver posts one phase 'plan' event up front (the full ordered test list), an 'info' step-prompt
// event per test (phase = step id; data carries prompt / look_for / strip_name / strip_purpose /
// strip_expect / deadline_utc / step_index / step_total), a 'milestone' verdict event per answered test
// (data.verdict + source + notes, step = data.step or the event phase), and a phase 'done' event with
// per-step results when the run ends. jobVerifications() folds all of that into one ordered list the
// stepper, the card and the results summary all read. With no plan event yet (an older driver, or the
// plan not posted) it falls back to the step prompts seen so far, so the strip still shows real progress.
const VERDICT_SET = new Set(["pass", "fail", "skip"]);
// One chip class per step state — the whole visual language of the stepper, in one place.
const VX_STATE_CLASS = {
  pass: "vx-pass", fail: "vx-fail", skip: "vx-skip",
  "no-verdict": "vx-noverdict", current: "vx-testing", upcoming: "vx-upcoming",
};

function jobVerifications(j) {
  const evs = j.events || [];
  // The newest plan event wins (a re-plan replaces an earlier one).
  let plan = null;
  for (const e of evs) {
    if (e.phase === "plan" && e.data && Array.isArray(e.data.plan) && e.data.plan.length) plan = e.data.plan;
  }
  // Step-prompt events, oldest-first. Also the fallback for a verdict with no step tag (a webapp
  // verdict POSTs without `step`, so its milestone carries no phase either — it answers whatever prompt
  // was the newest when it landed).
  const prompts = [];
  for (const e of evs) {
    if (e.level === "info" && e.phase && e.data && typeof e.data === "object" && "prompt" in e.data) prompts.push(e);
  }
  const promptBy = new Map(prompts.map((e) => [e.phase, e]));
  const newestPromptBefore = (id) => {
    let found = null;
    for (const p of prompts) if (p.id < id) found = p;
    return found;
  };
  // Newest verdict (and the explicit 'none' timeout marker) per step id.
  const verdictBy = new Map();
  const timedOut = new Set();
  for (const e of evs) {
    if (!e.data || typeof e.data !== "object") continue;
    let id = e.data.step || e.phase;
    if (!id) {
      const p = newestPromptBefore(e.id); // no step tag: attribute to the prompt it answered
      id = p ? p.phase : null;
    }
    if (!id) continue;
    if (e.level === "milestone" && VERDICT_SET.has(e.data.verdict)) {
      verdictBy.set(id, { verdict: e.data.verdict, source: e.data.source, notes: e.data.notes, late: e.data.late, at: e.created_at, evidence_id: e.data.evidence_id });
    } else if (e.data.verdict === "none") {
      timedOut.add(id);
    }
  }
  // Per-step results the driver writes into the closing phase 'done' event.
  const doneBy = new Map();
  for (const e of evs) {
    if (e.phase === "done" && e.data && Array.isArray(e.data.results)) {
      for (const r of e.data.results) if (r && (r.id || r.step)) doneBy.set(r.id || r.step, r);
    }
  }
  const items = plan
    ? plan.map((p, i) => ({
        id: p.id, index: p.index != null ? p.index : i + 1, title: p.title,
        strip_name: p.strip_name, strip_purpose: p.strip_purpose, strip_expect: p.strip_expect,
      }))
    : [...promptBy.entries()]
        .map(([id, e]) => ({
          id, index: (e.data || {}).step_index, title: e.message,
          strip_name: (e.data || {}).strip_name, strip_purpose: (e.data || {}).strip_purpose,
          strip_expect: (e.data || {}).strip_expect,
        }))
        .sort((a, b) => (a.index || 0) - (b.index || 0));
  const total = j.current_step && j.current_step.step_total != null ? j.current_step.step_total : items.length;
  const currentId = j.status === "running" && j.current_step ? j.current_step.phase : null;
  return items.map((it) => {
    const prompt = promptBy.get(it.id);
    const d = (prompt && prompt.data) || {};
    const dv = doneBy.get(it.id) || {};
    const verdict =
      verdictBy.get(it.id) ||
      (VERDICT_SET.has(dv.verdict) ? { verdict: dv.verdict, source: dv.source, notes: dv.notes } : null);
    const asked = !!prompt || !!dv.verdict;
    const isCurrent = it.id === currentId;
    const state = verdict
      ? verdict.verdict
      : isCurrent
        ? "current"
        : asked || timedOut.has(it.id)
          ? "no-verdict"
          : "upcoming";
    const deadline = isCurrent && j.current_step && j.current_step.deadline_utc
      ? j.current_step.deadline_utc
      : d.deadline_utc || null;
    return {
      id: it.id,
      index: it.index != null ? it.index : "?",
      total,
      name: it.strip_name || it.title || it.id,
      tested: d.strip_purpose || it.strip_purpose || d.prompt || "",
      expect: d.strip_expect || it.strip_expect || d.look_for || "",
      prompt: d.prompt,
      look_for: d.look_for,
      respond_hint: d.respond_hint,
      deadline_utc: deadline,
      asked_at: isCurrent && j.current_step && j.current_step.asked_at ? j.current_step.asked_at : prompt ? prompt.created_at : null,
      verdict,
      asked,
      isCurrent,
      state,
    };
  });
}

// ---- Screenshots tied to steps and verdicts ----------------------------------
// A screenshot is a `screenshot` job event. The demo driver adds data {step, step_index, step_total,
// reason, seq, verdict:{verdict,source,confirm_id,late}, full_res, width, height}; older events carry
// only {url, md5, bytes} and are grouped under 'unassigned' by time. This folds both shapes into one
// list the RESULTS table, the EVIDENCE grid and the NOW card all read.
const SHOT_REASONS = ["ready", "periodic", "verdict", "request", "verify"];
function shotReason(s) {
  return s.reason && SHOT_REASONS.includes(s.reason) ? s.reason : "shot";
}
// 24-hour HH:MM:SS in Central, for the compact captions.
function hms(utcIso) {
  const t = parseTs(utcIso);
  if (Number.isNaN(t)) return "";
  return new Intl.DateTimeFormat("en-GB", { timeZone: CT_ZONE, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(t));
}
function jobScreenshots(j) {
  const out = [];
  let auto = 0;
  for (const e of j.events || []) {
    if (e.level !== "screenshot") continue;
    const d = e.data || {};
    if (!d.url) continue;
    auto += 1;
    out.push({
      seq: typeof d.seq === "number" ? d.seq : auto,
      step: d.step || e.phase || null,
      step_index: typeof d.step_index === "number" ? d.step_index : null,
      step_total: typeof d.step_total === "number" ? d.step_total : null,
      reason: d.reason || null,
      url: d.url,
      md5: d.md5 || null,
      bytes: d.bytes,
      verdict: d.verdict && d.verdict.verdict ? d.verdict : null,
      full_res: d.full_res,
      width: d.width,
      height: d.height,
      at: e.created_at,
      id: e.id,
    });
  }
  return out;
}
// The full caption spec: '#seq · reason · HH:MM:SS · step', plus the verdict it was captured with.
function shotCaption(s) {
  const parts = [`#${s.seq}`, shotReason(s)];
  if (s.at) parts.push(hms(s.at));
  parts.push(s.step || "unassigned");
  if (s.verdict) parts.push(`${s.verdict.verdict}${s.verdict.source ? " via " + s.verdict.source : ""}`);
  return parts.join(" · ");
}
// One screenshot: a click-to-enlarge thumbnail. `small` drops the caption (for inline step rows).
function shotThumb(s, small = false) {
  const cap = shotCaption(s);
  const cls = `shot${small ? " shot-sm" : ""}${s.reason === "verdict" ? " shot-verdict" : ""}`;
  const title = `${cap} — click to enlarge`;
  const img = `<img src="${esc(s.url)}" alt="${esc(cap)}" loading="lazy">`;
  if (small) {
    return `<button type="button" class="shot-btn ${cls}" data-shot="${esc(s.url)}" data-caption="${esc(cap)}" title="${esc(title)}" aria-label="expand screenshot #${esc(s.seq)}">${img}</button>`;
  }
  return `<figure class="${cls}"><button type="button" class="shot-btn" data-shot="${esc(s.url)}" data-caption="${esc(cap)}" title="${esc(title)}" aria-label="expand screenshot #${esc(s.seq)}">${img}</button>
    <figcaption class="muted mono shot-cap">${esc(cap)}</figcaption></figure>`;
}
// The step a screenshot belongs to, matched to a step id (None renders as 'unassigned').
const shotsForStep = (j, stepId) => jobScreenshots(j).filter((s) => s.step === stepId);

// One stepper chip: 'k. name' + the verdict badge and its source; the current test gets a pulsing
// accent border, a done chip carries its outcome colour, an unanswered one an amber outline.
function vxChip(it) {
  const cls = VX_STATE_CLASS[it.state] || "vx-upcoming";
  const vb = it.verdict
    ? badge(it.verdict.verdict, it.verdict.verdict === "pass" ? "kind" : it.verdict.verdict === "fail" ? "warn" : "") +
      (it.verdict.source ? badge("via " + it.verdict.source) : "")
    : it.state === "no-verdict"
      ? badge("no verdict")
      : it.state === "current"
        ? badge("testing", "kind")
        : "";
  return `<button type="button" class="vx-chip ${cls}${it.isCurrent ? " vx-current" : ""}${it.verdict ? " vx-done" : ""}" data-vx-goto="${esc(
    it.id,
  )}"${it.isCurrent ? ' aria-current="step"' : ""}><span class="vx-cnum">${esc(it.index)}.</span> <span class="vx-cname">${esc(
    it.name,
  )}</span> ${vb}</button>`;
}

function jobStepper(j) {
  const items = jobVerifications(j);
  if (!items.length) return "";
  const done = items.filter((x) => x.verdict).length;
  return `<div class="vx-stepper" data-vx-stepper><span class="vx-slabel">Verification <span class="muted">${
    done
  }/${items.length} answered</span></span><div class="vx-chips">${items.map(vxChip).join("")}</div></div>`;
}

// The live countdown: a track + a fill whose width the 1 s ticker drives (a CSS width transition),
// plus the numeric 'm:ss left'. No re-render is needed for either to move.
function vxCountdown(it) {
  if (!it.deadline_utc) return "";
  const start = it.asked_at ? ` data-start="${esc(it.asked_at)}"` : "";
  return `<div class="vx-count"><div class="vx-bar"><span class="vx-bar-fill" data-deadline="${esc(
    it.deadline_utc,
  )}" data-vx-fill${start}></span></div><span class="vx-cd mono" data-deadline="${esc(it.deadline_utc)}">…</span></div>`;
}

// ---- SETTING UP / CLEANING UP countdown phases --------------------------------
// The demo driver posts one `info` event per setup/cleanup window while it stages the next test:
//   { level:'info', phase:'setup'|'cleanup', message:'SETTING UP TEST k/n NAME (10 s)',
//     data:{ countdown_s, until:'<UTC ISO>', step, step_index, step_total, name, next } }
// jobActivePhase() returns that event only while it is the newest activity — a later step prompt or
// a verdict supersedes it. The banner ticks down CLIENT-side from `until` (the 1 s updateCountdowns
// ticker), so it stays live between the 5 s region re-renders.
function jobActivePhase(j) {
  const evs = j.events || [];
  let last = null;
  for (const e of evs) {
    const prompt = e.level === "info" && e.data && "prompt" in e.data;
    const verdict = e.level === "milestone" && e.data && VERDICT_SET.has(e.data.verdict);
    const phase = e.level === "info" && e.data && e.data.until && (e.phase === "setup" || e.phase === "cleanup");
    if (prompt || verdict || phase) last = { e, phase };
  }
  return last && last.phase ? last.e : null;
}

// The state banner: 'SETTING UP: TEST k/n NAME  8s' / 'CLEANING UP: TEST k/n -> next: …  6s'.
function vxPhaseBanner(j) {
  const e = jobActivePhase(j);
  if (!e) return "";
  const d = e.data || {};
  const k = d.step_index != null ? d.step_index : d.step != null ? d.step : "";
  const n = d.step_total;
  const name = d.name || "";
  const of = `${esc(k)}${n ? `/${esc(n)}` : ""}`;
  const cd = `<span class="vx-phase-cd mono" data-cd-sec="${esc(d.until)}">…</span>`;
  if (e.phase === "setup") {
    return `<div class="vx-phase vx-phase-setup" data-vx-phase="setup" data-until="${esc(d.until)}">
      <span class="vx-phase-kind">SETTING UP</span>
      <span class="vx-phase-main">TEST ${of}${name ? ` <strong>${esc(name)}</strong>` : ""}</span>${cd}</div>`;
  }
  const tail = d.next ? `→ next: ${esc(d.next)}` : "→ finishing";
  return `<div class="vx-phase vx-phase-cleanup" data-vx-phase="cleanup" data-until="${esc(d.until)}">
    <span class="vx-phase-kind">CLEANING UP</span>
    <span class="vx-phase-main">TEST ${of}</span>
    <span class="vx-phase-next">${tail}</span>${cd}</div>`;
}

// The NOW TESTING card: the current test rendered big and distinct, with the verdict buttons.
function nowTestingCard(j) {
  const cs = j.current_step;
  if (j.status !== "running" || !cs) return "";
  const cur = jobVerifications(j).find((x) => x.isCurrent) || {};
  const k = cs.step_index != null ? cs.step_index : cur.index != null ? cur.index : "?";
  const n = cs.step_total != null ? cs.step_total : cur.total != null ? cur.total : "?";
  const name = cur.name || cs.phase || "";
  const tested = cur.tested || cs.prompt || "";
  const expect = cur.expect || cs.look_for || "";
  const hint = cs.respond_hint || "in game: type !pass / !fail / !skip plus a note in chat";
  const verdict = cur.verdict || cs.verdict || null;
  // Screenshots for THIS test: a shot belongs to the step named by cs.phase.
  const shots = jobScreenshots(j).filter((s) => (s.step || null) === (cs.phase || null));
  const lastShot = shots.length ? shots[shots.length - 1] : null;
  const camera = lastShot
    ? `<div class="vx-camera">📷 <strong>${shots.length}</strong> shot${shots.length === 1 ? "" : "s"} this test — last ${hms(lastShot.at)} (${shotReason(lastShot)}) ${shotThumb(lastShot, true)}</div>`
    : `<div class="vx-camera muted">📷 no screenshots yet for this test</div>`;
  // When a verdict is recorded, link the verdict screenshot once its event arrives (match on the
  // confirm id, else the step); until then, say one WILL be saved with the verdict.
  const matchingShot = verdict
    ? shots.slice().reverse().find((s) => s.verdict && verdict.evidence_id && s.verdict.confirm_id === verdict.evidence_id) ||
      shots.slice().reverse().find((s) => s.reason === "verdict" || s.verdict)
    : null;
  // A recorded verdict (from this page, chat, console or the coordinator) reads the same either way.
  const recorded = verdict
    ? `<div class="vx-recorded">${badge(
        "Recorded: " + String(verdict.verdict).toUpperCase(),
        verdict.verdict === "pass" ? "kind" : verdict.verdict === "fail" ? "warn" : "",
      )}${verdict.source ? ` <span class="muted">(via ${esc(verdict.source)})</span>` : ""}${
        verdict.notes ? ` — “${esc(verdict.notes)}”` : ""
      }</div>` +
      `<div class="vx-verdict-shot">${matchingShot
        ? `${shotThumb(matchingShot, true)} <span class="muted">screenshot at verdict (${hms(matchingShot.at)})</span>`
        : `<span class="muted">a screenshot will be saved with your verdict</span>`}</div>`
    : "";
  const waiting = verdict
    ? `<div class="vx-waiting"><span class="vx-spin"></span> Waiting for the demo to move to the next test…</div>`
    : "";
  // While the driver is staging (setup) or tearing down (cleanup) the next test the verdict window is
  // NOT open — the buttons are disabled and read 'opens in Ns' / 'recorded · window closed'.
  const phase = jobActivePhase(j);
  const blocked = !!phase;
  let gate = "";
  if (phase && phase.phase === "setup") {
    gate = `<p class="vx-gate">Verdict window opens in <span class="mono" data-cd-sec="${esc((phase.data || {}).until)}">…</span> — setting up.</p>`;
  } else if (phase && phase.phase === "cleanup") {
    gate = `<p class="vx-gate">${verdict ? "Verdict recorded · " : ""}window closed — cleaning up this test.</p>`;
  }
  const dis = blocked ? " disabled" : "";
  const form = `<form data-job-verdict="${esc(j.id)}" method="post" action="javascript:void 0" class="vx-form">
      <p><input name="notes" placeholder="note (optional)"${dis}></p>
      <p class="vx-btns">
        <button type="submit" name="verdict" value="pass" class="vx-btn vx-btn-pass"${dis}>✓ Pass</button>
        <button type="submit" name="verdict" value="fail" class="vx-btn vx-btn-fail"${dis}>✗ Fail</button>
        <button type="submit" name="verdict" value="skip" class="vx-btn vx-btn-skip"${dis}>– Skip</button>
      </p>
      ${gate}
      <div class="result"></div></form>`;
  // The screenshot-request control lives in the NOW card (per the user). A pending request (set when
  // the button was pressed, cleared when the driver uploads) reads 'requested — waiting for the driver'.
  const pending = !!(j.params && j.params.pending_shot);
  const shotForm = `<form data-job-shot="${esc(j.id)}" method="post" action="javascript:void 0" class="vx-shotform">
      ${pending ? `<p class="vx-pending">📷 screenshot requested — waiting for the driver</p>` : ""}
      <p><input name="note" placeholder="request a screenshot (optional note)"></p>
      <button type="submit">Request screenshot</button><div class="result"></div></form>`;
  return `<div class="card vx-card" data-vx-card data-vx-step="${esc(cs.phase || "")}" data-vx-name="${esc(
    name,
  )}" data-vx-index="${esc(k)}" data-vx-total="${esc(n)}">
    <div class="vx-card-head"><span class="vx-live">● LIVE</span> <span class="vx-testof">TEST ${esc(k)} of ${esc(n)}</span></div>
    <h2 class="vx-title">${esc(name)} <span class="mono muted">${esc(cs.phase || "")}</span></h2>
    ${tested ? `<p class="vx-field"><span class="vx-lbl">What is tested</span> ${esc(tested)}</p>` : ""}
    ${expect ? `<p class="vx-field"><span class="vx-lbl">What you should see</span> ${esc(expect)}</p>` : ""}
    ${vxCountdown(cur)}
    ${camera}
    <p class="muted vx-hint">${esc(hint)}</p>
    ${recorded}${waiting}${form}${shotForm}</div>`;
}

// ---- Job detail page: an organized hierarchy ---------------------------------
// HEADER (title/status/meta/URL) -> NOW (phase banner + stepper + card, or a compact finished
// summary) -> RESULTS (per-step table) -> SUMMARY (4 blocks) -> EVIDENCE (thumbnail grid) ->
// TIMELINE (grouped by step, collapsible, noisy lines folded) -> LINKS (test run / project /
// related jobs). Everything the old page showed is still here, just grouped so a long run scans.
const jobCounts = (j) => {
  const items = jobVerifications(j);
  const count = (v) => items.filter((x) => x.verdict && x.verdict.verdict === v).length;
  const pass = count("pass"), fail = count("fail"), skip = count("skip");
  return { pass, fail, skip, none: items.length - pass - fail - skip, total: items.length };
};

// A clipboard button whose value is handled by the DELEGATED click listener in boot() (so it keeps
// working across live re-renders).
const copyBtn = (value, label) => `<button type="button" class="copy-btn" data-copy="${esc(value)}">${esc(label)}</button>`;

function jobHeader(j) {
  const p = j.params || {};
  const cell = (label, val) => (val ? `<div class="jh-cell"><div class="jh-k">${esc(label)}</div><div class="jh-v">${val}</div></div>` : "");
  const cells = [
    cell("Host", j.host_id ? link(["hosts", j.host_id], j.host_id) : null),
    cell("Kind", badge(j.kind)),
    cell("Worker", j.worker ? `<span class="mono">${esc(j.worker)}</span>` : null),
    cell("Phase", j.phase ? esc(j.phase) : null),
    cell("Started", fmtTime(j.started_at || j.created_at)),
    cell("Finished", j.finished_at ? fmtTime(j.finished_at) : `<span class="muted">${isLive(j) ? "running" : "—"}</span>`),
    cell("Duration", `${runTime(j)}${isLive(j) ? ` <span class="muted">(running)</span>` : ""}`),
    cell("Project", j.project_id ? link([state.game, "projects", j.project_id], j.project_id) : null),
  ].filter(Boolean).join("");
  const url = j.web_url || "";
  return `<div class="card job-header">
    <div class="jh-top">
      <div class="jh-title">
        <h1>${esc(j.title)}</h1>
        <div class="jh-tags">${jobStatusBadge(j.status)}${badge(j.kind)}${jobFlags(j)} <span class="mono muted">${esc(j.id)}</span></div>
      </div>
      <div class="jh-actions">${url ? copyBtn(url, "Copy job URL") : ""}</div>
    </div>
    <p class="jh-target mono muted">${esc(j.target)}</p>
    <div class="jh-meta">${cells}</div>
    <div class="jh-intake2">
      <p><span class="jh-k">Purpose</span> ${esc(p.purpose || "—")}</p>
      <p><span class="jh-k">Expected</span> ${esc(p.expected || "—")}</p>
    </div>
  </div>`;
}

// The NOW / STATE region. While running: the SETTING UP / CLEANING UP banner, the stepper and the
// current-test card. When terminal: a compact 'finished' summary (counts + the stepper).
function jobNow(j) {
  if (j.status === "running") {
    return `<div class="job-now" id="job-now">${vxPhaseBanner(j)}${jobStepper(j)}${nowTestingCard(j)}</div>`;
  }
  const items = jobVerifications(j);
  const c = jobCounts(j);
  const cls = j.status === "passed" ? "ok" : j.status === "failed" ? "bad" : j.status === "cancelled" ? "muted" : "warn";
  const word = {
    passed: "Run passed", failed: "Run failed", cancelled: "Run cancelled",
    inconclusive: "Run ended with no pass/fail verdict", error: "Run errored",
  }[j.status] || "Run ended";
  const line = c.total
    ? `<strong>${c.pass}</strong> pass · <strong>${c.fail}</strong> fail · <strong>${c.skip}</strong> skip · <strong>${c.none}</strong> no verdict`
    : `<span class="muted">no verification steps recorded</span>`;
  return `<div class="job-now job-done">
    <div class="vx-complete vx-complete-${cls}">${word} — ${line}</div>
    ${items.length ? jobStepper(j) : ""}
  </div>`;
}

// RESULTS: one row per step — verdict, source, note, late flag, when, and its screenshots (the
// verdict shot marked right beside the verdict badge).
function jobResultsTable(j) {
  const items = jobVerifications(j);
  if (!items.length) return "";
  const vb = (v) => badge(v, v === "pass" ? "kind" : v === "fail" ? "warn" : "");
  return `<h2>Results <span class="muted">${items.length} step${items.length === 1 ? "" : "s"}</span></h2>
    <div class="card"><table class="vx-rtable">
    <tr><th>#</th><th>Test</th><th>Verdict</th><th>Source</th><th>Note</th><th>Late</th><th>When</th><th>Shots</th></tr>
    ${items
      .map((it) => {
        const v = it.verdict;
        const shots = shotsForStep(j, it.id);
        const verdictShot = shots.find((s) => s.reason === "verdict" || s.verdict);
        const verdictCell = (v ? vb(v.verdict) : `<span class="muted">no verdict</span>`) +
          (verdictShot ? `<div class="vx-vshot">${shotThumb(verdictShot, true)} <span class="muted">screenshot at verdict</span></div>` : "");
        return `<tr><td class="mono">${esc(it.index)}</td>
      <td>${esc(it.name)}<div class="muted mono">${esc(it.id)}</div></td>
      <td>${verdictCell}</td>
      <td>${v && v.source ? badge("via " + v.source) : `<span class="muted">—</span>`}</td>
      <td class="muted">${esc((v && v.notes) || "—")}</td>
      <td>${v && v.late ? badge("late", "warn") : `<span class="muted">—</span>`}</td>
      <td class="muted">${v && v.at ? fmtTime(v.at) : `<span class="muted">—</span>`}</td>
      <td>${shots.length ? `<div class="vx-shotcell">${shots.map((s) => shotThumb(s, true)).join("")}</div>` : `<span class="muted">—</span>`}</td></tr>`;
      })
      .join("")}</table></div>`;
}

// SUMMARY: the four close-out blocks, labelled.
function jobSummaryBlocks(j) {
  if (!j.summary) return "";
  const s = j.summary;
  const b = (l, t) => `<div class="sm-block"><div class="sm-k">${esc(l)}</div><div class="sm-v">${esc(t || "—")}</div></div>`;
  return `<h2>Close-out summary</h2><div class="card sm-grid">${b("What", s.what)}${b("Why", s.why)}${b("Learned", s.learned)}${b("Next", s.next)}</div>`;
}

// INTAKE: the run config + cleanup list (purpose/expected already read in the header).
function jobIntake(j) {
  const p = j.params || {};
  const cell = (label, val) => `<div class="jh-cell"><div class="jh-k">${esc(label)}</div><div class="jh-v">${val}</div></div>`;
  return `<h2>Intake &amp; cleanup</h2><div class="card"><div class="jh-meta">
    ${cell("Cap", `${esc(p.max_minutes)} min`)}
    ${cell("Screenshots", esc(p.screenshots || "—"))}
    ${cell("Confirm gate", p.needs_confirm ? "on" : "off")}
    ${cell("Watching", p.watching ? "yes" : "no")}
    ${p.branch ? cell("Branch", `<span class="mono">${esc(p.branch)}</span>`) : ""}
    ${p.commit ? cell("Commit", `<span class="mono">${esc(p.commit)}</span>`) : ""}
    ${p.reason ? cell("Cap reason", esc(p.reason)) : ""}
  </div>${p.cleanup && p.cleanup.length ? `<p class="muted">Cleanup: ${p.cleanup.map(esc).join(", ")}</p>` : ""}</div>`;
}

// EVIDENCE: screenshots grouped by step (with '#seq · reason · HH:MM:SS · step' captions), and a
// download table for non-image artifacts. Old (untagged) shots group under 'unassigned'.
function jobEvidence(j) {
  const isImg = (a) => /\.(png|jpe?g)$/i.test(a.name);
  const shots = jobScreenshots(j);
  const others = j.artifacts.filter((a) => !isImg(a));
  const groups = new Map();
  for (const s of shots) {
    const key = s.step || "unassigned";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  // Any image artifact with no matching screenshot event still appears (old pages, manual uploads).
  const known = new Set(shots.map((s) => s.url));
  const orphanImgs = j.artifacts.filter((a) => isImg(a) && !known.has(a.url));
  const total = shots.length + orphanImgs.length;
  const orphanHtml = orphanImgs
    .map((a) => `<figure class="shot"><a href="${esc(a.url)}" download><img src="${esc(a.url)}" alt="${esc(a.name)}" loading="lazy"></a><figcaption class="muted mono shot-cap">${esc(a.name)}</figcaption></figure>`)
    .join("");
  const gallery = total
    ? `<h2>Evidence <span class="muted">${total} screenshot${total === 1 ? "" : "s"} · grouped by step</span></h2>
      ${[...groups.entries()]
        .map(([step, list]) => `<div class="ev-group"><h3 class="ev-step mono">${esc(step)} <span class="muted">${list.length}</span></h3>
          <div class="shot-grid">${list.map((s) => shotThumb(s)).join("")}</div></div>`)
        .join("")}
      ${orphanHtml ? `<div class="ev-group"><h3 class="ev-step mono">unassigned <span class="muted">${orphanImgs.length}</span></h3><div class="shot-grid">${orphanHtml}</div></div>` : ""}`
    : "";
  const files = others.length
    ? `<h2>Files <span class="muted">${others.length}</span></h2><table><tr><th>Name</th><th>Size</th><th>MD5</th></tr>${others
        .map(
          (a) =>
            `<tr><td><a href="${esc(a.url)}" download>${esc(a.name)}</a></td>` +
            `<td class="muted">${a.size} B</td><td class="mono muted">${esc(a.md5)}</td></tr>`,
        )
        .join("")}</table>`
    : "";
  return gallery + files;
}

// LINKS: the linked canonical records, clickable, plus any RELATED jobs that share a link (so a
// re-run of the same test is one hop away). Related jobs are computed by the caller (best effort).
function jobLinks(j, related = []) {
  const href = (kind, ref) => {
    if (kind === "project") return `#/${enc(state.game)}/projects/${enc(ref)}`;
    if (kind === "test_run" || kind === "build" || kind === "deploy" || kind === "evidence") return `#/activity`;
    return null;
  };
  const recordRows = j.links
    .map((l) => {
      const h = href(l.kind, l.ref_id);
      return `<tr><td>${badge(l.kind)}</td><td class="mono">${h ? `<a href="${h}">${esc(l.ref_id)}</a>` : esc(l.ref_id)}</td></tr>`;
    })
    .join("");
  const recs = j.links.length
    ? `<table><tr><th>Kind</th><th>Ref</th></tr>${recordRows}</table>`
    : `<p class="muted">No linked records.</p>`;
  const rel = related.length
    ? `<div class="muted" style="margin-top:8px">Related jobs</div><ul class="rel-jobs">${related
        .map((o) => `<li>${link(["jobs", o.id], o.id)} ${jobStatusBadge(o.status)} <span class="muted">· ${esc(o.title)}</span></li>`)
        .join("")}</ul>`
    : "";
  return `<h2>Links</h2><div class="card">${recs}${rel}</div>`;
}

// TIMELINE: grouped by phase/step into collapsible blocks, level-coloured. Routine info lines
// (heartbeats, repeated screenshot/poll lines) fold into a 'show N more' toggle; warn / error /
// crash / milestone / learning and user-feedback / ledger-correction events are ALWAYS visible.
const TIMELINE_NOISE = /heartbeat|still running|no new output|screenshot:|^shot shot-|director (up|alive|launched)|polling|^\s*$/i;
const TIMELINE_IMPORTANT_LEVELS = new Set(["warn", "error", "crash", "milestone", "learning"]);
const TIMELINE_IMPORTANT_PHASES = new Set(["user-feedback", "ledger-correction"]);
const isTimelineNoise = (e) => e.level === "info" && TIMELINE_NOISE.test(e.message || "");
const timelineRow = (e) => {
  const tail =
    e.data && e.data.log_tail
      ? `<details><summary class="muted">log tail</summary><pre>${esc(e.data.log_tail)}</pre></details>`
      : "";
  const vd = e.data && e.data.verdict
    ? ` ${badge(e.data.verdict, e.data.verdict === "pass" ? "kind" : e.data.verdict === "fail" ? "warn" : "")}` +
      `${e.data.source ? ` ${badge("via " + e.data.source)}` : ""}`
    : "";
  return `<li class="tl-ev tl-${esc(e.level)}"${e.phase ? ` data-vx-step="${esc(e.phase)}"` : ""}>${badge(e.level, JOB_LEVEL_CLASS[e.level] || "")}${vd} ` +
    `<span class="muted">${fmtTime(e.created_at)}</span>${e.phase ? ` <span class="mono muted">${esc(e.phase)}</span>` : ""}` +
    `<div>${esc(e.message)}</div>${tail}</li>`;
};
function jobTimelineGrouped(events) {
  if (!events || !events.length) return `<h2>Timeline</h2><p class="muted">No events yet.</p>`;
  const groups = new Map();
  for (const e of events) {
    const key = e.phase || "(general)";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  }
  const blocks = [...groups.entries()]
    .sort((a, b) => a[1][0].id - b[1][0].id)
    .map(([key, evs]) => {
      const important = evs.filter((e) => !isTimelineNoise(e));
      const noise = evs.filter(isTimelineNoise);
      const open = evs.some((e) => TIMELINE_IMPORTANT_LEVELS.has(e.level) || TIMELINE_IMPORTANT_PHASES.has(e.phase));
      const count = `${important.length}${noise.length ? ` <span class="muted">(+${noise.length} routine)</span>` : ""}`;
      const rows = important.map(timelineRow).join("");
      const folded = noise.length
        ? `<details class="tl-noise"><summary class="muted">show ${noise.length} more routine line${noise.length === 1 ? "" : "s"}</summary><ul class="timeline">${noise.map(timelineRow).join("")}</ul></details>`
        : "";
      return `<details class="tl-group"${open ? " open" : ""}><summary><span class="mono">${esc(key)}</span> <span class="muted">${count}</span></summary>
        ${rows ? `<ul class="timeline">${rows}</ul>` : ""}${folded}</details>`;
    })
    .join("");
  return `<h2>Timeline <span class="muted">${events.length} events</span></h2>${blocks}`;
}

function jobDetail(j, related = []) {
  const vx = jobVerifications(j);
  const results = j.status !== "running" || vx.some((x) => x.verdict) ? jobResultsTable(j) : "";
  return `${nowLine()}${jobHeader(j)}${jobNow(j)}${results}${jobSummaryBlocks(j)}${jobIntake(j)}${jobEvidence(j)}${jobLinks(j, related)}${jobTimelineGrouped(j.events)}`;
}

// Load a job plus the RELATED jobs that share one of its linked records (a re-run of the same test is
// then one hop away). Best effort: a failure just renders without the related list.
async function loadJobDetail(jobId) {
  const j = await api(`/jobs/${enc(jobId)}`);
  let related = [];
  try {
    const refs = new Set((j.links || []).map((l) => `${l.kind}:${l.ref_id}`));
    if (refs.size) {
      const all = await api("/jobs", { limit: 200 });
      related = all.filter((o) => o.id !== j.id && (o.links || []).some((l) => refs.has(`${l.kind}:${l.ref_id}`)));
    }
  } catch { /* related jobs are best effort */ }
  return jobDetail(j, related);
}

// Clipboard + screenshot lightbox helpers. Wired through ONE delegated click listener (boot), so
// they survive the periodic #live re-render.
function copyText(el) {
  const v = el.dataset.copy || "";
  const restore = el.textContent;
  const done = () => { el.textContent = "Copied ✓"; setTimeout(() => (el.textContent = restore), 1500); };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(v).then(done, done);
  else done();
}
function openLightbox(url, caption) {
  let box = document.getElementById("lightbox");
  if (!box) {
    box = document.createElement("div");
    box.id = "lightbox";
    box.className = "lightbox";
    box.innerHTML = `<figure><img alt=""><figcaption class="muted"></figcaption></figure>`;
    box.addEventListener("click", closeLightbox);
    document.body.appendChild(box);
  }
  box.querySelector("img").src = url;
  box.querySelector("figcaption").textContent = caption || "";
  box.classList.add("open");
}
function closeLightbox() {
  const box = document.getElementById("lightbox");
  if (box) box.classList.remove("open");
}

// Minimal Markdown renderer for docs: escape first, then add a small safe subset of formatting.
function markdown(src) {
  const blocks = [];
  let text = String(src).replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code>${esc(code)}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  text = esc(text)
    .replace(/(?:^\|.*\|[ \t]*(?:\n|$))+/gm, (block) => {
      const rows = block.trim().split("\n").filter((r) => !/^\|[\s|:-]+\|$/.test(r.trim()));
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const [head, ...body] = rows;
      return `<table><tr>${cells(head).map((c) => `<th>${c}</th>`).join("")}</tr>${body
        .map((r) => `<tr>${cells(r).map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</table>`;
    })
    .replace(/^### (.*)$/gm, "<h4>$1</h4>").replace(/^## (.*)$/gm, "<h3>$1</h3>").replace(/^# (.*)$/gm, "<h2>$1</h2>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/^\s*[-*] (.*)$/gm, "<li>$1</li>")
    .replace(/^:::(\w+)\s*$/gm, '<div class="muted">[$1]</div>').replace(/^:::\s*$/gm, "")
    .replace(/\n{2,}/g, "<br><br>");
  return text.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[Number(i)]);
}

async function api(path, params = {}) {
  const url = new URL("/api" + path, location.origin);
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    (Array.isArray(v) ? v : [v]).forEach((x) => url.searchParams.append(k, x));
  }
  const res = await fetch(url);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
  return body;
}

// ------------------------------------------------------------------ routing
// The sidebar, clustered by intent instead of one flat list of 17 links:
//   Reference — the indexed game data you look things up in (the app's primary purpose)
//   Workflow  — the run pipeline, read top-down as its lifecycle (plan → queue → run → machines)
//   Authoring — offline helpers used while writing scripts/configs
//   Index     — install-wide numbers
// Reference leads on purpose: this install is first of all a docs index, and leading with it also keeps
// the top of the sidebar where it has always been, so grouping is additive rather than a re-learn. The
// Workflow stages are ordered as a lifecycle because that IS their relation: Orchestration plans the
// work, Jobs is the human-facing queue/ledger for it, Activity is the live record of the runs it
// produced, and Hosts are the machines those runs hold. Validate is NOT a stage — it checks a
// script/cfg offline, before anything is planned or queued — so it sits under Authoring and is offered
// from the pipeline strip on the workflow pages.
const NAV_GROUPS = [
  ["Reference", [
    ["search", "Search"], ["convars", "ConVars & Commands"], ["entities", "Entities (Hammer)"],
    ["schema", "Schema"], ["events", "Game Events"], ["script", "cs_script API"], ["examples", "Examples"],
    ["pulse", "Pulse"], ["docs", "Docs (source2.wiki)"], ["diff", "Build Diff"],
  ]],
  ["Workflow", [
    ["overview", "Overview"], ["projects", "Orchestration"], ["jobs", "Jobs"], ["activity", "Activity"], ["hosts", "Hosts"],
  ], "one dashboard, then plan → queue → live runs → machines"],
  ["Authoring", [
    ["validate", "Validate"], ["prompts", "Prompts"],
  ]],
  ["Index", [
    ["stats", "Stats"],
  ]],
];
// Flat, in sidebar order — the route default and the active highlight use this.
const SECTIONS = NAV_GROUPS.flatMap(([, items]) => items);
// Pages that are about the whole install (hosts/runs/jobs), not one game — routed as #/<section>, no game prefix.
const GLOBAL_SECTIONS = new Set(["overview", "hosts", "jobs", "activity"]);

// The workflow pages are stages of ONE loop, so each of them carries this strip: the whole pipeline in
// order with the current stage lit, plus the offline Validate check on the side. It answers "how do
// these relate?" on the page itself, and every stage is one click from every other.
const WORKFLOW_STAGES = [
  ["projects", "Plan", "Orchestration", "multi-step DAG + quality gates; the agent works it step by step"],
  ["jobs", "Queue", "Jobs", "one record per requested run: intake, timeline, verdicts, close-out summary"],
  ["activity", "Run", "Activity", "live build/test/deploy runs across all hosts, service-fed"],
  ["hosts", "Machines", "Hosts", "the hosts themselves and who holds each one's lease right now"],
];
const sectionHref = (id) => (GLOBAL_SECTIONS.has(id) ? `#/${id}` : `#/${enc(state.game)}/${id}`);
function pipelineStrip(active) {
  const stages = WORKFLOW_STAGES.map(([id, step, label, tip], i) =>
    `<a class="pipe-a ${id === active ? "active" : ""}" href="${sectionHref(id)}" title="${esc(tip)}">` +
    `<span class="pipe-n">${i + 1}</span> ${esc(step)} · ${esc(label)}</a>`).join("");
  const check = `<a class="pipe-a ${active === "validate" ? "active" : ""}" href="${sectionHref("validate")}" ` +
    `title="check a cs_script / .cfg against this build offline — before it is planned or queued">✓ Validate</a>`;
  return `<div class="pipe"><span class="pipe-t">Workflow</span>${stages}<span class="pipe-sep"></span>${check}</div>`;
}

function parseHash() {
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { parts, params: Object.fromEntries(new URLSearchParams(query)) };
}
function go(parts, params = {}) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null && v !== false));
  location.hash = "#/" + parts.map(enc).join("/") + (qs.toString() ? "?" + qs : "");
}
const link = (parts, label, params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return `<a href="#/${parts.map(enc).join("/")}${qs ? "?" + qs : ""}">${esc(label)}</a>`;
};

function renderNav(active) {
  const g = state.games.find((x) => x.id === state.game);
  const item = ([id, label]) => {
    const disabled = (id === "script" || id === "examples") && !g?.script_api;
    const href = sectionHref(id);
    return `<a href="${href}" class="${id === active ? "active" : ""} ${disabled ? "disabled" : ""}">${esc(label)}</a>`;
  };
  $("#nav").innerHTML = NAV_GROUPS.map(
    ([title, items, hint]) =>
      `<div class="nav-group">${esc(title)}</div>${hint ? `<div class="nav-hint">${esc(hint)}</div>` : ""}${items
        .map(item)
        .join("")}`,
  ).join("");
  $("#build").textContent = g?.current_build ? `build ${g.current_build} · ${g.patch_version || ""}` : "not indexed";
}

// A stale tab loaded with `?verdict=…` in location.search is the signature of the old native-GET
// submit bug (`/?notes=&verdict=pass#/jobs/…`). We must NEVER auto-submit it: strip the query from
// the address bar (history.replaceState, no reload) and surface a dismissible notice asking the
// user to re-send, since the verdict may never have reached the server.
let staleVerdictNotice = null;
function stripStaleVerdictQuery() {
  try {
    const qs = new URLSearchParams(location.search);
    if (!qs.has("verdict")) return;
    staleVerdictNotice = qs.get("verdict") || "";
    history.replaceState(null, "", location.pathname + location.hash);
  } catch { /* no URLSearchParams/history: nothing to strip */ }
}
function showStaleVerdictNotice(verdict) {
  const el = document.createElement("div");
  el.className = "card stale-verdict";
  el.innerHTML = `<span class="stale-ico">⚠</span> <strong>A verdict from an old page</strong>` +
    `${verdict ? ` (${esc(verdict)})` : ""} may not have been recorded — please re-send it on the current test.` +
    ` <button type="button" class="stale-dismiss">Dismiss</button>`;
  el.querySelector(".stale-dismiss").addEventListener("click", () => el.remove());
  view.prepend(el);
}

async function route() {
  let { parts, params } = parseHash();
  stripStaleVerdictQuery();
  if (parts[0] && state.games.some((g) => g.id === parts[0])) {
    state.game = parts.shift();
    localStorage.setItem("s2docs.game", state.game);
    $("#game").value = state.game;
  }
  const [section = "search", ...rest] = parts;
  renderNav(section);
  document.title = "Source 2 Docs"; // a fresh page resets the tab title (job pages re-set it below)
  clearTimeout(liveTimer); // any auto-refresh from the previous page stops now
  liveTimer = null;
  liveDirty = false; // a fresh page starts unedited; the previous page's typing is gone
  if (section === "search" && params.q !== undefined) $("#q").value = params.q;
  view.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const handler = ROUTES[section] || ROUTES.search;
    view.innerHTML = await handler(rest, params);
    bindFilters(section, rest, params);
    jobPostRender(view); // stepper->card transition detection + document.title for a job page
    // Surface (once) the notice for a stale ?verdict= query captured at the top of route().
    if (staleVerdictNotice != null) { showStaleVerdictNotice(staleVerdictNotice); staleVerdictNotice = null; }
    // A live page (Hosts/Activity) parked a render fn in pendingLive; refill its #live region on a timer
    // so the strip updates without re-rendering the header or jumping the scroll position.
    // `ms` may be a function (e.g. poll fast while a run is active, slow when idle); a recursive
    // setTimeout re-evaluates it after every tick, which setInterval cannot do.
    if (pendingLive) {
      const { selector, render, ms } = pendingLive;
      pendingLive = null;
      const delay = () => (typeof ms === "function" ? ms() : ms);
      const tick = async () => {
        const el = view.querySelector(selector);
        if (!el) { liveTimer = null; return; }
        // Guard (1): never replace a live region the user is editing — that swap would wipe the notes
        // box / screenshot note mid-typing. Skip this render (just reschedule) while an input inside the
        // region has focus or holds text the user typed but has not submitted.
        if (liveBeingEdited(el)) { liveTimer = setTimeout(tick, delay()); return; }
        try {
          // Guard (2): if a background render does run, snapshot each input's value/focus/caret by a
          // stable key and restore it after the innerHTML swap, so nothing half-typed is dropped.
          const saved = snapshotLiveInputs(el);
          el.innerHTML = await render();
          restoreLiveInputs(el, saved);
          jobPostRender(el); // detect a moved-on test and animate/banner it
        } catch { /* keep the last good view */ }
        liveTimer = setTimeout(tick, delay());
      };
      const region = view.querySelector(selector);
      if (region) region.addEventListener("input", liveInputListener); // mark dirty the moment the user types
      liveTimer = setTimeout(tick, delay());
    }
  } catch (e) {
    view.innerHTML = `<h1>Error</h1><p class="err">${esc(e.message)}</p>`;
  }
  window.scrollTo(0, 0);
}

// Auto-refresh plumbing for the Hosts and Activity pages (a live render fn for a #live region).
let liveTimer = null;
let pendingLive = null;
const liveBody = (selector, render, ms = 5000) => { pendingLive = { selector, render, ms }; };

// ---- Keeping a live region safe to type in ------------------------------------------------
// A live page (job detail) parks forms INSIDE its #live region, and the tick above replaces that
// region's innerHTML on every poll. Two guards stop that from eating the user's typing:
//   (1) liveBeingEdited() — the tick skips the render (reschedules only) while any input/textarea in
//       the region has focus or holds unsubmitted text. `liveDirty` is set the instant the user types
//       (see liveInputListener) and cleared after a successful submit (bindJobVerdict / bindJobShot).
//   (2) snapshotLiveInputs()/restoreLiveInputs() — even when a render does run (nothing was focused,
//       nothing was dirty), each input's value, focus and caret are saved by a stable key and restored
//       across the swap, so a background refresh can never silently drop a value.
let liveDirty = false;
function liveInputListener(ev) {
  const t = ev.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) liveDirty = true;
}
function liveBeingEdited(root) {
  const a = document.activeElement;
  if (a && root.contains(a) && (a.tagName === "INPUT" || a.tagName === "TEXTAREA")) return true; // focused
  if (liveDirty) return true; // typed something, not submitted yet
  // belt-and-braces: a text box that already holds text the user put there (never a checkbox/radio)
  return [...root.querySelectorAll("input, textarea")].some(
    (i) => String(i.value || "").length > 0 && i.type !== "checkbox" && i.type !== "radio",
  );
}
const liveInputKey = (el, i) => el.name || el.getAttribute("data-live-key") || el.id || `${el.tagName}:${i}`;
function snapshotLiveInputs(root) {
  return [...root.querySelectorAll("input, textarea")].map((el, i) => ({
    key: liveInputKey(el, i),
    i,
    value: el.value,
    focused: el === document.activeElement,
    start: el.selectionStart,
    end: el.selectionEnd,
  }));
}
function restoreLiveInputs(root, snap) {
  if (!snap || !snap.length) return;
  const els = [...root.querySelectorAll("input, textarea")];
  const used = new Set();
  for (const s of snap) {
    if (!s.value) continue;
    // Match by stable key first, then fall back to the old positional slot.
    let el = els.find((e, i) => !used.has(i) && liveInputKey(e, i) === s.key);
    if (!el) el = els[s.i];
    if (!el) continue;
    const at = els.indexOf(el);
    if (used.has(at) || el.value) continue; // don't stomp a fresh render's own value
    used.add(at);
    el.value = s.value;
    if (s.focused) {
      el.focus();
      try { el.setSelectionRange(s.start, s.end); } catch { /* number/email inputs reject this */ }
    }
  }
}

// Filters: <form class="filters"> whose inputs map 1:1 to hash params.
function bindFilters(section, rest, params) {
  const form = view.querySelector("form.filters");
  if (form) {
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const p = {};
      for (const el of form.elements) if (el.name) p[el.name] = el.type === "checkbox" ? (el.checked ? "1" : "") : el.value;
      go([state.game, section, ...rest], p);
    });
    form.querySelectorAll("select, input[type=checkbox]").forEach((el) => el.addEventListener("change", () => form.requestSubmit()));
  }
  view.querySelectorAll("[data-offset]").forEach((b) =>
    b.addEventListener("click", () => go([state.game, section, ...rest], { ...params, offset: b.dataset.offset })),
  );
  view.querySelectorAll("form[data-prompt]").forEach(bindPromptForm);
  view.querySelectorAll("form[data-validate]").forEach(bindValidateForm);
  view.querySelectorAll("[data-copy-md]").forEach(bindCopyMd);
  view.querySelectorAll("form[data-start-project]").forEach(bindStartProject);
  view.querySelectorAll("form[data-step]").forEach(bindStepForm);
  // The dynamic JOB forms (data-job-verdict / data-job-shot) are NOT bound here. They live inside
  // the #live region, whose innerHTML is replaced on every auto-refresh, and a per-form listener is
  // destroyed by that swap — leaving the Pass/Fail/Skip buttons to do a NATIVE GET submit and lose
  // the verdict. One delegated listener (bindJobFormDelegation, wired once in boot) handles them
  // for the life of the page, so a re-render can never unbind them.
}

// Copy the current (filtered) diff as a Markdown table. The table is built into a hidden <textarea>
// at render time — that box IS the fallback: when the clipboard API is unavailable (or denied), reveal
// it and select the text so the user can copy by hand.
function bindCopyMd(btn) {
  const box = view.querySelector("#diff-md");
  btn.addEventListener("click", async () => {
    const text = box ? box.value : "";
    try {
      await navigator.clipboard.writeText(text);
      const label = btn.textContent;
      btn.textContent = "Copied ✓";
      setTimeout(() => (btn.textContent = label), 1500);
    } catch {
      if (box) {
        box.hidden = false;
        box.focus();
        box.select();
      }
    }
  });
}

// job-form-delegation:start
// Delegated handlers: ONE `submit` listener on the document (bindJobFormDelegation, wired in boot)
// routes every dynamic job form here, so a live re-render that swaps the form's innerHTML can never
// leave it without a handler. The old per-form binding did exactly that — after the first refresh
// the Pass/Fail/Skip buttons had no listener and did a NATIVE GET submit to
// `/?notes=…&verdict=pass#/jobs/…`, silently losing the verdict (reported 2026-10-15, job
// j-cfce382e). Each handler still calls preventDefault itself, so a native submit is impossible.
async function bindJobShot(form, ev) {
  if (ev) ev.preventDefault();
  const note = form.elements.note.value.trim() || null;
  const r = await postJson(`/api/jobs/${enc(form.dataset.jobShot)}/screenshot-request`, { note });
  if (!r.ok) {
    form.querySelector(".result").innerHTML = `<p class="err">${esc(r.body.detail)}</p>`;
    return;
  }
  liveDirty = false; // request recorded: let auto-refresh resume and re-render immediately
  route();
}

// The NOW TESTING card's Pass/Fail/Skip buttons: the pressed button's value is the verdict; the note
// rides along. On click all three disable and the pressed one shows a spinner + 'Sending…'; on success
// the card re-renders into the recorded + waiting state; on error the buttons come back with the error
// inline. A verdict that arrives from chat/console shows the same recorded state on the next refresh.
async function bindJobVerdict(form, ev) {
  if (ev) ev.preventDefault();
  const pressed = ev && ev.submitter;
  const verdict = pressed && pressed.value;
  if (!verdict) return;
  const notes = form.elements.notes.value.trim() || null;
  const btns = [...form.querySelectorAll("button[name=verdict]")];
  const label = pressed ? pressed.innerHTML : "";
  btns.forEach((b) => (b.disabled = true)); // pressed state: every button locks
  if (pressed) pressed.innerHTML = `<span class="vx-spin"></span> Sending…`;
  const r = await postJson(`/api/jobs/${enc(form.dataset.jobVerdict)}/verdict`, { verdict, notes, source: "web" });
  if (!r.ok) {
    // Re-enable so the user can retry, and show the failure inline in red.
    btns.forEach((b) => (b.disabled = false));
    if (pressed) pressed.innerHTML = label;
    form.querySelector(".result").innerHTML = `<p class="err vx-error">${esc(r.body.detail || "could not record the verdict")}</p>`;
    return;
  }
  // The verdict is recorded: clear the note box and refresh the card at once into the recorded +
  // 'waiting for the demo to move on' state. Clearing liveDirty lets auto-refresh take over again.
  liveDirty = false;
  form.elements.notes.value = "";
  await refreshJobLive(form.dataset.jobVerdict);
}

// ONE delegated `submit` listener for the dynamic job forms. Attached to the document (not to the
// forms), so replacing a form's markup on the 5 s live re-render can never unbind it — this is the
// fix for the silently-lost-verdict bug. Any form marked as a job form is ALWAYS preventDefault'd,
// so even a form we do not recognise can never navigate the page away.
function bindJobFormDelegation() {
  document.addEventListener("submit", (ev) => {
    const form = ev.target;
    if (!form || form.tagName !== "FORM" || typeof form.matches !== "function") return;
    if (form.matches("form[data-job-verdict]")) bindJobVerdict(form, ev);
    else if (form.matches("form[data-job-shot]")) bindJobShot(form, ev);
    else if (form.matches("form[data-job-form]")) ev.preventDefault();
  });
}
// job-form-delegation:end

// Re-render just the job page's #live region from the server (keeps the scroll position and any
// transition animating in place) — used after a verdict lands so the card shows the recorded state.
async function refreshJobLive(jobId) {
  const live = view.querySelector("#live");
  if (!live) return;
  try {
    live.innerHTML = await loadJobDetail(jobId);
    jobPostRender(live);
  } catch {
    /* keep the last good view */
  }
}

// ---- Stepper/card transitions across the 5 s refresh --------------------------
// The whole #live region is re-rendered on every poll, so "the current test changed" is detected by
// comparing this refresh's card step id to the last one rendered. A change animates the card and pops
// a transient banner; document.title mirrors 'TEST k/n — NAME' so the browser tab tracks progress too.
let vxPrevStep = null;
function jobPostRender(root) {
  const card = root.querySelector("[data-vx-card]");
  const step = card ? card.dataset.vxStep : null;
  if (!card) {
    vxPrevStep = null;
    return;
  }
  const k = card.dataset.vxIndex, n = card.dataset.vxTotal, name = card.dataset.vxName || "";
  document.title = `TEST ${k}/${n} — ${name}`;
  if (vxPrevStep && step && step !== vxPrevStep) {
    card.classList.add("vx-enter");
    setTimeout(() => card.classList.remove("vx-enter"), 800);
    showTransitionBanner(k, n, name);
  }
  vxPrevStep = step;
}
// A transient 'Moved on to TEST k: NAME' banner. Appended to <body> so the region's re-render cannot
// wipe it mid-flight; it fades and removes itself after ~4 s.
function showTransitionBanner(k, n, name) {
  const el = document.createElement("div");
  el.className = "vx-banner";
  el.textContent = `Moved on to TEST ${k}: ${name}`;
  document.body.appendChild(el);
  setTimeout(() => el.classList.add("vx-gone"), 3500);
  setTimeout(() => el.remove(), 4200);
}
// Clicking a done chip scrolls to that test's timeline entry (the verdict milestone if there is one).
function gotoStepTimeline(id) {
  if (!id) return;
  const rows = [...view.querySelectorAll(`[data-vx-step="${window.CSS && CSS.escape ? CSS.escape(id) : id}"]`)];
  const target = rows[rows.length - 1];
  if (target) target.scrollIntoView({ behavior: "smooth", block: "center" });
}

// Live countdown to each step's deadline_utc (the NOW TESTING card): a 1 s client-side ticker, so the
// numeric 'm:ss left' AND the bar fill keep moving between the 5 s region re-renders. Elements are
// re-read every tick. A [data-vx-fill] element is the bar (width % via a CSS transition); anything
// else with [data-deadline] is the numeric readout.
function updateCountdowns() {
  document.querySelectorAll("[data-deadline]").forEach((el) => {
    const t = Date.parse(el.dataset.deadline);
    if (Number.isNaN(t)) return;
    const left = Math.floor((t - Date.now()) / 1000);
    if (el.hasAttribute("data-vx-fill")) {
      const s = el.dataset.start ? Date.parse(el.dataset.start) : NaN;
      const tot = Number.isNaN(s) ? null : (t - s) / 1000;
      const frac = tot && tot > 0 ? Math.max(0, Math.min(1, left / tot)) : left > 0 ? 1 : 0;
      el.style.width = `${(frac * 100).toFixed(1)}%`;
      return;
    }
    el.textContent = left <= 0 ? "0:00 (expired)" : `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} left`;
  });
  // The SETTING UP / CLEANING UP countdowns tick in whole seconds ('8s'), not 'm:ss left'.
  document.querySelectorAll("[data-cd-sec]").forEach((el) => {
    const t = Date.parse(el.dataset.cdSec);
    if (Number.isNaN(t)) return;
    const left = Math.max(0, Math.ceil((t - Date.now()) / 1000));
    el.textContent = left > 0 ? `${left}s` : "0s";
  });
}

const pager = (page) => {
  const { total, limit, offset } = page;
  if (total <= limit) return `<p class="muted">${total} result${total === 1 ? "" : "s"}</p>`;
  const prev = Math.max(0, offset - limit), next = offset + limit;
  return `<div class="pager">
    <button data-offset="${prev}" ${offset === 0 ? "disabled" : ""}>← Prev</button>
    <span class="muted">${offset + 1}–${Math.min(next, total)} of ${total}</span>
    <button data-offset="${next}" ${next >= total ? "disabled" : ""}>Next →</button></div>`;
};
const input = (name, value, ph) => `<input name="${name}" value="${esc(value || "")}" placeholder="${esc(ph)}">`;
const select = (name, value, options) =>
  `<select name="${name}">${options.map(([v, l]) => `<option value="${esc(v)}" ${v === (value || "") ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;

// ---- Build Diff helpers -----------------------------------------------------
// Each member change renders as two labelled cells: the value that existed BEFORE and the value that
// exists AFTER. A missing side reads '— absent' (an added row must not look like it has a before
// value). Long values are never cut mid-token: they stay whole inside a bounded scroll box behind a
// <details> expander, and the preview stops on a word boundary. For `changed` rows the shared
// prefix/suffix is trimmed so only the differing span stands out. Field-level rows cluster under their
// parent key (the API's `group`); a flat section (group null, e.g. convars) renders one table.
// diff-helpers:start
const CHANGE_RANK = { added: 0, changed: 1, removed: 2 };
const DIFF_VAL_MAX = 160; // values longer than this collapse into an expander
const changeBadge = (c) => badge(c, c === "removed" ? "warn" : c === "added" ? "kind" : "");
const absentCell = () => `<span class="muted">— absent</span>`;
// A word-boundary preview: cut at the last space, never inside a token; say how much is hidden.
const wordPreview = (s, n = 90) => {
  if (s.length <= n) return s;
  const cut = s.slice(0, n);
  const sp = cut.lastIndexOf(" ");
  const head = sp > n * 0.5 ? cut.slice(0, sp) : cut;
  return `${head} … (${s.length - head.length} more chars)`;
};
// One side's value. Whole value inside a bounded scroll box; nothing is silently truncated.
const diffVal = (v) => {
  if (v == null || v === "") return absentCell();
  const s = String(v);
  if (s.length <= DIFF_VAL_MAX) return `<span class="mono">${esc(s)}</span>`;
  return (
    `<details class="diff-long"><summary class="mono">${esc(wordPreview(s))}</summary>` +
    `<pre class="mono diff-val">${esc(s)}</pre></details>`
  );
};
// The shared head/tail length of two strings, so a `changed` pair shows only what differs.
function commonEdges(a, b) {
  const min = Math.min(a.length, b.length);
  let p = 0;
  while (p < min && a[p] === b[p]) p++;
  let s = 0;
  while (s < min - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
  return [p, s];
}
const elideHead = (s, n = 40) => (s.length > n ? "…" + s.slice(s.length - n) : s);
const elideTail = (s, n = 40) => (s.length > n ? s.slice(0, n) + "…" : s);
// One cell of a `changed` row: shared prefix/suffix muted, the differing span highlighted.
const diffChangedCell = (v, other, side) => {
  if (v == null) return absentCell();
  const a = String(v);
  const b = String(other ?? "");
  const [p, suf] = commonEdges(a, b);
  const mid = a.slice(p, a.length - suf);
  const pre = p ? `<span class="muted">${esc(elideHead(a.slice(0, p)))}</span>` : "";
  const post = suf ? `<span class="muted">${esc(elideTail(a.slice(a.length - suf)))}</span>` : "";
  const body = mid ? `<span class="diff-${side}">${esc(mid)}</span>` : `<span class="muted">∅</span>`;
  return `<span class="mono">${pre}${body}${post}</span>`;
};
const diffRow = (r) => {
  let before, after;
  if (r.change === "changed") {
    before = diffChangedCell(r.before, r.after, "old");
    after = diffChangedCell(r.after, r.before, "new");
  } else if (r.change === "added") {
    before = absentCell();
    after = diffVal(r.after);
  } else {
    before = diffVal(r.before);
    after = absentCell();
  }
  return (
    `<tr class="diff-${r.change}"><td>${changeBadge(r.change)}</td>` +
    `<td class="mono">${esc(r.name)}</td><td>${before}</td><td>${after}</td></tr>`
  );
};
const DIFF_HEAD = "<tr><th>Change</th><th>Name</th><th>Before</th><th>After</th></tr>";
const diffTable = (rows) => `<table class="diff">${DIFF_HEAD}${rows.map(diffRow).join("")}</table>`;
const sortDiffRows = (rows) =>
  rows.slice().sort((a, b) =>
    (CHANGE_RANK[a.change] - CHANGE_RANK[b.change]) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
const diffGrouped = (rows) => {
  const groups = new Map();
  for (const r of rows) {
    const g = r.group || "(ungrouped)";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(r);
  }
  return [...groups.entries()]
    .sort((x, y) => (x[0] < y[0] ? -1 : x[0] > y[0] ? 1 : 0))
    .map(
      ([g, rs]) =>
        `<h3 class="diff-group mono">${esc(g)} <span class="muted">(${rs.length})</span></h3>` + diffTable(rs),
    )
    .join("");
};
// Every section in file order: sections with changes render in full (grouped, with a truncation note
// when the API capped them); a section with no changes collapses to one muted line.
const diffSectionsHtml = (d, match) =>
  Object.entries(d.sections)
    .map(([sec, rows]) => {
      const total = d.totals && d.totals[sec] != null ? d.totals[sec] : rows.length;
      if (!total) return `<p class="muted diff-none">${esc(sec)}: no changes</p>`;
      const shown = sortDiffRows(rows.filter(match));
      if (!shown.length) return `<p class="muted diff-none">${esc(sec)}: no changes match the filter</p>`;
      const cap = total > rows.length
        ? `<span class="muted"> · showing first ${rows.length} of ${total}</span>`
        : "";
      const body = shown.some((r) => r.group != null) ? diffGrouped(shown) : diffTable(shown);
      return `<h2>${esc(sec)} <span class="muted">(${shown.length})</span>${cap}</h2>${body}`;
    })
    .join("");
// Header totals: N added / N removed / N changed, the active-section count, which sections the API
// capped, and (when a `match` filter is passed) how many rows that filter shows. The server's `counts`
// are per-section change-type counts taken BEFORE its per-section row cap, so they stay exact on a
// capped section — counting the rows we received under-reports there. An older server that omits
// `counts` falls back to counting the rows we got (`exact: false`).
const diffCounts = (d, match) => {
  const counts = { added: 0, removed: 0, changed: 0 };
  let shown = 0;
  let sections = 0;
  const capped = [];
  for (const [sec, rows] of Object.entries(d.sections)) {
    const t = d.totals && d.totals[sec] != null ? d.totals[sec] : rows.length;
    if (t) sections += 1;
    shown += rows.length;
    if (t > rows.length) capped.push(sec);
  }
  const server = d.counts && Object.keys(d.counts).length ? d.counts : null;
  if (server) {
    for (const c of Object.values(server)) {
      counts.added += c.added || 0;
      counts.removed += c.removed || 0;
      counts.changed += c.changed || 0;
    }
  } else {
    for (const rows of Object.values(d.sections)) for (const r of rows) counts[r.change] += 1;
  }
  const realTotal = Object.values(d.totals || {}).reduce((a, b) => a + b, 0) || shown;
  const matched = match ? Object.values(d.sections).reduce((n, rows) => n + rows.filter(match).length, 0) : null;
  return { ...counts, total: shown, matched, sections, capped, exact: !!server, truncated: realTotal > shown };
};
// The clipboard table is built from the CURRENT (filtered) view, so what the user copies is what they
// see. Pipe/newline escaping keeps the markdown table from breaking on a value.
const mdCell = (v) => (v == null || v === "" ? "—" : String(v).replace(/\|/g, "\\|").replace(/\n/g, "<br>"));
const diffMarkdown = (d, match) => {
  const lines = [`# Build diff — ${d.game} ${d.from_build} → ${d.to_build}`, ""];
  for (const [sec, rows] of Object.entries(d.sections)) {
    const shown = sortDiffRows(rows.filter(match));
    if (!shown.length) continue;
    lines.push(`## ${sec}`, "", "| Change | Group | Name | Before | After |", "| --- | --- | --- | --- | --- |");
    for (const r of shown) {
      lines.push(`| ${r.change} | ${mdCell(r.group)} | \`${r.name}\` | ${mdCell(r.before)} | ${mdCell(r.after)} |`);
    }
    lines.push("");
  }
  return lines.join("\n");
};
// diff-helpers:end

// Map a search hit (kind + key) to its detail route.
function hitLink(hit) {
  const g = hit.game, k = hit.key;
  switch (hit.kind) {
    case "convar": case "command": return link([g, "convars", k], hit.name);
    case "entity": { const [side, cls] = k.split(":"); return link([g, "entities", side, cls], hit.name); }
    case "schema_class": case "schema_enum": {
      const i = k.indexOf(":"); return link([g, "schema", k.slice(i + 1)], hit.name, { module: k.slice(0, i) });
    }
    case "event": return link([g, "events", k], hit.name);
    case "script": { const [t, mem] = k.includes(".") ? k.split(".") : [k, ""]; return link([g, "script", t], k, mem ? { m: mem } : {}); }
    case "pulse": return link([g, "pulse", k], hit.name);
    case "example": return link([g, "examples", k.replace(/^cs_script\//, "")], k);
    case "doc": return link([state.game, "docs"], hit.name, { key: k });
    default: return esc(hit.name);
  }
}

// ---------------------------------------------------- orchestration DAG helpers
// Longest-path depth per step (0 = no deps), used to lay the DAG out in dependency columns.
function stepDepths(steps) {
  const byId = Object.fromEntries(steps.map((s) => [s.id, s]));
  const depth = {};
  const visit = (id, seen) => {
    if (depth[id] != null) return depth[id];
    if (seen.has(id)) return 0; // cycle guard: treat the back-edge as depth 0
    seen.add(id);
    const deps = (byId[id]?.depends_on || []).filter((d) => byId[d]);
    depth[id] = deps.length ? 1 + Math.max(...deps.map((d) => visit(d, seen))) : 0;
    return depth[id];
  };
  steps.forEach((s) => visit(s.id, new Set()));
  return depth;
}

// A compact step-level DAG: one column per dependency depth, connected left→right.
function stepDag(steps) {
  if (!steps.length) return `<p class="muted">No steps.</p>`;
  const depth = stepDepths(steps);
  const maxDepth = Math.max(0, ...Object.values(depth));
  const cols = [];
  for (let d = 0; d <= maxDepth; d++) cols.push(steps.filter((s) => depth[s.id] === d));
  return `<div class="dag">${cols
    .map(
      (layer, i) =>
        `<div class="dag-col">${layer
          .map(
            (s) =>
              `<div class="dag-node ${esc(s.status)}"><strong>${esc(s.title)}</strong>` +
              `<div class="muted mono">${esc(s.id)} · ${esc(s.kind)} · ${esc(s.status)}${s.gate ? " · gate:" + esc(s.gate) : ""}</div>` +
              `<div class="muted">${stepTime(s)}</div></div>`,
          )
          .join("")}</div>` + (i < cols.length - 1 ? `<div class="dag-arrow">→</div>` : ""),
    )
    .join("")}</div>`;
}

// A step's duration: updated_at - started_at (both from the API); '—' until the step has been touched.
const stepTime = (s) => (runSeconds(s) == null ? `<span class="muted">—</span>` : `${runTime(s)}${isLive(s) ? " (running)" : ""}`);

// --------------------------------------------------------------------- Activity
// ONE chronological sequence across the three services. The three feeds arrive as separate lists
// (builds / test runs / deploys), each sorted by updated_at — but a run's updated_at is a heartbeat,
// and the reaper bounces it when it finalizes a dead session, so an hour-old reaped run would jump
// to the top. The timeline therefore sorts on each run's OWN start (started_at, else created_at).
// A run that belongs to a job (via the job ledger's links) carries a job link; consecutive rows for
// the same job nest under one heading, so build -> test -> deploy reads top-down as a single unit.
// activity-timeline:start
// A run's start time — NEVER updated_at (see above). Test runs carry their own started_at; a build
// or deploy starts at created_at.
const activityStart = (r) => r.started_at || r.created_at || "";
// A reaped run: finalized by the reaper after its host lease was lost. Its state is a plain 'idle',
// which reads like a healthy idle run — the note ('reaped: lease lost') is the honest signal.
const REAPED_RE = /reap|lease lost/i;
const runNote = (r) => (r && (r.note || r.notes)) || "";
const isReaped = (r) => REAPED_RE.test(runNote(r));
// The muted 'reaped' tag that replaces the bare 'idle' badge on a reaped row; the run's own note
// rides in the tooltip so the reason is still reachable.
const reapedTag = (r) =>
  `<span class="badge muted reaped" title="${esc(firstLine(runNote(r)) || "reaped: lease lost")}">reaped</span>`;

// Normalize the three feeds into ONE row shape, and attach the job that owns each row (looked up
// from the job ledger's links: {kind, ref_id} -> job id). Sort newest-first on the run's OWN start.
const activityRows = (builds, tests, deploys, jobs) => {
  const owner = new Map();
  const statusById = new Map();
  (jobs || []).forEach((j) => {
    statusById.set(j.id, j.status || null);
    (j.links || []).forEach((l) => owner.set(`${l.kind}:${l.ref_id}`, j.id));
  });
  // The job a run belongs to, plus that job's status: the chain heading shows it, so a run's ledger
  // verdict ('inconclusive' = ended with no pass/fail call) is visible from the timeline itself.
  const own = (key) => {
    const id = owner.get(key) || null;
    return { job: id, jobStatus: id ? statusById.get(id) || null : null };
  };
  const rows = [];
  (builds || []).forEach((b) =>
    rows.push({ type: "build", at: activityStart(b), state: b.status, name: b.kind || "build", id: b.id, host: b.host_id, run: b, ...own(`build:${b.id}`) }));
  (tests || []).forEach((t) =>
    rows.push({ type: "test", at: activityStart(t), state: t.state, name: t.name, id: t.id, host: t.host_id, run: t, ...own(`test_run:${t.id}`) }));
  (deploys || []).forEach((d) =>
    rows.push({ type: "deploy", at: activityStart(d), state: d.status, name: d.addon || d.id, id: d.id, host: d.host_id, run: d, ...own(`deploy:${d.id}`) }));
  const key = (r) => { const t = parseTs(r.at); return Number.isNaN(t) ? -Infinity : t; };
  return rows.sort((a, b) => key(b) - key(a));
};

// Collapse consecutive same-job rows into one chain, so a job's build -> test -> deploy reads as a
// single unit. A row with no job, or a lone one, stays a standalone chain of length 1.
const activityChains = (rows) => {
  const out = [];
  rows.forEach((r) => {
    const last = out[out.length - 1];
    if (r.job && last && last.job === r.job) last.rows.push(r);
    else out.push({ job: r.job, rows: [r] });
  });
  return out;
};

// One timeline row: time | type chip | state (+reaped) | name/kind (with any note) | duration |
// host link | job link ('—' when the run carries no job linkage).
const activityRowHtml = (r) => {
  const st = isReaped(r.run) ? reapedTag(r.run) : stateBadge(r.state);
  const note = runNote(r.run);
  return `<tr class="tl-row tl-${r.type}"><td class="muted">${fmtTime(r.at)}</td>` +
    `<td>${badge(r.type, "tl-" + r.type)}</td>` +
    `<td>${st}${r.run.lock ? lockStuckBadge(r.run.lock) : ""}</td>` +
    `<td class="mono">${esc(r.name)}${note ? `<div class="muted">${esc(firstLine(note))}</div>` : ""}</td>` +
    `<td class="muted">${runTime(r.run)}</td>` +
    `<td>${r.host ? link(["hosts", r.host], r.host) : "—"}</td>` +
    `<td>${r.job ? link(["jobs", r.job], r.job) : "—"}</td></tr>`;
};

// The whole timeline table: standalone rows inline, 2+ consecutive same-job rows nested under a
// job heading (the heading names the chain, e.g. 'build → test → deploy').
const activityTimelineHtml = (rows) => {
  if (!rows.length) return `<p class="muted">No runs recorded yet.</p>`;
  const head =
    `<tr><th>Time</th><th>Type</th><th>State</th><th>Name / kind</th><th>Duration</th><th>Host</th><th>Job</th></tr>`;
  const body = activityChains(rows)
    .map((c) => {
      if (!c.job || c.rows.length === 1) return activityRowHtml(c.rows[0]);
      const types = c.rows.map((r) => r.type).join(" → ");
      const nest = c.rows.map((r) => activityRowHtml(r).replace(`<tr class="tl-row`, `<tr class="tl-row tl-nested`)).join("");
      // The chain heading carries the job's status badge, so an 'inconclusive' chain (a run that ended
      // with no verdict) is readable as such right in the timeline.
      const js = c.rows[0].jobStatus ? ` ${jobStatusBadge(c.rows[0].jobStatus)}` : "";
      return `<tr class="tl-job-head"><td colspan="7" class="muted">job ${link(["jobs", c.job], c.job)}${js} <span class="mono tl-chain">${esc(types)}</span></td></tr>${nest}`;
    })
    .join("");
  return `<table class="timeline">${head}${body}</table>`;
};
// activity-timeline:end

// Shown when nothing has flowed through the build/test/deploy services yet — the failure mode that
// read as 'the page is broken' when in fact no work had been recorded.
const activityEmptyState = () => `<div class="card">
  <strong>No activity yet — and that is expected until work goes through the services.</strong>
  <p class="muted">This page is <strong>service-fed</strong>: a row appears here when work goes through the s2docs
  <strong>build / test / deploy</strong> services. It is <em>not</em> a view of any CS2 that happens to be running
  somewhere — if nothing has been recorded yet, the page is correctly empty.</p>
  <p class="muted">Start something and it shows up within a few seconds:</p>
  <ul class="muted">
    <li><code>build_start</code> (MCP) or <code>s2docs build start &lt;host&gt; -- &lt;command&gt;</code> — a compile/command on a host</li>
    <li><code>test_start</code> or <code>s2docs test start &lt;name&gt;</code> — an in-game test session (the HUD strip)</li>
    <li><code>deploy_push</code> or <code>s2docs deploy push …</code> — files pushed to a host</li>
  </ul>
  <p class="muted">Read-only: nothing on this page mutates a host or a run.</p></div>`;

// ------------------------------------------------------------------- views
const ROUTES = {
  async search(_, p) {
    if (!p.q) return home();
    const kinds = ["", "convar", "command", "entity", "schema_class", "schema_enum", "event", "script", "pulse"];
    const page = await api("/search", { q: p.q, game: p.all ? "" : state.game, kind: p.kind, limit: 25, offset: p.offset });
    return `<h1>Search: ${esc(p.q)}</h1>
      <form class="filters">${input("q", p.q, "query")}${select("kind", p.kind, kinds.map((k) => [k, k || "all kinds"]))}
        <label><input type="checkbox" name="all" ${p.all ? "checked" : ""}> all games</label></form>
      ${page.items.map((h) => `<div class="hit">${badge(h.kind, "kind")}${p.all ? badge(h.game) : ""} <strong>${hitLink(h)}</strong>
        <span class="muted">${esc(h.title)}</span>${h.snippet ? `<div class="snip">${snippet(h.snippet)}</div>` : ""}</div>`).join("") || "<p>No results.</p>"}
      ${pager(page)}`;
  },

  async convars([name], p) {
    if (name) {
      const c = await api(`/games/${state.game}/convars/${enc(name)}`);
      return `<p>${link([state.game, "convars"], "← ConVars")}</p><h1 class="mono">${esc(c.name)}</h1>
        <p>${badge(c.kind, "kind")}${c.flags.map((f) => link([state.game, "convars"], f, { flag: f })).map((a) => `<span class="badge">${a}</span>`).join("")}</p>
        <table><tr><th>Default</th><td class="mono">${esc(c.default_value ?? "—")}</td></tr>
        <tr><th>Min</th><td class="mono">${esc(c.min_value ?? "—")}</td></tr><tr><th>Max</th><td class="mono">${esc(c.max_value ?? "—")}</td></tr></table>
        <h2>Description</h2><pre>${esc(c.description || "No description provided.")}</pre>
        <h2>Usage</h2><pre>${esc(c.kind === "convar" ? `${c.name} ${c.default_value ?? ""}`.trim() : c.name)}</pre>`;
    }
    const [page, flags] = await Promise.all([
      api(`/games/${state.game}/convars`, { q: p.q, kind: p.kind, flag: p.flag, limit: 100, offset: p.offset }),
      api(`/games/${state.game}/convar-flags`),
    ]);
    return `<h1>Console variables &amp; commands</h1>
      <form class="filters">${input("q", p.q, "name or description contains…")}
        ${select("kind", p.kind, [["", "convars + commands"], ["convar", "convars"], ["command", "commands"]])}
        ${select("flag", p.flag, [["", "any flag"], ...flags.map((f) => [f.flag, `${f.flag} (${f.count})`])])}</form>
      <table><tr><th>Name</th><th>Default</th><th>Flags</th><th>Description</th></tr>
      ${page.items.map((c) => `<tr><td class="mono">${link([state.game, "convars", c.name], c.name)}</td>
        <td class="mono">${esc(c.default_value ?? "")}</td><td>${c.flags.map((f) => badge(f)).join("")}</td>
        <td>${esc(firstLine(c.description))}</td></tr>`).join("")}</table>${pager(page)}`;
  },

  async entities([side, cls], p) {
    if (cls) {
      const e = await api(`/games/${state.game}/entities/${enc(side)}/${enc(cls)}`);
      const rows = (kind) => e.members.filter((m) => m.kind === kind);
      const srcBadge = (m) => m.source === "valve_fgd" ? badge("editor key", "kind") : m.source === "runtime" ? badge("runtime only") : "";
      const entLink = (n) => link([state.game, "entities", e.side, n], n);
      const memberTable = (kind, cols) => {
        const items = rows(kind);
        if (!items.length) return `<p class="muted">None.</p>`;
        return `<table><tr>${cols.map((c) => `<th>${c}</th>`).join("")}</tr>${items.map((m) =>
          `<tr class="${m.defined_in !== e.classname ? "inherited" : ""}">
           <td class="mono">${esc(m.name)}${m.label && m.label !== m.name ? `<div class="muted">${esc(m.label)}</div>` : ""}</td>
           <td class="mono">${esc(m.type)}${m.default_value != null ? `<div class="muted">= ${esc(m.default_value)}</div>` : ""}</td>
           <td>${rich(m.description || "")}${m.choices ? `<div class="muted">${m.choices.map(([v, l]) => `${esc(v)}=${esc(l)}`).join(", ")}</div>` : ""}
           ${m.notes ? `<div class="muted mono">${esc(m.notes)}</div>` : ""}</td>
           <td>${m.defined_in === e.classname ? "" : esc(m.defined_in)} ${srcBadge(m)}${m.component ? badge(m.component) : ""}</td></tr>`).join("")}</table>`;
      };
      return `<p>${link([state.game, "entities"], "← Entities", { side: e.side })}</p>
        <h1 class="mono">${esc(e.classname)}</h1>
        <p>${badge(e.side, "kind")}${badge(e.class_type)}${e.cpp_class ? badge(e.cpp_class) : ""}${e.tool_name ? badge("Hammer: " + e.tool_name) : ""}
          ${e.origin === "valve_fgd" ? badge("editor-only (not registered at runtime)", "warn") : ""}${e.fgd_file ? badge("Valve FGD: " + e.fgd_file) : ""}</p>
        ${e.description ? `<p>${rich(e.description)}</p>` : ""}${e.tool_tip ? `<p class="muted">${esc(e.tool_tip)}</p>` : ""}
        <p class="muted">Runtime inherits: ${e.inheritance.map(entLink).join(" → ") || "—"}</p>
        ${e.fgd_inheritance.length ? `<p class="muted">Valve FGD bases: ${e.fgd_inheritance.map(esc).join(" → ")}</p>` : ""}
        <h2>Outputs (${rows("output").length})</h2>${memberTable("output", ["Output", "Param", "Description", "From"])}
        <h2>Inputs (${rows("input").length})</h2>${memberTable("input", ["Input", "Param", "Description", "From"])}
        <h2>Keyvalues (${rows("keyvalue").length})</h2>${memberTable("keyvalue", ["Key / label", "Type / default", "Description", "From"])}`;
    }
    const s = p.side || "server";
    const page = await api(`/games/${state.game}/entities`, { q: p.q, side: s, io: p.io, include_base: p.base === "1", limit: 100, offset: p.offset });
    return `<h1>Hammer entities</h1>
      <form class="filters">${input("q", p.q, "classname, C++ class or Hammer name contains…")}${input("io", p.io, "has input/output (e.g. OnTrigger)")}
        ${select("side", s, [["server", "server"], ["client", "client"]])}
        <label><input type="checkbox" name="base" ${p.base ? "checked" : ""}> include base classes</label></form>
      <table><tr><th>Classname</th><th>Hammer name</th><th>Description</th><th>Bases</th></tr>
      ${page.items.map((e) => `<tr><td class="mono">${link([state.game, "entities", e.side, e.classname], e.classname)}
        ${e.origin === "valve_fgd" ? badge("editor-only", "warn") : ""}</td><td>${esc(e.tool_name || "")}</td>
        <td class="muted">${esc(stripTags(e.description || "").slice(0, 120))}</td><td class="mono">${e.bases.map(esc).join(", ")}</td></tr>`).join("")}</table>${pager(page)}`;
  },

  async examples([name]) {
    if (name) {
      const ex = await api(`/games/${state.game}/examples/${enc(name)}`);
      return `<p>${link([state.game, "examples"], "← Examples")}</p><h1 class="mono">${esc(ex.name)}</h1>
        <p>${badge(ex.origin === "valve" ? "Valve sample" : "community", "kind")}${badge(ex.api)}<span class="muted mono"> ${esc(ex.path)}</span></p>
        <p class="muted">${esc(ex.description || "")}</p><pre>${esc(ex.content)}</pre>`;
    }
    const list = await api(`/games/${state.game}/examples`);
    if (!list.length) return `<h1>Examples</h1><p class="muted">No examples for this game.</p>`;
    return `<h1>Examples</h1><p class="muted">Sample scripts shipped by Valve in the <code>cs_script_demo</code> addon.</p>
      <table><tr><th>Name</th><th>API</th><th>Description</th></tr>${list.map((x) => `<tr><td class="mono">${link([state.game, "examples", x.name], x.name)}</td>
      <td>${esc(x.api)}</td><td class="muted">${esc(x.description || "")}</td></tr>`).join("")}</table>`;
  },

  async docs(_, p) {
    if (p.key) {
      const d = await api("/doc", { key: p.key });
      return `<p>${link([state.game, "docs"], "← Docs")}</p><h1>${esc(d.title)}</h1>
        <p>${badge(d.source, "kind")}${d.game ? badge(d.game) : badge("all games")}
        <a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.url)}</a></p>
        ${d.description ? `<p class="muted">${esc(d.description)}</p>` : ""}
        <div class="doc">${markdown(d.body || "")}</div>
        <p class="muted">License: ${esc(d.license || "unknown")}. Content from ${esc(d.source)}; see the original page for images and edits.</p>`;
    }
    const page = await api("/docs", { game: state.game, q: p.q, limit: 100, offset: p.offset });
    return `<h1>Docs</h1><p class="muted">Community documentation from <a href="https://www.source2.wiki" target="_blank" rel="noopener">source2.wiki</a> (CC BY-SA 4.0).</p>
      <form class="filters">${input("q", p.q, "title or path contains…")}</form>
      <table><tr><th>Title</th><th>Path</th><th>Game</th></tr>${page.items.map((d) => `<tr><td>${link([state.game, "docs"], d.title, { key: d.key })}</td>
      <td class="muted mono">${esc(d.key.replace(/^s2wiki:/, ""))}</td><td>${esc(d.game || "all")}</td></tr>`).join("")}</table>${pager(page)}`;
  },

  async schema([name], p) {
    if (name) {
      const c = await api(`/games/${state.game}/schema/${enc(name)}`, { module: p.module });
      const sch = (n) => link([state.game, "schema"], n, { q: n });
      return `<p>${link([state.game, "schema"], "← Schema")}</p><h1 class="mono">${esc(c.name)}</h1>
        <p>${badge(c.kind, "kind")}${badge(c.module)}${c.underlying_type ? badge(c.underlying_type) : ""}${c.metadata.map((x) => badge(x)).join("")}</p>
        ${c.inheritance.length ? `<p class="muted">Inherits: ${c.inheritance.map(sch).join(" → ")}</p>` : ""}
        ${c.derived.length ? `<p class="muted">Derived: ${c.derived.slice(0, 60).map(sch).join(", ")}${c.derived.length > 60 ? " …" : ""}</p>` : ""}
        <h2>${c.kind === "enum" ? "Members" : "Fields"} (${c.fields.length})</h2>
        <table><tr><th>Name</th><th>${c.kind === "enum" ? "Value" : "Type"}</th><th>Metadata</th><th>From</th></tr>
        ${c.fields.map((f) => `<tr class="${f.defined_in !== c.name ? "inherited" : ""}"><td class="mono">${esc(f.name)}</td>
          <td class="mono">${esc(c.kind === "enum" ? f.value : f.type)}${c.kind !== "enum" && f.value ? ` <span class="muted">= ${esc(f.value)}</span>` : ""}</td>
          <td>${f.metadata.map((x) => badge(x)).join("")}</td><td>${f.defined_in === c.name ? "" : sch(f.defined_in)}</td></tr>`).join("")}</table>`;
    }
    const [page, mods] = await Promise.all([
      api(`/games/${state.game}/schema`, { q: p.q, module: p.module, kind: p.kind, field: p.field, limit: 100, offset: p.offset }),
      api(`/games/${state.game}/schema-modules`),
    ]);
    return `<h1>Schema classes &amp; enums</h1>
      <form class="filters">${input("q", p.q, "name contains…")}${input("field", p.field, "declares field (e.g. m_iHealth)")}
        ${select("module", p.module, [["", "all modules"], ...mods.map((m) => [m.module, `${m.module} (${m.count})`])])}
        ${select("kind", p.kind, [["", "classes + enums"], ["class", "classes"], ["enum", "enums"]])}</form>
      <table><tr><th>Name</th><th>Module</th><th>Kind</th><th>Bases</th><th>#</th></tr>
      ${page.items.map((c) => `<tr><td class="mono">${link([state.game, "schema", c.name], c.name, { module: c.module })}</td>
        <td>${esc(c.module)}</td><td>${esc(c.kind)}</td><td class="mono">${c.bases.map(esc).join(", ")}</td><td>${c.field_count}</td></tr>`).join("")}</table>${pager(page)}`;
  },

  async events([name], p) {
    if (name) {
      const e = await api(`/games/${state.game}/events/${enc(name)}`);
      return `<p>${link([state.game, "events"], "← Events")}</p><h1 class="mono">${esc(e.name)}</h1>
        <p>${badge(e.source_file, "kind")}${e.overrides.map((o) => badge("overrides " + o)).join("")}</p>
        ${e.description ? `<p>${esc(e.description)}</p>` : ""}
        <table><tr><th>Field</th><th>Type</th><th>Description</th></tr>
        ${e.fields.map((f) => `<tr><td class="mono">${esc(f.name)}</td><td class="mono">${esc(f.type)}</td><td>${esc(f.description || "")}</td></tr>`).join("")}</table>`;
    }
    const page = await api(`/games/${state.game}/events`, { q: p.q, limit: 100, offset: p.offset });
    return `<h1>Game events</h1><form class="filters">${input("q", p.q, "name or description contains…")}</form>
      <table><tr><th>Event</th><th>Fields</th><th>File</th><th>Description</th></tr>
      ${page.items.map((e) => `<tr><td class="mono">${link([state.game, "events", e.name], e.name)}</td>
        <td class="mono muted">${e.fields.map((f) => esc(f.name)).join(", ")}</td><td>${esc(e.source_file)}</td>
        <td>${esc(e.description || "")}</td></tr>`).join("")}</table>${pager(page)}`;
  },

  async script([type], p) {
    if (type) {
      const t = await api(`/games/${state.game}/script-api/types/${enc(type)}`);
      const typeLink = (sig) => esc(sig).replace(/\b([A-Z][A-Za-z0-9]+)\b/g, (m) =>
        knownTypes.has(m) && m !== t.name ? `<a href="#/${enc(state.game)}/script/${enc(m)}">${m}</a>` : m);
      const knownTypes = new Set((await api(`/games/${state.game}/script-api/types`)).map((s) => s.name));
      const rows = t.members.map((m) => `<tr id="m-${esc(m.name)}" class="${m.parent !== t.name ? "inherited" : ""}">
        <td class="mono"><strong>${esc(m.name)}</strong>${m.deprecated ? badge("deprecated", "warn") : ""}</td>
        <td><pre>${typeLink(m.signature)}</pre>${m.doc ? `<div>${esc(m.doc).replace(/\n/g, "<br>")}</div>` : ""}</td>
        <td>${m.parent !== t.name ? link([state.game, "script", m.parent], m.parent) : ""}</td></tr>`).join("");
      setTimeout(() => p.m && document.getElementById("m-" + p.m)?.scrollIntoView({ block: "center" }), 0);
      return `<p>${link([state.game, "script"], "← cs_script API")}</p>
        <h1 class="mono">${esc(t.name === "Domain" ? "Instance (Domain)" : t.name)}</h1>
        <p>${badge(t.kind, "kind")}${t.extends ? ` extends ${link([state.game, "script", t.extends], t.extends)}` : ""}</p>
        <pre>${typeLink(t.signature)}</pre>${t.doc ? `<p>${esc(t.doc).replace(/\n/g, "<br>")}</p>` : ""}
        <table><tr><th>Member</th><th>Signature</th><th>From</th></tr>${rows}</table>`;
    }
    const types = await api(`/games/${state.game}/script-api/types`);
    const groups = {};
    types.forEach((t) => (groups[t.kind] ||= []).push(t));
    return `<h1>cs_script API <span class="muted">(point_script)</span></h1>
      <p>JavaScript API for CS2 maps. Scripts are attached with a <code>point_script</code> entity and
      import from <code>"cs_script/point_script"</code>. Download Valve's declarations for editor IntelliSense:
      <a href="/api/games/${enc(state.game)}/script-api/files/point_script.d.ts">point_script.d.ts</a> ·
      <a href="/api/games/${enc(state.game)}/script-api/files/tsconfig.json">tsconfig.json</a></p>
      <pre>import { Instance } from "cs_script/point_script";\nInstance.OnRoundStart(() => Instance.Msg("Round started"));</pre>
      ${Object.entries(groups).map(([kind, items]) => `<h2>${esc(kind)}s (${items.length})</h2><div class="grid">
        ${items.map((t) => `<div class="card">${link([state.game, "script", t.name], t.name === "Domain" ? "Instance (Domain)" : t.name)}
        <div class="muted">${esc(firstLine(t.doc) || t.signature).slice(0, 110)}</div></div>`).join("")}</div>`).join("")}`;
  },

  async pulse([id], p) {
    if (id) {
      const b = await api(`/games/${state.game}/pulse/${enc(id)}`);
      return `<p>${link([state.game, "pulse"], "← Pulse")}</p><h1 class="mono">${esc(b.library)}.${esc(b.name)}</h1>
        <p>${badge(b.kind, "kind")}${badge(b.module)}${b.friendly_name ? badge(b.friendly_name) : ""}</p>
        <pre>${esc(b.signature)}</pre>${b.description ? `<p>${esc(b.description)}</p>` : ""}
        <h2>Metadata</h2><pre>${esc(JSON.stringify(b.metadata, null, 2))}</pre>`;
    }
    const page = await api(`/games/${state.game}/pulse`, { q: p.q, kind: p.kind, limit: 100, offset: p.offset });
    return `<h1>Pulse bindings</h1><p class="muted">Node bindings for Pulse, Valve's visual scripting system.</p>
      <form class="filters">${input("q", p.q, "name, library or friendly name…")}
        ${select("kind", p.kind, [["", "all kinds"], ["input", "input"], ["output", "output"], ["function", "function"], ["class", "class"]])}</form>
      <table><tr><th>Library</th><th>Name</th><th>Kind</th><th>Friendly name</th></tr>
      ${page.items.map((b) => `<tr><td class="mono">${esc(b.library)}</td><td class="mono">${link([state.game, "pulse", String(b.id)], b.name)}</td>
        <td>${esc(b.kind)}</td><td>${esc(b.friendly_name || "")}</td></tr>`).join("")}</table>${pager(page)}`;
  },

  async diff(_, p) {
    const builds = await api(`/games/${state.game}/builds`);
    if (builds.length < 2)
      return `<h1>Build diff</h1><p class="muted">Only one build is indexed (${esc(builds[0]?.build_number ?? "none")}).
        Run <code>s2docs sync</code> after the next game update to diff patches.</p>`;
    const d = await api(`/games/${state.game}/diff`, { from: p.from, to: p.to });
    const opts = builds.map((b) => [String(b.build_number), `${b.build_number} (${b.patch_version || ""})`]);
    const byNum = Object.fromEntries(builds.map((b) => [b.build_number, b]));
    // Build metadata for the summary header, from GET /api/games/{game}/builds.
    const meta = (n) => {
      const b = byNum[n] || {};
      const bits = [
        b.patch_version && `patch ${b.patch_version}`,
        b.version_date,
        b.source_commit && `commit ${String(b.source_commit).slice(0, 10)}`,
      ].filter(Boolean);
      return `<div class="diff-build"><strong>build ${esc(n)}</strong>` +
        (bits.length ? `<div class="muted">${esc(bits.join(" · "))}</div>` : "") + `</div>`;
    };
    // Filters come from the hash query (deep-linkable like ?from=&to=): change type + a name substring.
    const q = (p.q || "").trim().toLowerCase();
    const change = p.change || "";
    const match = (r) => (!change || r.change === change) && (!q || r.name.toLowerCase().includes(q));
    const c = diffCounts(d, match);
    const md = diffMarkdown(d, match);
    // Dropdown VALUES stay '' / added / removed / changed so ?change= deep links keep working; the
    // labels carry the exact per-type totals (from the server's pre-limit `counts`).
    const changeOpts = [
      ["", `all changes (${c.added + c.removed + c.changed})`],
      ["added", `added (${c.added})`],
      ["removed", `removed (${c.removed})`],
      ["changed", `changed (${c.changed})`],
    ];
    const capNote = c.capped.length
      ? ` (${c.capped.slice(0, 3).join(", ")}${c.capped.length > 3 ? ", …" : ""})`
      : "";
    const filtering = change || q;
    return `<h1>Build diff</h1>
      <form class="filters">${select("from", String(d.from_build), opts)}${select("to", String(d.to_build), opts)}
        ${select("change", p.change, changeOpts)}
        ${input("q", p.q, "filter changed names…")}</form>
      <div class="diff-head">${meta(d.from_build)}<span class="diff-arrow">→</span>${meta(d.to_build)}</div>
      <p class="diff-counts">${badge(c.added + " added", "kind")}${badge(c.removed + " removed", "warn")}${badge(c.changed + " changed")}
        <span class="muted">total changes across ${c.sections} section${c.sections === 1 ? "" : "s"}${
          c.truncated ? ` · sections capped at ${d.limit || 500} rows${capNote}` : ""
        }${c.exact ? "" : " · counted from the rows shown"}</span>${
          filtering ? `<span class="muted"> · filter active: showing ${c.matched} matching row${c.matched === 1 ? "" : "s"}</span>` : ""
        }</p>
      <div class="diff-actions"><button type="button" data-copy-md>Copy as Markdown</button>
        <textarea id="diff-md" class="diff-md mono" hidden aria-label="diff as Markdown">${esc(md)}</textarea></div>
      ${diffSectionsHtml(d, match)}`;
  },

  // Game-independent: ONE mission-control wallboard, not a tabbed copy of the other pages. It is
  // organized as: a HERO (the one thing happening right now, biggest text), a machine DOCK (one LED
  // chip per host: pulsing gold = busy, red = STUCK, gray = idle), then three lanes — the QUEUE
  // rail, the VERDICT WALL (one colored tile per job, like a CI status board) and a compact FEED.
  // Every tile/line links to its detail page; polls like Activity (5 s live, 15 s idle).
  async overview() {
    const body = async () => {
      const [builds, tests, deploys, jobs, hosts, projects] = await Promise.all([
        api("/builds"),
        api("/test-runs"),
        api("/deploys"),
        api("/jobs", { limit: 200 }).catch(() => []),
        api("/hosts").catch(() => []),
        api("/projects", { limit: 50 }).catch(() => []),
      ]);
      const isActiveBuild = (b) => b.status === "running" || b.status === "queued";
      const activeBuilds = builds.filter((b) => b.status === "running");
      const queuedBuilds = builds.filter((b) => b.status === "queued");
      const activeTests = tests.filter((t) => t.state === "running");
      const openJobs = jobs.filter((j) => j.status === "running" || j.status === "queued");
      // Drives the poll cadence (same contract as the Activity and Jobs pages).
      state.activityActive = activeBuilds.length > 0 || activeTests.length > 0;
      state.jobsActive = openJobs.length > 0;

      const idle = [builds, tests, deploys, jobs].flat().map((r) => r.updated_at || r.created_at || "").filter(Boolean).sort().pop();

      // ---- HERO: the one thing happening right now. A running test leads (it is what a human is
      // being asked to watch); a running build is the next hero; queued jobs are a quieter hero.
      const heroParts = [];
      activeTests
        .slice()
        .sort((a, b) => ((a.updated_at || "") < (b.updated_at || "") ? 1 : -1))
        .forEach((t, i) => {
          heroParts.push(
            `<div class="ovw-hero"><div style="display:flex;align-items:center;gap:10px">${badge("running", "kind")}` +
            `<span class="ovw-st" style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)">test</span>` +
            `${i === 0 ? `<span class="muted" style="font-size:12px">${human(t.elapsed_sec)} in</span>` : ""}` +
            `${t.host_id ? `<span class="muted" style="font-size:12px;margin-left:auto">on ${link(["hosts", t.host_id], t.host_id)}</span>` : ""}</div>` +
            `<h2 style="margin-top:6px">${esc(t.name)}</h2>` +
            `<div class="mono" style="margin-top:4px">${esc(t.step || "(no step)")}</div>` +
            `${t.lock ? `<div style="margin-top:6px">${lockCell(t.lock)}${lockStuckBadge(t.lock)}</div>` : ""}</div>`,
          );
        });
      activeBuilds
        .slice()
        .sort((a, b) => ((a.updated_at || "") < (b.updated_at || "") ? 1 : -1))
        .forEach((b) => {
          heroParts.push(
            `<div class="ovw-hero"><div style="display:flex;align-items:center;gap:10px">${stateBadge(b.status)}` +
            `<span class="ovw-st" style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)">build</span>` +
            `<span class="muted" style="font-size:12px">${runTime(b)} in</span>` +
            `${b.host_id ? `<span class="muted" style="font-size:12px;margin-left:auto">on ${link(["hosts", b.host_id], b.host_id)}</span>` : ""}</div>` +
            `<h2 style="margin-top:6px">${esc(b.kind === "compile" ? "Compiling" : b.kind || "Building")}</h2>` +
            `<div class="mono" style="margin-top:4px">${esc(runPhase(b) || b.command || b.id)}</div>` +
            `${materialWarning(b)}</div>`,
          );
        });
      if (!heroParts.length) {
        const openQueued = [...jobs.filter((j) => j.status === "queued")];
        heroParts.push(
          openQueued.length
            ? `<div class="ovw-hero"><h2>Queued: ${link(["jobs", openQueued[0].id], openQueued[0].title || openQueued[0].id)}</h2>` +
              `<div class="muted" style="margin-top:4px">${openQueued[0].kind} · waiting for a free host${idle ? ` · last activity ${fmtTime(idle)}` : ""}</div></div>`
            : `<div class="ovw-hero"><h2>All quiet</h2><div class="muted" style="margin-top:4px">Nothing running, nothing queued.` +
              `${idle ? ` Last activity ${fmtTime(idle)}.` : ""} Start something from ${link(["jobs"], "Jobs")} or MCP.</div></div>`,
        );
      }
      const hero = heroParts.join("");

      // ---- DOCK: one LED chip per host — liveness at a glance, click for detail.
      const dock = hosts.length
        ? hosts
            .map((h) => {
              const busy = h.lock && h.lock.fresh && h.lock.owner;
              const stuck = h.lock && h.lock.stuck;
              return `<a href="${`#/hosts/${enc(h.id)}`}" title="${esc(gpuOf(h))}${h.lock && h.lock.owner ? ` · ${h.lock.owner}${h.lock.phase ? ` — ${h.lock.phase}` : ""}` : " · idle"}">` +
                `<span class="ovw-led ${stuck ? "stuck" : busy ? "busy" : ""}"></span><span><strong>${esc(h.id)}</strong>` +
                `<div class="muted" style="font-size:11px">${esc(h.lock && h.lock.phase ? h.lock.phase : stuck ? "STUCK" : busy ? "busy" : "idle")}</div></span></a>`;
            })
            .join("")
        : `<span class="muted">no hosts registered</span>`;
      const dockHtml = `<div style="display:flex;align-items:baseline;gap:10px;margin:2px 0 6px"><span class="ovw-st" style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)">Machines</span>` +
        `<span class="muted" style="font-size:12px">${hosts.filter((h) => h.lock && h.lock.fresh && h.lock.owner).length}/${hosts.length} busy</span></div>` +
        `<div class="ovw-dock">${dock}</div>`;

      // ---- LANES: queue rail | verdict wall | feed.
      const queueLine = (badgeHtml, html) =>
        `<div class="ovw-line">${badgeHtml}<span style="min-width:0">${html}</span></div>`;
      const queueItems = [
        ...jobs.filter((j) => j.status === "queued").map((j) =>
          queueLine(stateBadge(j.status), `<strong>JOB</strong> ${link(["jobs", j.id], j.id)} — ${esc(j.title || "")}` +
            `${j.host_id ? ` <span class="muted">· ${esc(j.host_id)}</span>` : ""}`)),
        ...queuedBuilds.map((b) =>
          queueLine(stateBadge(b.status), `<span class="mono">${esc(b.id)}</span> <span class="muted">· ${esc(b.kind)} · ${b.host_id ? esc(b.host_id) : "unassigned"}</span>`)),
      ];
      const queueRail = `<div class="ovw-panel"><div class="ovw-st" style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin-bottom:6px">Queued</div>` +
        (queueItems.length ? queueItems.join("") : `<p class="muted" style="margin:0">Nothing queued.</p>`) + `</div>`;

      // The verdict wall: one tile per job, newest first, like a CI status board — a screenful of
      // colour answers "is the suite healthy?" faster than any table. Tile colour is the job's
      // status; the label is the id; the tooltip carries the full title.
      const TILE = {
        passed: "passed", running: "running", queued: "queued", failed: "failed", error: "failed",
        cancelled: "cancelled", inconclusive: "inconclusive",
      };
      const recentJobs = jobs
        .slice()
        .sort((a, b) => ((a.created_at || "") < (b.created_at || "") ? 1 : -1))
        .slice(0, 40);
      const wall = recentJobs
        .map((j) => {
          const cls = TILE[j.status] || "inconclusive";
          return `<a class="ovw-tile ${cls}" href="${`#/jobs/${enc(j.id)}`}" title="${esc(`${j.id} — ${j.title || ""} · ${j.status}${j.created_at ? ` · ${fmtTime(j.created_at)}` : ""}`)}">` +
            `<span class="ovw-st">${esc(j.status)}</span><div class="ovw-id">${esc(j.id)}</div>` +
            `<div class="ovw-age">${esc(j.kind)}${j.created_at ? ` · ${fmtTime(j.created_at)}` : ""}</div></a>`;
        })
        .join("");
      const wallHtml = `<div class="ovw-panel"><div style="display:flex;align-items:baseline;gap:10px;margin-bottom:8px">` +
        `<span class="ovw-st" style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)">Verdict wall</span>` +
        `<span class="muted" style="font-size:12px">${jobs.length} job${jobs.length === 1 ? "" : "s"} · ${link(["jobs"], "full ledger →")}</span></div>` +
        (wall || `<p class="muted" style="margin:0">No jobs recorded yet.</p>`) + `</div>`;

      // The feed: the newest runs as one-liners (the Activity table stays the full record).
      const allRows = activityRows(builds, tests, deploys, jobs);
      const feed = allRows
        .slice(0, 14)
        .map((r) =>
          queueLine(badge(r.type, "tl-" + r.type), `${esc(r.name)} ${stateBadge(r.state)} ` +
            `<span class="muted">· ${fmtTime(r.at)} · ${r.host ? esc(r.host) : "—"}${r.job ? ` · ${link(["jobs", r.job], r.job)}` : ""}</span>`),
        )
        .join("");
      const feedHtml = `<div class="ovw-panel"><div style="display:flex;align-items:baseline;gap:10px;margin-bottom:6px">` +
        `<span class="ovw-st" style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)">Feed</span>` +
        `<span class="muted" style="font-size:12px">${allRows.length} run${allRows.length === 1 ? "" : "s"} · ${link(["activity"], "Activity →")}</span></div>` +
        (feed || `<p class="muted" style="margin:0">No runs recorded yet.</p>`) + `</div>`;

      const plans = projects.filter((x) => (x.status || "active") === "active");
      const plansHtml = plans.length
        ? `<div class="ovw-panel" style="margin-bottom:12px"><div class="ovw-st" style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin-bottom:6px">Open plans</div>` +
          `<p style="margin:0">${plans.slice(0, 6).map((x) => link([state.game, "projects", x.id], x.title || x.id)).join(" · ")} · ${link([state.game, "projects"], "all →")}</p></div>`
        : "";

      return hero + plansHtml + dockHtml +
        `<div class="ovw-lanes">${queueRail}${wallHtml}${feedHtml}</div>`;
    };
    liveBody("#live", body, () => (state.activityActive || state.jobsActive ? 5000 : 15000));
    return `<h1>Overview</h1>${pipelineStrip("")}
      <p class="muted" style="max-width:900px">Mission control. The live thing up top, machines below it, then queue / verdict wall / feed — every tile links to its record; refreshes every 5 s while anything is live.</p>
      <div id="live">${await body()}</div>`;
  },

  // "projects" are orchestration DAGs (multi-step plans the server tracks), distinct from the
  // build/test/deploy runs on the Activity page. Each project opens to a step-level view.
  async projects([pid]) {
    const statusBadge = (s) => badge(s, s === "done" ? "kind" : s === "blocked" ? "warn" : "");
    if (pid) {
      const [p, b, ev] = await Promise.all([
        api(`/projects/${enc(pid)}`),
        api(`/projects/${enc(pid)}/brief`),
        api("/evidence", { project_id: pid }).catch(() => []),
      ]);
      // Group evidence rows per step: total, gate-relevant (non-note) count, and per-kind breakdown.
      const evByStep = {};
      for (const e of ev || []) {
        const key = e.step_id || "(project)";
        const c = (evByStep[key] ||= { all: 0, nonnote: 0, kinds: {} });
        c.all += 1;
        if (e.kind !== "note") c.nonnote += 1;
        c.kinds[e.kind] = (c.kinds[e.kind] || 0) + 1;
      }
      // An 'evidence' gate passes only with >=1 stored non-note row; other gates pass when the step is done.
      const gateOk = (s) => (s.gate === "evidence" ? (evByStep[s.id]?.nonnote || 0) > 0 : s.status === "done");
      const evCell = (id) => {
        const c = evByStep[id];
        if (!c) return `<span class="muted">none</span>`;
        const kinds = Object.entries(c.kinds).map(([k, n]) => `${n} ${k}`).join(", ");
        return `${badge(`${c.all} row${c.all === 1 ? "" : "s"}`, "kind")}<div class="muted">${c.nonnote} non-note (gate)</div>${
          kinds ? `<div class="muted mono">${esc(kinds)}</div>` : ""
        }`;
      };
      const stepRowsHtml = p.steps
        .map(
          (s) =>
            `<tr><td><strong>${esc(s.title)}</strong><div class="muted mono">${esc(s.id)} · ${esc(s.kind)}</div></td>` +
            `<td>${statusBadge(s.status)}</td>` +
            `<td>${s.gate ? badge(s.gate, gateOk(s) ? "kind" : "warn") : `<span class="muted">—</span>`}</td>` +
            `<td>${evCell(s.id)}</td>` +
            `<td class="muted">${fmtTime(s.updated_at)}</td>` +
            `<td class="muted">${stepTime(s)}</td>` +
            `<td class="mono muted">${s.depends_on.map(esc).join(", ") || "—"}</td>` +
            `<td>${esc((s.notes || "").slice(0, 300))}${s.artifacts.length ? `<div class="muted mono">${s.artifacts.map(esc).join("<br>")}</div>` : ""}</td></tr>`,
        )
        .join("");
      const ready = b.next_steps.map((s) => `<div class="card" style="margin-bottom:10px">
          <strong>${esc(s.title)}</strong> ${badge(s.kind)} ${s.gate ? badge("gate: " + s.gate, "warn") : ""}
          <pre>${esc(s.instructions)}</pre>
          ${s.tools.length ? `<p class="muted">Tools: ${s.tools.map((t) => `<code>${esc(t)}</code>`).join(" ")}</p>` : ""}
          ${s.prompt_text ? `<details><summary>Prompt: ${esc(s.prompt_name)}</summary><pre>${esc(s.prompt_text)}</pre></details>` : ""}
          <form data-step="${esc(s.step_id)}" data-project="${esc(pid)}">
            <textarea name="notes" rows="2" placeholder="notes / evidence"></textarea>
            <p>${["in_progress", "done", "blocked", "skipped"].map((st) => `<button name="status" value="${st}">${st}</button>`).join(" ")}</p>
            <div class="result"></div></form></div>`).join("");
      return `<p>${link([state.game, "projects"], "← Orchestration")}</p><h1>${esc(p.title)}</h1>${pipelineStrip("projects")}
        <p>${statusBadge(p.status)}${badge(p.workflow)}${badge(p.game)}${p.addon ? badge("addon: " + p.addon) : ""} <span class="mono muted">${esc(p.id)}</span></p>
        <p class="muted">This is an <strong>orchestration DAG</strong> — the server's multi-step plan — not a build/test run.
          For the runs these steps drive on hosts, see ${link(["activity"], "Activity")}.</p>
        <p>${esc(p.goal)}</p><p class="muted">${esc(b.guidance)}</p>
        <p class="muted">Agent hand-off: <code>continue project ${esc(p.id)}</code> (MCP tool <code>next_step</code>)</p>
        <h2>Step graph</h2>${stepDag(p.steps)}
        <h2>Steps (${esc(b.progress)})</h2>
        <table><tr><th>Step</th><th>State</th><th>Gate</th><th>Evidence</th><th>Updated</th><th>Duration</th><th>Depends on</th><th>Notes / artifacts</th></tr>${stepRowsHtml}</table>
        ${ready ? `<h2>Ready now</h2>${ready}` : ""}
        <h2>Timeline</h2>${timelineRows(p.recent_events)}`;
    }
    const [projects, workflows] = await Promise.all([api("/projects"), api("/workflows")]);
    const wfOptions = workflows.filter((w) => w.games.includes(state.game)).map((w) => [w.id, w.title]);
    return `<h1>Orchestration</h1>${pipelineStrip("projects")}<p class="muted">Multi-step <strong>orchestration DAGs</strong>: the server holds the plan and the
      quality gates, your agent does the work. These are plans, not runs — for the build/test/deploy runs they drive, see
      ${link(["activity"], "Activity")}. Start one here or from an MCP client (prompt <code>orchestrate</code>), then hand
      the project id to your agent. Click a project to open its step-level view: step states, gates, evidence and notes.</p>
      <form data-start-project class="card">
        <p>${select("workflow", wfOptions[0]?.[0] || "", wfOptions)}</p>
        <p><textarea name="goal" rows="2" placeholder="Goal, e.g. an isometric Diablo-style spectator view of the match"></textarea></p>
        <p><input name="addon" placeholder="addon name (optional, lowercase_snake)"></p>
        <p><textarea name="inputs" rows="2" placeholder="inputs, one key=value per line (optional)"></textarea></p>
        <button type="submit">Start project</button><div class="result"></div></form>
      <h2>Workflows</h2>${workflows.map((w) => `<details><summary><strong>${esc(w.title)}</strong> <span class="mono muted">${esc(w.id)}</span> ${w.games.map((g) => badge(g)).join("")}</summary>
        <p>${esc(w.description)}</p><ol>${w.steps.map((s) => `<li class="mono">${esc(s)}</li>`).join("")}</ol></details>`).join("")}
      <h2>All projects</h2>${projects.length ? `<table><tr><th>Project</th><th>Workflow</th><th>Status</th><th>Progress</th><th>Runtime</th><th>Updated</th></tr>
      ${projects.map((p) => `<tr><td>${link([state.game, "projects", p.id], p.title)}<div class="muted">${esc(p.goal.slice(0, 90))}</div></td>
        <td class="mono">${esc(p.workflow)}</td><td>${statusBadge(p.status)}</td><td>${esc(p.progress)}</td>
        <td class="muted">${runTime(p)}${isLive(p) ? `<div class="muted">running</div>` : ""}</td>
        <td class="muted">${fmtTime(p.updated_at)}</td></tr>`).join("")}</table>`
        : `<p class="muted">No projects yet.</p>`}`;
  },

  // Game-independent: the host registry + live lock state. Read-only (visibility while tests run).
  async hosts([hostId]) {
    if (hostId) {
      const body = async () => {
        const [h, locks, builds, deploys, tests] = await Promise.all([
          api(`/hosts/${enc(hostId)}`),
          api(`/hosts/${enc(hostId)}/locks`, { limit: 20 }),
          api("/builds", { host_id: hostId }),
          api("/deploys", { host_id: hostId }),
          api("/test-runs", { host_id: hostId }),
        ]);
        const facts = h.facts && Object.keys(h.facts).length ? `<details><summary>Facts (${Object.keys(h.facts).length} top-level keys)</summary><pre>${esc(JSON.stringify(h.facts, null, 2))}</pre></details>` : "";
        return `${nowLine()}<p>${h.shell ? badge(h.shell) : ""}${h.os ? badge(h.os) : ""}${h.hostname ? badge(h.hostname) : ""}<span class="mono muted"> ${esc(h.ssh_target)}</span></p>
          <p>${lockCell(h.lock)}<span class="muted"> · probed ${h.probed_at ? fmtTime(h.probed_at) : "never"}</span></p>
          ${h.notes ? `<p class="muted">${esc(h.notes)}</p>` : ""}
          ${facts}
          <h2>Lock history</h2>${lockRows(locks)}
          <h2>Recent builds (${builds.length})</h2>${buildRows(builds.slice(0, 15))}
          <h2>Recent deployments (${deploys.length})</h2>${deployRows(deploys.slice(0, 15))}
          <h2>Recent test runs (${tests.length})</h2>${testRows(tests.slice(0, 15))}`;
      };
      liveBody("#live", body);
      return `<p>${link(["hosts"], "← Hosts")}</p><h1 class="mono">${esc(hostId)}</h1>${pipelineStrip("hosts")}<div id="live">${await body()}</div>`;
    }
    const body = async () => {
      const hosts = await api("/hosts");
      if (!hosts.length)
        return `${nowLine()}<p class="muted">No hosts registered. Register one over MCP (<code>register_host</code>) or the CLI (<code>s2docs host register …</code>).</p>`;
      return `${nowLine()}<table><tr><th>Host</th><th>Shell</th><th>GPU</th><th>Probed</th><th>Lock</th></tr>${hosts
        .map(
          (h) =>
            `<tr><td>${link(["hosts", h.id], h.id)}<div class="muted mono">${esc(h.hostname || h.ssh_target || "")}</div></td>` +
            `<td>${h.shell ? badge(h.shell) : "—"}${h.os ? badge(h.os) : ""}</td><td>${esc(gpuOf(h))}</td>` +
            `<td class="muted">${h.probed_at ? fmtTime(h.probed_at) : "never"}</td>` +
            `<td>${lockCell(h.lock)}</td></tr>`,
        )
        .join("")}</table>`;
    };
    liveBody("#live", body);
    return `<h1>Hosts</h1>${pipelineStrip("hosts")}<p class="muted">Test/build hosts and who is driving each right now (owner, fresh vs stale, heartbeat age). Auto-refreshes every 5 s. Click a host for its facts, lock history and recent activity.</p><div id="live">${await body()}</div>`;
  },

  // Game-independent: the live view a person watches to answer "what is running right now".
  async activity() {
    const body = async () => {
      const [builds, tests, deploys, jobs] = await Promise.all([
        api("/builds"),
        api("/test-runs"),
        api("/deploys"),
        // The job ledger: a run's job linkage lives here (job_links), not on the run row — see the
        // activity-timeline helpers. A failed fetch degrades to 'no job links', never a blank page.
        api("/jobs", { limit: 200 }).catch(() => []),
      ]);
      const isActiveBuild = (b) => b.status === "running" || b.status === "queued";
      const activeBuilds = builds.filter(isActiveBuild);
      // EVERY running session, not just the single /test-runs/current probe: more than one host can
      // run a test at once, and the page must show all of them.
      const activeTests = tests.filter((t) => t.state === "running");
      // Drives the poll cadence: fast while anything is live, slow when the system is idle.
      state.activityActive = activeBuilds.length > 0 || activeTests.length > 0;

      if (!builds.length && !tests.length && !deploys.length) return nowLine() + activityEmptyState();

      // ---- NOW: everything currently running, freshest first (updated_at = heartbeat-ish for a run).
      const ts = (r) => (r.updated_at || r.created_at || r.started_at || "");
      const nowCards = [];
      activeTests
        .slice()
        .sort((a, b) => (ts(a) < ts(b) ? 1 : -1))
        .forEach((t) =>
          nowCards.push(
            `<div class="card" style="margin-bottom:8px">${stateBadge(t.state)} <strong>TEST</strong> ${esc(t.name)} —
             <span class="mono">${esc(t.step || "(no step)")}</span>
             <span class="muted">· ${human(t.elapsed_sec)} · host ${
               t.host_id ? link(["hosts", t.host_id], t.host_id) : "—"
             }${t.project_id ? ` · ${esc(t.project_id)}` : ""}</span>${lockStuckBadge(t.lock)}</div>`,
          ),
        );
      activeBuilds
        .slice()
        .sort((a, b) => (ts(a) < ts(b) ? 1 : -1))
        .forEach((b) =>
          nowCards.push(
            `<div class="card" style="margin-bottom:8px">${stateBadge(b.status)} <span class="mono">${esc(b.id)}</span> on ${link(
              ["hosts", b.host_id],
              b.host_id,
            )} <span class="muted">· ${esc(b.kind)} · up ${runTime(b)}</span>${materialWarning(b)}${
              runPhase(b) ? `<div class="muted mono">${esc(runPhase(b))}</div>` : `<div class="muted mono">${esc(b.command)}</div>`
            }</div>`,
          ),
        );
      const newest = [builds, tests, deploys]
        .flat()
        .map(ts)
        .filter(Boolean)
        .sort()
        .pop();
      const idleNote = newest ? ` Last activity ${fmtTime(newest)}.` : "";
      const now = nowCards.length ? nowCards.join("") : `<p class="muted">Nothing is running right now.${idleNote}</p>`;

      // ---- BY HOST: everything recent, grouped under the host it ran on (unassigned last).
      const groups = new Map();
      const bucket = (id) => (groups.has(id) ? groups.get(id) : (groups.set(id, { builds: [], tests: [], deploys: [] }), groups.get(id)));
      builds.forEach((b) => bucket(b.host_id || "(unassigned)").builds.push(b));
      tests.forEach((t) => bucket(t.host_id || "(unassigned)").tests.push(t));
      deploys.forEach((d) => bucket(d.host_id || "(unassigned)").deploys.push(d));
      const hostBlocks = [...groups.entries()]
        .map(([hid, g]) => {
          const active = g.builds.filter(isActiveBuild).length + g.tests.filter((t) => t.state === "running").length;
          const label = hid === "(unassigned)" ? `<strong>${esc(hid)}</strong>` : link(["hosts", hid], hid);
          return `<h3>${label} <span class="muted">${active ? `${active} active · ` : ""}${g.builds.length} build${g.builds.length === 1 ? "" : "s"} · ${
            g.tests.length
          } test${g.tests.length === 1 ? "" : "s"} · ${g.deploys.length} deploy${g.deploys.length === 1 ? "" : "s"}</span></h3>
            ${g.builds.length ? buildRows(g.builds.slice(0, 8)) : ""}
            ${g.tests.length ? `<div class="muted" style="margin:6px 0 2px">test runs</div>${testRows(g.tests.slice(0, 6))}` : ""}
            ${g.deploys.length ? `<div class="muted" style="margin:6px 0 2px">deployments</div>${deployRows(g.deploys.slice(0, 6))}` : ""}`;
        })
        .join("");

      // ---- TIMELINE: one chronological sequence, all three types, sorted by each run's OWN start.
      const rows = activityRows(builds, tests, deploys, jobs);
      const linked = rows.filter((r) => r.job).length;
      const timeline = `<h2>Timeline <span class="muted">${rows.length} run${rows.length === 1 ? "" : "s"} · newest first · all types in one sequence</span></h2>
        <p class="muted">One row per run, ordered by when it <strong>started</strong> — never its last heartbeat, so a run the reaper
        finalized after an hour cannot jump back to the top. Consecutive runs that belong to the same job nest under a job heading
        (build → test → deploy). ${linked ? `${linked} row${linked === 1 ? "" : "s"} linked to a job.` : "No run is linked to a job yet."}
        Job links come from the job ledger (a run row itself carries no job field); a run no job has linked reads "—".</p>
        ${activityTimelineHtml(rows)}`;

      return `${nowLine()}<h2>Now${state.activityActive ? ` ${badge("live", "kind")}` : ""}</h2>${now}
        ${timeline}
        <h2>By host</h2>${hostBlocks}`;
    };
    // Fast (5 s) while a run is active; slow (15 s) when idle so an idle install isn't hammered.
    liveBody("#live", body, () => (state.activityActive ? 5000 : 15000));
    return `<h1>Activity</h1>${pipelineStrip("activity")}<p class="muted">What is running right now across all hosts — build runs, test runs and deployments, without SSHing anywhere.
      This view is <strong>service-fed</strong>: rows appear when work goes through the s2docs build/test/deploy services
      (MCP <code>build_start</code>/<code>test_start</code>/<code>deploy_push</code>, or the CLI), <em>not</em> from any CS2 running anywhere.
      The <strong>Timeline</strong> below is ONE chronological sequence across all three types, ordered by when each run
      <strong>started</strong> (a run's late heartbeat never reorders it), with consecutive runs for the same job nested together.
      Test-run rows mirror the in-game HUD strip. Every row also carries its <strong>run time</strong>: live while a run is going, the
      final duration once it finishes. Auto-refreshes every 5 s while a run is active, 15 s when idle.</p><div id="live">${await body()}</div>`;
  },

  // Game-independent: the job ledger + queue — one record per requested test/demo/final run.
  async jobs([jobId]) {
    if (jobId) {
      const body = async () => loadJobDetail(jobId);
      liveBody("#live", body, () => 5000);
      return `<p>${link(["jobs"], "← Jobs")}</p>${pipelineStrip("jobs")}<div id="live">${await body()}</div>`;
    }
    const body = async () => {
      const [allJobs, hosts] = await Promise.all([api("/jobs"), api("/hosts")]);
      state.jobsActive = allJobs.some((j) => j.status === "running");
      return jobBoard(allJobs, hosts);
    };
    // Fast (5 s) while any job is running; slow (15 s) when the queue is idle.
    liveBody("#live", body, () => (state.jobsActive ? 5000 : 15000));
    return `<h1>Jobs</h1>${pipelineStrip("jobs")}<p class="muted">One record per requested run: the intake is the gate, the timeline is the
      record, screenshots/logs are artifacts, and a job closes out with a mandatory summary (what / why / learned /
      next). Jobs <strong>never</strong> claim the host lease — the build/test/deploy tools still do; a job links to
      those records. The queue is per host: running on top, then queued by position. Auto-refreshes every 5 s while a
      job is running, 15 s when idle.</p><div id="live">${await body()}</div>`;
  },

  async validate() {
    const g = state.games.find((x) => x.id === state.game);
    const scriptForm = g?.script_api ? `<h2>cs_script</h2>
      <p class="muted">Type-checked by the TypeScript compiler against this build's <code>point_script.d.ts</code>.</p>
      <form data-validate="cs-script"><textarea name="code" rows="14" class="mono" placeholder='import { Instance } from "cs_script/point_script";'></textarea>
      <p><button type="submit">Check script</button></p><div class="result"></div></form>` : "";
    return `<h1>Validate</h1>${pipelineStrip("validate")}
      <p class="muted">Validate is the <strong>offline gate</strong>, not a stage of the run pipeline: it type-checks a
      cs_script or a .cfg against this build's own index before anything is planned (${link([state.game, "projects"], "Orchestration")}),
      queued (${link(["jobs"], "Jobs")}) or run on a host (${link(["activity"], "Activity")}). It touches no host and starts
      nothing.</p>${scriptForm}
      <h2>Config (.cfg)</h2><p class="muted">Every command is checked against the indexed convars/commands for ${esc(g?.name || state.game)}.</p>
      <form data-validate="cfg"><textarea name="text" rows="10" class="mono" placeholder="sv_cheats 1&#10;mp_roundtime 3"></textarea>
      <p>${select("context", "server", [["server", "server cfg"], ["client", "client cfg"], ["any", "any"]])} <button type="submit">Check cfg</button></p>
      <div class="result"></div></form>`;
  },

  async prompts() {
    const prompts = await api("/prompts");
    return `<h1>Prompts</h1><p class="muted">Guided workflows for LLM agents. The MCP server exposes the same prompts.
      Render one here to preview the grounded text.</p>
      ${prompts.map((pr) => `<div class="card" style="margin-bottom:12px"><strong class="mono">${esc(pr.name)}</strong> — ${esc(pr.title)}
        <p class="muted">${esc(pr.description)}</p><form data-prompt="${esc(pr.name)}">
        ${pr.arguments.map((a) => `<p><label>${esc(a.name)}${a.required ? " *" : ""} <span class="muted">${esc(a.description)}</span>
          <textarea name="${esc(a.name)}" rows="2" ${a.required ? "required" : ""}>${a.name === "game" ? esc(state.game) : ""}</textarea></label></p>`).join("")}
        <button type="submit">Render</button><pre hidden></pre></form></div>`).join("")}`;
  },

  async stats() {
    const s = await api("/stats");
    return `<h1>Index stats</h1><p class="muted">Schema version ${s.schema_version}</p>
      ${s.games.map((g) => `<h2>${esc(g.name)} <span class="muted">build ${esc(g.current_build ?? "—")} · ${esc(g.patch_version ?? "")} · ${esc(g.version_date ?? "")}</span></h2>
        <div class="grid">${Object.entries(s.counts[g.id] || {}).map(([k, n]) => `<div class="card"><div class="n">${n.toLocaleString()}</div><div class="muted">${esc(k)}</div></div>`).join("")}</div>`).join("")}`;
  },
};

// `#/<game>/orchestration` is an alias for the projects route (the page was renamed, not the data).
ROUTES.orchestration = ROUTES.projects;

async function home() {
  const s = await api("/stats");
  const c = s.counts[state.game] || {};
  const g = state.games.find((x) => x.id === state.game);
  const tile = (n, label, sec) => `<a class="card" href="#/${enc(state.game)}/${sec}"><div class="n">${(n || 0).toLocaleString()}</div><div class="muted">${esc(label)}</div></a>`;
  return `<h1>${esc(g?.name || state.game)}</h1>
    <p class="muted">Pulled from game build ${esc(g?.current_build ?? "—")} via SteamTracking GameTracking dumps.
    Search above, or browse a section.</p>
    <div class="grid">${tile(c.convars, "console variables", "convars")}${tile(c.commands, "console commands", "convars")}
    ${tile(c.entities, "Hammer entities", "entities")}${tile(c.schema_classes, "schema classes", "schema")}
    ${tile(c.schema_enums, "schema enums", "schema")}${tile(c.game_events, "game events", "events")}
    ${c.script_symbols ? tile(c.script_symbols, "cs_script symbols", "script") : ""}${tile(c.pulse_bindings, "Pulse bindings", "pulse")}</div>`;
}

async function postJson(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return { ok: res.ok, body: await res.json().catch(() => ({})) };
}

function bindStartProject(form) {
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = form.elements;
    const inputs = Object.fromEntries(f.inputs.value.split("\n").map((l) => l.split("=")).filter((kv) => kv.length === 2 && kv[0].trim())
      .map(([k, v]) => [k.trim(), v.trim()]));
    const r = await postJson("/api/projects", { workflow: f.workflow.value, goal: f.goal.value.trim(), game: state.game,
      addon: f.addon.value.trim() || null, inputs });
    if (!r.ok) { form.querySelector(".result").innerHTML = `<p class="err">${esc(r.body.detail)}</p>`; return; }
    go([state.game, "projects", r.body.project_id]);
  });
}

function bindStepForm(form) {
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const status = ev.submitter?.value || "in_progress";
    const r = await postJson(`/api/projects/${enc(form.dataset.project)}/steps/${enc(form.dataset.step)}`,
      { status, notes: form.elements.notes.value.trim() || null });
    if (!r.ok) {
      form.querySelector(".result").innerHTML = `<pre class="err">${esc(typeof r.body.detail === "string" ? r.body.detail : JSON.stringify(r.body.detail))}</pre>`;
      return;
    }
    route();
  });
}

function bindValidateForm(form) {
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const out = form.querySelector(".result");
    const kind = form.dataset.validate;
    const body = { game: state.game };
    for (const el of form.elements) if (el.name) body[el.name] = el.value;
    out.innerHTML = `<p class="muted">Checking…</p>`;
    const res = await fetch(`/api/validate/${kind}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const r = await res.json();
    if (!res.ok) { out.innerHTML = `<p class="err">${esc(r.detail || res.statusText)}</p>`; return; }
    const sevClass = { error: "warn", warning: "kind", info: "" };
    out.innerHTML = `<p>${r.ok ? badge("OK", "kind") : badge("FAILED", "warn")} ${r.errors} error(s), ${r.warnings} warning(s)
      ${r.checker ? `<span class="muted">· ${esc(r.checker)}</span>` : ""}</p>
      ${r.diagnostics.length ? `<table><tr><th>Line</th><th>Severity</th><th>Category</th><th>Message</th></tr>${r.diagnostics.map((d) =>
        `<tr><td class="mono">${d.line}:${d.column}</td><td>${badge(d.severity, sevClass[d.severity])}</td><td class="mono">${esc(d.category)}</td>
         <td>${esc(d.message)}${d.hint ? `<div class="muted">${esc(d.hint)}</div>` : ""}</td></tr>`).join("")}</table>` : ""}`;
  });
}

function bindPromptForm(form) {
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const out = form.querySelector("pre");
    const args = {};
    for (const el of form.elements) if (el.name && el.value.trim()) args[el.name] = el.value.trim();
    out.hidden = false;
    out.textContent = "Rendering…";
    const res = await fetch(`/api/prompts/${enc(form.dataset.prompt)}/render`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ arguments: args }),
    });
    const body = await res.json();
    out.textContent = res.ok ? body.text : `Error: ${body.detail}`;
  });
}

// --------------------------------------------------------------------- boot
async function boot() {
  try {
    state.games = await api("/games");
  } catch (e) {
    view.innerHTML = `<h1>No data</h1><p class="err">${esc(e.message)}</p><pre>uv run s2docs sync</pre>`;
    return;
  }
  if (!state.games.some((g) => g.id === state.game)) state.game = state.games[0]?.id || "cs2";
  $("#game").innerHTML = state.games.map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("");
  $("#game").value = state.game;
  $("#game").addEventListener("change", (e) => {
    const { parts, params } = parseHash();
    if (state.games.some((g) => g.id === parts[0])) parts.shift();
    state.game = e.target.value;
    // Hosts/Activity are install-wide, not per game — stay on them when the game changes.
    if (GLOBAL_SECTIONS.has(parts[0])) { go(parts); return; }
    // Keep the section (and search query) but drop detail paths that may not exist in the other game.
    go([state.game, parts[0] || "search"], parts[0] === "search" || !parts[0] ? params : {});
  });
  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    go([state.game, "search"], { q: $("#q").value.trim() });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
      e.preventDefault();
      $("#q").focus();
    }
  });
  // Delegated CLICK handling for everything inside the periodically re-rendered #live region: the
  // stepper chips scroll to a step, the job-URL copy buttons copy, and a screenshot opens the
  // full-resolution lightbox. Delegated so a re-render can never unbind them.
  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (!t || !t.closest) return;
    const chip = t.closest("[data-vx-goto]");
    if (chip) { gotoStepTimeline(chip.dataset.vxGoto); return; }
    const copy = t.closest("[data-copy]");
    if (copy) { copyText(copy); return; }
    const shot = t.closest("[data-shot]");
    if (shot) { openLightbox(shot.dataset.shot, shot.dataset.caption); return; }
    if (t.id === "lightbox") closeLightbox();
  });
  // ONE delegated submit listener for the dynamic job forms (verdict / screenshot request). Wired
  // here, on the document, so a live re-render of the #live region can never unbind them.
  bindJobFormDelegation();
  window.addEventListener("hashchange", route);
  setInterval(updateCountdowns, 1000); // ticks the NOW TESTING deadline regardless of which page is up
  route();
}
boot();
