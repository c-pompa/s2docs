import pytest
from fastapi.testclient import TestClient

from s2docs.api.app import create_app


@pytest.fixture
def client(db):
    with TestClient(create_app(db.path)) as c:
        yield c


def test_games_and_stats(client):
    games = client.get("/api/games").json()
    assert games[0]["id"] == "cs2" and games[0]["current_build"] == 1000
    stats = client.get("/api/stats").json()
    assert stats["counts"]["cs2"]["convars"] == 5 and stats["schema_version"] >= 1


def test_search_endpoint(client):
    r = client.get("/api/search", params={"q": "roundtime", "game": "cs2"})
    assert r.status_code == 200 and r.json()["items"][0]["key"] == "mp_roundtime"
    assert client.get("/api/search", params={"q": ""}).status_code == 422
    assert client.get("/api/search", params={"q": "x", "kind": "bogus"}).status_code == 400


def test_detail_endpoints(client):
    assert client.get("/api/games/cs2/convars/mp_roundtime").json()["max_value"] == "60"
    ent = client.get("/api/games/cs2/entities/server/trigger_multiple").json()
    assert ent["inheritance"][0] == "trigger"
    assert client.get("/api/games/cs2/schema/CCSPlayerPawn").json()["inheritance"] == ["CBaseEntity"]
    assert client.get("/api/games/cs2/events/player_death").json()["source_file"] == "mod.gameevents"
    assert client.get("/api/games/cs2/script-api/types/Domain").json()["kind"] == "class"
    assert client.get("/api/games/cs2/pulse", params={"q": "Enable"}).json()["total"] == 1


def test_errors(client):
    assert client.get("/api/games/tf2/convars").status_code == 404
    assert client.get("/api/games/cs2/convars/nope").status_code == 404
    assert client.get("/api/games/deadlock/convars").status_code == 404  # not ingested
    assert client.get("/api/games/cs2/convars", params={"limit": 1000}).status_code == 422
    assert client.get("/api/games/cs2/diff").status_code == 404  # single build


def test_raw_file_download(client):
    r = client.get("/api/games/cs2/script-api/files/point_script.d.ts")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert client.get("/api/games/cs2/script-api/files/..%2F..%2Fetc%2Fpasswd").status_code in (404, 422)


def test_prompts(client):
    names = [p["name"] for p in client.get("/api/prompts").json()]
    assert "cs2_script" in names
    r = client.post("/api/prompts/cs2_script/render", json={"arguments": {"task": "speed boost on jump"}})
    text = r.json()["text"]
    assert r.status_code == 200 and "speed boost on jump" in text and "- OnPlayerJump:" in text
    assert "OnReload" not in text  # deprecated members are excluded from the index
    assert client.post("/api/prompts/cs2_script/render", json={"arguments": {}}).status_code == 400
    assert client.post("/api/prompts/nope/render", json={"arguments": {}}).status_code == 404


def test_frontend_served(client):
    assert "Source 2 Docs" in client.get("/").text
    assert client.get("/app.js").status_code == 200


def test_missing_database_returns_503(tmp_path):
    with TestClient(create_app(tmp_path / "missing.db", mount_mcp=False)) as c:
        assert c.get("/api/games").status_code == 503
