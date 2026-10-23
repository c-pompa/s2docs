# s2docs — Hermes Agent plugin

Source 2 documentation and mod tooling for **CS2** and **Deadlock**, packaged as a portable
[Agent Plugins v1](https://agent-plugins.org) package. One install ships:

- `mcp.json` — the s2docs MCP server, launched over stdio for every new session while the plugin
  is enabled (`uvx --from git+…@<pinned sha> s2docs mcp`). No `mcp_servers` edit in `config.yaml`.
- `skills/s2docs-source2/` — a workflow skill: which lookup tool to use for which kind of thing,
  how to author and validate an addon, and how the host/build/deploy/evidence/job pipeline fits
  together, plus a tool table generated from a live `tools/list`.

The server reads a **local SQLite index** built from Valve's own game data (see
[`../README.md`](../README.md) for `s2docs sync` / `s2docs load`). Nothing leaves the machine.

## Install

```bash
hermes plugins install s2docs          # from the Hermes plugin catalog (not listed yet — see Status)
hermes plugins enable s2docs
```

Then index at least one game, or point the plugin at an existing index:

```bash
s2docs sync --game cs2               # builds the index in the default data dir
hermes config set S2DOCS_DATA_DIR /absolute/path/to/data   # or reuse an existing index
```

Restart Hermes (or save the setting in Desktop → Settings → Plugins → s2docs) so the stdio server
is launched with the new directory. In Hermes Desktop the same two settings live on the plugin's
settings page.

Requirements: `uv` on `PATH` (`uvx` is how the pinned server is launched; the first start builds
the package from the pinned commit into uv's cache) and `git` (the pinned ref is a git URL). Tools
that build or deploy also need a registered Windows host reachable over SSH — see `list_hosts`.

## Settings

| Setting | Default | CLI |
|---|---|---|
| Index directory | per-user (`~/.local/share/s2docs`, Windows `%LOCALAPPDATA%\s2docs`) | `hermes config set S2DOCS_DATA_DIR /abs/path` |
| Web UI base URL | `http://127.0.0.1:8812` | `hermes config set S2DOCS_PUBLIC_URL http://host:8812` |

Both are passed to the server through `mcp.json` as `${VAR}` references. If the variable is not
defined, Hermes hands the server the literal `${VAR}` string; s2docs detects that and falls back
to its documented default with a warning on stderr, so an unset setting can never create a second
store under a directory named `${S2DOCS_DATA_DIR}`.

To use a different store than the plugin's own, override the server in `config.yaml` — a config
entry with the same name replaces the plugin's, transport and trust included:

```yaml
mcp_servers:
  s2docs:
    command: uvx
    args: ["--from", "git+<same pinned URL from mcp.json>", "s2docs", "mcp"]
    env:
      S2DOCS_DATA_DIR: /absolute/path/to/data
```

## Approvals

The plugin declares the server `trust: untrusted`, so **every write-capable call** (29 of the 72
tools: addon writes, builds, deploys, evidence, test runs, job state) asks for approval before it
runs, and read-only lookups (43 tools) run without a prompt. Unattended runs (cron, `hermes chat
-q`) cannot approve, so writes are refused there — enqueue a job instead and let the user answer.

## Disclosure

- **Network:** none, except what the tools you call are for: SSH to hosts you registered
  (`probe_host`, `build_*`, `deploy_*`) and, at first start, the package build from the pinned git
  ref. Source2Wiki pages are fetched by `s2docs sync`, not by the server at query time.
- **Reads:** its own index/data directory, plus the addon workspace inside it.
- **Writes:** the index and addon workspace, and any host you asked it to touch.
- **Credentials:** none of its own. SSH uses your existing agent/keys; Hermes never passes other
  environment variables to the stdio server.
- **Background processes:** build runs are server-owned subprocesses/SSH sessions started by
  `build_start` and visible in the web UI; they keep running if the agent disconnects, by design
  (a map compile takes minutes).

## Status

Package source: <https://github.com/c-pompa/s2docs> (public mirror; the canonical repository is
private). The package validates (`hermes plugins validate plugin/`) and the pinned launch path
yields a live 73-tool server, installed anonymously from the public mirror. Catalog submission
status and the re-pin procedure are in `docs/AGENT_PLUGIN.md`.
