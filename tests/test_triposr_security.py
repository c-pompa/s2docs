"""TripoSR worker (deploy/triposr): auth + URL-input policy, without torch.

The worker's server.py imports torch at module top, so its auth and input policy live in
deploy/triposr/app/security.py (stdlib only) and are tested here. A static check of server.py
pins that it actually uses them and that the URL-fetch path stays deleted.
"""

from __future__ import annotations

import ast
import hmac
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "triposr"
APP = ROOT / "app"


def _load_security():
    spec = importlib.util.spec_from_file_location("triposr_security", APP / "security.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sec = _load_security()
TOKEN = "0123456789abcdef" * 4  # gitleaks:allow (test fixture, not a credential)


# --------------------------------------------------------------------------- refuse to start


@pytest.mark.parametrize("env", [{}, {"TSR_API_TOKEN": ""}, {"TSR_API_TOKEN": "   "}])
def test_require_token_refuses_to_start_without_a_token(env):
    with pytest.raises(SystemExit) as exc:
        sec.require_token(env)
    assert "TSR_API_TOKEN" in str(exc.value)
    assert "refusing to start" in str(exc.value)


def test_require_token_returns_the_token():
    assert sec.require_token({"TSR_API_TOKEN": TOKEN}) == TOKEN
    assert sec.require_token({"TSR_API_TOKEN": f" {TOKEN}\r\n"}) == TOKEN


# --------------------------------------------------------------------------- bearer check


def test_bearer_ok_accepts_only_the_exact_header():
    assert sec.bearer_ok(f"Bearer {TOKEN}", TOKEN)
    for bad in (
        None,
        "",
        TOKEN,
        f"bearer {TOKEN}",
        f"Bearer {TOKEN} ",
        f"Bearer {TOKEN[:-1]}",
        "Bearer ",
        f"Basic {TOKEN}",
        f"Bearer {TOKEN}x",
    ):
        assert not sec.bearer_ok(bad, TOKEN), bad


def test_bearer_ok_never_passes_with_an_empty_token():
    assert not sec.bearer_ok("Bearer ", "")
    assert not sec.bearer_ok("", "")


def test_bearer_ok_non_ascii_header_is_unequal_not_an_error():
    assert not sec.bearer_ok("Bearer \u00e9\u00e9\u00e9", TOKEN)


def test_bearer_ok_uses_compare_digest(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(sec.hmac, "compare_digest", spy)
    assert sec.bearer_ok(f"Bearer {TOKEN}", TOKEN)
    assert len(calls) == 1


# --------------------------------------------------------------------------- URL inputs


def test_url_inputs_are_detected_whatever_their_value():
    assert sec.url_input_keys({"image_url": "http://192.0.2.20:8812/"}) == ["image_url"]
    assert sec.url_input_keys({"image_url": ""}) == ["image_url"]
    assert sec.url_input_keys({"url": "x", "formats": "glb"}) == ["url"]
    assert sec.url_input_keys({"image_b64": "AAAA", "formats": "glb"}) == []
    assert "not accepted" in sec.URL_INPUT_REFUSAL


# --------------------------------------------------------------------------- server.py uses them


SERVER = (APP / "server.py").read_text()
TREE = ast.parse(SERVER)


def _func(name):
    return next(n for n in ast.walk(TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def test_server_has_no_url_fetch_path():
    imported = {a.name for n in ast.walk(TREE) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(TREE) if isinstance(n, ast.ImportFrom) and n.module}
    assert not {m for m in imported if m.split(".")[0] in ("urllib", "requests", "httpx", "aiohttp")}
    names = {n.name for n in ast.walk(TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_fetch_url" not in names
    assert "ALLOW_IMAGE_URL" not in SERVER
    assert "urlopen" not in SERVER


def test_server_requires_token_at_import():
    assigns = [
        n
        for n in TREE.body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "API_TOKEN" for t in n.targets)
    ]
    assert len(assigns) == 1
    call = assigns[0].value
    assert isinstance(call, ast.Call) and getattr(call.func, "id", None) == "require_token"


def test_server_auth_uses_constant_time_check_and_is_unconditional():
    src = ast.unparse(_func("_auth"))
    assert "bearer_ok(" in src
    assert "!=" not in src and "if API_TOKEN" not in src


def test_submit_refuses_url_inputs_in_both_branches():
    src = ast.unparse(_func("submit"))
    assert src.count("_refuse_url_inputs(") == 2
    assert src.index("_auth(request)") < src.index("_refuse_url_inputs(")


def test_compose_requires_token_and_has_no_url_switch():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r"TSR_API_TOKEN=\$\{TSR_API_TOKEN:\?", compose)
    assert "TSR_ALLOW_IMAGE_URL" not in compose
    assert '"127.0.0.1:18191:8000"' in compose


def test_env_example_ships_no_token():
    lines = [ln for ln in (ROOT / ".env.example").read_text().splitlines() if ln.startswith("TSR_API_TOKEN")]
    assert lines == ["TSR_API_TOKEN="]
