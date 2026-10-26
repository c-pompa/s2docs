"""Draw the s2docs architecture diagrams as large vector SVGs, committed with a timestamp.

Run this before and after a change and diff the two files: the label counts (tools, routes,
commands, migrations) are read from the tree at generation time, so growth is visible in the
picture, not just in git.

    uv run python scripts/arch_diagrams.py --out docs/arch --suffix 20261025-2257

Layout is hand-computed on purpose: mermaid renders through a bitmap at whatever width the
client picks, which is unreadable once a layer holds four boxes. This writes real SVG with
19px labels, a fixed canvas, and no external tool.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

INK, MUTED, BG = "#1f2328", "#57606a", "#ffffff"
BAND = {"source": "#eef6ff", "ingest": "#e8f5ec", "store": "#fff6e5", "domain": "#f3eefc",
        "surface": "#fdeef2", "exec": "#eaf4f8", "live": "#e9f7f1"}
REPO = Path(__file__).resolve().parents[1]


@dataclass
class Node:
    nid: str
    text: str
    w: int = 0
    h: int = 0
    x: float = 0
    y: float = 0


@dataclass
class Band:
    title: str
    color: str
    nodes: list[Node] = field(default_factory=list)
    box_y: float = 0
    box_h: float = 0


def layout(bands: list[Band], edges, title: str, note: str, font: int = 19, wrap: int = 26) -> str:
    pad, gap_y, gap_x, lpad = 26, 86, 44, 14
    for b in bands:
        for n in b.nodes:
            lines = textwrap.wrap(n.text, wrap)
            n.w = max(int(max(len(s) for s in lines) * font * 0.58) + 44, 190)
            n.h = len(lines) * int(font * 1.35) + 26
    widest = max(sum(n.w for n in b.nodes) + gap_x * (len(b.nodes) - 1) for b in bands)
    width = max(widest + pad * 2, 1500)
    y = 118 + lpad
    for b in bands:
        b.box_h = max(n.h for n in b.nodes) + lpad * 2 + 26
        tw = sum(n.w for n in b.nodes) + gap_x * (len(b.nodes) - 1)
        x = (width - tw) / 2
        for n in b.nodes:
            n.x, n.y = x, y + lpad + 26
            x += n.w + gap_x
        b.box_y = y
        y += b.box_h + gap_y
    height = y - gap_y + pad + 30
    by_id = {n.nid: n for b in bands for n in b.nodes}

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="ui-sans-serif,-apple-system,Segoe UI,Helvetica,Arial">',
           f'<rect width="{width}" height="{height}" fill="{BG}"/>',
           f'<text x="{pad}" y="52" font-size="30" font-weight="700" fill="{INK}">{escape(title)}</text>',
           f'<text x="{pad}" y="80" font-size="17" fill="{MUTED}">{escape(note)}</text>',
           f'<text x="{pad}" y="104" font-size="15" fill="{MUTED}">'
           f'boxes = real modules · arrows = the direction data or control actually flows</text>']
    for b in bands:
        out.append(f'<rect x="{pad/2}" y="{b.box_y}" width="{width-pad}" height="{b.box_h}" rx="16" '
                   f'fill="{BAND[b.color]}" stroke="#d8dee4"/>')
        out.append(f'<text x="{pad}" y="{b.box_y+30}" font-size="18" font-weight="700" fill="{MUTED}" '
                   f'letter-spacing="0.6">{escape(b.title)}</text>')
    for a, bb, *rest in edges:
        label = rest[0] if rest else ""
        na, nb = by_id[a], by_id[bb]
        x1, y1 = na.x + na.w / 2, na.y + na.h
        x2, y2 = nb.x + nb.w / 2, nb.y
        if nb.y < na.y:
            y1, y2 = na.y, nb.y + nb.h
        mid = (y1 + y2) / 2
        out.append(f'<path d="M{x1:.0f},{y1:.0f} C{x1:.0f},{mid:.0f} {x2:.0f},{mid:.0f} {x2:.0f},{y2:.0f}" '
                   f'fill="none" stroke="#8c959f" stroke-width="2.2"/>')
        d = 1 if y2 >= y1 else -1
        out.append(f'<polygon points="{x2:.0f},{y2:.0f} {x2-7:.0f},{y2-11*d:.0f} {x2+7:.0f},{y2-11*d:.0f}" '
                   f'fill="#8c959f"/>')
        if label:
            out.append(f'<rect x="{(x1+x2)/2-46}" y="{mid-13}" width="92" height="24" rx="6" fill="{BG}"/>')
            out.append(f'<text x="{(x1+x2)/2}" y="{mid+4}" font-size="15" fill="{MUTED}" '
                       f'text-anchor="middle">{escape(label)}</text>')
    for b in bands:
        for n in b.nodes:
            out.append(f'<rect x="{n.x:.0f}" y="{n.y:.0f}" width="{n.w}" height="{n.h}" rx="11" '
                       f'fill="#ffffff" stroke="#57606a" stroke-width="2"/>')
            lines = textwrap.wrap(n.text, wrap)
            ty = n.y + n.h / 2 - (len(lines) - 1) * font * 0.68 + font * 0.35
            for ln in lines:
                out.append(f'<text x="{n.x + n.w/2:.0f}" y="{ty:.0f}" font-size="{font}" '
                           f'fill="{INK}" text-anchor="middle">{escape(ln)}</text>')
                ty += font * 1.35
    out.append("</svg>")
    return "\n".join(out)


def counts() -> dict[str, str]:
    """Read the live numbers out of the tree so the picture cannot flatter the code."""
    def n(pattern: str, path: str) -> int:
        p = REPO / path
        return len(re.findall(pattern, p.read_text(encoding="utf-8"), re.MULTILINE)) if p.exists() else 0
    return {
        "routes": str(n(r'@[a-z_]+\.(get|post|put|delete)\(', "src/s2docs/api/routes.py")),
        "commands": str(n(r'add_parser\(', "src/s2docs/cli.py")),
        "tools": str(n(r'^\s{4}@server\.tool', "src/s2docs/mcp_server.py")),
        "migrations": str(len(list((REPO / "src/s2docs/db/migrations").glob("*.sql")))),
    }


def commit_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def data_plane(c: dict[str, str]) -> tuple[list[Band], list]:
    bands = [
        Band("1 · SOURCES — read-only, on disk", "source", [
            Node("a1", "Valve game data: VPK, dump files, FGD, .d.ts, game events, KV3"),
            Node("a2", "Source2Wiki pages")]),
        Band("2 · INGESTION — s2docs sync / load", "ingest", [
            Node("b1", "ingest/sources.py — parsers"), Node("b2", "ingest/wiki.py"),
            Node("b3", "ingest/loader.py")]),
        Band("3 · STORE — one SQLite file in S2DOCS_DATA_DIR", "store", [
            Node("c1", "content tables: convars, entities, schema, events, pulse, script symbols, docs"),
            Node("c2", "search_fts — FTS5"),
            Node("c3", "operational tables: builds, deploys, hosts, jobs, evidence, test runs, "
                      f"projects, assets, live — {c['migrations']} migrations")]),
        Band("4 · DOMAIN LAYER — shared code, no transport", "domain", [
            Node("d1", "db/repository.py + fts.py"),
            Node("d2", "domain stores — builds, deploys, hosts, jobs, evidence, assets"),
            Node("d3", "orchestration/engine.py + workflows.py"),
            Node("d4", "build/ + asset/ writers — model, material, validate")]),
        Band("5 · SURFACES — all read the same DB — "
             f"{c['commands']} CLI · {c['routes']} routes · {c['tools']} tools", "surface", [
            Node("e1", f"CLI — {c['commands']} commands"),
            Node("e2", f"FastAPI — {c['routes']} routes on :8812"),
            Node("e3", "Web UI — vanilla JS"),
            Node("e4", f"MCP stdio — {c['tools']} tools")]),
    ]
    edges = [("a1", "b1"), ("a2", "b2"), ("b1", "b3"), ("b2", "b3"), ("b3", "c1"), ("b3", "c2"),
             ("c1", "d1"), ("c2", "d1"), ("c3", "d2"),
             ("d1", "e1"), ("d1", "e2"), ("d1", "e4"),
             ("d2", "e1"), ("d2", "e2"), ("d2", "e4"),
             ("d3", "e2"), ("d4", "e1"), ("e2", "e3")]
    return bands, edges


def control_plane(c: dict[str, str]) -> tuple[list[Band], list]:
    bands = [
        Band("1 · WHO ASKS", "source", [Node("u", "User or agent")]),
        Band("2 · ANY SURFACE — "
             f"{c['commands']} CLI commands · {c['routes']} routes · {c['tools']} MCP tools", "surface", [
            Node("s", "CLI · HTTP :8812 · MCP tool")]),
        Band("3 · RECORD AND QUEUE — in-process", "exec", [
            Node("j", "jobs.py — queue, events, human-confirm verdict windows"),
            Node("p", "projects / steps + gates")]),
        Band("4 · EXECUTE ON A HOST — in-process, inside whichever surface started it", "exec", [
            Node("h", "hosts.py — registry, lease, heartbeat"),
            Node("b", "builds.py — ssh + resourcecompiler, log captured"),
            Node("d", "deploys.py — scp + MD5 readback")]),
        Band("5 · LIVE — drive a running game", "live", [
            Node("lv", "live/service.py — act() / status(), the one entry all three surfaces call"),
            Node("nc", "live/netcon.py + live/launch.py"),
            Node("cap", "live/capabilities.py — per-CS2-build proof gate")]),
        Band("6 · STATE + FAR SIDE", "store", [
            Node("db", "SQLite DB — the same file"), Node("w", "Windows host")]),
        Band("7 · COMPILER", "ingest", [Node("rc", "resourcecompiler → .vmdl_c, .vmat_c, .vpk")]),
        Band("8 · GAME", "domain", [Node("cs", "CS2 — netcon, interactive scheduled task, in-game frame")]),
        Band("9 · VERDICT", "surface", [Node("v", "verdict + evidence row")]),
    ]
    edges = [("u", "s"),
             ("s", "j"), ("s", "p"), ("s", "h"), ("s", "b"), ("s", "d"), ("s", "lv"),
             ("j", "db"), ("p", "db"), ("h", "db"), ("b", "db"), ("d", "db"), ("lv", "db"),
             ("h", "w", "ssh"), ("b", "w", "ssh"), ("d", "w", "scp"),
             ("lv", "nc"), ("cap", "lv", "gate"), ("nc", "cs", "netcon"),
             ("w", "rc"), ("rc", "cs"), ("cs", "v"), ("v", "j"), ("v", "db")]
    return bands, edges


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="docs/arch", help="directory to write the SVGs into")
    ap.add_argument("--suffix", default="", help="appended to each filename, e.g. 20261025-2257")
    ap.add_argument("--note", default="", help="second line under the title")
    args = ap.parse_args()

    c = counts()
    out = Path(args.out)
    if not out.is_absolute():
        out = REPO / out
    out.mkdir(parents=True, exist_ok=True)
    tail = f"-{args.suffix}" if args.suffix else ""
    sha = commit_sha()
    base_note = args.note or (f"commit {sha} · {c['commands']} CLI commands · {c['routes']} routes · "
                              f"{c['tools']} MCP tools · {c['migrations']} migrations")

    written = []
    for name, (bands, edges), title in (
        ("s2docs-data-plane", data_plane(c), "s2docs · A — data plane: what goes in, what comes out"),
        ("s2docs-control-plane", control_plane(c), "s2docs · B — control plane: what happens when it runs something"),
    ):
        svg = layout(bands, edges, title, base_note)
        path = out / f"{name}{tail}.svg"
        path.write_text(svg, encoding="utf-8")
        written.append(str(path.relative_to(REPO)))
        print(f"  wrote {path.relative_to(REPO)} ({len(svg)} bytes)")

    manifest = {"suffix": args.suffix, "commit": sha, "counts": c, "files": written}
    (out / f"manifest{tail}.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"  counts: {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
