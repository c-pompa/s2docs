"""Command line: `s2docs sync | load | serve | mcp | stats | prompt`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from s2docs.config import GAMES, get_game, get_settings
from s2docs.db.connection import Database


def _cmd_sync(args: argparse.Namespace) -> int:
    from s2docs.ingest.loader import load_build
    from s2docs.ingest.sources import sync_source

    settings = get_settings()
    db = Database(settings.db_path)
    games = [] if args.docs_only else ([args.game] if args.game else list(GAMES))
    rc = 0
    for gid in games:
        game = get_game(gid)
        try:
            root = settings.sources_dir / gid
            if not args.no_fetch:
                root = sync_source(game, settings.sources_dir)
            with db.writer() as conn:
                stats = load_build(conn, game, root, keep_builds=args.keep)
            verb = "replaced" if stats.replaced else "loaded"
            print(f"{gid}: {verb} build {stats.build_number}: {json.dumps(stats.counts)}")
        except Exception as exc:  # report and continue with the next game
            logging.getLogger("s2docs").exception("sync failed for %s", gid)
            print(f"{gid}: FAILED: {exc}", file=sys.stderr)
            rc = 1
    if not args.skip_docs:
        from s2docs.ingest.wiki import load_wiki, sync_wiki_source

        try:
            root = settings.sources_dir / "source2wiki"
            if not args.no_fetch or not root.exists():
                root = sync_wiki_source(settings.sources_dir)
            with db.writer() as conn:
                counts = load_wiki(conn, root)
            print(f"source2.wiki: loaded {json.dumps(counts)}")
        except Exception as exc:
            logging.getLogger("s2docs").exception("docs sync failed")
            print(f"source2.wiki: FAILED: {exc}", file=sys.stderr)
            rc = 1
    return rc


def _cmd_load(args: argparse.Namespace) -> int:
    """Load from an existing local checkout (e.g. a full GameTracking clone)."""
    from s2docs.ingest.loader import load_build

    db = Database(get_settings().db_path)
    with db.writer() as conn:
        stats = load_build(conn, get_game(args.game), Path(args.path).expanduser().resolve(), keep_builds=args.keep)
    print(f"{args.game}: build {stats.build_number}: {json.dumps(stats.counts)}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from s2docs.api.app import create_app

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from s2docs.mcp_server import create_mcp_server

    server = create_mcp_server(Database(get_settings().db_path))
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
    return 0


def _cmd_stats(_: argparse.Namespace) -> int:
    from s2docs.db.repository import Repository

    with Database(get_settings().db_path).reader() as conn:
        print(Repository(conn).stats().model_dump_json(indent=2))
    return 0


def _cmd_prompt(args: argparse.Namespace) -> int:
    from s2docs.db.repository import Repository
    from s2docs.prompts import render_prompt

    kv = dict(a.split("=", 1) for a in args.arg)
    with Database(get_settings().db_path).reader() as conn:
        print(render_prompt(Repository(conn), args.name, kv))
    return 0


def _print_diags(result) -> int:
    for d in result.diagnostics:
        print(f"  {d.line}:{d.column} {d.severity:7} {d.category:22} {d.message}")
        if d.hint:
            print(f"      hint: {d.hint}")
    print(f"{'OK' if result.ok else 'FAILED'}: {result.errors} error(s), {result.warnings} warning(s)")
    return 0 if result.ok else 1


def _cmd_validate_script(args: argparse.Namespace) -> int:
    from s2docs.build.validate_script import validate_cs_script
    from s2docs.db.repository import Repository

    path = Path(args.file)
    with Database(get_settings().db_path).reader() as conn:
        result = validate_cs_script(
            Repository(conn), path.read_text(encoding="utf-8"), game=args.game, filename=path.name
        )
    print(result.checker)
    return _print_diags(result)


def _cmd_validate_cfg(args: argparse.Namespace) -> int:
    from s2docs.build.validate_cfg import validate_cfg
    from s2docs.db.repository import Repository

    with Database(get_settings().db_path).reader() as conn:
        result = validate_cfg(
            Repository(conn), Path(args.file).read_text(encoding="utf-8"), game=args.game, context=args.context
        )
    print(f"{result.commands} command(s) checked")
    return _print_diags(result)


def _cmd_scaffold(args: argparse.Namespace) -> int:
    from s2docs.build.workspace import scaffold
    from s2docs.db.repository import Repository

    settings = get_settings()
    with Database(settings.db_path).reader() as conn:
        addon = scaffold(settings.workspace_dir, Repository(conn), game=args.game, name=args.name)
    print(addon.root)
    for f in addon.files:
        print(f"  {f.path} ({f.size} B)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="s2docs", description="Source 2 docs index, API and MCP server")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="Fetch GameTracking data and load it into SQLite")
    p.add_argument("--game", choices=list(GAMES))
    p.add_argument("--no-fetch", action="store_true", help="Reuse the existing checkout under data/sources")
    p.add_argument("--keep", type=int, default=5, help="Builds to keep per game (for diffs)")
    p.add_argument("--skip-docs", action="store_true", help="Don't refresh community docs (source2.wiki)")
    p.add_argument("--docs-only", action="store_true", help="Only refresh community docs")
    p.set_defaults(fn=_cmd_sync)

    p = sub.add_parser("load", help="Load a local GameTracking checkout")
    p.add_argument("game", choices=list(GAMES))
    p.add_argument("path")
    p.add_argument("--keep", type=int, default=5)
    p.set_defaults(fn=_cmd_load)

    p = sub.add_parser("serve", help="Run the web UI + JSON API + MCP (/mcp)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=_cmd_serve)

    p = sub.add_parser("mcp", help="Run the MCP server standalone")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    p.set_defaults(fn=_cmd_mcp)

    sub.add_parser("stats", help="Show indexed counts").set_defaults(fn=_cmd_stats)

    p = sub.add_parser("validate-script", help="Type-check a cs_script .js against point_script.d.ts")
    p.add_argument("file")
    p.add_argument("--game", default="cs2", choices=list(GAMES))
    p.set_defaults(fn=_cmd_validate_script)

    p = sub.add_parser("validate-cfg", help="Check a .cfg against indexed convars/commands")
    p.add_argument("file")
    p.add_argument("--game", default="cs2", choices=list(GAMES))
    p.add_argument("--context", default="server", choices=["server", "client", "any"])
    p.set_defaults(fn=_cmd_validate_cfg)

    p = sub.add_parser("scaffold", help="Create a new addon in the local workspace")
    p.add_argument("name")
    p.add_argument("--game", default="cs2", choices=["cs2"])
    p.set_defaults(fn=_cmd_scaffold)

    p = sub.add_parser("prompt", help="Render a prompt template, e.g. prompt cs2_script task='...'")
    p.add_argument("name")
    p.add_argument("arg", nargs="*", help="key=value")
    p.set_defaults(fn=_cmd_prompt)

    args = parser.parse_args(argv)
    # stdout is the MCP stdio channel: logs must go to stderr.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
