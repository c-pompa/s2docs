# s2docs architecture

Two pictures, generated from the tree, plus the list of seams the next changes are aimed at.
The SVG in `docs/arch/` is the authoritative version — this file explains it and records why each
seam is a problem.

Regenerate (the counts are read out of the source, so growth shows up in the picture):

```bash
uv run python scripts/arch_diagrams.py --out docs/arch --suffix "$(date +%Y%m%d-%H%M)"
```

Every run also writes `manifest.json` beside the SVGs with the commit sha and the counts.

## A — data plane (`s2docs-data-plane-*.svg`)

```
Valve game data + Source2Wiki
      ↓ ingest/sources.py · wiki.py · loader.py      (s2docs sync / load)
SQLite: content tables + search_fts (FTS5) + operational tables
      ↓ db/repository.py · domain stores · orchestration · build/ + asset/ writers
three surfaces: CLI · FastAPI + web UI on :8812 · MCP stdio
```

Content is read-only once indexed; everything the app *does* (builds, deploys, jobs, evidence,
assets, live actions) is a row in the same SQLite file.

## B — control plane (`s2docs-control-plane-*.svg`)

```
user or agent → any surface → record/queue (jobs, projects) ─┐
                            → execute on a host (hosts, builds, deploys) ─┤→ SQLite
                            → live (live/service.py → netcon) ───────────┘   → Windows host
                                                                            → resourcecompiler
                                                                            → CS2
                            verdict + evidence ←────────────────────────────┘
```

`live/service.py` is the only module where one entry point (`act()` / `status()`) is shared by all
three surfaces. That is the pattern the rest of the codebase lacks — see seam 1.

## Seams

| # | seam | why it costs us | fix |
|---|---|---|---|
| 1 | **Three surfaces, one capability each time.** 76 CLI commands, 97 routes, 81 MCP tools over the same domain functions, each wired by hand. `pipeline` is CLI-only; `live` is the exception that proves the rule. | every feature is built three times, and the surfaces drift (the MCP tools were 5 behind the CLI after the live-sandbox merge) | a declarative capability table + a test that fails when a surface lacks an operation it claims |
| 2 | **Builds run inside whichever process started them** (`builds._RUNNERS`, an in-process `Popen`). | a `serve` or MCP restart kills an in-flight compile — a 38-minute map build dies with the process that started it, and the run stays `running` forever | detach the run (`start_new_session`), persist the pid and log path, recover honestly at startup |
| 3 | **MCP tool lists are frozen at session start.** | the server gains a tool and the running session cannot see it; the only cure is a restart nobody is told about | a capability/surface summary tool, and docs that say a restart is required |
| 4 | **Schema churn.** 16 migrations, several rebuilding `*_new` / `*_stash` tables (SQLite `ALTER` limits). | a fresh index and a migrated one can diverge, and nothing checks | a test that a fresh migration leaves no scratch tables behind |
| 5 | **Ingestion is offline and silent.** `sync` / `load` only. | nothing says "your index predates the CS2 build you are asking about" | a read-only staleness check |
| 6 | **The chain is 2 of 7 stages real** — `asset` and `build` real, `concept`/`mesh` stubbed, `deploy`/`ingame` blocked. | `deploy` is marked blocked although `deploy.push` works and is tested; the label undersells the app | make `deploy` real when a host and destination are given; keep `ingame` blocked with its measured reason |
| 7 | **The plugin's tool table drifted one way.** The test asserts documented ⊆ registered. | new tools ship undocumented — the 5 live tools did exactly that | assert equality and regenerate in the same commit |
| 8 | **Live transport sits outside the domain layer.** `live/netcon.py`, `live/launch.py` own transport and process control. | two places now know how to reach a host, and the capability gate is only enforced on the paths that remember it | documented here; not fixed in this pass |

## Comparing a run

1. Pick the suffix of the baseline you want to compare against (`docs/arch/baseline-<suffix>/`).
2. Regenerate after the change.
3. Compare the `manifest.json` counts and the two SVGs side by side — the arrows and the layer
   bands are the same layout, so a new box or a moved arrow is the diff.
