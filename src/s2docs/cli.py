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
    games = [args.game] if args.game else list(GAMES)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="s2docs", description="Source 2 docs index, API and MCP server")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="Fetch GameTracking data and load it into SQLite")
    p.add_argument("--game", choices=list(GAMES))
    p.add_argument("--no-fetch", action="store_true", help="Reuse the existing checkout under data/sources")
    p.add_argument("--keep", type=int, default=5, help="Builds to keep per game (for diffs)")
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
