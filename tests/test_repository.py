import pytest

from s2docs.config import GAMES
from s2docs.db.repository import NotFound, Repository
from s2docs.ingest.loader import load_build
from tests.conftest import write_tree


def test_search_exact_and_split_identifier(repo: Repository):
    hits = repo.search("mp_roundtime", game="cs2").items
    assert hits[0].kind == "convar" and hits[0].key == "mp_roundtime"
    assert any(h.key == "mp_roundtime" for h in repo.search("roundtime").items)
    assert any(h.key == "server:CBaseEntity" for h in repo.search("health").items)


def test_search_ranks_pulse_after_entities(repo: Repository):
    hits = repo.search("OnTrigger", game="cs2").items
    kinds = [h.kind for h in hits]
    assert kinds.index("entity") < kinds.index("pulse")


def test_search_kind_filter_and_validation(repo: Repository):
    assert {h.kind for h in repo.search("player", kinds=["event"]).items} == {"event"}
    with pytest.raises(ValueError):
        repo.search("x", kinds=["bogus"])


def test_events_indexed_once_with_override(repo: Repository):
    assert [h.key for h in repo.search("player_death", kinds=["event"]).items] == ["player_death"]
    ev = repo.get_event("cs2", "player_death")
    assert ev.source_file == "mod.gameevents" and ev.overrides == ["core.gameevents"]
    assert [f.name for f in ev.fields] == ["userid", "attacker", "headshot"]


def test_convar_filters(repo: Repository):
    assert repo.get_convar("cs2", "MP_ROUNDTIME").name == "mp_roundtime"  # NOCASE
    assert [c.name for c in repo.list_convars("cs2", flag="cheat").items] == ["sv_multiline"]
    # '_' in user input is literal, not a LIKE wildcard
    assert repo.list_convars("cs2", q="p_r").total == 1
    assert repo.list_convars("cs2", kind="command").total == 2


def test_entity_inheritance_and_override(repo: Repository):
    ent = repo.get_entity("cs2", "trigger_multiple")
    assert ent.inheritance == ["trigger", "CBaseEntity", "root"]
    members = {(mm.kind, mm.name): mm for mm in ent.members}
    assert members[("output", "OnTrigger")].defined_in == "trigger_multiple"
    assert members[("input", "Kill")].defined_in == "trigger"  # nearest definition wins
    assert members[("keyvalue", "targetname")].defined_in == "CBaseEntity"
    own = repo.get_entity("cs2", "trigger_multiple", inherited=False)
    assert {mm.defined_in for mm in own.members} == {"trigger_multiple"}


def test_find_entities_by_io(repo: Repository):
    names = [e.classname for e in repo.list_entities("cs2", io="ontrigger", include_base=True).items]
    assert names == ["trigger_multiple"]


def test_schema_inheritance(repo: Repository):
    cls = repo.get_schema_class("cs2", "CCSPlayerPawn")
    assert cls.bases == ["CBaseEntity"] and cls.inheritance == ["CBaseEntity"]
    assert [f.name for f in cls.fields][:2] == ["m_nCharacterDefIndex", "m_iHealth"]
    assert repo.get_schema_class("cs2", "CBaseEntity").derived == ["CCSPlayerPawn"]
    assert repo.list_schema("cs2", field="m_iHealth").items[0].name == "CBaseEntity"
    enum = repo.get_schema_class("cs2", "CFuncMover::FollowStrategy_t")
    assert enum.kind == "enum" and enum.fields[1].value == "1"


def test_script_api(repo: Repository):
    pawn = repo.get_script_type("cs2", "CSPlayerPawn")
    assert pawn.inherited_from == ["Entity"]
    assert [mm.name for mm in pawn.members] == ["GetHealth", "GetEntityName"]
    assert repo.find_script_symbols("cs2", "Domain.TraceLine")[0].kind == "method"
    assert repo.find_script_symbols("cs2", "TraceLine")[0].qualified_name == "Domain.TraceLine"
    assert "declare module" in repo.raw_file("cs2", "cs_script/point_script.d.ts")


def test_not_found_and_unknown_build(repo: Repository):
    with pytest.raises(NotFound):
        repo.get_convar("cs2", "nope")
    with pytest.raises(NotFound):
        repo.build_id("deadlock")


def test_reload_same_build_is_idempotent(db, tmp_path):
    tree = write_tree(tmp_path / "src")
    with db.writer() as conn:
        stats = load_build(conn, GAMES["cs2"], tree)
        assert stats.replaced
        assert conn.execute("SELECT count(*) FROM builds").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM convars").fetchone()[0] == 7
        # Old FTS rows were replaced, not duplicated.
        assert conn.execute("SELECT count(*) FROM search_fts WHERE name = 'mp_roundtime'").fetchone()[0] == 1


def test_second_build_diff_and_prune(db, tmp_path):
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "b2", build=1001, extra_convar="sv_new"))
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "b3", build=1002, extra_convar="sv_new"), keep_builds=2)
        builds = [r[0] for r in conn.execute("SELECT build_number FROM builds ORDER BY build_number")]
        assert builds == [1001, 1002]  # 1000 pruned
        # Search index holds the current build only.
        assert conn.execute("SELECT count(DISTINCT build_id) FROM search_fts").fetchone()[0] == 1
    with db.reader() as conn:
        repo = Repository(conn)
        assert repo.games()[0].current_build == 1002
        assert repo.diff_builds("cs2").sections["convars"] == []
        diff = repo.diff_builds("cs2", 1001, 1002)
        assert diff.from_build == 1001
    # Diff against a build without sv_new
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "b4", build=1003))
    with db.reader() as conn:
        added = Repository(conn).diff_builds("cs2").sections["convars"]
        assert [(e.name, e.change) for e in added] == [("convar:sv_new", "removed")]


# ------------------------------------------------------------------- field-level diff
def test_diff_is_field_level_and_grouped(db, tmp_path):
    """One added schema field is exactly one row (member-scoped), grouped under its class."""
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "f1", build=2001))
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "f2", build=2002, pawn_extra_field="float32 m_flNew;"))
    with db.reader() as conn:
        diff = Repository(conn).diff_builds("cs2", 2001, 2002)
    rows = diff.sections["schema"]
    assert [(e.name, e.change, e.group) for e in rows] == [("CCSPlayerPawn.m_flNew", "added", "server:CCSPlayerPawn")]
    # the value is the member's own type, not a flattened dump of the whole class
    assert rows[0].after == "float32" and rows[0].before is None
    assert diff.totals["schema"] == 1 and diff.limit == 500


def test_diff_reports_removed_field_with_full_name(db, tmp_path):
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "r1", build=2101, pawn_extra_field="float32 m_flGone;"))
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "r2", build=2102))
    with db.reader() as conn:
        rows = Repository(conn).diff_builds("cs2", 2101, 2102).sections["schema"]
    assert [(e.name, e.change) for e in rows] == [("CCSPlayerPawn.m_flGone", "removed")]
    assert rows[0].before == "float32" and rows[0].after is None and rows[0].group == "server:CCSPlayerPawn"


def test_diff_convar_description_change_is_changed(db, tmp_path):
    """A description edit on an existing convar is one `changed` row, not added+removed."""
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "c1", build=2201))
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "c2", build=2202, roundtime_desc="New round time blurb."))
    with db.reader() as conn:
        rows = Repository(conn).diff_builds("cs2", 2201, 2202).sections["convars"]
    assert [(e.name, e.change) for e in rows] == [("convar:mp_roundtime", "changed")]
    assert rows[0].before == "5 How many minutes each round takes." and rows[0].after == "5 New round time blurb."


def test_diff_covers_pulse_and_hammer_sections(db, tmp_path):
    """Sections indexed but previously absent from the diff (pulse, hammer) are covered."""
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "p1", build=2301))
        load_build(
            conn,
            GAMES["cs2"],
            write_tree(
                tmp_path / "p2",
                build=2302,
                pulse_extra='input Disable(PVAL_EHANDLE:trigger _Target "trigger");',
            ),
        )
    with db.reader() as conn:
        diff = Repository(conn).diff_builds("cs2", 2301, 2302)
    assert {"pulse", "hammer_classes"} <= set(diff.sections)
    adds = [(e.name, e.change, e.group) for e in diff.sections["pulse"] if e.change == "added"]
    assert adds == [("CBaseTrigger_API.Disable (input)", "added", "server:CBaseTrigger_API")]


def test_diff_script_api_ignores_ordinal_reorder(db, tmp_path):
    """A bare symbol reorder (ordinal shift) is not a change: identity is api/parent/name/kind."""
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "s1", build=2401))
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "s2", build=2402))
        conn.execute(
            "UPDATE script_symbols SET ordinal = ordinal + 100 "
            "WHERE build_id = (SELECT id FROM builds WHERE build_number = 2402)"
        )
        conn.commit()
    with db.reader() as conn:
        assert Repository(conn).diff_builds("cs2", 2401, 2402).sections["script_api"] == []


def test_diff_script_api_reports_signature_change(db, tmp_path):
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "g1", build=2501))
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "g2", build=2502))
        conn.execute(
            "UPDATE script_symbols SET signature = 'GetHealth(): string' "
            "WHERE name = 'GetHealth' AND build_id = (SELECT id FROM builds WHERE build_number = 2502)"
        )
        conn.commit()
    with db.reader() as conn:
        rows = Repository(conn).diff_builds("cs2", 2501, 2502).sections["script_api"]
    assert [(e.name, e.change) for e in rows] == [("cs_script:CSPlayerPawn.GetHealth", "changed")]
    assert rows[0].before == "GetHealth(): number" and rows[0].after == "GetHealth(): string"
