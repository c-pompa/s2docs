# Shipping s2docs as a Hermes plugin (as a catalog plugin)

Goal: `hermes plugins install s2docs` in any Hermes install, and the agent gets the s2docs tool
surface plus the workflow skill — the same way a catalog plugin ships its MCP server.

This document records how Hermes actually loads such a plugin (read from the runtime, not the
docs), what this repo now contains, what was measured, and what is still blocking catalog admission.

## 1. How a plugin gets an MCP server into a session

Hermes has **two** catalogs, and they are different mechanisms:

| | `optional-mcps/` | `plugin-catalog/` |
|---|---|---|
| what | a bare MCP server manifest | a whole plugin package (may bundle MCP servers, skills, Desktop UI) |
| install | `hermes mcp install <name>` | `hermes plugins install <name>` |
| units | 65 entries | 706 entries in-tree (762 in the live catalog JSON) |
| approval | PR merged into `hermes-agent` | PR merged into `hermes-agent`, exact 40-hex sha pin |

The live catalog the Desktop browser and `hermes plugins search` read is
`https://hermes-agent.nousresearch.com/docs/api/plugin-catalog.json`, regenerated from the in-tree
`plugin-catalog/*.yaml` on every docs build (6h TTL client cache, in-tree copy as fallback). So an
entry can reach users without a Hermes update — but only a reviewed in-tree YAML can create one.

the vendor's entry (`plugin-catalog/<vendor>.yaml` upstream, tier `official`) points at
the vendors' plugin repository, one package subdirectory, pinned at a 40-hex sha.
That directory is a **portable Agent Plugins v1 package**:

```
plugin.json          # v1 manifest + extensions.com.nousresearch.hermes (servers, trust, costs, settings)
mcp.json             # mcpServers: a pinned npx server over stdio, cwd ${PLUGIN_DATA}
skills/<skill>/      # the workflow skill (+ references/tools.md generated from a live tools/list)
desktop/             # optional Desktop UI (plugin.js), dashboard/ for a server page
```

Runtime mechanics that decide our design (`hermes_cli/agent_plugins.py`, `hermes_cli/config.py`):

- **stdio** entries: `command` must be a bare executable (`uvx`, `npx`) or a `./` path inside the
  package; `args`/`env`/`cwd` may use `${PLUGIN_ROOT}` and `${PLUGIN_DATA}`; the subprocess gets a
  *filtered* environment (PATH/HOME/… only) plus the declared `env`.
- **streamable-http** entries: `url` must be `https`, or `http` only for `localhost`/loopback, and
  **no placeholder expansion** in the URL or headers. A local-service plugin can point at
  `http://127.0.0.1:<port>/mcp`, but cannot inject a token or a configurable port.
- `${VAR}` values in the joined config are expanded from the profile environment at load. If the
  variable is **not defined**, Hermes warns and keeps the literal `${VAR}` string.
- `trust: untrusted` in `plugin.json` makes every write-capable tool call ask for approval;
  `timeout` sets the per-call deadline.
- Portable packages **install disabled** (`hermes plugins enable <name>`), and a `config.yaml`
  `mcp_servers.<same name>` entry **replaces** the package's server (transport, trust and all).

## 2. What this repo now ships

```
plugin/plugin.json                                  # v1 manifest, server trust untrusted, settings form
plugin/mcp.json                                     # stdio: uvx --from git+…@<sha> s2docs mcp
plugin/skills/s2docs-source2/SKILL.md               # workflow skill (lookup → author → validate → pipeline)
plugin/skills/s2docs-source2/references/tools.md    # generated tool table, 72 rows
plugin/README.md                                    # install, settings, approvals, disclosure
scripts/plugin_tools_md.py                          # regenerates the table from a live tools/list
tests/test_plugin_package.py                        # the package cannot drift from the server
```

Launch path: `uvx --from git+<repo>@<40-hex sha> s2docs mcp`. `uvx` is the analogue of an npm
`npx`: a per-session stdio server built from an immutable pin, no `mcp_servers` editing, no
"please keep a daemon running". The Server is the same FastMCP instance the HTTP mount uses
(`create_mcp_server`), so both transports expose one surface.

s2docs is a **local-first** tool: `uvx` runs it against a local SQLite index. That makes it a
portable stdio package, not a hosted connector — the opposite of 0bull's hosted Streamable HTTP MCP
entry, and the reason no OAuth/settings secret is needed at all.

## 3. What was measured (not assumed)

| check | result |
|---|---|
| `hermes plugins validate plugin/` | **passed** — portable manifest, config schema, server trust, security scan `safe`, isolation `portable` |
| `hermes plugins validate --json plugin/` | `ok: true`, 5 checks, 0 warnings |
| runtime loader (`load_agent_plugin`) | 0 diagnostics, 1 skill `s2docs-source2`, 1 server `s2docs` → `uvx …`, `trust=untrusted`, `timeout=300` |
| live `tools/list` from **this checkout** | 72 tools (43 read-only, 29 write-capable) |
| live `tools/list` from the **pinned git ref** (`--from-manifest`) | 72 tools, same set — the declared launch path really starts the server |
| `pytest tests/test_plugin_package.py tests/test_data_dir.py` | 16 passed |

Tool count note: the Hermes deferred-tool catalog lists 76 `mcp__s2docs__*` names while a live
`tools/list` returns 72. The four extra are Hermes's own generic per-server helpers
(`list_prompts`, `list_resources`, `read_resource`, `get_prompt`), which it registers for every MCP
server — they are not tools the server defines. The package documents the live `tools/list` count
(72), because that is what the server is responsible for; the helpers work for us too, since the
server does expose prompts (`list_prompts` → `get_prompt_text`, or `get_prompt` directly).

Two traps were caught while wiring this:

1. **`S2DOCS_DATA_DIR` unexpanded is a path.** With the variable undefined, `mcp.json`'s
   `"${S2DOCS_DATA_DIR}"` reaches the server literally, and `Path("${S2DOCS_DATA_DIR}")` would
   create a *shadow store* under a directory of that name. `config.get_settings` now rejects any
   value carrying an unexpanded `${…}` for the data dir, DB, workspace, auth token and public URL,
   warns on stderr, and falls back to the documented default (`src/s2docs/config.py::_env_ref`,
   covered by `tests/test_data_dir.py`).
2. **`mcp` 2.3.0 pagination is `params=`, not `cursor=`.** `session.list_tools(cursor=…)` raises
   `TypeError`, and since the first page holds all 72 tools the broken branch never ran. The
   generator paginates with `types.PaginatedRequestParams` and the tool-table test asserts the row
   set equals the registry, so a paging regression fails the suite instead of silently truncating.

## 4. Catalog admission — state

- **Public repo + exact 40-hex pin (rule 2).** Done: <https://github.com/c-pompa/s2docs> is public
  and anonymously cloneable (`git clone` with the credential helper disabled works). The canonical
  repository stays private on GitLab; the public repo is a mirror published from it. The plugin
  package lives at `plugin/` (`subdir: plugin`).
- **Licence.** MIT, the same licence as other catalog plugins (identical text, our own copyright holder). Carried at the
  repo root and inside the package (`plugin/LICENSE`), matching the reference's layout, plus
  `license: MIT` in `plugin.json`.
- **Machine-specific identifiers.** Genericized before publication: private LAN/tailnet addresses →
  RFC 5737 documentation ranges, machine names → role names, `/Volumes/...` paths → placeholders,
  author email dropped from package metadata. `DEFAULT_PUBLIC_URL` is now
  `http://127.0.0.1:8812`; a deployment served on a LAN address sets `S2DOCS_PUBLIC_URL`.
- **Declared capabilities match reality (rule 6).** `provides_tools: []` is correct for a portable
  package — its tools come from the MCP server, not from Python registrations. `requires_env` is
  empty on purpose: every setting has a working default.
- **Install scan (rule 7).** `hermes plugins validate plugin/` = `security scan: safe`.
- **Credentials (rule 11).** s2docs holds none of its own; SSH uses the user's agent.
- **Disclosure (rule 13).** `plugin/README.md` § Disclosure.
- **Compatibility metadata (rule 14).** `requires_hermes: ">=0.21.5"` — the build this package was
  validated and live-launched on. Raise it only with a release that makes a *newer* feature a
  requirement; lower it only after testing an older release.
- **Draft entry.** Below. Submission is an owner PR to `NousResearch/hermes-agent` adding
  `plugin-catalog/s2docs.yaml`; SHA bumps are separate PRs.

### Two pins, and why they differ by one commit

`plugin-catalog/s2docs.yaml`'s `sha` pins the **package** commit (the commit a reviewer reads).
`plugin/mcp.json` pins a **server source** commit for `uvx --from`. Both point at the same public
repo, and the mcp.json pin can never equal the commit that contains it (a commit cannot name its
own sha). So a re-pin is two commits:

1. push the code change; note the resulting sha `S` on the public repo;
2. commit `mcp.json` with `@S` (plus the `version` label), then use *that* commit's sha in the
   catalog entry.

A package whose server is an npm version avoids the dance entirely.

### Draft catalog entry

```yaml
name: s2docs
title: s2docs
repo: https://github.com/c-pompa/s2docs
sha: <40-hex sha of the package commit>
subdir: plugin
version: "0.1.0"
description: >-
  Source 2 documentation and mod tooling for CS2 and Deadlock from a local index of Valve's own game
  data: convars, Hammer entities, schema classes, game events, cs_script and Pulse APIs, Source2Wiki
  pages, plus the mod pipeline (scaffold addons, validate scripts/cfg, build maps on a registered
  Windows host over SSH, deploy and verify hashes, record evidence, test runs and jobs). Needs uv and
  an index built with `s2docs sync`; every write-capable call asks for approval. Disclosure — nothing
  is sent to a third party; it SSHes only to hosts you register, writes its index/addon workspace,
  and starts build runs that outlive the session by design.
maintainer: c-pompa
tier: community
category: tools
requires_hermes: ">=0.21.5"
docs_url: https://github.com/c-pompa/s2docs/tree/main/plugin
platforms: [macos, linux, windows]
capabilities:
  provides_tools: []
  provides_hooks: []
  provides_middleware: []
  requires_env: []
```

### Re-check commands

```bash
python scripts/plugin_tools_md.py                 # table from this checkout
python scripts/plugin_tools_md.py --from-manifest # table from the pinned public commit (release check)
hermes plugins validate plugin/                   # the admission gate, locally
pytest tests/test_plugin_package.py -q            # package/server drift
```

When the server pin moves: bump `plugin/mcp.json`'s `--from` ref, bump `plugin.json`'s `version`,
regen the tool table, run the four commands above, push, then open the SHA-bump PR with the upstream
commit range being adopted.
