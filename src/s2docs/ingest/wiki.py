"""Ingest source2.wiki (Docusaurus, CC BY-SA 4.0) into the `docs` table.

The repo is cloned shallow + sparse (Markdown/MDX only, no images). MDX is
reduced to plain Markdown: imports/exports dropped, known components rendered
as text (`<Convar name="x"/>` -> `x`, `<Game name="cs2"/>` -> Counter-Strike 2),
unknown JSX tags stripped (inner text kept), code blocks untouched.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from s2docs.config import SOURCE2WIKI_LICENSE, SOURCE2WIKI_REPO, SOURCE2WIKI_SITE
from s2docs.ingest.sources import _git, source_commit

log = logging.getLogger(__name__)

SOURCE = "source2wiki"
KEY_PREFIX = "s2wiki:"
SPARSE = ["/docs/**/*.md", "/docs/**/*.mdx", "/LICENSE-CONTENT"]

GAME_NAMES = {
    "cs2": "Counter-Strike 2",
    "hla": "Half-Life: Alyx",
    "dota2": "Dota 2",
    "steamvr": "SteamVR",
    "deadlock": "Deadlock",
    "sbox": "s&box",
}
# Path segment -> game id for docs that are specific to one game.
PATH_GAMES = {"Counter-Strike 2": "cs2", "Deadlock": "deadlock"}

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_FENCE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]+`)", re.S)  # fenced blocks and inline code are kept verbatim
_ATTR = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|\{([^}]*)\})')
_SELF_CLOSING = re.compile(r"<([A-Z][A-Za-z0-9.]+)((?:\s+[^<>]*?)?)\s*/>", re.S)
_OPEN_CLOSE_TAG = re.compile(
    r"</?(?:[A-Z][A-Za-z0-9.]+|div|span|details|summary|img|a|p|sup|sub|center|iframe|video|source|figure|figcaption|kbd)"
    r"(?:\s+[^<>]*?)?\s*/?>",
    re.S,
)
_NUM_PREFIX = re.compile(r"^\d+-")


@dataclass(slots=True)
class WikiPage:
    key: str
    url: str
    title: str
    description: str | None
    game: str | None
    body: str
    source_path: str


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONT.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            meta[key.strip()] = value.strip().strip("'\"")
    return meta, text[m.end() :]


def _attrs(raw: str) -> dict[str, str]:
    return {m.group(1): next(g for g in m.groups()[1:] if g is not None) for m in _ATTR.finditer(raw)}


def _render_component(tag: str, attrs: dict[str, str]) -> str:
    name = attrs.get("name", "")
    label = attrs.get("label")
    if tag == "Game":
        return GAME_NAMES.get(name, name)
    if tag in ("Convar", "Entity"):
        return f"`{name}`" if name else ""
    if tag == "Tool":
        text = label or name
        link = attrs.get("link")
        return f"[{text}]({link})" if link else text
    if tag in ("CS", "Alyx", "Dota", "SteamVR"):
        return {"CS": "Counter-Strike 2", "Alyx": "Half-Life: Alyx", "Dota": "Dota 2", "SteamVR": "SteamVR"}[tag]
    return label or ""


def mdx_to_markdown(text: str) -> str:
    """Strip MDX-specific syntax; leave fenced code untouched."""
    parts = _FENCE.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # code fence
            out.append(part)
            continue
        lines = [ln for ln in part.splitlines(keepends=True) if not re.match(r"^\s*(import|export)\s", ln)]
        chunk = "".join(lines)
        chunk = _SELF_CLOSING.sub(lambda m: _render_component(m.group(1), _attrs(m.group(2) or "")), chunk)
        # Drop remaining JSX/HTML tags but keep their inner text. Keep <br> as newlines.
        chunk = re.sub(r"<br\s*/?>", "\n", chunk, flags=re.I)
        chunk = _OPEN_CLOSE_TAG.sub("", chunk)
        chunk = re.sub(r"\{/\*.*?\*/\}", "", chunk, flags=re.S)  # MDX comments
        out.append(chunk)
    text = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _url_path(rel: Path, slug: str | None) -> str:
    """Docusaurus routing: strip numeric prefixes and extensions; index files map to their folder."""
    parts = [_NUM_PREFIX.sub("", p) for p in rel.with_suffix("").parts]
    if parts and parts[-1].lower() in ("index", "readme"):
        parts = parts[:-1]
    if slug:
        if slug.startswith("/"):
            return slug
        parts = [*parts[:-1], slug]
    return "/" + "/".join(parts)


def parse_page(path: Path, docs_root: Path) -> WikiPage | None:
    rel = path.relative_to(docs_root)
    if any(p.startswith("_") for p in rel.parts):  # partials (_steamvr.mdx) are imported into other pages
        return None
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta, body = _front_matter(raw)
    if meta.get("draft") == "true" or meta.get("unlisted") == "true":
        return None
    md = mdx_to_markdown(body)
    title = meta.get("title")
    if not title:
        h1 = re.search(r"^#\s+(.+)$", md, re.M)
        title = h1.group(1).strip() if h1 else _NUM_PREFIX.sub("", rel.stem)
    url_path = _url_path(rel, meta.get("slug"))
    game = next((g for seg, g in PATH_GAMES.items() if seg in rel.parts), None)
    return WikiPage(
        key=KEY_PREFIX + url_path,
        url=SOURCE2WIKI_SITE + quote(url_path),
        title=title,
        description=meta.get("description"),
        game=game,
        body=md,
        source_path=str(rel),
    )


def sync_wiki_source(sources_dir: Path) -> Path:
    dest = sources_dir / "source2wiki"
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--depth", "1", "--filter=blob:none", "--no-checkout", SOURCE2WIKI_REPO, str(dest))
        _git("sparse-checkout", "set", "--no-cone", *SPARSE, cwd=dest)
        _git("checkout", cwd=dest)
    else:
        _git("sparse-checkout", "set", "--no-cone", *SPARSE, cwd=dest)
        _git("fetch", "--depth", "1", "origin", "HEAD", cwd=dest)
        _git("reset", "--hard", "FETCH_HEAD", cwd=dest)
    return dest


def load_wiki(conn: sqlite3.Connection, root: Path) -> dict[str, int]:
    """Replace all source2.wiki docs atomically. Returns counts."""
    from s2docs.db.fts import split_identifier
    from s2docs.ingest.loader import _ident  # local import: shared identifier indexing

    docs_root = root / "docs"
    pages = [pg for f in sorted(docs_root.rglob("*.md*")) if (pg := parse_page(f, docs_root))]
    commit = source_commit(root)
    known_games = {r[0] for r in conn.execute("SELECT id FROM games")}
    with conn:
        conn.execute("DELETE FROM search_fts WHERE kind = 'doc' AND ref_key LIKE ?", (KEY_PREFIX + "%",))
        conn.execute("DELETE FROM docs WHERE source = ?", (SOURCE,))
        for pg in pages:
            game = pg.game if pg.game in known_games else None
            conn.execute(
                "INSERT INTO docs (source, url, game_id, title, body, license, doc_key, source_path, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (url) DO NOTHING",
                (SOURCE, pg.url, game, pg.title, pg.body, SOURCE2WIKI_LICENSE, pg.key, pg.source_path, pg.description),
            )
            # Index code identifiers inside the page both whole and split.
            idents = " ".join(_ident(t) for t in set(re.findall(r"`([A-Za-z_][\w.]*)`", pg.body)))
            conn.execute(
                "INSERT INTO search_fts (name, name_parts, title, body, kind, game_id, build_id, ref_key) "
                "VALUES (?, ?, ?, ?, 'doc', ?, NULL, ?)",
                (
                    pg.title,
                    split_identifier(pg.title),
                    f"source2.wiki — {pg.source_path}",
                    "\n".join(x for x in (pg.description, pg.body, idents) if x),
                    game or "*",
                    pg.key,
                ),
            )
    log.info("source2.wiki: %d pages (commit %s)", len(pages), (commit or "")[:10])
    return {"docs": len(pages)}
