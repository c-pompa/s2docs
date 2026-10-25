import pytest

from s2docs.live import capabilities as caps

# The real `status` block from spike S0 (pomps-pc, CS2 1.41.9.2, 2026-10-25):
# cache/scratch/live/s0.out lines 37-42, pasted verbatim. The build key is what the version line carries.
STATUS = """\
----- Status -----
@ Current  :  game
source   : slot 0
version  : 1.41.9.2/14192 10955 insecure  public
steamid  : [I:1:0] (72057594037927936)
players  : 1 humans, 3 bots (0 max) (not hibernating) (unreserved)"""


def test_cs2_version_is_the_key_status_prints():
    assert caps.cs2_version(STATUS) == "1.41.9.2/14192"


def test_no_version_line_is_a_capability_error():
    with pytest.raises(caps.CapabilityError, match="version"):
        caps.cs2_version("no status")


def test_record_table_require(db):
    caps.record(db, "1.41.9.2/14192", "runtime_precache", "pass", "[hs] model models/weapons/w_rif_ak47.vmdl spawned")
    t = caps.table(db, "1.41.9.2/14192")
    assert t["runtime_precache"]["status"] == "pass" and t["script_reload"]["status"] == "unknown"
    caps.require(db, "1.41.9.2/14192", "runtime_precache")
    with pytest.raises(caps.CapabilityError, match="unknown"):
        caps.require(db, "1.41.9.2/14192", "script_reload")
    with pytest.raises(caps.CapabilityError, match="unknown on build 1.41.9.3/14200"):
        caps.require(db, "1.41.9.3/14200", "runtime_precache")  # a new CS2 build forgets old passes
    with pytest.raises(ValueError):
        caps.record(db, "1.41.9.2/14192", "teleport_everything", "pass", "x")


def test_rest_capabilities_round_trip(tmp_path, monkeypatch):
    """GET reports the ledger for the host's running build; POST records a spike result for that build."""
    from fastapi.testclient import TestClient

    from s2docs import hosts
    from s2docs.api.app import create_app
    from s2docs.db.connection import Database
    from s2docs.live import service

    class FakeNetcon:
        host, port = "10.88.20.235", 2121

        def run(self, lines, **_):
            return STATUS

    base = Database(tmp_path / "cap.db")
    hosts.register_host(base, "pomps-pc", "pomps-pc", shell="cmd")
    monkeypatch.setattr(service, "client_for", lambda db_, host_id: FakeNetcon())
    with TestClient(create_app(base.path)) as c:
        got = c.get("/api/live/capabilities?host_id=pomps-pc").json()
        assert got["cs2_build"] == "1.41.9.2/14192"
        assert got["capabilities"]["bots_team_switch"]["status"] == "unknown"
        wrote = c.post("/api/live/capabilities", json={"cs2_build": "1.41.9.2/14192", "name": "bots_team_switch",
                                                       "status": "pass", "evidence": "4 T + 5 CT bots, human on T"})
        assert wrote.status_code == 200
        after = c.get("/api/live/capabilities?host_id=pomps-pc").json()["capabilities"]
        assert after["bots_team_switch"]["status"] == "pass"
        assert c.post("/api/live/capabilities", json={"cs2_build": "1.41.9.2/14192", "name": "nope",
                                                      "status": "pass", "evidence": "x"}).status_code == 422
