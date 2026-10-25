"""The `s2studio` Hermes Desktop plugin that ships in this repo (hermes-desktop/s2studio/).

Hermes loads a plugin as FILES, not as an importable module: plugin.yaml names the entry points and
dashboard/plugin_api.py is mounted at /api/plugins/<id>/. So every test here loads the files by path,
exactly as Hermes does, and drives the backend against a real s2docs app (contract test) with only
the model call and the netcon socket faked — the routes and response shapes are the ones the page
will really see.
"""

import importlib.util
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from s2docs import hosts
from s2docs.api.app import create_app
from s2docs.db.connection import Database
from s2docs.live import service

PLUGIN = Path(__file__).resolve().parents[1] / "hermes-desktop" / "s2studio"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- Task 3.1: manifest + Python half


def test_manifest_and_python_half():
    """The manifest Hermes reads, the hidden web-dashboard tab, and the auxiliary task registration."""
    manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "s2studio" == PLUGIN.name  # the folder name IS the plugin id
    assert manifest["manifest_version"] == 2
    assert set(manifest["config_schema"]) == {"s2docs_url", "default_host", "chat_auto_run"}
    assert manifest["config_schema"]["default_host"]["default"] == "pomps-pc"

    dash = json.loads((PLUGIN / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert dash["name"] == "s2studio" and dash["api"] == "plugin_api.py"
    assert dash["entry"] == "dist/index.js" and dash["tab"]["hidden"] is True
    assert (PLUGIN / "dashboard" / "dist" / "index.js").is_file()

    calls = {}

    class Ctx:
        def register_auxiliary_task(self, task, **kw):
            calls[task] = kw

    _load("s2studio_pkg", PLUGIN / "__init__.py").register(Ctx())
    assert calls["s2studio_intent"]["display_name"] == "Source 2 Studio intents"


# ------------------------------------------------- Task 3.2/3.3: backend proxy + intent (contract)

# One `status` PLAYER ROW of a bot, the shape spike S0 measured (see tests/test_live_service.py).
BOT_ROW = "  10      BOT    0    0     active      0 'Colin'"


class FakeClient:
    """A netcon console: `status` answers with `bots` bot rows, everything else prints nothing."""

    host, port, bots = "10.88.20.235", 2121, 9

    def __init__(self, bots: int = 9):
        self.bots = bots

    def run(self, lines, **_):
        return "\n".join([BOT_ROW] * self.bots) if "status" in lines else ""


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """The plugin's s2() routed into a REAL s2docs app (contract test); the model is faked per test."""
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "pomps-pc", "pomps-pc", shell="cmd")
    monkeypatch.setattr(service, "client_for", lambda db_, host_id: FakeClient())
    s2docs = TestClient(create_app(db.path))
    mod = _load("s2studio_api", PLUGIN / "dashboard" / "plugin_api.py")

    def s2(method, path, body=None, *, timeout=30.0, raw=False):
        r = s2docs.request(method, path, json=body)
        if r.status_code >= 400:
            raise mod.HTTPException(status_code=r.status_code, detail=r.json().get("detail"))
        return (r.content, r.headers.get("content-type")) if raw else r.json()

    monkeypatch.setattr(mod, "s2", s2)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/s2studio")
    with s2docs:
        yield mod, TestClient(app)


INTENT = "/api/plugins/s2studio/intent"
BOTS = {"intents": [{"action": "bots", "team_size": 5, "me": "t"}]}
FENCE = "`" * 3  # a literal triple backtick inside this test's own source


def test_intent_turns_text_into_a_plan(wired, monkeypatch):
    """A model answer in a fenced block still plans: the fence is stripped, s2docs compiles the lines."""
    mod, c = wired
    fenced = FENCE + 'json\n{"intents":[{"action":"bots","team_size":5,"me":"t"}]}\n' + FENCE
    monkeypatch.setattr(mod, "_call_llm", lambda system, text: fenced)
    out = c.post(INTENT, json={"text": "bots for 5v5 including me on T", "host_id": "pomps-pc"}).json()
    assert out["envelope"] == BOTS
    assert out["plan"]["lines"].count("bot_add_ct") == 5
    assert out["plan"]["lines"].count("bot_add_t") == 4


def test_model_prompt_carries_schema_and_ids(wired, monkeypatch):
    """The prompt is built from the live intent-schema route: no schema or ids drift between the two."""
    mod, c = wired
    seen = {}

    def fake(system, text):
        seen.setdefault("system", system)
        return '{"unsupported": "no weather control"}'

    monkeypatch.setattr(mod, "_call_llm", fake)
    c.post(INTENT, json={"text": "make it rain", "host_id": "pomps-pc"})
    assert '"intents"' in seen["system"] and "chicken" in seen["system"]


def test_unsupported_and_garbage(wired, monkeypatch):
    mod, c = wired
    monkeypatch.setattr(mod, "_call_llm", lambda s, t: '{"unsupported": "no weather control"}')
    assert c.post(INTENT, json={"text": "rain", "host_id": "pomps-pc"}).json() == {"unsupported": "no weather control"}
    monkeypatch.setattr(mod, "_call_llm", lambda s, t: "sorry I cannot")
    assert c.post(INTENT, json={"text": "rain", "host_id": "pomps-pc"}).status_code == 502


def test_intent_rejects_empty_or_overlong_text(wired):
    _, c = wired
    assert c.post(INTENT, json={"text": "  ", "host_id": "pomps-pc"}).status_code == 422
    assert c.post(INTENT, json={"text": "x" * 501, "host_id": "pomps-pc"}).status_code == 422


def test_act_and_runs_proxy(wired):
    """Acting through the page is the same route MCP and the CLI use, judged by the same readback."""
    mod, c = wired
    act = c.post("/api/plugins/s2studio/act", json={"host_id": "pomps-pc", "envelope": BOTS, "text": "5v5"}).json()
    assert act["status"] == "verified" and act["source"] == "plugin"
    assert c.get("/api/plugins/s2studio/runs").status_code == 200
    assert c.get("/api/plugins/s2studio/catalog").json()["spawnables"][0]["id"] == "chicken"


def test_live_status_proxy(wired):
    """The status chips read the real /api/live/status shape: reachable, sandbox, map, cs2_build."""
    _, c = wired
    st = c.get("/api/plugins/s2studio/live/status?host_id=pomps-pc").json()
    assert st["host_id"] == "pomps-pc" and st["reachable"] is True and st["sandbox"] is False
    assert st["netcon"] == "10.88.20.235:2121"


def test_ids_are_checked_before_proxying(wired):
    """A path parameter never reaches s2docs unvalidated (no traversal, no query smuggling)."""
    _, c = wired
    assert c.get("/api/plugins/s2studio/runs/..%2Fhosts").status_code in (404, 422)
    assert c.get("/api/plugins/s2studio/artifact/j-1/..%2Fx").status_code in (404, 422)


# ---------------------------------------------------------------- Task 3.4: the page


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_plugin_js_parses(tmp_path):
    src = (PLUGIN / "desktop" / "plugin.js").read_text(encoding="utf-8")
    (tmp_path / "plugin.mjs").write_text(src, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(tmp_path / "plugin.mjs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_page_only_calls_routes_the_backend_serves(wired):
    """The page and plugin_api.py ship together: a path the page calls must be a route this backend has."""
    mod, _ = wired
    src = (PLUGIN / "desktop" / "plugin.js").read_text(encoding="utf-8")
    called = {m.split("?")[0].rstrip("/") for m in re.findall(r"api\.rest\(\s*['\"`]([^'\"`]+)", src)}
    assert called, "the page calls the backend through ctx.rest"
    routes = {r.path for r in mod.router.routes if hasattr(r, "path")}
    for path in called:
        # `/runs/${id}` is a literal prefix: its first segment must be a route of ours
        head = "/" + path.strip("/").split("/")[0]
        assert any(r == path or r.startswith(head) for r in routes), f"{path} is not served by {sorted(routes)}"


def test_s2_reaches_a_real_s2docs_server(tmp_path, monkeypatch):
    """The one hop the contract test fakes: base_url -> a real server, and s2docs' error text passed on."""
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "pomps-pc", "pomps-pc", shell="cmd")
    server = uvicorn.Server(uvicorn.Config(create_app(db.path, mount_mcp=False),
                                          host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    try:
        monkeypatch.setenv("S2STUDIO_S2DOCS_URL",
                           f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}")
        mod = _load("s2studio_api_socket", PLUGIN / "dashboard" / "plugin_api.py")
        assert mod.s2("GET", "/api/live/spawnables")[0]["id"] == "chicken"
        with pytest.raises(mod.HTTPException) as err:
            mod.s2("GET", "/api/jobs/j-000000ff")
        assert err.value.status_code == 404 and err.value.detail
    finally:
        server.should_exit = True
        thread.join(timeout=10)
