import json

import pytest
from fastapi.testclient import TestClient

from s2docs import hosts
from s2docs.api.app import create_app
from s2docs.db.connection import Database
from s2docs.live import recipes, service
from s2docs.live.formation import positions
from s2docs.live.intent import IntentEnvelope


class FakeClient:
    def __init__(self, replies):
        self.replies, self.sent = replies, []

    def run(self, lines, **_):
        self.sent.append(list(lines))
        return "\n".join(self.replies.get(line, "") for line in lines)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(d, "pomps-pc", "pomps-pc", shell="cmd")
    return d


def bots_env():
    return IntentEnvelope.model_validate({"intents": [{"action": "bots", "team_size": 5, "me": "t"}]})


# Real `status` player rows from S0 (pomps-pc, CS2 1.41.9.2): human, empty slots, bots.
HUMAN = "   0    00:37    0    0     active 786432 'Water Eater'"
EMPTY = "65535 [NoChan]    0    0 challenging      0 ''"
ROWS9 = "\n".join([HUMAN, EMPTY] + [f"  {10 + i}      BOT    0    0     active      0 'Bot{i}'" for i in range(9)])


def test_netcon_address_is_registry_data(db):
    with pytest.raises(service.LiveError, match="no netcon address"):
        service.client_for(db, "pomps-pc")
    service.set_netcon(db, "pomps-pc", "10.88.20.235:2121")
    c = service.client_for(db, "pomps-pc")
    assert (c.host, c.port) == ("10.88.20.235", 2121)
    with pytest.raises(service.LiveError, match="host:port"):
        service.set_netcon(db, "pomps-pc", "10.88.20.235;rm")


def test_bots_verified_by_status_bot_rows(db):
    client = FakeClient({"hs_ping": "", "status": ROWS9})
    act = service.act(db, "pomps-pc", bots_env(), source="cli", text="5v5 me on T", client=client)
    assert act.status == "verified" and act.lines.count("bot_add_ct") == 5
    assert client.sent[-1] == ["sleep 3", "status"]  # the readback is its own round trip
    assert service.list_actions(db, "pomps-pc")[0].id == act.id


def test_bot_count_mismatch_is_failed_not_verified(db):
    client = FakeClient({"hs_ping": "", "status": "  10      BOT    0    0     active      0 'Only'",
                         "bot_add_t": "BOT Colin joined"})
    act = service.act(db, "pomps-pc", bots_env(), source="cli", client=client)
    assert act.status == "failed" and "expected 9 bots" in act.detail


def test_sandbox_spawn_refused_when_sandbox_absent(db):
    env = IntentEnvelope.model_validate({"intents": [{"action": "spawn", "what": "chicken", "follow": True}]})
    act = service.act(db, "pomps-pc", env, source="cli", client=FakeClient({"hs_ping": "Unknown command"}))
    assert act.status == "refused" and "sandbox" in act.detail and act.lines == []


def _sandbox_chicken():
    return IntentEnvelope.model_validate(
        {"intents": [{"action": "spawn", "what": "chicken", "count": 4, "formation": "line", "follow": True}]})


def test_sandbox_spawn_verified_by_the_hs_spawn_line(db):
    """With the sandbox loaded the readback is its own console line: `[hs] spawned 4 chicken at ...` (Task 4.4)."""
    client = FakeClient({"hs_ping": "[hs] pong 0",
                         "hs_spawn chicken 4 line 48 1": "[hs] spawned 4 chicken at 1,2,3;4,5,6;7,8,9;10,11,12"})
    act = service.act(db, "pomps-pc", _sandbox_chicken(), source="cli", client=client)
    assert act.status == "verified" and act.lines == ["sv_cheats 1", "hs_spawn chicken 4 line 48 1"]
    assert "spawn: 4 Chicken (line, following you)" in act.detail


def test_sandbox_error_line_is_failed_with_its_text(db):
    """A runtime refusal (`[hs] error no human player`) is a failed action, not a silent verified one."""
    client = FakeClient({"hs_ping": "[hs] pong 0", "hs_spawn chicken 4 line 48 1": "[hs] error no human player"})
    act = service.act(db, "pomps-pc", _sandbox_chicken(), source="cli", client=client)
    assert act.status == "failed" and "[hs] error no human player" in act.detail


def test_host_leased_by_a_test_run_is_refused(db):
    hosts.claim_host(db, "pomps-pc", "verify_demo/j-1", ttl_sec=600)
    with pytest.raises(service.LiveError, match="verify_demo/j-1"):
        service.act(db, "pomps-pc", bots_env(), source="cli", client=FakeClient({}))


# Real `ent_find chicken` replies from S1 (pomps-pc, CS2 1.41.9.2). The header line itself contains the
# class name, so an empty search must count 0, not 1.
ENT_FIND_HEADER = 'Searching for entities with class/target name containing substrings: "chicken"'
ENT_FIND_ONE = ENT_FIND_HEADER + "\n   'chicken' : '' (entindex 354)"
# Measured on pomps-pc 2026-10-25 (CS2 1.41.9.2/14192), standing still, two identical samples: `getpos`
# prints the EYE at z 156.874420, `getpos_exact` the FEET at z 95.296288 — an offset of 61.58, not the 64
# the console recipe used to subtract. The console spawn reads the exact form and adds no offset.
GETPOS_EYE = "setpos -428.000000 -843.000000 156.874420;setang 0.000000 133.500031 0.000000"
GETPOS_EXACT = "setpos_exact -428.000000 -843.000000 95.296288;setang_exact 0.000000 133.500031 0.000000"


def _console_chicken(db, ent_find_reply):
    env = IntentEnvelope.model_validate({"intents": [{"action": "spawn", "what": "chicken", "count": 1}]})
    client = FakeClient({"hs_ping": "Unknown command", "getpos_exact": GETPOS_EXACT,
                         "ent_find chicken": ent_find_reply})
    return service.act(db, "pomps-pc", env, source="cli", client=client)


def test_pose_is_the_measured_feet_pose_not_the_eye():
    """`getpos` (eye) must not be parsed at all: its z is 156.87 where the feet are at 95.30."""
    assert service.parse_pose(GETPOS_EYE) is None
    assert service.parse_pose(GETPOS_EXACT) == recipes.Pose(-428.0, -843.0, 95.296288, 133.500031)
    assert not hasattr(recipes, "GETPOS_EYE_HEIGHT")  # the +64 guess is gone, not merely unused


def test_console_spawn_lands_on_the_measured_feet(db):
    env = IntentEnvelope.model_validate({"intents": [{"action": "spawn", "what": "chicken", "count": 1}]})
    client = FakeClient({"hs_ping": "Unknown command", "getpos_exact": GETPOS_EXACT,
                         "ent_find chicken": ENT_FIND_ONE})
    act = service.act(db, "pomps-pc", env, source="cli", client=client)
    assert act.status == "verified"
    assert client.sent[1] == ["sv_cheats 1", "getpos_exact"]
    x, y, z = positions((-428.0, -843.0, 95.296288), 133.500031, 1, "line", 48, 24.0)[0]
    assert act.lines[1] == f'ent_create chicken {{"origin" "{x} {y} {z}"}}'
    assert z == 95.3  # the feet, not 156.9 and not 95.3 - 64


def test_console_spawn_verified_only_by_entity_rows(db):
    assert _console_chicken(db, ENT_FIND_ONE).status == "verified"


def test_console_spawn_with_no_entities_is_failed(db):
    act = _console_chicken(db, ENT_FIND_HEADER)
    assert act.status == "failed" and "listed 0 chicken" in act.detail



# ---------------------------------------------- clear readback (measured: `Removed 3 chickens`)
def _clear_env(what="chicken"):
    return IntentEnvelope.model_validate({"intents": [{"action": "clear", "what": what}]})


def test_console_clear_verified_by_the_removed_line(db):
    """`ent_remove_all chicken` printed `Removed 3 chickens` on pomps-pc: that is the readback."""
    client = FakeClient({"hs_ping": "Unknown command", "ent_remove_all chicken": "Removed 3 chickens"})
    act = service.act(db, "pomps-pc", _clear_env(), source="cli", client=client)
    assert act.status == "verified" and act.status != "sent"
    assert client.sent[-1] == ["ent_find chicken"]  # the row check still runs as the fallback


def test_console_clear_falls_back_to_zero_entity_rows(db):
    """No removal line at all (nothing to remove) is verified only when ent_find lists no row."""
    client = FakeClient({"hs_ping": "Unknown command", "ent_remove_all chicken": "",
                         "ent_find chicken": ENT_FIND_HEADER})
    assert service.act(db, "pomps-pc", _clear_env(), source="cli", client=client).status == "verified"


def test_console_clear_failed_when_entities_remain(db):
    client = FakeClient({"hs_ping": "Unknown command", "ent_remove_all chicken": "Removed 0 chickens",
                         "ent_find chicken": ENT_FIND_ONE})
    act = service.act(db, "pomps-pc", _clear_env(), source="cli", client=client)
    assert act.status == "failed" and "still lists 1 chicken" in act.detail


def test_sandbox_clear_verified_by_the_hs_cleared_line(db):
    client = FakeClient({"hs_ping": "[hs] pong 4", "hs_clear chicken": "[hs] cleared 4",
                         "ent_find chicken": ENT_FIND_HEADER})
    act = service.act(db, "pomps-pc", _clear_env(), source="cli", client=client)
    assert act.status == "verified" and client.sent[-1] == ["ent_find chicken"]


def test_sandbox_clear_all_verified_by_its_own_count(db):
    """`hs_clear all` names no class to re-check, so its own `[hs] cleared N` line is the readback."""
    client = FakeClient({"hs_ping": "[hs] pong 4", "hs_clear all": "[hs] cleared 4"})
    act = service.act(db, "pomps-pc", _clear_env(None), source="cli", client=client)
    assert act.status == "verified" and client.sent[-1] == ["sv_cheats 1", "hs_clear all"]


def test_sandbox_clear_all_failed_without_the_count_line(db):
    act = service.act(db, "pomps-pc", _clear_env(None), source="cli",
                      client=FakeClient({"hs_ping": "[hs] pong 4", "hs_clear all": "[hs] error no runtime"}))
    assert act.status == "failed" and "the console said" in act.detail


# ---------------------------------------------------------------- Task 4.6: load the sandbox
SPAWNGROUP_LOADED = "loaded spawngroup(  1) : SV: [0: hermes_sandbox]"


def _deployment(db, host_id="pomps-pc", *, status="verified", paths=("maps/hermes_sandbox.vpk",
                                                                     "maps/scripts/hermes_sandbox.vjs_c"),
                deploy_id="d-test"):
    entries = [{"path": p, "size": 3, "md5": "0" * 32, "verified": True} for p in paths]
    with db.writer() as conn, conn:
        conn.execute("INSERT INTO deployments (id, host_id, addon, dest_root, entries, status) VALUES (?,?,?,?,?,?)",
                     (deploy_id, host_id, "hermes_sandbox", "C:/x", json.dumps(entries), status))
    return deploy_id


def test_load_sandbox_needs_a_verified_deployment(db):
    did = _deployment(db, status="pushed")
    with pytest.raises(service.LiveError, match="verified"):
        service.load_sandbox(db, "pomps-pc", did, client=FakeClient({}))


def test_load_sandbox_refuses_a_deployment_without_the_vpk(db):
    """Without the .vpk, spawn_group_load kicks the client and kills the server."""
    did = _deployment(db, paths=("maps/scripts/hermes_sandbox.vjs_c",))
    with pytest.raises(service.LiveError, match="vpk"):
        service.load_sandbox(db, "pomps-pc", did, client=FakeClient({}))


def test_load_sandbox_refuses_a_deployment_on_another_host(db):
    hosts.register_host(db, "desktop-pc", "desktop-pc")
    did = _deployment(db, host_id="desktop-pc")
    with pytest.raises(service.LiveError, match="desktop-pc"):
        service.load_sandbox(db, "pomps-pc", did, client=FakeClient({}))


def test_load_sandbox_verified_by_the_spawngroup_and_pong(db):
    did = _deployment(db)
    client = FakeClient({"status": SPAWNGROUP_LOADED, "hs_ping": "[hs] pong 0"})
    out = service.load_sandbox(db, "pomps-pc", did, client=client)
    assert out["status"] == "verified" and out["spawngroups"] == ["hermes_sandbox"]
    assert out["lines"] == ["sv_cheats 1", "spawn_group_load hermes_sandbox", "sleep 3", "status", "hs_ping"]


def test_load_sandbox_failed_when_the_group_or_the_pong_is_missing(db):
    did = _deployment(db)
    out = service.load_sandbox(db, "pomps-pc", did, client=FakeClient({"status": "spawnpoints", "hs_ping": ""}))
    assert out["status"] == "failed" and "hermes_sandbox" in out["detail"]


def test_load_sandbox_rest_route(db, monkeypatch):
    _deployment(db)
    monkeypatch.setattr(service, "client_for",
                        lambda db_, host_id: FakeClient({"status": SPAWNGROUP_LOADED, "hs_ping": "[hs] pong 0"}))
    with TestClient(create_app(db.path)) as c:
        ok = c.post("/api/live/sandbox/load", json={"host_id": "pomps-pc", "deploy_id": "d-test"})
        assert ok.status_code == 200 and ok.json()["status"] == "verified"
        bad = c.post("/api/live/sandbox/load", json={"host_id": "pomps-pc", "deploy_id": "d-nope"})
        assert bad.status_code == 404


def test_rest_plan_schema_and_actions(db, monkeypatch):
    monkeypatch.setattr(service, "client_for", lambda db_, host_id: FakeClient({"hs_ping": "", "status": ROWS9}))
    with TestClient(create_app(db.path)) as c:
        assert c.put("/api/live/hosts/pomps-pc", json={"netcon_addr": "10.88.20.235:2121"}).status_code == 200
        assert c.put("/api/live/hosts/pomps-pc", json={"netcon_addr": "x;y"}).status_code == 409
        assert c.get("/api/live/hosts").json() == [{"host_id": "pomps-pc", "netcon_addr": "10.88.20.235:2121"}]
        schema = c.get("/api/live/intent-schema").json()
        assert "intents" in schema["schema"]["properties"] and schema["spawnables"][0]["id"] == "chicken"
        body = {"host_id": "pomps-pc", "envelope": {"intents": [{"action": "bots", "team_size": 5, "me": "t"}]}}
        plan = c.post("/api/live/plan", json=body).json()
        assert plan["lines"].count("bot_add_t") == 4
        bad = c.post("/api/live/plan", json={"host_id": "pomps-pc", "envelope": {"intents": [{"action": "nuke"}]}})
        assert bad.status_code == 422
        act = c.post("/api/live/actions", json={**body, "source": "plugin", "text": "5v5 me on T"})
        assert act.status_code == 201 and act.json()["status"] == "verified"
        assert c.get("/api/live/actions?host_id=pomps-pc").json()[0]["id"] == act.json()["id"]


def test_cli_lists_actions_as_json(db, capsys, monkeypatch):
    """`s2docs live actions --json` reads the same store the service wrote to (S2DOCS_DB points it there)."""
    from s2docs import cli
    from s2docs.config import get_settings

    service.act(db, "pomps-pc", bots_env(), source="cli", client=FakeClient({"hs_ping": "", "status": ROWS9}))
    monkeypatch.setenv("S2DOCS_DB", str(db.path))
    get_settings.cache_clear()
    assert cli.main(["--json", "live", "actions"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "s2docs/v1" and payload["actions"][0]["status"] == "verified"
