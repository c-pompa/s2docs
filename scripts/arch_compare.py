"""Put two dated diagram sets side by side, with the counted differences called out.

    uv run python scripts/arch_compare.py docs/arch/baseline-20261025-2257 docs/arch/after-20261026-0140

Writes an HTML page next to the newer set and prints the counts diff. The SVGs are inlined, so the
page is a single file that survives being copied anywhere.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LABELS = {"commands": "CLI commands", "routes": "HTTP routes", "tools": "MCP tools",
          "migrations": "migrations"}


def load(dirpath: Path) -> tuple[dict, dict[str, str]]:
    """Return the manifest and the {plane name: svg text} for one dated directory."""
    found = sorted(dirpath.glob("manifest*.json"))
    manifest = json.loads(found[-1].read_text(encoding="utf-8")) if found else {}
    svgs = {}
    for f in sorted(dirpath.glob("*.svg")):
        name = "data plane" if "data-plane" in f.name else "control plane"
        svgs[name] = f.read_text(encoding="utf-8")
    return manifest, svgs


def strip_preamble(svg: str) -> str:
    return svg.split("?>", 1)[-1] if svg.lstrip().startswith("<?xml") else svg


def diff_rows(a: dict, b: dict) -> list[tuple[str, str, str, str]]:
    rows = []
    for key, label in LABELS.items():
        before = str((a.get("counts") or {}).get(key, "?"))
        after = str((b.get("counts") or {}).get(key, "?"))
        if before == after:
            verdict = "same"
        elif before.isdigit() and after.isdigit():
            delta = int(after) - int(before)
            verdict = f"{'+' if delta > 0 else ''}{delta}"
        else:
            verdict = "changed"
        rows.append((label, before, after, verdict))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    before, after = Path(args.before), Path(args.after)
    if not before.is_absolute():
        before = REPO / before
    if not after.is_absolute():
        after = REPO / after
    ma, sa = load(before)
    mb, sb = load(after)
    rows = diff_rows(ma, mb)

    print(f"  before: {before.name}  commit {ma.get('commit', '?')}")
    print(f"  after:  {after.name}  commit {mb.get('commit', '?')}")
    for label, x, y, verdict in rows:
        print(f"    {label:<14} {x:>4} -> {y:<4} {verdict}")

    panels = []
    for plane in ("data plane", "control plane"):
        panels.append(f"<h2>{plane}</h2><div class='pair'>"
                      f"<figure><figcaption>BEFORE · {before.name}</figcaption>"
                      f"{strip_preamble(sa.get(plane, '<p>missing</p>'))}</figure>"
                      f"<figure><figcaption>AFTER · {after.name}</figcaption>"
                      f"{strip_preamble(sb.get(plane, '<p>missing</p>'))}</figure></div>")
    table = "".join(f"<tr><td>{label}</td><td>{x}</td><td>{y}</td><td class='v'>{v}</td></tr>"
                    for label, x, y, v in rows)

    html = f"""<!doctype html><meta charset="utf-8">
<style>
 body {{ margin:0; padding:18px; font:14px ui-sans-serif,-apple-system,"Segoe UI",Helvetica,Arial;
         color:var(--foreground,#1f2328); }}
 h1 {{ font-size:18px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:22px 0 8px; }}
 .meta {{ color:var(--muted-foreground,#57606a); font-size:13px; margin-bottom:14px; }}
 table {{ border-collapse:collapse; font-size:14px; }}
 td,th {{ border:1px solid var(--border,#d8dee4); padding:5px 12px; text-align:left; }}
 th {{ background:var(--card,#fff); }} td.v {{ font-weight:600; }}
 .pair {{ display:flex; gap:16px; align-items:flex-start; overflow-x:auto; }}
 figure {{ margin:0; flex:0 0 auto; }}
 figcaption {{ font-size:13px; color:var(--muted-foreground,#57606a); margin-bottom:6px; }}
 figure svg {{ border:1px solid var(--border,#d8dee4); border-radius:10px; background:#fff;
               width:900px; height:auto; }}
</style>
<h1>s2docs architecture, before vs after</h1>
<div class="meta">before {ma.get('commit','?')} ({before.name}) &nbsp;·&nbsp;
after {mb.get('commit','?')} ({after.name}) &nbsp;·&nbsp; same layout, so a new box or a moved arrow is the diff</div>
<table><tr><th>surface</th><th>before</th><th>after</th><th>delta</th></tr>{table}</table>
{''.join(panels)}
"""
    out = Path(args.out) if args.out else (after / f"compare-{before.name}-vs-{after.name}.html")
    out.write_text(html, encoding="utf-8")
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
