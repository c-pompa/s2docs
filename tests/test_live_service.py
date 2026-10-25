import pytest

from s2docs import hosts
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
