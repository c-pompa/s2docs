"""Phase A: Valve hand-written FGDs, Valve sample scripts, source2.wiki docs."""

from fastapi.testclient import TestClient

from s2docs.api.app import create_app
from s2docs.db.repository import NotFound, Repository
from s2docs.ingest.parsers import resolve_hammer_fgds
from s2docs.ingest.wiki import mdx_to_markdown
from tests.conftest import VALVE_BASE_FGD, VALVE_MOD_FGD

# ----------------------------------------------------------------- parser


def _resolve():
    files = {"csgo.fgd": VALVE_MOD_FGD, "base.fgd": VALVE_BASE_FGD}
    return resolve_hammer_fgds("csgo.fgd", files.get)


def test_hammer_fgd_include_exclude_override():
    classes = _resolve()
    assert "removed_thing" not in classes  # @exclude
    tm = classes["trigger_multiple"]
    assert tm.description == "A trigger volume that can be triggered multiple times.<br>Second line."  # '+' concat
    assert tm.bases == ["Targetname"]
    wait = next(m for m in tm.members if m.name == "wait")
    assert (wait.label, wait.default, wait.description) == ("Delay Before Reset", "2", "Overridden default.")
    assert tm.tool_name == "Trigger (multiple)"  # metadata from @OverrideClass
    flags = next(m for m in tm.members if m.name == "spawnflags")
    assert flags.choices == [("1", "Clients"), ("2", "NPCs")]


def test_hammer_fgd_metadata_meta_and_quoted_choices():
    thing = _resolve()["editor_only_thing"]
    assert (thing.tool_name, thing.tool_tip) == ("Editor Thing", "Only placed in Hammer")
    mode = thing.members[0]
    assert mode.meta == {"group": "Misc"} and mode.default == "1" and mode.choices == [("0", "Off"), ("1", "On")]
    target = _resolve()["Targetname"].members[0]
    assert target.label == "Name" and target.default is None and target.meta == {"sort_priority": "90"}


# ------------------------------------------------------------- repository


def test_entity_merges_valve_fgd(repo: Repository):
    e = repo.get_entity("cs2", "trigger_multiple")
    assert e.origin == "both" and e.fgd_file == "base.fgd" and e.tool_name == "Trigger (multiple)"
    assert e.description.startswith("A trigger volume")  # Valve text wins over the runtime dump
    assert e.fgd_inheritance == ["Targetname"]
    members = {(mm.kind, mm.name): mm for mm in e.members}
    wait = members[("keyvalue", "wait")]
    assert wait.source == "both" and wait.label == "Delay Before Reset" and wait.default_value == "2"
    assert members[("output", "OnTrigger")].description == "Fired whenever the trigger is activated."
    assert members[("keyvalue", "spawnflags")].source == "valve_fgd"  # editor-only key
    assert members[("input", "Kill")].source == "runtime"


def test_editor_only_entity(repo: Repository):
    e = repo.get_entity("cs2", "editor_only_thing")
    assert e.origin == "valve_fgd" and e.cpp_class is None and e.tool_name == "Editor Thing"
    assert {mm.name for mm in e.members} == {"mode", "targetname", "DoIt"}
    listed = {x.classname: x.origin for x in repo.list_entities("cs2", limit=200).items}
    assert listed["editor_only_thing"] == "valve_fgd" and listed["trigger_multiple"] == "both"
    assert [x.classname for x in repo.list_entities("cs2", io="DoIt").items] == ["editor_only_thing"]
    assert [x.classname for x in repo.list_entities("cs2", q="Editor Thing").items] == ["editor_only_thing"]
    hits = repo.search("placed in Hammer", game="cs2").items
    assert hits[0].key == "server:editor_only_thing"


def test_examples(repo: Repository):
    [ex] = repo.list_examples("cs2")
    assert ex.name == "hello.js" and ex.origin == "valve" and "OnPlayerJump" in ex.description
    assert "Instance.Msg" in repo.get_example("cs2", "hello").content
    assert repo.get_example("cs2", "cs_script/hello.js").name == "hello.js"
    assert repo.search("OnPlayerJump", kinds=["example"]).items[0].key == "cs_script/hello.js"


# ------------------------------------------------------------------- docs


def test_mdx_conversion():
    md = mdx_to_markdown(
        'import X from "y"\n<Game name="cs2"/> uses <Convar name="sv_cheats"/>, CUtlVector<T> and `a<b>`.\n'
        "<details><summary>More</summary>inner</details>\n```js\nconst a = <div/>;\n```"
    )
    assert md.startswith("Counter-Strike 2 uses `sv_cheats`, CUtlVector<T> and `a<b>`.")
    assert "Moreinner" in md and "const a = <div/>;" in md and "import" not in md


def test_docs_loaded_with_urls_and_game(repo: Repository):
    docs = {d.key: d for d in repo.list_docs().items}
    assert set(docs) == {"s2wiki:/Basics", "s2wiki:/Scripting/Counter-Strike 2/cs_script/introduction"}
    intro = repo.get_doc("s2wiki:/Scripting/Counter-Strike 2/cs_script/introduction")
    assert intro.game == "cs2" and intro.url.endswith("/Scripting/Counter-Strike%202/cs_script/introduction")
    assert "Counter-Strike 2 scripts use `sv_cheats`" in intro.body and intro.license.startswith("CC BY-SA")
    assert repo.get_doc(intro.url).key == intro.key


def test_docs_searchable_and_survive_game_reload(db, tmp_path):
    from s2docs.config import GAMES
    from s2docs.ingest.loader import load_build
    from tests.conftest import write_tree

    with db.reader() as conn:
        hits = Repository(conn).search("OnPlayerJump", game="cs2", kinds=["doc"]).items
        assert [h.key for h in hits] == ["s2wiki:/Scripting/Counter-Strike 2/cs_script/introduction"]
        # general docs ('*') appear under any game filter
        assert Repository(conn).search("basics", game="deadlock", kinds=["doc"]).total == 1
    with db.writer() as conn:
        load_build(conn, GAMES["cs2"], write_tree(tmp_path / "again"))
    with db.reader() as conn:
        assert Repository(conn).search("OnPlayerJump", kinds=["doc"]).total == 1


def test_docs_and_examples_api(db):
    with TestClient(create_app(db.path)) as c:
        assert c.get("/api/docs", params={"game": "cs2"}).json()["total"] == 2
        r = c.get("/api/doc", params={"key": "s2wiki:/Basics"})
        assert r.status_code == 200 and r.json()["title"] == "Basics"
        assert c.get("/api/doc", params={"key": "nope"}).status_code == 404
        assert c.get("/api/games/cs2/examples/hello.js").json()["origin"] == "valve"
        e = c.get("/api/games/cs2/entities/server/editor_only_thing").json()
        assert e["origin"] == "valve_fgd" and "sort_key" not in e["members"][0]


def test_not_found_doc(repo: Repository):
    try:
        repo.get_doc("s2wiki:/missing")
    except NotFound:
        pass
    else:
        raise AssertionError("expected NotFound")
