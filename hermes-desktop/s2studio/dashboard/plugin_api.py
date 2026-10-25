"""s2studio backend, mounted at /api/plugins/s2studio/.

A thin proxy over the s2docs REST API plus ONE model call: a sentence -> IntentEnvelope JSON (auxiliary
task 's2studio_intent'). s2docs validates every intent and owns the console allowlist, so nothing the
model writes reaches CS2 verbatim: the worst a bad answer can do is be rejected by `POST /api/live/plan`
(422) or compile to allowlisted lines. This half reads and writes nothing of its own — the registry, the
action log and the catalog all live in s2docs.

Routes:
  GET  /hosts                 -> s2docs GET /api/hosts (the Live tab's host picker)
  GET  /catalog               -> { spawnables, assets }: what can be spawned and what was promoted
  GET  /runs?limit=N          -> s2docs GET /api/jobs
  GET  /runs/{job_id}         -> { job, screenshots }
  GET  /artifact/{job}/{name} -> { data_url } (base64, <= 8 MB; the page shows screenshots inline)
  GET  /live/status?host_id=  -> s2docs GET /api/live/status (reachable, map, sandbox, cs2_build)
  GET  /live/actions?host_id= -> s2docs GET /api/live/actions (the recent-actions list)
  POST /intent {text, host_id} -> { envelope, plan } | { unsupported } (the model call lives here)
  POST /act {host_id, envelope, text} -> the s2docs LiveAction (source="plugin", judged by readback)
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query

router = APIRouter()
PLUGIN = "s2studio"
DEFAULT_URL = "http://127.0.0.1:8812"
MAX_ARTIFACT = 8 * 1024 * 1024
# Real s2docs ids: jobs `j-a8c2bd0d`, live actions `la-1f0c9b22`, assets `a-…`. Nothing else is proxied.
_ID = re.compile(r"^[a-z]{1,4}-[0-9a-f]{4,32}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

_SYSTEM = """You translate ONE request about a running Counter-Strike 2 game into JSON. Reply with a single JSON
object and nothing else: either an object valid against this JSON schema
{schema}
or {{"unsupported": "<one short sentence why>"}} when the request cannot be expressed with it.
Spawnable ids for "what": {ids}. Never invent ids. Never write console commands.
'5v5 including me on T' -> bots team_size 5, me "t". 'following me' -> follow true.
'stacked' / 'on top of each other' -> formation "stack". 'in a straight line' -> formation "line".
'remove everything you spawned' -> clear with no "what"."""


def _settings() -> dict:
    try:
        from hermes_cli.config import load_config
    except ImportError:  # outside Hermes (the s2docs test suite) there is no config at all
        return {}
    entries = ((load_config() or {}).get("plugins") or {}).get("entries") or {}
    return (entries.get(PLUGIN) or {}).get("settings") or {}


def base_url() -> str:
    return (os.environ.get("S2STUDIO_S2DOCS_URL") or _settings().get("s2docs_url") or DEFAULT_URL).rstrip("/")


def s2(method: str, path: str, body: Any = None, *, timeout: float = 30.0, raw: bool = False):
    """One call to s2docs. An error becomes the same status here, with s2docs' own detail text."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base_url() + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read(MAX_ARTIFACT + 1)
            if raw:
                return payload, r.headers.get("Content-Type", "application/octet-stream")
            return json.loads(payload or b"null")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")[:2000]
        try:
            detail = json.loads(text).get("detail")
        except ValueError:
            detail = None
        raise HTTPException(status_code=e.code, detail=detail or text) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise HTTPException(status_code=502, detail=f"s2docs at {base_url()} did not answer: {e}") from e


def _call_llm(system: str, text: str) -> str:
    from agent.auxiliary_client import call_llm
    resp = call_llm(task="s2studio_intent", temperature=0.0, max_tokens=600, timeout=60,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": text}])
    return resp.choices[0].message.content or ""


def _parse(raw: str) -> dict:
    """The model's answer as a JSON object: a fenced block is fine, no JSON at all is a 502."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise HTTPException(status_code=502, detail=f"the intent model answered without JSON: {raw[:200]!r}")
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"the intent model's JSON did not parse: {e}") from e
    if not isinstance(out, dict):
        raise HTTPException(status_code=502, detail="the intent model's JSON is not an object")
    return out


def to_intent(text: str) -> dict:
    """The prompt is built from the server's own schema, so the vocabulary cannot drift from s2docs'."""
    meta: dict = s2("GET", "/api/live/intent-schema")
    system = _SYSTEM.format(schema=json.dumps(meta["schema"]), ids=", ".join(s["id"] for s in meta["spawnables"]))
    return _parse(_call_llm(system, text))


def _check(value: str, rx: re.Pattern, what: str) -> str:
    if not rx.fullmatch(value or ""):
        raise HTTPException(status_code=422, detail=f"invalid {what}")
    return value


@router.get("/hosts")
def hosts_():
    return s2("GET", "/api/hosts")


@router.get("/catalog")
def catalog():
    return {"spawnables": s2("GET", "/api/live/spawnables"), "assets": s2("GET", "/api/assets")}


@router.get("/runs")
def runs(limit: int = Query(30, ge=1, le=200)):
    return s2("GET", f"/api/jobs?limit={limit}")


@router.get("/runs/{job_id}")
def run(job_id: str):
    _check(job_id, _ID, "job id")
    return {"job": s2("GET", f"/api/jobs/{job_id}"), "screenshots": s2("GET", f"/api/jobs/{job_id}/screenshots")}


@router.get("/artifact/{job_id}/{name}")
def artifact(job_id: str, name: str):
    _check(job_id, _ID, "job id")
    _check(name, _NAME, "artifact name")
    data, ctype = s2("GET", f"/api/artifacts/{job_id}/{urllib.parse.quote(name)}", raw=True)
    if len(data) > MAX_ARTIFACT:
        raise HTTPException(status_code=413, detail="artifact larger than 8 MB")
    return {"data_url": f"data:{ctype};base64,{base64.b64encode(data).decode()}"}


@router.get("/live/status")
def live_status(host_id: str):
    return s2("GET", f"/api/live/status?host_id={urllib.parse.quote(host_id)}", timeout=20)


@router.get("/live/actions")
def live_actions(host_id: str | None = None):
    q = f"?host_id={urllib.parse.quote(host_id)}" if host_id else ""
    return s2("GET", f"/api/live/actions{q}")


@router.post("/intent")
def intent(body: Annotated[dict, Body()]):
    text = str(body.get("text") or "").strip()
    if not 1 <= len(text) <= 500:
        raise HTTPException(status_code=422, detail="text: 1-500 characters")
    env = to_intent(text)
    if "unsupported" in env:
        return {"unsupported": str(env["unsupported"])[:300]}
    plan = s2("POST", "/api/live/plan", {"host_id": str(body.get("host_id") or ""), "envelope": env})
    return {"envelope": env, "plan": plan}


@router.post("/act")
def act(body: Annotated[dict, Body()]):
    return s2("POST", "/api/live/actions", {"host_id": body.get("host_id"), "envelope": body.get("envelope"),
                                            "source": "plugin", "text": body.get("text")}, timeout=120)
