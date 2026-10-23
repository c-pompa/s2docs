"""Item 2: opt-in bearer-token auth for mutating /api routes and the whole /mcp endpoint.

Off by default (no S2DOCS_AUTH_TOKEN = no auth, exactly as before). When set, every non-GET/HEAD/OPTIONS
/api request and every /mcp request needs the token; GET routes and the static web UI stay open. The
token itself is never reported — only the boolean `auth_required`.
"""

from __future__ import annotations

import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import hosts
from s2docs.api.app import create_app
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.mcp_server import create_mcp_server

TOKEN = "s3cr3t-token-please"


@pytest.fixture
def adb(tmp_path) -> Database:
    db = Database(tmp_path / "auth.db")
    hosts.register_host(db, "build-pc", "pompa@192.0.2.172", shell="powershell")
    return db


@pytest.fixture
def client_factory(adb, monkeypatch):
    """Yield a factory that builds a TestClient with S2DOCS_AUTH_TOKEN set (or unset for None)."""

    def make(token: str | None) -> TestClient:
        if token is None:
            monkeypatch.delenv("S2DOCS_AUTH_TOKEN", raising=False)
        else:
            monkeypatch.setenv("S2DOCS_AUTH_TOKEN", token)
        get_settings.cache_clear()
        return TestClient(create_app(adb.path))

    yield make
    get_settings.cache_clear()


LOCK = "/api/hosts/build-pc/lock"
LOCK_BODY = {"owner": "ws/tester"}


def test_mutation_without_token_is_401(client_factory):
    with client_factory(TOKEN) as c:
        r = c.post(LOCK, json=LOCK_BODY)
        assert r.status_code == 401
        assert "token" in r.json()["detail"].lower()


def test_bearer_token_allows_mutation(client_factory):
    with client_factory(TOKEN) as c:
        r = c.post(LOCK, json=LOCK_BODY, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200 and r.json()["owner"] == "ws/tester"


def test_x_token_header_allows_mutation(client_factory):
    with client_factory(TOKEN) as c:
        r = c.post(LOCK, json=LOCK_BODY, headers={"X-S2docs-Token": TOKEN})
        assert r.status_code == 200 and r.json()["owner"] == "ws/tester"


def test_wrong_token_is_401(client_factory):
    with client_factory(TOKEN) as c:
        r = c.post(LOCK, json=LOCK_BODY, headers={"Authorization": "Bearer not-the-token"})
        assert r.status_code == 401


def test_get_routes_and_web_ui_stay_open(client_factory):
    with client_factory(TOKEN) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/hosts").status_code == 200
        assert c.get("/").status_code == 200  # static web UI


def test_mcp_endpoint_is_gated(client_factory):
    with client_factory(TOKEN) as c:
        assert c.post("/mcp", json={}).status_code == 401
        # with the token it is no longer a 401 (MCP itself may still reject the empty body)
        ok = c.post("/mcp", json={}, headers={"X-S2docs-Token": TOKEN})
        assert ok.status_code != 401


def test_unset_token_leaves_everything_open(client_factory):
    with client_factory(None) as c:
        assert c.post(LOCK, json=LOCK_BODY).status_code == 200
        assert c.post("/mcp", json={}).status_code != 401
        assert c.get("/api/health").json()["auth_required"] is False


def test_health_reports_auth_required_never_the_token(client_factory):
    with client_factory(TOKEN) as c:
        r = c.get("/api/health")
        assert r.json()["auth_required"] is True
        assert TOKEN not in r.text


def test_mcp_server_info_reports_auth_required(adb, monkeypatch):
    monkeypatch.setenv("S2DOCS_AUTH_TOKEN", TOKEN)
    get_settings.cache_clear()
    try:
        server = create_mcp_server(adb)

        async def main():
            async with Client(server) as c:
                return await c.call_tool("server_info", {})

        result = anyio.run(main)
        info = result.structured_content
        assert info["auth_required"] is True
        assert TOKEN not in json.dumps(info)
    finally:
        get_settings.cache_clear()


# ------------------------------------------------ non-ASCII headers must be 401, never 500
# hmac.compare_digest raises TypeError on a str with non-ASCII characters, so a header value that
# decodes to non-ASCII (any latin-1 byte >= 0x80) turned a bad token into an HTTP 500. httpx refuses
# to *send* such a header from the client, so drive the ASGI app with a hand-built scope.
def _raw_request(app, method: str, path: str, headers: list[tuple[bytes, bytes]]) -> int:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    status: dict[str, int] = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    async def main():
        await app(scope, receive, send)

    anyio.run(main)
    return status.get("code", 0)


@pytest.mark.parametrize(
    "headers",
    [
        [(b"authorization", b"Bearer caf\xe9")],  # 'café' once latin-1-decoded
        [(b"x-s2docs-token", b"\xff\xfe bogus")],
    ],
)
def test_non_ascii_header_is_401_not_500(client_factory, headers):
    with client_factory(TOKEN) as c:
        assert _raw_request(c.app, "POST", "/api/hosts/build-pc/lock", headers) == 401


def test_non_ascii_header_via_mcp_is_401(client_factory):
    with client_factory(TOKEN) as c:
        assert _raw_request(c.app, "POST", "/mcp", [(b"authorization", b"Bearer na\xefve")]) == 401


# ---------------------------------------------------------------- path gating boundaries
def test_apifoo_is_not_gated_as_api(client_factory):
    """/apifoo must not be treated as an /api route: it is unrouted, so never a 401."""
    with client_factory(TOKEN) as c:
        assert c.post("/apifoo", json={}).status_code != 401


def test_mcp_trailing_slash_alias_is_gated(client_factory):
    with client_factory(TOKEN) as c:
        assert c.post("/mcp/", json={}).status_code == 401


def test_mcpfoo_is_not_gated(client_factory):
    with client_factory(TOKEN) as c:
        assert c.post("/mcpfoo", json={}).status_code != 401


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_other_mutating_methods_are_gated(client_factory, method):
    with client_factory(TOKEN) as c:
        r = c.request(method, LOCK, json=LOCK_BODY)
        assert r.status_code == 401


@pytest.mark.parametrize("method", ["OPTIONS", "HEAD"])
def test_safe_methods_stay_open(client_factory, method):
    with client_factory(TOKEN) as c:
        # not gated -> whatever the router/static layer returns, but never a 401
        assert c.request(method, "/api/health").status_code != 401


def test_exact_api_path_is_gated(client_factory):
    with client_factory(TOKEN) as c:
        assert c.post("/api", json={}).status_code == 401

