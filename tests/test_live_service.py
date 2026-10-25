import json

import pytest
from fastapi.testclient import TestClient

from s2docs import hosts
from s2docs.api.app import create_app
from s2docs.db.connection import Database
from s2docs.live import service
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


def test_host_leased_by_a_test_run_is_refused(db):
    hosts.claim_host(db, "pomps-pc", "verify_demo/j-1", ttl_sec=600)
    with pytest.raises(service.LiveError, match="verify_demo/j-1"):
        service.act(db, "pomps-pc", bots_env(), source="cli", client=FakeClient({}))


# Real `ent_find chicken` replies from S1 (pomps-pc, CS2 1.41.9.2). The header line itself contains the
# class name, so an empty search must count 0, not 1.
ENT_FIND_HEADER = 'Searching for entities with class/target name containing substrings: "chicken"'
ENT_FIND_ONE = ENT_FIND_HEADER + "\n   'chicken' : '' (entindex 354)"
GETPOS = "setpos -657.271362 -755.879639 182.555878;setang 0.000000 -152.499847 0.000000"


def _console_chicken(db, ent_find_reply):
    env = IntentEnvelope.model_validate({"intents": [{"action": "spawn", "what": "chicken", "count": 1}]})
    client = FakeClient({"hs_ping": "Unknown command", "getpos": GETPOS, "ent_find chicken": ent_find_reply})
    return service.act(db, "pomps-pc", env, source="cli", client=client)


def test_console_spawn_verified_only_by_entity_rows(db):
    assert _console_chicken(db, ENT_FIND_ONE).status == "verified"


def test_console_spawn_with_no_entities_is_failed(db):
    act = _console_chicken(db, ENT_FIND_HEADER)
    assert act.status == "failed" and "listed 0 chicken" in act.detail



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
