from s2docs.db.fts import build_match_query, split_identifier
from s2docs.ingest import parsers as p
from tests.conftest import (
    COMMANDS,
    CONVARS,
    CORE_EVENTS,
    DTS,
    PULSE,
    SCHEMA_BASE,
    SCHEMA_ENUM,
    SERVER_FGD,
    STEAM_INF,
)


def test_convars_header_variants():
    by_name = {v.name: v for v in p.parse_convars(CONVARS, "convar")}
    rt = by_name["mp_roundtime"]
    assert (rt.default_value, rt.min_value, rt.max_value) == ("5", "0.1", "60")
    assert rt.flags == ["gamedll", "notify", "replicated", "release"]
    assert by_name["hostname"].default_value == ""
    assert by_name["sv_multiline"].description == "First line.\nSecond line."
    assert by_name["nodesc"].description is None


def test_commands_without_default():
    cmds = list(p.parse_convars(COMMANDS, "command"))
    assert [c.name for c in cmds] == ["bot_kick", "+lookatweapon"]
    assert cmds[0].default_value is None and cmds[0].flags == ["gamedll", "release"]


def test_fgd_entities_members_choices_components():
    ents = {e.classname: e for e in p.parse_fgd(SERVER_FGD, "server")}
    assert ents["root"].cpp_class == "CEntityInstance"
    assert ents["trigger_multiple"].bases == ["trigger"]
    base = ents["CBaseEntity"]
    movetype = next(m for m in base.members if m.name == "MoveType")
    assert movetype.choices == [("0", "MOVETYPE_NONE"), ("2", "MOVETYPE_WALK")]
    origin = next(m for m in base.members if m.name == "origin")
    assert origin.component == "CBodyComponent" and origin.type == "vector"
    getname = next(m for m in ents["root"].members if m.name == "GetName")
    assert getname.description == "Get the name of the entity."
    assert "Pulse node" in getname.notes


def test_schema_class_fields_defaults_metadata():
    cls = p.parse_schema_header(SCHEMA_BASE, "server")
    assert cls.name == "CBaseEntity" and cls.bases == ["CEntityInstance"]
    assert cls.metadata == ["MNetworkVarNames"]
    fields = {f.name: f for f in cls.fields}
    assert fields["m_iHealth"].metadata == ["MNetworkEnable"]
    assert fields["m_aThinkFunctions"].type == "CUtlVector< thinkfunc_t >"
    assert fields["m_flMax"].value == "1"
    assert fields["m_vecArr"].type == "Vector[3]"


def test_schema_nested_enum_name():
    enum = p.parse_schema_header(SCHEMA_ENUM, "server")
    assert enum.name == "CFuncMover::FollowStrategy_t"
    assert enum.underlying_type == "uint32_t"
    assert [(f.name, f.value) for f in enum.fields] == [("FOLLOW_FORWARD", "0"), ("FOLLOW_REVERSE", "1")]


def test_gameevents_comments_become_descriptions():
    events = {e.name: e for e in p.parse_gameevents(CORE_EVENTS, "core.gameevents")}
    assert events["player_death"].description == "a player died"
    assert events["player_death"].fields[0].description == "user ID who died"
    assert events["server_spawn"].fields[0].type == "string"


def test_pulse_bindings():
    b = list(p.parse_pulse_bindings(PULSE, "server"))
    assert [(x.library, x.kind, x.name) for x in b] == [
        ("CBaseTrigger_API", "input", "Enable"),
        ("CBaseTrigger_API", "output", "OnTrigger"),
    ]
    assert b[0].friendly_name == "Enable" and b[0].metadata == {"hidden_in_tool": True}


def test_dts_symbols():
    parsed = p.parse_dts(DTS)
    assert parsed.module == "cs_script/point_script"
    syms = {(s.parent, s.name): s for s in parsed.symbols}
    trace = syms[("Domain", "TraceLine")]
    assert trace.kind == "method" and "end: Vector" in trace.signature
    assert trace.doc.startswith("Trace a point")
    assert syms[("Domain", "OnReload")].deprecated
    assert syms[(None, "CSPlayerPawn")].extends == "Entity"
    assert syms[("CSRoundEndReason", "TERRORISTS_WIN")].kind == "enum_member"
    assert syms[("TraceResult", "hitEntity")].kind == "property"  # last member without ';'
    assert syms[(None, "Instance")].kind == "const"
    assert syms[(None, "Vector")].kind == "type"


def test_steam_inf():
    info = p.parse_steam_inf(STEAM_INF.format(build=42))
    assert info.build_number == 42 and info.patch_version == "1.0.42"


def test_split_identifier():
    assert split_identifier("m_flSimulationTime") == "m fl simulation time"
    assert split_identifier("OnPlayerJump") == "on player jump"
    assert split_identifier("mp_roundtime") == "mp roundtime"


def test_match_query_neutralises_fts_syntax():
    q = build_match_query('foo" OR NEAR(x) name:*')
    # Every token is quoted; no bare operators survive.
    assert q == '"foo"* AND "or"* AND "near"* AND "x"* AND "name"*'
    assert build_match_query("  !!  ") is None
