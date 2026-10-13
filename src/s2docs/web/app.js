// Source 2 Docs: a small vanilla-JS SPA over the JSON API. Hash routes: #/<game>/<section>/...
// All dynamic text goes through esc() before it reaches innerHTML.

const $ = (sel) => document.querySelector(sel);
const view = $("#view");
const state = { games: [], game: localStorage.getItem("s2docs.game") || "cs2", activityActive: false };

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
const parseTs = (iso) => {
  const s = String(iso);
  return Date.parse(/[Zz]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z");
};
const secsAgo = (iso) => {
  if (!iso) return null;
  const t = parseTs(iso);
  return Number.isNaN(t) ? null : Math.max(0, Math.floor((Date.now() - t) / 1000));
};
const human = (s) => {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
};
const runElapsed = (r) => {
  const start = parseTs(r.created_at);
  const end = r.status === "running" || r.status === "queued" ? Date.now() : parseTs(r.updated_at);
  const secs = Math.floor((end - start) / 1000);
  return Number.isNaN(secs) ? null : Math.max(0, secs);
};
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
  return `${badge(lock.owner, lock.fresh && !lock.stuck ? "kind" : "warn")}${lockStuckBadge(lock)}<div class="muted">${state} · heartbeat ${human(lock.age_sec)} ago · ttl ${human(lock.ttl_sec)}${phase}${
    lock.stuck && lock.stuck_reason ? `<br>${esc(lock.stuck_reason)}` : ""
  }</div>`;
};

const lockRows = (evs) =>
  evs.length
    ? `<table><tr><th>When</th><th>Event</th><th>Owner</th><th>Detail</th></tr>${evs
        .map(
          (e) =>
            `<tr><td class="muted">${esc(e.ts)}</td><td>${stateBadge(e.event)}</td><td class="mono">${esc(e.owner)}</td>` +
            `<td class="muted">${esc(e.detail || "")}</td></tr>`,
        )
        .join("")}</table>`
    : `<p class="muted">No lock history.</p>`;

const buildRows = (runs) =>
  runs.length
    ? `<table><tr><th>Run</th><th>Host</th><th>Status</th><th>Elapsed</th><th>Summary</th></tr>${runs
        .map(
          (r) =>
            `<tr><td class="mono">${esc(r.id)}<div class="muted">${esc(r.kind)}</div></td>` +
            `<td>${link(["hosts", r.host_id], r.host_id)}</td>` +
            `<td>${stateBadge(r.status)}${lockStuckBadge(r.lock)}${r.exit_code != null ? `<div class="muted mono">exit ${r.exit_code}</div>` : ""}</td>` +
            `<td class="muted">${human(runElapsed(r))}</td><td class="muted">${esc(summaryText(r.summary))}${materialWarning(r)}${
              runPhase(r) ? `<div class="muted">${esc(runPhase(r))}</div>` : ""
            }</td></tr>`,
        )
        .join("")}</table>`
    : `<p class="muted">No build runs.</p>`;

const deployRows = (ds) =>
  ds.length
    ? `<table><tr><th>Deploy</th><th>Host</th><th>Status</th><th>Files</th><th>When</th></tr>${ds
        .map((d) => {
          const total = d.entries.length;
          const verified = d.entries.filter((e) => e.verified).length;
          return (
            `<tr><td class="mono">${esc(d.id)}${d.addon ? `<div class="muted">${esc(d.addon)}</div>` : ""}</td>` +
            `<td>${link(["hosts", d.host_id], d.host_id)}</td><td>${stateBadge(d.status)}</td>` +
            `<td class="muted"><strong>${verified}</strong>/${total} verified</td>` +
            `<td class="muted">${human(secsAgo(d.created_at))} ago</td></tr>`
          );
        })
        .join("")}</table>`
    : `<p class="muted">No deployments.</p>`;

// The test-run fields mirror the in-game TEST STATUS HUD strip: name / step / state / elapsed.
const testRows = (ts) =>
  ts.length
    ? `<table><tr><th>Name</th><th>Step</th><th>State</th><th>Elapsed</th><th>Host</th></tr>${ts
        .map(
          (t) =>
            `<tr><td class="mono">${esc(t.name)}</td><td>${esc(t.step || "—")}</td><td>${stateBadge(t.state)}</td>` +
            `<td class="mono">${human(t.elapsed_sec)}</td><td>${t.host_id ? link(["hosts", t.host_id], t.host_id) : "—"}</td></tr>`,
        )
        .join("")}</table>`
    : `<p class="muted">No test runs.</p>`;

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
const SECTIONS = [
  ["search", "Search"], ["convars", "ConVars & Commands"], ["entities", "Entities (Hammer)"],
  ["schema", "Schema"], ["events", "Game Events"], ["script", "cs_script API"], ["examples", "Examples"],
  ["pulse", "Pulse"], ["docs", "Docs (source2.wiki)"], ["validate", "Validate"], ["projects", "Orchestration"],
  ["hosts", "Hosts"], ["activity", "Activity"], ["diff", "Build Diff"], ["prompts", "Prompts"], ["stats", "Stats"],
];
// Pages that are about the whole install (hosts/runs), not one game — routed as #/<section>, no game prefix.
const GLOBAL_SECTIONS = new Set(["hosts", "activity"]);

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
  $("#nav").innerHTML = SECTIONS.map(([id, label]) => {
    const disabled = (id === "script" || id === "examples") && !g?.script_api;
    const href = GLOBAL_SECTIONS.has(id) ? `#/${id}` : `#/${enc(state.game)}/${id}`;
    return `<a href="${href}" class="${id === active ? "active" : ""} ${disabled ? "disabled" : ""}">${esc(label)}</a>`;
  }).join("");
  $("#build").textContent = g?.current_build ? `build ${g.current_build} · ${g.patch_version || ""}` : "not indexed";
}

async function route() {
  let { parts, params } = parseHash();
  if (parts[0] && state.games.some((g) => g.id === parts[0])) {
    state.game = parts.shift();
    localStorage.setItem("s2docs.game", state.game);
    $("#game").value = state.game;
  }
  const [section = "search", ...rest] = parts;
  renderNav(section);
  clearTimeout(liveTimer); // any auto-refresh from the previous page stops now
  liveTimer = null;
  if (section === "search" && params.q !== undefined) $("#q").value = params.q;
  view.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const handler = ROUTES[section] || ROUTES.search;
    view.innerHTML = await handler(rest, params);
    bindFilters(section, rest, params);
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
        try { el.innerHTML = await render(); } catch { /* keep the last good view */ }
        liveTimer = setTimeout(tick, delay());
      };
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
  view.querySelectorAll("form[data-start-project]").forEach(bindStartProject);
  view.querySelectorAll("form[data-step]").forEach(bindStepForm);
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
              `<div class="muted mono">${esc(s.id)} · ${esc(s.kind)} · ${esc(s.status)}${s.gate ? " · gate:" + esc(s.gate) : ""}</div></div>`,
          )
          .join("")}</div>` + (i < cols.length - 1 ? `<div class="dag-arrow">→</div>` : ""),
    )
    .join("")}</div>`;
}

// --------------------------------------------------------------------- Activity
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
    return `<h1>Build diff</h1><form class="filters">${select("from", String(d.from_build), opts)}${select("to", String(d.to_build), opts)}</form>
      ${Object.entries(d.sections).map(([sec, rows]) => `<h2>${esc(sec)} (${rows.length})</h2>${rows.length ? `<table>
        <tr><th>Change</th><th>Name</th><th>Before → After</th></tr>${rows.map((r) => `<tr><td>${badge(r.change, r.change === "removed" ? "warn" : "kind")}</td>
        <td class="mono">${esc(r.name)}</td><td class="mono muted">${esc((r.before || "").slice(0, 160))}${r.change === "changed" ? " → " : ""}${esc((r.after || "").slice(0, 160))}</td></tr>`).join("")}</table>` : `<p class="muted">No changes.</p>`}`).join("")}`;
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
            `<td class="muted">${s.updated_at ? `${esc(human(secsAgo(s.updated_at)))} ago` : "—"}</td>` +
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
      return `<p>${link([state.game, "projects"], "← Orchestration")}</p><h1>${esc(p.title)}</h1>
        <p>${statusBadge(p.status)}${badge(p.workflow)}${badge(p.game)}${p.addon ? badge("addon: " + p.addon) : ""} <span class="mono muted">${esc(p.id)}</span></p>
        <p class="muted">This is an <strong>orchestration DAG</strong> — the server's multi-step plan — not a build/test run.
          For the runs these steps drive on hosts, see ${link(["activity"], "Activity")}.</p>
        <p>${esc(p.goal)}</p><p class="muted">${esc(b.guidance)}</p>
        <p class="muted">Agent hand-off: <code>continue project ${esc(p.id)}</code> (MCP tool <code>next_step</code>)</p>
        <h2>Step graph</h2>${stepDag(p.steps)}
        <h2>Steps (${esc(b.progress)})</h2>
        <table><tr><th>Step</th><th>State</th><th>Gate</th><th>Evidence</th><th>Updated</th><th>Depends on</th><th>Notes / artifacts</th></tr>${stepRowsHtml}</table>
        ${ready ? `<h2>Ready now</h2>${ready}` : ""}`;
    }
    const [projects, workflows] = await Promise.all([api("/projects"), api("/workflows")]);
    const wfOptions = workflows.filter((w) => w.games.includes(state.game)).map((w) => [w.id, w.title]);
    return `<h1>Orchestration</h1><p class="muted">Multi-step <strong>orchestration DAGs</strong>: the server holds the plan and the
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
      <h2>All projects</h2>${projects.length ? `<table><tr><th>Project</th><th>Workflow</th><th>Status</th><th>Progress</th><th>Updated</th></tr>
      ${projects.map((p) => `<tr><td>${link([state.game, "projects", p.id], p.title)}<div class="muted">${esc(p.goal.slice(0, 90))}</div></td>
        <td class="mono">${esc(p.workflow)}</td><td>${statusBadge(p.status)}</td><td>${esc(p.progress)}</td><td class="muted">${esc(p.updated_at)}</td></tr>`).join("")}</table>`
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
        return `<p>${h.shell ? badge(h.shell) : ""}${h.os ? badge(h.os) : ""}${h.hostname ? badge(h.hostname) : ""}<span class="mono muted"> ${esc(h.ssh_target)}</span></p>
          <p>${lockCell(h.lock)}<span class="muted"> · probed ${h.probed_at ? esc(human(secsAgo(h.probed_at))) + " ago" : "never"}</span></p>
          ${h.notes ? `<p class="muted">${esc(h.notes)}</p>` : ""}
          ${facts}
          <h2>Lock history</h2>${lockRows(locks)}
          <h2>Recent builds (${builds.length})</h2>${buildRows(builds.slice(0, 15))}
          <h2>Recent deployments (${deploys.length})</h2>${deployRows(deploys.slice(0, 15))}
          <h2>Recent test runs (${tests.length})</h2>${testRows(tests.slice(0, 15))}`;
      };
      liveBody("#live", body);
      return `<p>${link(["hosts"], "← Hosts")}</p><h1 class="mono">${esc(hostId)}</h1><div id="live">${await body()}</div>`;
    }
    const body = async () => {
      const hosts = await api("/hosts");
      if (!hosts.length)
        return `<p class="muted">No hosts registered. Register one over MCP (<code>register_host</code>) or the CLI (<code>s2docs host register …</code>).</p>`;
      return `<table><tr><th>Host</th><th>Shell</th><th>GPU</th><th>Probed</th><th>Lock</th></tr>${hosts
        .map(
          (h) =>
            `<tr><td>${link(["hosts", h.id], h.id)}<div class="muted mono">${esc(h.hostname || h.ssh_target || "")}</div></td>` +
            `<td>${h.shell ? badge(h.shell) : "—"}${h.os ? badge(h.os) : ""}</td><td>${esc(gpuOf(h))}</td>` +
            `<td class="muted">${h.probed_at ? esc(human(secsAgo(h.probed_at))) + " ago" : "never"}</td>` +
            `<td>${lockCell(h.lock)}</td></tr>`,
        )
        .join("")}</table>`;
    };
    liveBody("#live", body);
    return `<h1>Hosts</h1><p class="muted">Test/build hosts and who is driving each right now (owner, fresh vs stale, heartbeat age). Auto-refreshes every 5 s. Click a host for its facts, lock history and recent activity.</p><div id="live">${await body()}</div>`;
  },

  // Game-independent: the live view a person watches to answer "what is running right now".
  async activity() {
    const body = async () => {
      const [builds, tests, deploys, current] = await Promise.all([
        api("/builds"),
        api("/test-runs"),
        api("/deploys"),
        api("/test-runs/current").catch(() => null),
      ]);
      const isActiveBuild = (b) => b.status === "running" || b.status === "queued";
      const activeBuilds = builds.filter(isActiveBuild);
      const activeTest = current && current.state === "running" ? current : null;
      // Drives the poll cadence: fast while anything is live, slow when the system is idle.
      state.activityActive = activeBuilds.length > 0 || !!activeTest;

      if (!builds.length && !tests.length && !deploys.length) return activityEmptyState();

      // ---- NOW: everything currently running, freshest first (updated_at = heartbeat-ish for a run).
      const ts = (r) => (r.updated_at || r.created_at || r.started_at || "");
      const nowCards = [];
      if (activeTest)
        nowCards.push(
          `<div class="card" style="margin-bottom:8px">${stateBadge(activeTest.state)} <strong>TEST</strong> ${esc(activeTest.name)} —
           <span class="mono">${esc(activeTest.step || "(no step)")}</span>
           <span class="muted">· ${human(activeTest.elapsed_sec)} · host ${
             activeTest.host_id ? link(["hosts", activeTest.host_id], activeTest.host_id) : "—"
           }${activeTest.project_id ? ` · ${esc(activeTest.project_id)}` : ""}</span></div>`,
        );
      activeBuilds
        .slice()
        .sort((a, b) => (ts(a) < ts(b) ? 1 : -1))
        .forEach((b) =>
          nowCards.push(
            `<div class="card" style="margin-bottom:8px">${stateBadge(b.status)} <span class="mono">${esc(b.id)}</span> on ${link(
              ["hosts", b.host_id],
              b.host_id,
            )} <span class="muted">· ${esc(b.kind)} · up ${human(runElapsed(b))}</span>${materialWarning(b)}${
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
      const idleNote = newest ? ` Last activity ${human(secsAgo(newest))} ago.` : "";
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

      return `<h2>Now${state.activityActive ? ` ${badge("live", "kind")}` : ""}</h2>${now}
        <h2>By host</h2>${hostBlocks}
        <h2>Full history</h2>
        <details><summary class="muted">Build runs (${builds.length})</summary>${buildRows(builds.slice(0, 30))}</details>
        <details><summary class="muted">Test runs (${tests.length})</summary>${testRows(tests.slice(0, 30))}</details>
        <details><summary class="muted">Deployments (${deploys.length})</summary>${deployRows(deploys.slice(0, 30))}</details>`;
    };
    // Fast (5 s) while a run is active; slow (15 s) when idle so an idle install isn't hammered.
    liveBody("#live", body, () => (state.activityActive ? 5000 : 15000));
    return `<h1>Activity</h1><p class="muted">What is running right now across all hosts — build runs, test runs and deployments, without SSHing anywhere.
      This view is <strong>service-fed</strong>: rows appear when work goes through the s2docs build/test/deploy services
      (MCP <code>build_start</code>/<code>test_start</code>/<code>deploy_push</code>, or the CLI), <em>not</em> from any CS2 running anywhere.
      Test-run rows mirror the in-game HUD strip (name / step / state / elapsed). Auto-refreshes every 5 s while a run is active, 15 s when idle.</p><div id="live">${await body()}</div>`;
  },

  async validate() {
    const g = state.games.find((x) => x.id === state.game);
    const scriptForm = g?.script_api ? `<h2>cs_script</h2>
      <p class="muted">Type-checked by the TypeScript compiler against this build's <code>point_script.d.ts</code>.</p>
      <form data-validate="cs-script"><textarea name="code" rows="14" class="mono" placeholder='import { Instance } from "cs_script/point_script";'></textarea>
      <p><button type="submit">Check script</button></p><div class="result"></div></form>` : "";
    return `<h1>Validate</h1>${scriptForm}
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
  window.addEventListener("hashchange", route);
  route();
}
boot();
