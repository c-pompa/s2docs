// Source 2 Docs: a small vanilla-JS SPA over the JSON API. Hash routes: #/<game>/<section>/...
// All dynamic text goes through esc() before it reaches innerHTML.

const $ = (sel) => document.querySelector(sel);
const view = $("#view");
const state = { games: [], game: localStorage.getItem("s2docs.game") || "cs2" };

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const enc = encodeURIComponent;
const snippet = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<mark>$1</mark>");
const badge = (t, cls = "") => `<span class="badge ${cls}">${esc(t)}</span>`;
const firstLine = (s) => (s || "").split("\n")[0];
// Valve FGD text contains <br> line breaks; everything else stays escaped.
const rich = (s) => esc(s).replace(/&lt;br\s*\/?&gt;/gi, "<br>");
const stripTags = (s) => String(s).replace(/<[^>]+>/g, " ");

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
  ["pulse", "Pulse"], ["docs", "Docs (source2.wiki)"], ["diff", "Build Diff"], ["prompts", "Prompts"], ["stats", "Stats"],
];

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
    return `<a href="#/${enc(state.game)}/${id}" class="${id === active ? "active" : ""} ${disabled ? "disabled" : ""}">${esc(label)}</a>`;
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
  if (section === "search" && params.q !== undefined) $("#q").value = params.q;
  view.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const handler = ROUTES[section] || ROUTES.search;
    view.innerHTML = await handler(rest, params);
    bindFilters(section, rest, params);
  } catch (e) {
    view.innerHTML = `<h1>Error</h1><p class="err">${esc(e.message)}</p>`;
  }
  window.scrollTo(0, 0);
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
