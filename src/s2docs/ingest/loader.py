"""Load a GameTracking checkout into SQLite as one build, atomically.

The whole build is written in a single transaction: readers see either the
previous build or the complete new one, never a partial state. Re-ingesting
the same build number replaces it (idempotent).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from s2docs.config import Game
from s2docs.db.fts import split_identifier
from s2docs.ingest import parsers as p
from s2docs.ingest.sources import source_commit

log = logging.getLogger(__name__)

# core < game < mod: later files override earlier event definitions.
_EVENT_PRECEDENCE = {"core.gameevents": 0, "game.gameevents": 1, "mod.gameevents": 2}
DEFAULT_KEEP_BUILDS = 5


@dataclass
class LoadStats:
    game: str
    build_number: int
    counts: dict[str, int] = field(default_factory=dict)
    replaced: bool = False

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n


def _ident(name: str) -> str:
    """Index an identifier both whole and split, so 'health' finds m_iHealth and 'trigger' finds OnTrigger."""
    parts = split_identifier(name)
    return f"{name} {parts}" if parts and parts != name.lower() else name


class _FtsWriter:
    """Buffers search rows and flushes with executemany."""

    def __init__(self, conn: sqlite3.Connection, game_id: str, build_id: int):
        self.conn, self.game_id, self.build_id = conn, game_id, build_id
        self.rows: list[tuple] = []

    def add(
        self,
        kind: str,
        ref_key: str,
        name: str,
        title: str,
        body: Iterable[str | None] | str | None,
    ) -> None:
        if body is None:
            text = ""
        elif isinstance(body, str):
            text = body
        else:
            text = "\n".join(b for b in body if b)
        self.rows.append(
            (
                name,
                split_identifier(name),
                title,
                text,
                kind,
                self.game_id,
                self.build_id,
                ref_key,
            )
        )
        if len(self.rows) >= 5000:
            self.flush()

    def flush(self) -> None:
        if self.rows:
            self.conn.executemany(
                "INSERT INTO search_fts (name, name_parts, title, body, kind, game_id, build_id, ref_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                self.rows,
            )
            self.rows.clear()


def load_build(
    conn: sqlite3.Connection,
    game: Game,
    root: Path,
    *,
    keep_builds: int = DEFAULT_KEEP_BUILDS,
) -> LoadStats:
    dump = root / "DumpSource2"
    if not dump.is_dir():
        raise FileNotFoundError(f"{dump} not found; is {root} a GameTracking checkout?")
    info = p.parse_steam_inf((root / "game" / game.mod_dir / "steam.inf").read_text(encoding="utf-8"))
    stats = LoadStats(game=game.id, build_number=info.build_number)

    with conn:  # one transaction for the whole build
        conn.execute(
            "INSERT INTO games (id, name, app_id, mod_dir, source_repo) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET name = excluded.name, app_id = excluded.app_id, "
            "mod_dir = excluded.mod_dir, source_repo = excluded.source_repo",
            (game.id, game.name, game.app_id, game.mod_dir, game.repo),
        )
        old = conn.execute(
            "SELECT id FROM builds WHERE game_id = ? AND build_number = ?",
            (game.id, info.build_number),
        ).fetchone()
        if old:
            stats.replaced = True
            conn.execute("DELETE FROM search_fts WHERE build_id = ?", (old["id"],))
            conn.execute("DELETE FROM builds WHERE id = ?", (old["id"],))

        build_id = conn.execute(
            "INSERT INTO builds (game_id, build_number, patch_version, source_revision, version_date, source_commit) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (
                game.id,
                info.build_number,
                info.patch_version,
                info.source_revision,
                info.version_date,
                source_commit(root),
            ),
        ).fetchone()[0]

        # Search index only covers the current build of each game.
        conn.execute("DELETE FROM search_fts WHERE game_id = ?", (game.id,))
        fts = _FtsWriter(conn, game.id, build_id)

        _load_convars(conn, build_id, dump, fts, stats)
        _load_entities(conn, build_id, dump, fts, stats)
        _load_schemas(conn, build_id, dump, fts, stats)
        _load_events(conn, build_id, root, game, fts, stats)
        _load_pulse(conn, build_id, dump, fts, stats)
        if game.script_api == "cs_script":
            _load_cs_script(conn, build_id, root, fts, stats)
        fts.flush()

        conn.execute("UPDATE builds SET is_current = 0 WHERE game_id = ?", (game.id,))
        conn.execute("UPDATE builds SET is_current = 1 WHERE id = ?", (build_id,))
        _prune_builds(conn, game.id, keep_builds)

    log.info("loaded %s build %s: %s", game.id, info.build_number, stats.counts)
    return stats


def _prune_builds(conn: sqlite3.Connection, game_id: str, keep: int) -> None:
    conn.execute(
        "DELETE FROM builds WHERE game_id = ? AND is_current = 0 AND id NOT IN ("
        "  SELECT id FROM builds WHERE game_id = ? ORDER BY build_number DESC LIMIT ?)",
        (game_id, game_id, max(keep, 1)),
    )


def _load_convars(conn, build_id, dump: Path, fts: _FtsWriter, stats: LoadStats) -> None:
    for fname, kind in (("convars.txt", "convar"), ("commands.txt", "command")):
        path = dump / fname
        if not path.exists():
            continue
        for v in p.parse_convars(path.read_text(encoding="utf-8", errors="replace"), kind):
            cur = conn.execute(
                "INSERT INTO convars (build_id, kind, name, default_value, min_value, max_value, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING id",
                (
                    build_id,
                    kind,
                    v.name,
                    v.default_value,
                    v.min_value,
                    v.max_value,
                    v.description,
                ),
            ).fetchone()
            if cur is None:  # duplicate name in dump; first wins
                continue
            conn.executemany(
                "INSERT OR IGNORE INTO convar_flags (convar_id, flag) VALUES (?, ?)",
                [(cur[0], f) for f in v.flags],
            )
            fts.add(kind, v.name, v.name, kind, [v.description, " ".join(v.flags)])
            stats.bump(kind)


def _load_entities(conn, build_id, dump: Path, fts: _FtsWriter, stats: LoadStats) -> None:
    for side in ("server", "client"):
        path = dump / "entities" / f"{side}.fgd"
        if not path.exists():
            continue
        for ent in p.parse_fgd(path.read_text(encoding="utf-8", errors="replace"), side):
            row = conn.execute(
                "INSERT INTO entities (build_id, side, classname, class_type, cpp_class, description) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING id",
                (
                    build_id,
                    side,
                    ent.classname,
                    ent.class_type,
                    ent.cpp_class,
                    ent.description,
                ),
            ).fetchone()
            if row is None:
                continue
            eid = row[0]
            conn.executemany(
                "INSERT INTO entity_bases (entity_id, ordinal, base_name) VALUES (?, ?, ?)",
                [(eid, i, b) for i, b in enumerate(ent.bases)],
            )
            conn.executemany(
                "INSERT INTO entity_members "
                "(entity_id, ordinal, kind, name, type, description, notes, component, choices) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        eid,
                        i,
                        m.kind,
                        m.name,
                        m.type,
                        m.description,
                        m.notes,
                        m.component,
                        json.dumps(m.choices) if m.choices else None,
                    )
                    for i, m in enumerate(ent.members)
                ],
            )
            io = " ".join(f"{m.kind} {_ident(m.name)}" for m in ent.members if m.kind != "keyvalue")
            kvs = " ".join(_ident(m.name) for m in ent.members if m.kind == "keyvalue")
            descs = [m.description for m in ent.members if m.description]
            fts.add(
                "entity",
                f"{side}:{ent.classname}",
                ent.classname,
                f"{side} entity",
                [ent.description, ent.cpp_class, " ".join(ent.bases), io, kvs, *descs],
            )
            stats.bump(f"entity_{side}")
            stats.bump("entity_members", len(ent.members))


def _load_schemas(conn, build_id, dump: Path, fts: _FtsWriter, stats: LoadStats) -> None:
    schema_dir = dump / "schemas"
    if not schema_dir.is_dir():
        return
    for header in sorted(schema_dir.rglob("*.h")):
        module = header.parent.name
        cls = p.parse_schema_header(header.read_text(encoding="utf-8", errors="replace"), module)
        if cls is None:
            continue
        row = conn.execute(
            "INSERT INTO schema_classes (build_id, module, name, kind, underlying_type, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING id",
            (
                build_id,
                module,
                cls.name,
                cls.kind,
                cls.underlying_type,
                json.dumps(cls.metadata),
            ),
        ).fetchone()
        if row is None:
            continue
        cid = row[0]
        conn.executemany(
            "INSERT INTO schema_class_bases (class_id, ordinal, base_name) VALUES (?, ?, ?)",
            [(cid, i, b) for i, b in enumerate(cls.bases)],
        )
        conn.executemany(
            "INSERT INTO schema_fields (class_id, ordinal, name, type, value, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            [(cid, i, f.name, f.type, f.value, json.dumps(f.metadata)) for i, f in enumerate(cls.fields)],
        )
        kind = "schema_enum" if cls.kind == "enum" else "schema_class"
        friendly = [m for f in cls.fields for m in f.metadata if "FriendlyName" in m or "Description" in m]
        fts.add(
            kind,
            f"{module}:{cls.name}",
            cls.name,
            f"{module} {cls.kind}",
            [
                " ".join(cls.bases),
                " ".join(_ident(f.name) for f in cls.fields),
                *cls.metadata,
                *friendly,
            ],
        )
        stats.bump(kind)
        stats.bump("schema_fields", len(cls.fields))


def _load_events(conn, build_id, root: Path, game: Game, fts: _FtsWriter, stats: LoadStats) -> None:
    files = sorted((root / "game").glob("*/pak01_dir/resource/*.gameevents"))
    effective: dict[str, tuple[int, p.GameEvent, str]] = {}  # name -> (precedence, event, file)
    for path in files:
        prec = _EVENT_PRECEDENCE.get(path.name, 1)
        for ev in p.parse_gameevents(path.read_text(encoding="utf-8", errors="replace"), path.name):
            row = conn.execute(
                "INSERT INTO game_events (build_id, source_file, precedence, name, description) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING id",
                (build_id, path.name, prec, ev.name, ev.description),
            ).fetchone()
            if row is None:
                continue
            conn.executemany(
                "INSERT INTO game_event_fields (event_id, ordinal, name, type, description) VALUES (?, ?, ?, ?, ?)",
                [(row[0], i, f.name, f.type, f.description) for i, f in enumerate(ev.fields)],
            )
            if ev.name not in effective or prec > effective[ev.name][0]:
                effective[ev.name] = (prec, ev, path.name)
            stats.bump("event_definitions")
    # Index only the effective (highest precedence) definition of each event.
    for _, ev, fname in effective.values():
        fts.add(
            "event",
            ev.name,
            ev.name,
            f"game event ({fname})",
            [ev.description, *(f"{f.name} {f.type} {f.description or ''}" for f in ev.fields)],
        )
        stats.bump("event")


def _load_pulse(conn, build_id, dump: Path, fts: _FtsWriter, stats: LoadStats) -> None:
    for path in sorted((dump / "module_metadata").glob("*/pulse_bindings.txt")):
        module = path.parent.name
        for b in p.parse_pulse_bindings(path.read_text(encoding="utf-8", errors="replace"), module):
            pid = conn.execute(
                "INSERT INTO pulse_bindings (build_id, module, library, kind, name, signature, friendly_name, "
                "description, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    build_id,
                    module,
                    b.library,
                    b.kind,
                    b.name,
                    b.signature,
                    b.friendly_name,
                    b.description,
                    json.dumps(b.metadata),
                ),
            ).fetchone()[0]
            fts.add(
                "pulse",
                str(pid),
                b.name,
                f"pulse {b.kind} {b.library}",
                [b.friendly_name, b.description, b.signature],
            )
            stats.bump("pulse")


_CS_SCRIPT_DIR = Path("content/csgo_addons/cs_script_demo/maps/scripts")


def _load_cs_script(conn, build_id, root: Path, fts: _FtsWriter, stats: LoadStats) -> None:
    script_dir = root / _CS_SCRIPT_DIR
    dts_path = script_dir / "point_script.d.ts"
    if not dts_path.exists():
        log.warning("cs_script declarations not found at %s", dts_path)
        return
    text = dts_path.read_text(encoding="utf-8")
    for extra in ("point_script.d.ts", "tsconfig.json"):
        f = script_dir / extra
        if f.exists():
            conn.execute(
                "INSERT INTO raw_files (build_id, path, content) VALUES (?, ?, ?)",
                (build_id, f"cs_script/{extra}", f.read_text(encoding="utf-8")),
            )
    parsed = p.parse_dts(text)
    for s in parsed.symbols:
        conn.execute(
            "INSERT INTO script_symbols (build_id, api, module, parent, name, kind, signature, doc, extends, "
            "deprecated, ordinal) VALUES (?, 'cs_script', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                build_id,
                s.module,
                s.parent,
                s.name,
                s.kind,
                s.signature,
                s.doc,
                s.extends,
                int(s.deprecated),
                s.ordinal,
            ),
        )
        qualified = f"{s.parent}.{s.name}" if s.parent else s.name
        if s.ordinal == 0:
            fts.add(
                "script",
                qualified,
                s.name,
                f"cs_script {s.kind} {qualified}",
                [s.signature, s.doc],
            )
        stats.bump("script_symbols")
