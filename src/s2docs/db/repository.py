"""Read-side query layer. All SQL used by the API and MCP lives here.

Rules:
- Every value is a bound parameter. The only dynamic SQL fragments are
  placeholder lists (`?, ?, ?`) and whitelisted column names.
- Each method resolves a game + build to a `build_id` first; the default is
  the game's current build.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from s2docs import models as m
from s2docs.config import GAMES
from s2docs.db.connection import schema_version
from s2docs.db.fts import build_match_query

MAX_LIMIT = 200


class NotFound(LookupError):
    pass


def _clamp(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)


def _like(q: str) -> str:
    """Substring LIKE pattern with user wildcards escaped (use with ESCAPE '\\')."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _placeholders(values: Sequence[object]) -> str:
    return ", ".join("?" for _ in values)


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------ games
    def games(self) -> list[m.GameInfo]:
        rows = self.conn.execute(
            "SELECT g.id, g.name, g.app_id, g.mod_dir, b.build_number, b.patch_version, b.version_date "
            "FROM games g LEFT JOIN builds b ON b.game_id = g.id AND b.is_current = 1 ORDER BY g.id"
        ).fetchall()
        return [
            m.GameInfo(
                id=r["id"],
                name=r["name"],
                app_id=r["app_id"],
                mod_dir=r["mod_dir"],
                current_build=r["build_number"],
                patch_version=r["patch_version"],
                version_date=r["version_date"],
                script_api=GAMES[r["id"]].script_api if r["id"] in GAMES else None,
            )
            for r in rows
        ]

    def builds(self, game: str) -> list[m.BuildInfo]:
        rows = self.conn.execute(
            "SELECT build_number, patch_version, source_revision, version_date, source_commit, ingested_at, "
            "is_current FROM builds WHERE game_id = ? ORDER BY build_number DESC",
            (game,),
        ).fetchall()
        return [m.BuildInfo(**{**dict(r), "is_current": bool(r["is_current"])}) for r in rows]

    def build_id(self, game: str, build: int | None = None) -> int:
        if build is None:
            row = self.conn.execute("SELECT id FROM builds WHERE game_id = ? AND is_current = 1", (game,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id FROM builds WHERE game_id = ? AND build_number = ?",
                (game, build),
            ).fetchone()
        if row is None:
            what = f"build {build}" if build else "any ingested build"
            raise NotFound(f"game {game!r} has no {what}; run `s2docs sync --game {game}`")
        return row["id"]

    # ----------------------------------------------------------------- search
    def search(
        self,
        query: str,
        *,
        game: str | None = None,
        kinds: Sequence[str] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> m.Page[m.SearchHit]:
        limit, offset = _clamp(limit, offset)
        match = build_match_query(query)
        if match is None:
            return m.Page(items=[], total=0, limit=limit, offset=offset)

        where = ["search_fts MATCH ?"]
        params: list[object] = [match]
        if game:
            # '*' marks general docs that apply to every game.
            where.append("(game_id = ? OR game_id = '*')")
            params.append(game)
        if kinds:
            bad = set(kinds) - set(m.SEARCH_KINDS)
            if bad:
                raise ValueError(f"unknown kinds: {sorted(bad)}")
            where.append(f"kind IN ({_placeholders(kinds)})")
            params.extend(kinds)
        where_sql = " AND ".join(where)

        total = self.conn.execute(f"SELECT count(*) FROM search_fts WHERE {where_sql}", params).fetchone()[0]
        rows = self.conn.execute(
            f"""
            SELECT kind, game_id, ref_key, name, title,
                   snippet(search_fts, 3, '**', '**', '…', 16) AS snippet,
                   bm25(search_fts, 10.0, 6.0, 2.0, 1.0) AS score
            FROM search_fts
            WHERE {where_sql}
            -- Exact identifier hits first. Pulse bindings mirror entity I/O names (one per module),
            -- so they go after other kinds unless the caller asked for kind=pulse.
            ORDER BY (lower(name) = lower(?) AND kind != 'pulse') DESC, kind = 'pulse', score
            LIMIT ? OFFSET ?
            """,
            [*params, query.strip(), limit, offset],
        ).fetchall()
        items = [
            m.SearchHit(
                kind=r["kind"],
                game=r["game_id"],
                key=r["ref_key"],
                name=r["name"],
                title=r["title"],
                snippet=r["snippet"] or None,
                score=round(-r["score"], 4),
            )
            for r in rows
        ]
        return m.Page(items=items, total=total, limit=limit, offset=offset)

    # ---------------------------------------------------------------- convars
    def _convar_from_row(self, r: sqlite3.Row) -> m.ConVar:
        flags = r["flags"].split(" ") if r["flags"] else []
        return m.ConVar(
            name=r["name"],
            kind=r["kind"],
            default_value=r["default_value"],
            min_value=r["min_value"],
            max_value=r["max_value"],
            flags=flags,
            description=r["description"],
        )

    _CONVAR_SELECT = (
        "SELECT c.name, c.kind, c.default_value, c.min_value, c.max_value, c.description, "
        "(SELECT group_concat(flag, ' ') FROM convar_flags f WHERE f.convar_id = c.id) AS flags "
        "FROM convars c"
    )

    def list_convars(
        self,
        game: str,
        *,
        q: str | None = None,
        kind: str | None = None,
        flag: str | None = None,
        build: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> m.Page[m.ConVar]:
        limit, offset = _clamp(limit, offset)
        where: list[str] = ["c.build_id = ?"]
        params: list[object] = [self.build_id(game, build)]
        if kind:
            where.append("c.kind = ?")
            params.append(kind)
        if q:
            where.append("(c.name LIKE ? ESCAPE '\\' OR c.description LIKE ? ESCAPE '\\')")
            params += [_like(q), _like(q)]
        if flag:
            where.append("EXISTS (SELECT 1 FROM convar_flags f WHERE f.convar_id = c.id AND f.flag = ?)")
            params.append(flag)
        where_sql = " AND ".join(where)
        total = self.conn.execute(f"SELECT count(*) FROM convars c WHERE {where_sql}", params).fetchone()[0]
        order = "ORDER BY (c.name LIKE ? ESCAPE '\\') DESC, c.name" if q else "ORDER BY c.name"
        order_params = [q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"] if q else []
        rows = self.conn.execute(
            f"{self._CONVAR_SELECT} WHERE {where_sql} {order} LIMIT ? OFFSET ?",
            [*params, *order_params, limit, offset],
        ).fetchall()
        return m.Page(
            items=[self._convar_from_row(r) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    def get_convar(self, game: str, name: str, *, build: int | None = None) -> m.ConVar:
        row = self.conn.execute(
            f"{self._CONVAR_SELECT} WHERE c.build_id = ? AND c.name = ? ORDER BY c.kind DESC LIMIT 1",
            (self.build_id(game, build), name),
        ).fetchone()
        if row is None:
            raise NotFound(f"no convar/command named {name!r} in {game}")
        return self._convar_from_row(row)

    def convar_names(self, game: str) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT name FROM convars WHERE build_id = ?", (self.build_id(game),))]

    def convar_flags(self, game: str) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT f.flag, count(*) AS n FROM convar_flags f JOIN convars c ON c.id = f.convar_id "
            "WHERE c.build_id = ? GROUP BY f.flag ORDER BY n DESC",
            (self.build_id(game),),
        ).fetchall()
        return [(r["flag"], r["n"]) for r in rows]

    # --------------------------------------------------------------- entities
    # Two sources are merged:
    #   runtime   — DumpSource2 FGD: what the game binary actually registers (authoritative names/types)
    #   valve_fgd — Valve's hand-written Hammer FGD: labels, help text, defaults, choices, editor-only keys
    # Server entities may exist only in Valve's FGD (editor/compile-time classes).

    _ENTITY_UNION = """
        WITH ents AS (
            SELECT e.classname, e.side, e.class_type, e.cpp_class,
                   coalesce(h.description, e.description) AS description,
                   h.tool_name, e.id AS eid, h.id AS hid,
                   CASE WHEN h.id IS NULL THEN 'runtime' ELSE 'both' END AS origin
            FROM entities e
            LEFT JOIN hammer_classes h
              ON h.build_id = e.build_id AND h.root = 'hammer' AND h.classname = e.classname AND e.side = 'server'
            WHERE e.build_id = :b
            UNION ALL
            SELECT h.classname, 'server', h.class_type, NULL, h.description, h.tool_name, NULL, h.id, 'valve_fgd'
            FROM hammer_classes h
            WHERE h.build_id = :b AND h.root = 'hammer'
              AND NOT EXISTS (SELECT 1 FROM entities e
                              WHERE e.build_id = h.build_id AND e.side = 'server' AND e.classname = h.classname)
        )
    """
    _ENTITY_BASES = """
        coalesce(
          (SELECT group_concat(base_name, ',') FROM
             (SELECT base_name FROM entity_bases b WHERE b.entity_id = ents.eid ORDER BY ordinal)),
          (SELECT group_concat(base_name, ',') FROM
             (SELECT base_name FROM hammer_class_bases b WHERE b.class_id = ents.hid ORDER BY ordinal))
        ) AS bases
    """

    def list_entities(
        self,
        game: str,
        *,
        q: str | None = None,
        side: str | None = "server",
        io: str | None = None,
        include_base: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> m.Page[m.EntitySummary]:
        """List entities. `io` filters to entities that *declare* an input/output with that name."""
        limit, offset = _clamp(limit, offset)
        where: list[str] = []
        params: dict[str, object] = {"b": self.build_id(game)}
        if side:
            where.append("side = :side")
            params["side"] = side
        if not include_base:
            where.append("class_type NOT IN ('BaseClass', 'OverrideClass')")
        if q:
            where.append(
                "(classname LIKE :q ESCAPE '\\' OR cpp_class LIKE :q ESCAPE '\\' OR tool_name LIKE :q ESCAPE '\\')"
            )
            params["q"] = _like(q)
        if io:
            where.append(
                "(EXISTS (SELECT 1 FROM entity_members em WHERE em.entity_id = ents.eid "
                "         AND em.kind IN ('input', 'output') AND em.name = :io COLLATE NOCASE)"
                " OR EXISTS (SELECT 1 FROM hammer_members hm WHERE hm.class_id = ents.hid "
                "         AND hm.kind IN ('input', 'output') AND hm.name = :io COLLATE NOCASE))"
            )
            params["io"] = io
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.conn.execute(f"{self._ENTITY_UNION} SELECT count(*) FROM ents {where_sql}", params).fetchone()[0]
        rows = self.conn.execute(
            f"{self._ENTITY_UNION} SELECT ents.*, {self._ENTITY_BASES} FROM ents {where_sql} "
            "ORDER BY classname, side LIMIT :limit OFFSET :offset",
            {**params, "limit": limit, "offset": offset},
        ).fetchall()
        return m.Page(items=[self._entity_summary(r) for r in rows], total=total, limit=limit, offset=offset)

    @staticmethod
    def _entity_summary(r: sqlite3.Row) -> m.EntitySummary:
        return m.EntitySummary(
            classname=r["classname"],
            side=r["side"],
            class_type=r["class_type"],
            cpp_class=r["cpp_class"],
            description=r["description"],
            bases=r["bases"].split(",") if r["bases"] else [],
            tool_name=r["tool_name"],
            origin=r["origin"],
        )

    def _runtime_members(self, bid: int, side: str, root_id: int, classname: str, max_depth: int):
        return self.conn.execute(
            """
            WITH RECURSIVE chain(id, classname, depth, path) AS (
                SELECT ?, ?, 0, ',' || ? || ','
                UNION ALL
                SELECT e.id, e.classname, c.depth + 1, c.path || e.id || ','
                FROM chain c
                JOIN entity_bases b ON b.entity_id = c.id
                JOIN entities e ON e.build_id = ? AND e.side = ? AND e.classname = b.base_name
                WHERE c.depth < ? AND instr(c.path, ',' || e.id || ',') = 0
            ),
            nearest AS (SELECT id, classname, min(depth) AS depth FROM chain GROUP BY id),
            ranked AS (
                SELECT em.*, n.classname AS defined_in, n.depth,
                       row_number() OVER (PARTITION BY em.kind, em.name ORDER BY n.depth, em.ordinal) AS rn
                FROM nearest n JOIN entity_members em ON em.entity_id = n.id
            )
            SELECT * FROM ranked WHERE rn = 1
            """,
            (root_id, classname, root_id, bid, side, max_depth),
        ).fetchall()

    def _hammer_chain(self, bid: int, classname: str, max_depth: int) -> tuple[list[str], list[sqlite3.Row]]:
        """Valve FGD inheritance chain (nearest first) and its members, nearest definition winning."""
        chain = self.conn.execute(
            """
            WITH RECURSIVE chain(id, classname, depth, path) AS (
                SELECT id, classname, 0, ',' || id || ',' FROM hammer_classes
                WHERE build_id = ? AND root = 'hammer' AND classname = ?
                UNION ALL
                SELECT h.id, h.classname, c.depth + 1, c.path || h.id || ','
                FROM chain c
                JOIN hammer_class_bases b ON b.class_id = c.id
                JOIN hammer_classes h ON h.build_id = ? AND h.root = 'hammer' AND h.classname = b.base_name
                WHERE c.depth < ? AND instr(c.path, ',' || h.id || ',') = 0
            )
            SELECT id, classname, min(depth) AS depth FROM chain GROUP BY id ORDER BY depth, classname
            """,
            (bid, classname, bid, max_depth),
        ).fetchall()
        if not chain:
            return [], []
        ids = [r["id"] for r in chain]
        depth = {r["id"]: r["depth"] for r in chain}
        owner = {r["id"]: r["classname"] for r in chain}
        rows = self.conn.execute(
            f"SELECT * FROM hammer_members WHERE class_id IN ({_placeholders(ids)})", ids
        ).fetchall()
        best: dict[tuple[str, str], tuple[tuple[int, int], sqlite3.Row, str]] = {}
        for r in rows:
            key = (r["kind"], r["name"].lower())
            rank = (depth[r["class_id"]], r["ordinal"])
            if key not in best or rank < best[key][0]:
                best[key] = (rank, r, owner[r["class_id"]])
        ordered = sorted(best.values(), key=lambda x: x[0])
        merged = [dict(r) | {"defined_in": cls} for _, r, cls in ordered]
        return [r["classname"] for r in chain[1:]], merged  # type: ignore[return-value]

    def get_entity(self, game: str, classname: str, *, side: str = "server", inherited: bool = True) -> m.Entity:
        bid = self.build_id(game)
        max_depth = 64 if inherited else 0
        root = self.conn.execute(
            """SELECT e.*, (SELECT group_concat(base_name, ',') FROM
                   (SELECT base_name FROM entity_bases b WHERE b.entity_id = e.id ORDER BY ordinal)) AS bases
               FROM entities e WHERE e.build_id = ? AND e.side = ? AND e.classname = ?""",
            (bid, side, classname),
        ).fetchone()
        hclass = None
        if side == "server":
            hclass = self.conn.execute(
                """SELECT h.*, (SELECT group_concat(base_name, ',') FROM
                       (SELECT base_name FROM hammer_class_bases b WHERE b.class_id = h.id ORDER BY ordinal)) AS bases
                   FROM hammer_classes h WHERE h.build_id = ? AND h.root = 'hammer' AND h.classname = ?""",
                (bid, classname),
            ).fetchone()
        if root is None and hclass is None:
            raise NotFound(f"no {side} entity {classname!r} in {game}")

        fgd_inheritance, fgd_members = self._hammer_chain(bid, classname, max_depth) if hclass else ([], [])
        fgd_by_key = {(r["kind"], r["name"].lower()): r for r in fgd_members}

        members: list[m.EntityMember] = []
        runtime_keys: set[tuple[str, str]] = set()
        runtime_rows = self._runtime_members(bid, side, root["id"], root["classname"], max_depth) if root else []
        for r in runtime_rows:
            key = (r["kind"], r["name"].lower())
            runtime_keys.add(key)
            v = fgd_by_key.get(key)
            choices = json.loads(r["choices"]) if r["choices"] else None
            if v is not None and v["choices"]:
                choices = json.loads(v["choices"])
            members.append(
                m.EntityMember(
                    kind=r["kind"],
                    name=r["name"],
                    type=r["type"],
                    label=v["label"] if v else None,
                    default_value=v["default_value"] if v else None,
                    description=(v["description"] if v and v["description"] else r["description"]),
                    notes=r["notes"],
                    component=r["component"],
                    choices=choices,
                    defined_in=r["defined_in"],
                    source="both" if v else "runtime",
                    sort_key=(r["depth"], r["ordinal"]),
                )
            )
        for v in fgd_members:
            if (v["kind"], v["name"].lower()) in runtime_keys:
                continue
            members.append(
                m.EntityMember(
                    kind=v["kind"],
                    name=v["name"],
                    type=v["type"],
                    label=v["label"],
                    default_value=v["default_value"],
                    description=v["description"],
                    choices=json.loads(v["choices"]) if v["choices"] else None,
                    defined_in=v["defined_in"],
                    source="valve_fgd",
                    sort_key=(99, v["ordinal"]),
                )
            )
        kind_order = {"keyvalue": 0, "input": 1, "output": 2}
        members.sort(key=lambda x: (kind_order[x.kind], x.sort_key))

        runtime_chain = []
        if root is not None:
            runtime_chain = [
                r["classname"]
                for r in self.conn.execute(
                    """
                    WITH RECURSIVE chain(id, classname, depth) AS (
                        SELECT ?, ?, 0
                        UNION
                        SELECT e.id, e.classname, c.depth + 1 FROM chain c
                        JOIN entity_bases b ON b.entity_id = c.id
                        JOIN entities e ON e.build_id = ? AND e.side = ? AND e.classname = b.base_name
                        WHERE c.depth < 64
                    )
                    SELECT classname, min(depth) AS d FROM chain WHERE depth > 0
                    GROUP BY classname ORDER BY d, classname
                    """,
                    (root["id"], root["classname"], bid, side),
                )
            ]

        src = root if root is not None else hclass
        return m.Entity(
            classname=src["classname"],
            side=side,
            class_type=(hclass["class_type"] if hclass else root["class_type"]),
            cpp_class=root["cpp_class"] if root else None,
            description=(hclass["description"] if hclass and hclass["description"] else src["description"]),
            bases=(src["bases"] or "").split(",") if src["bases"] else [],
            tool_name=hclass["tool_name"] if hclass else None,
            tool_tip=hclass["tool_tip"] if hclass else None,
            tool_group=hclass["tool_group"] if hclass else None,
            fgd_file=hclass["source_file"] if hclass else None,
            origin="both" if (root and hclass) else ("runtime" if root else "valve_fgd"),
            inheritance=runtime_chain,
            fgd_inheritance=fgd_inheritance,
            members=members,
        )

    # ----------------------------------------------------------------- schema
    def list_schema(
        self,
        game: str,
        *,
        q: str | None = None,
        module: str | None = None,
        kind: str | None = None,
        field: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> m.Page[m.SchemaClassSummary]:
        limit, offset = _clamp(limit, offset)
        where: list[str] = ["s.build_id = ?"]
        params: list[object] = [self.build_id(game)]
        if q:
            where.append("s.name LIKE ? ESCAPE '\\'")
            params.append(_like(q))
        if module:
            where.append("s.module = ?")
            params.append(module)
        if kind:
            where.append("s.kind = ?")
            params.append(kind)
        if field:
            where.append("EXISTS (SELECT 1 FROM schema_fields f WHERE f.class_id = s.id AND f.name = ?)")
            params.append(field)
        where_sql = " AND ".join(where)
        total = self.conn.execute(f"SELECT count(*) FROM schema_classes s WHERE {where_sql}", params).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT s.*,
                  (SELECT group_concat(base_name, ',') FROM
                     (SELECT base_name FROM schema_class_bases b WHERE b.class_id = s.id ORDER BY ordinal)) AS bases,
                  (SELECT count(*) FROM schema_fields f WHERE f.class_id = s.id) AS field_count
                FROM schema_classes s WHERE {where_sql} ORDER BY s.name, s.module LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
        items = [
            m.SchemaClassSummary(
                name=r["name"],
                module=r["module"],
                kind=r["kind"],
                underlying_type=r["underlying_type"],
                bases=r["bases"].split(",") if r["bases"] else [],
                field_count=r["field_count"],
            )
            for r in rows
        ]
        return m.Page(items=items, total=total, limit=limit, offset=offset)

    def schema_modules(self, game: str) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT module, count(*) AS n FROM schema_classes WHERE build_id = ? GROUP BY module ORDER BY module",
            (self.build_id(game),),
        ).fetchall()
        return [(r["module"], r["n"]) for r in rows]

    def get_schema_class(
        self, game: str, name: str, *, module: str | None = None, inherited: bool = True
    ) -> m.SchemaClass:
        bid = self.build_id(game)
        root = self.conn.execute(
            """SELECT * FROM schema_classes WHERE build_id = ? AND name = ? AND (? IS NULL OR module = ?)
               ORDER BY CASE module WHEN 'server' THEN 0 WHEN 'client' THEN 1 ELSE 2 END, module LIMIT 1""",
            (bid, name, module, module),
        ).fetchone()
        if root is None:
            raise NotFound(f"no schema class/enum {name!r} in {game}" + (f" module {module}" if module else ""))

        # Bases resolve by name, preferring the same module as the child.
        rows = self.conn.execute(
            """
            WITH RECURSIVE chain(id, name, module, depth) AS (
                SELECT ?, ?, ?, 0
                UNION
                SELECT s.id, s.name, s.module, c.depth + 1
                FROM chain c
                JOIN schema_class_bases b ON b.class_id = c.id
                JOIN schema_classes s ON s.id = (
                    SELECT id FROM schema_classes
                    WHERE build_id = ? AND name = b.base_name
                    ORDER BY module = c.module DESC, module LIMIT 1)
                WHERE c.depth < ?
            )
            SELECT id, name, min(depth) AS depth FROM chain GROUP BY id ORDER BY depth
            """,
            (root["id"], root["name"], root["module"], bid, 64 if inherited else 0),
        ).fetchall()
        ids = [r["id"] for r in rows]
        owner = {r["id"]: r["name"] for r in rows}
        depth = {r["id"]: r["depth"] for r in rows}
        frows = self.conn.execute(
            f"SELECT * FROM schema_fields WHERE class_id IN ({_placeholders(ids)}) ORDER BY class_id, ordinal",
            ids,
        ).fetchall()
        frows = sorted(frows, key=lambda r: (depth[r["class_id"]], r["ordinal"]))
        fields = [
            m.SchemaField(
                name=r["name"],
                type=r["type"],
                value=r["value"],
                metadata=json.loads(r["metadata"]),
                defined_in=owner[r["class_id"]],
            )
            for r in frows
        ]
        bases = [
            r["base_name"]
            for r in self.conn.execute(
                "SELECT base_name FROM schema_class_bases WHERE class_id = ? ORDER BY ordinal",
                (root["id"],),
            )
        ]
        derived = [
            r["name"]
            for r in self.conn.execute(
                """SELECT DISTINCT s.name FROM schema_class_bases b JOIN schema_classes s ON s.id = b.class_id
                   WHERE s.build_id = ? AND b.base_name = ? ORDER BY s.name""",
                (bid, root["name"]),
            )
        ]
        return m.SchemaClass(
            name=root["name"],
            module=root["module"],
            kind=root["kind"],
            underlying_type=root["underlying_type"],
            bases=bases,
            field_count=sum(1 for f in fields if f.defined_in == root["name"]),
            metadata=json.loads(root["metadata"]),
            inheritance=[r["name"] for r in rows[1:]],
            derived=derived,
            fields=fields,
        )

    # ----------------------------------------------------------------- events
    def list_events(self, game: str, *, q: str | None = None, limit: int = 100, offset: int = 0) -> m.Page[m.GameEvent]:
        limit, offset = _clamp(limit, offset)
        where: list[str] = ["e.build_id = ?"]
        params: list[object] = [self.build_id(game)]
        if q:
            where.append("(e.name LIKE ? ESCAPE '\\' OR e.description LIKE ? ESCAPE '\\')")
            params += [_like(q), _like(q)]
        where_sql = " AND ".join(where)
        # Effective definition per name = highest precedence file.
        base = f"""
            SELECT * FROM (
              SELECT e.*, row_number() OVER (PARTITION BY e.name ORDER BY e.precedence DESC) AS rn
              FROM game_events e WHERE {where_sql}) WHERE rn = 1"""
        total = self.conn.execute(f"SELECT count(*) FROM ({base})", params).fetchone()[0]
        rows = self.conn.execute(f"{base} ORDER BY name LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        return m.Page(
            items=[self._event(r) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    def _event(self, r: sqlite3.Row, overrides: list[str] | None = None) -> m.GameEvent:
        fields = [
            m.GameEventField(name=f["name"], type=f["type"], description=f["description"])
            for f in self.conn.execute(
                "SELECT name, type, description FROM game_event_fields WHERE event_id = ? ORDER BY ordinal",
                (r["id"],),
            )
        ]
        return m.GameEvent(
            name=r["name"],
            source_file=r["source_file"],
            description=r["description"],
            fields=fields,
            overrides=overrides or [],
        )

    def get_event(self, game: str, name: str) -> m.GameEvent:
        rows = self.conn.execute(
            "SELECT * FROM game_events WHERE build_id = ? AND name = ? ORDER BY precedence DESC",
            (self.build_id(game), name),
        ).fetchall()
        if not rows:
            raise NotFound(f"no game event {name!r} in {game}")
        return self._event(rows[0], overrides=[r["source_file"] for r in rows[1:]])

    # ------------------------------------------------------------- script api
    def _script_symbol(self, r: sqlite3.Row) -> m.ScriptSymbol:
        return m.ScriptSymbol(
            api=r["api"],
            name=r["name"],
            qualified_name=f"{r['parent']}.{r['name']}" if r["parent"] else r["name"],
            parent=r["parent"],
            kind=r["kind"],
            signature=r["signature"],
            doc=r["doc"],
            extends=r["extends"],
            deprecated=bool(r["deprecated"]),
        )

    def list_script_types(self, game: str, *, api: str = "cs_script") -> list[m.ScriptSymbol]:
        rows = self.conn.execute(
            """SELECT * FROM script_symbols WHERE build_id = ? AND api = ? AND parent IS NULL
               ORDER BY CASE kind WHEN 'const' THEN 0 WHEN 'class' THEN 1 WHEN 'interface' THEN 2
                                  WHEN 'enum' THEN 3 ELSE 4 END, name""",
            (self.build_id(game), api),
        ).fetchall()
        return [self._script_symbol(r) for r in rows]

    def get_script_type(self, game: str, name: str, *, api: str = "cs_script", inherited: bool = True) -> m.ScriptType:
        bid = self.build_id(game)
        row = self.conn.execute(
            "SELECT * FROM script_symbols WHERE build_id = ? AND api = ? AND parent IS NULL AND name = ? "
            "ORDER BY ordinal LIMIT 1",
            (bid, api, name),
        ).fetchone()
        if row is None:
            raise NotFound(f"no {api} type {name!r} in {game}")
        chain, seen, cur = [], {name}, row["extends"]
        while inherited and cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            nxt = self.conn.execute(
                "SELECT extends FROM script_symbols WHERE build_id = ? AND api = ? AND parent IS NULL AND name = ?",
                (bid, api, cur),
            ).fetchone()
            cur = nxt["extends"] if nxt else None
        owners = [name, *chain]
        members = self.conn.execute(
            f"""SELECT * FROM script_symbols WHERE build_id = ? AND api = ? AND parent IN ({_placeholders(owners)})
                ORDER BY id""",
            [bid, api, *owners],
        ).fetchall()
        order = {o: i for i, o in enumerate(owners)}
        members = sorted(members, key=lambda r: (order[r["parent"]], r["id"]))
        base = self._script_symbol(row)
        return m.ScriptType(
            **base.model_dump(),
            members=[self._script_symbol(r) for r in members],
            inherited_from=chain,
        )

    def find_script_symbols(
        self, game: str, name: str, *, api: str = "cs_script", limit: int = 50
    ) -> list[m.ScriptSymbol]:
        """Resolve 'Domain.TraceLine', 'TraceLine' or a partial name."""
        bid = self.build_id(game)
        parent, _, member = name.rpartition(".")
        if parent:
            rows = self.conn.execute(
                "SELECT * FROM script_symbols WHERE build_id = ? AND api = ? AND parent = ? AND name = ? "
                "ORDER BY ordinal",
                (bid, api, parent, member),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM script_symbols WHERE build_id = ? AND api = ? AND (name = ? OR name LIKE ? ESCAPE '\\')
                   ORDER BY name = ? DESC, parent IS NOT NULL, name LIMIT ?""",
                (bid, api, member, _like(member), member, _clamp(limit, 0)[0]),
            ).fetchall()
        return [self._script_symbol(r) for r in rows]

    def raw_file(self, game: str, path: str) -> str:
        row = self.conn.execute(
            "SELECT content FROM raw_files WHERE build_id = ? AND path = ?",
            (self.build_id(game), path),
        ).fetchone()
        if row is None:
            raise NotFound(f"no raw file {path!r} for {game}")
        return row["content"]

    # --------------------------------------------------------------- examples
    def _example(self, r: sqlite3.Row, with_content: bool) -> m.Example:
        return m.Example(
            api=r["api"],
            name=r["name"],
            path=r["path"],
            origin=r["origin"],
            description=r["description"],
            content=r["content"] if with_content else None,
        )

    def list_examples(self, game: str, *, api: str | None = None) -> list[m.Example]:
        rows = self.conn.execute(
            "SELECT * FROM examples WHERE build_id = ? AND (? IS NULL OR api = ?) ORDER BY api, name",
            (self.build_id(game), api, api),
        ).fetchall()
        return [self._example(r, False) for r in rows]

    def get_example(self, game: str, name: str, *, api: str = "cs_script") -> m.Example:
        name = name.removeprefix(f"{api}/")
        row = self.conn.execute(
            "SELECT * FROM examples WHERE build_id = ? AND api = ? AND (name = ? OR name = ? || '.js')",
            (self.build_id(game), api, name, name),
        ).fetchone()
        if row is None:
            raise NotFound(f"no {api} example {name!r} in {game}")
        return self._example(row, True)

    # ------------------------------------------------------------------- docs
    def _doc(self, r: sqlite3.Row, with_body: bool) -> m.Doc:
        return m.Doc(
            key=r["doc_key"],
            source=r["source"],
            title=r["title"],
            url=r["url"],
            game=r["game_id"],
            description=r["description"],
            license=r["license"],
            body=r["body"] if with_body else None,
        )

    def list_docs(
        self, *, game: str | None = None, q: str | None = None, limit: int = 50, offset: int = 0
    ) -> m.Page[m.Doc]:
        limit, offset = _clamp(limit, offset)
        where: list[str] = ["doc_key IS NOT NULL"]
        params: list[object] = []
        if game:
            where.append("(game_id = ? OR game_id IS NULL)")
            params.append(game)
        if q:
            where.append("(title LIKE ? ESCAPE '\\' OR source_path LIKE ? ESCAPE '\\')")
            params += [_like(q), _like(q)]
        where_sql = " AND ".join(where)
        total = self.conn.execute(f"SELECT count(*) FROM docs WHERE {where_sql}", params).fetchone()[0]
        rows = self.conn.execute(
            f"SELECT * FROM docs WHERE {where_sql} ORDER BY source_path LIMIT ? OFFSET ?", [*params, limit, offset]
        ).fetchall()
        return m.Page(items=[self._doc(r, False) for r in rows], total=total, limit=limit, offset=offset)

    def get_doc(self, key: str) -> m.Doc:
        row = self.conn.execute("SELECT * FROM docs WHERE doc_key = ? OR url = ?", (key, key)).fetchone()
        if row is None:
            raise NotFound(f"no doc {key!r}")
        return self._doc(row, True)

    # ------------------------------------------------------------------ pulse
    def list_pulse(
        self,
        game: str,
        *,
        q: str | None = None,
        library: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> m.Page[m.PulseBinding]:
        limit, offset = _clamp(limit, offset)
        where: list[str] = ["build_id = ?"]
        params: list[object] = [self.build_id(game)]
        if q:
            where.append("(name LIKE ? ESCAPE '\\' OR library LIKE ? ESCAPE '\\' OR friendly_name LIKE ? ESCAPE '\\')")
            params += [_like(q)] * 3
        if library:
            where.append("library = ?")
            params.append(library)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        where_sql = " AND ".join(where)
        total = self.conn.execute(f"SELECT count(*) FROM pulse_bindings WHERE {where_sql}", params).fetchone()[0]
        rows = self.conn.execute(
            f"SELECT * FROM pulse_bindings WHERE {where_sql} ORDER BY library, name, module LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        items = [
            m.PulseBinding(
                id=r["id"],
                module=r["module"],
                library=r["library"],
                kind=r["kind"],
                name=r["name"],
                signature=r["signature"],
                friendly_name=r["friendly_name"],
                description=r["description"],
                metadata=json.loads(r["metadata"]),
            )
            for r in rows
        ]
        return m.Page(items=items, total=total, limit=limit, offset=offset)

    def get_pulse(self, game: str, binding_id: int) -> m.PulseBinding:
        row = self.conn.execute(
            "SELECT * FROM pulse_bindings WHERE id = ? AND build_id = ?",
            (binding_id, self.build_id(game)),
        ).fetchone()
        if row is None:
            raise NotFound(f"no pulse binding {binding_id} in {game}")
        return m.PulseBinding(**{**dict(row), "metadata": json.loads(row["metadata"])})

    # ------------------------------------------------------------------- diff
    def diff_builds(
        self,
        game: str,
        from_build: int | None = None,
        to_build: int | None = None,
        *,
        limit: int = 500,
    ) -> m.BuildDiff:
        to_id = self.build_id(game, to_build)
        to_num = self.conn.execute("SELECT build_number FROM builds WHERE id = ?", (to_id,)).fetchone()[0]
        if from_build is None:
            row = self.conn.execute(
                "SELECT id, build_number FROM builds WHERE game_id = ? AND build_number < ? "
                "ORDER BY build_number DESC LIMIT 1",
                (game, to_num),
            ).fetchone()
            if row is None:
                raise NotFound(f"{game} has only one ingested build ({to_num}); nothing to diff yet")
            from_id, from_num = row["id"], row["build_number"]
        else:
            from_id, from_num = self.build_id(game, from_build), from_build

        def keyed(sql: str) -> list[m.DiffEntry]:
            """`sql` selects (key, value) for build ?; compare the two builds."""
            out: list[m.DiffEntry] = []
            q = f"""
                WITH a AS ({sql.replace("?", ":a")}), b AS ({sql.replace("?", ":b")})
                SELECT b.k AS name, 'added' AS change, NULL AS before, b.v AS after
                  FROM b LEFT JOIN a ON a.k = b.k WHERE a.k IS NULL
                UNION ALL
                SELECT a.k, 'removed', a.v, NULL FROM a LEFT JOIN b ON a.k = b.k WHERE b.k IS NULL
                UNION ALL
                SELECT a.k, 'changed', a.v, b.v FROM a JOIN b ON a.k = b.k WHERE a.v IS NOT b.v
                ORDER BY 2, 1 LIMIT :lim
            """
            for r in self.conn.execute(q, {"a": from_id, "b": to_id, "lim": limit}):
                out.append(m.DiffEntry(name=r[0], change=r[1], before=r[2], after=r[3]))
            return out

        sections = {
            "convars": keyed(
                "SELECT kind || ':' || name AS k, coalesce(default_value, '') || ' ' || coalesce(description, '') AS v "
                "FROM convars WHERE build_id = ?"
            ),
            "entities": keyed(
                "SELECT e.side || ':' || e.classname AS k, (SELECT group_concat(kind || ' ' || name, ', ') FROM "
                "(SELECT kind, name FROM entity_members WHERE entity_id = e.id ORDER BY kind, name)) AS v "
                "FROM entities e WHERE e.build_id = ?"
            ),
            "schema": keyed(
                "SELECT s.module || ':' || s.name AS k, (SELECT group_concat(name || ' ' || coalesce(type, value, ''), "
                "'; ') FROM (SELECT name, type, value FROM schema_fields WHERE class_id = s.id ORDER BY ordinal)) AS v "
                "FROM schema_classes s WHERE s.build_id = ?"
            ),
            "events": keyed(
                "SELECT e.source_file || ':' || e.name AS k, (SELECT group_concat(name || ' ' || type, ', ') FROM "
                "(SELECT name, type FROM game_event_fields WHERE event_id = e.id ORDER BY ordinal)) AS v "
                "FROM game_events e WHERE e.build_id = ?"
            ),
            "script_api": keyed(
                "SELECT api || ':' || coalesce(parent || '.', '') || name || '#' || ordinal AS k, signature AS v "
                "FROM script_symbols WHERE build_id = ?"
            ),
        }
        return m.BuildDiff(game=game, from_build=from_num, to_build=to_num, sections=sections)

    # ------------------------------------------------------------------ stats
    def stats(self) -> m.Stats:
        counts: dict[str, dict[str, int]] = {}
        queries = {
            "convars": "SELECT count(*) FROM convars WHERE build_id = ? AND kind = 'convar'",
            "commands": "SELECT count(*) FROM convars WHERE build_id = ? AND kind = 'command'",
            "entities": "SELECT count(*) FROM entities WHERE build_id = ?",
            "schema_classes": "SELECT count(*) FROM schema_classes WHERE build_id = ? AND kind = 'class'",
            "schema_enums": "SELECT count(*) FROM schema_classes WHERE build_id = ? AND kind = 'enum'",
            "game_events": "SELECT count(DISTINCT name) FROM game_events WHERE build_id = ?",
            "script_symbols": "SELECT count(*) FROM script_symbols WHERE build_id = ?",
            "pulse_bindings": "SELECT count(*) FROM pulse_bindings WHERE build_id = ?",
            "valve_fgd_classes": "SELECT count(*) FROM hammer_classes WHERE build_id = ? AND root = 'hammer'",
            "examples": "SELECT count(*) FROM examples WHERE build_id = ?",
        }
        games = self.games()
        for g in games:
            if g.current_build is None:
                continue
            bid = self.build_id(g.id)
            counts[g.id] = {k: self.conn.execute(sql, (bid,)).fetchone()[0] for k, sql in queries.items()}
        counts["docs"] = {
            r["source"]: r["n"] for r in self.conn.execute("SELECT source, count(*) AS n FROM docs GROUP BY source")
        }
        return m.Stats(schema_version=schema_version(self.conn), games=games, counts=counts)
