import struct
import zlib

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from s2docs import assets, hosts, jobs
from s2docs.api.app import create_app
from s2docs.db.connection import Database
from s2docs.db.repository import NotFound
from s2docs.mcp_server import create_mcp_server

LICENCE = "CC0-1.0 (TripoSR output, generated here)"


def _png(w: int = 3, h: int = 2, color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    """A tiny real PNG, built with only zlib/struct (no Pillow)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(color) * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def cdb(tmp_path) -> Database:
    db = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(db, "build-pc", "pompa@x", shell="powershell")
    return db


@pytest.fixture
def client(cdb):
    with TestClient(create_app(cdb.path)) as c:
        yield c


def _build(cdb, status: str = "succeeded", *, kind: str = "compile") -> str:
    """A build run row in `status` (the catalog reads its state; its lifecycle has its own tests)."""
    import secrets

    rid = "b-" + secrets.token_hex(4)
    with cdb.writer() as conn, conn:
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner) VALUES (?, ?, ?, ?, ?, ?)",
            (rid, "build-pc", kind, "resourcecompiler -nop4 addon", status, f"build:{rid}"),
        )
    return rid


def _deploy(cdb, status: str = "verified") -> str:
    import secrets

    did = "d-" + secrets.token_hex(4)
    with cdb.writer() as conn, conn:
        conn.execute(
            "INSERT INTO deployments (id, host_id, dest_root, status) VALUES (?, ?, ?, ?)",
            (did, "build-pc", "C:/addon", status),
        )
    return did


def _job(cdb, *, verdict: str | None = "pass", status: str = "passed", frame: str = "frame.png") -> str:
    """A job that rendered an in-game frame and got a verdict (the model gate's evidence)."""
    jid = jobs.enqueue(
        cdb, kind="demo", title="hammered probe", target="ws/asset@abc123", purpose="p", expected="e",
        host_id="build-pc",
    ).id
    jobs.start(cdb, jid, worker="ws/asset")
    if verdict:
        jobs.event(
            cdb, jid, level="milestone", message=f"verdict {verdict}",
            data={"verdict": verdict, "source": "chat"},
        )
    jobs.save_artifact(cdb, jid, frame, _png(), "image/png")
    if status in ("queued", "running"):
        return jid
    jobs.finish(cdb, jid, status=status, summary={"what": "a", "why": "b", "learned": "c", "next": "d"})
    return jid


def _promo(cdb, **over) -> dict:
    body = {
        "kind": "model",
        "name": "Hammered Rock 01",
        "build_id": _build(cdb),
        "licence": LICENCE,
        "prompt": "a weathered granite rock, photo scanned look",
        "mesh_sha256": "a" * 64,
        "tris": 4210,
        "vmdl_path": "models/hammered/rock01.vmdl",
        "vmat_path": "materials/hammered/rock01.vmat",
        "tags": ["prop", "Rock"],
        "created_by": "ws/asset",
    }
    body.update(over)
    return body


def _model_body(cdb, **over) -> dict:
    jid = over.pop("frame_job_id", None) or _job(cdb)
    body = _promo(cdb, frame_job_id=jid, frame_artifact="frame.png")
    body.update(over)
    return body


# ------------------------------------------------------------------ happy path
def test_promote_happy_path_then_get_by_id_and_slug(client, cdb):
    jid = _job(cdb)
    did = _deploy(cdb)
    body = _promo(cdb, frame_job_id=jid, frame_artifact="frame.png", deploy_id=did)

    created = client.post("/api/assets", json=body)
    assert created.status_code == 201, created.text
    asset = created.json()
    assert asset["id"].startswith("a-") and asset["kind"] == "model"
    assert asset["slug"] == "hammered-rock-01"
    assert asset["build_id"] == body["build_id"] and asset["deploy_id"] == did
    assert asset["frame_job_id"] == jid and asset["frame_artifact"] == "frame.png"
    assert asset["licence"] == LICENCE and asset["retired_at"] is None
    assert asset["tags"] == ["prop", "rock"]  # lowercased + de-duplicated on write

    by_id = client.get(f"/api/assets/{asset['id']}")
    assert by_id.status_code == 200 and by_id.json() == asset
    by_slug = client.get(f"/api/assets/{asset['slug']}")
    assert by_slug.status_code == 200 and by_slug.json()["id"] == asset["id"]
    assert [a["id"] for a in client.get("/api/assets").json()] == [asset["id"]]


def test_promote_stores_the_explicit_slug_and_normalizes_it(client, cdb):
    body = _model_body(cdb, name="Rock 02", slug="hammered/prop/rock-02")
    assert client.post("/api/assets", json=body).status_code == 422  # not kebab-case
    body["slug"] = "hammered-rock-02"
    created = client.post("/api/assets", json=body)
    assert created.status_code == 201 and created.json()["slug"] == "hammered-rock-02"


def test_action_kind_needs_no_frame_but_still_needs_a_succeeded_build(client, cdb):
    body = {
        "kind": "action",
        "name": "Jump Boost Script",
        "build_id": _build(cdb),
        "licence": LICENCE,
        "vmat_path": None,
        "tags": ["script"],
    }
    created = client.post("/api/assets", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["frame_job_id"] is None and created.json()["slug"] == "jump-boost-script"


# ------------------------------------------------------------------ rejections
def test_reject_build_that_did_not_succeed(client, cdb):
    failed = _build(cdb, "failed")
    body = _promo(cdb, build_id=failed)
    rejected = client.post("/api/assets", json=body)
    assert rejected.status_code == 409
    assert "failed" in rejected.json()["detail"] and rejected.json()["conflict"] is True
    # still not succeeded while it runs
    running = _build(cdb, "running")
    assert client.post("/api/assets", json=_promo(cdb, build_id=running)).status_code == 409


def test_reject_model_without_a_frame(client, cdb):
    no_job = _promo(cdb)
    r = client.post("/api/assets", json=no_job)
    assert r.status_code == 422 and r.json()["fields"] == ["frame_job_id"]
    only_job = _promo(cdb, frame_job_id=_job(cdb))
    r2 = client.post("/api/assets", json=only_job)
    assert r2.status_code == 422 and r2.json()["fields"] == ["frame_artifact"]


def test_reject_frame_the_job_never_stored(client, cdb):
    jid = _job(cdb)
    body = _promo(cdb, frame_job_id=jid, frame_artifact="not-a-frame.png")
    r = client.post("/api/assets", json=body)
    assert r.status_code == 422 and r.json()["fields"] == ["frame_artifact"]
    # a recorded shot whose bytes are gone is a dead link, not evidence
    jobs.artifact_path(jid, "frame.png").unlink()
    r2 = client.post("/api/assets", json=_promo(cdb, frame_job_id=jid, frame_artifact="frame.png"))
    assert r2.status_code == 422 and "not stored" in r2.json()["detail"]


def test_reject_fail_verdict_and_failed_job(client, cdb):
    failed_verdict = _job(cdb, verdict="fail", status="passed")
    r = client.post("/api/assets", json=_promo(cdb, frame_job_id=failed_verdict, frame_artifact="frame.png"))
    assert r.status_code == 409 and "verdict is 'fail'" in r.json()["detail"]

    failed_status = _job(cdb, verdict="pass", status="failed")
    r2 = client.post("/api/assets", json=_promo(cdb, frame_job_id=failed_status, frame_artifact="frame.png"))
    assert r2.status_code == 409 and "failed" in r2.json()["detail"]

    no_verdict = _job(cdb, verdict=None, status="running")
    r3 = client.post("/api/assets", json=_promo(cdb, frame_job_id=no_verdict, frame_artifact="frame.png"))
    assert r3.status_code == 409 and "no verdict" in r3.json()["detail"]


def test_skip_verdict_is_not_fail(client, cdb):
    jid = _job(cdb, verdict="skip", status="passed")
    created = client.post("/api/assets", json=_promo(cdb, frame_job_id=jid, frame_artifact="frame.png"))
    assert created.status_code == 201, created.text


def test_reject_missing_licence_and_build_id(client, cdb):
    body = _model_body(cdb)
    body.pop("licence")
    r = client.post("/api/assets", json=body)
    assert r.status_code == 422 and r.json()["fields"] == ["licence"]
    r2 = client.post("/api/assets", json={**body, "licence": "   "})
    assert r2.status_code == 422 and r2.json()["fields"] == ["licence"]
    body.pop("build_id")
    r3 = client.post("/api/assets", json=body)
    assert r3.status_code == 422 and set(r3.json()["fields"]) == {"licence", "build_id"}


def test_reject_duplicate_slug(client, cdb):
    body = _model_body(cdb)
    first = client.post("/api/assets", json=body)
    assert first.status_code == 201
    again = client.post("/api/assets", json=_model_body(cdb, name=body["name"]))
    assert again.status_code == 409 and "already taken" in again.json()["detail"]  # derived from the same name
    explicit = client.post(
        "/api/assets", json=_model_body(cdb, name="Other", slug=body.get("slug") or "hammered-rock-01")
    )
    assert explicit.status_code == 409


def test_unknown_references_are_404(client, cdb):
    assert client.post("/api/assets", json=_promo(cdb, build_id="b-nope")).status_code == 404
    assert client.post("/api/assets", json=_promo(cdb, deploy_id="d-nope")).status_code == 404
    assert client.post(
        "/api/assets", json=_promo(cdb, frame_job_id="j-nope", frame_artifact="frame.png")
    ).status_code == 404
    assert client.get("/api/assets/a-nope").status_code == 404


# --------------------------------------------------------------- provenance
def test_provenance_links_the_records(client, cdb):
    jid = _job(cdb)
    did = _deploy(cdb)
    body = _promo(cdb, frame_job_id=jid, frame_artifact="frame.png", deploy_id=did)
    aid = client.post("/api/assets", json=body).json()["id"]

    prov = client.get(f"/api/assets/{aid}/provenance")
    assert prov.status_code == 200, prov.text
    data = prov.json()
    assert data["asset"]["id"] == aid
    assert data["build"]["id"] == body["build_id"] and data["build"]["status"] == "succeeded"
    assert data["deploy"]["id"] == did and data["deploy"]["status"] == "verified"
    assert data["job"]["id"] == jid
    assert data["frame_url"] == f"/api/artifacts/{jid}/frame.png"
    assert data["build"]["web_url"] and data["job"]["web_url"]  # request-derived page URLs


# -------------------------------------------------------------------- retire
def test_retire_keeps_provenance_and_hides_from_the_default_list(client, cdb):
    jid = _job(cdb)
    did = _deploy(cdb)
    body = _promo(cdb, frame_job_id=jid, frame_artifact="frame.png", deploy_id=did)
    aid = client.post("/api/assets", json=body).json()["id"]

    retired = client.post(f"/api/assets/{aid}/retire", json={"reason": "superseded by rock 02"})
    assert retired.status_code == 200 and retired.json()["retired_at"]
    assert retired.json()["retired_reason"] == "superseded by rock 02"

    assert client.get("/api/assets").json() == []
    assert [a["id"] for a in client.get("/api/assets?include_retired=true").json()] == [aid]
    # the row, its links and its provenance survive
    assert client.get(f"/api/assets/{aid}").status_code == 200
    prov = client.get(f"/api/assets/{aid}/provenance").json()
    assert prov["build"]["id"] == body["build_id"] and prov["job"]["id"] == jid and prov["deploy"]["id"] == did
    # idempotent: a retry neither errors nor un-retires
    again = client.post(f"/api/assets/{aid}/retire")
    assert again.status_code == 200 and again.json()["retired_at"] == retired.json()["retired_at"]


# ------------------------------------------------------------------ filters
def test_list_filters_by_kind_and_tag_and_is_newest_first(client, cdb):
    model = _model_body(cdb, tags=["prop", "rock"])
    action = {
        "kind": "action", "name": "Jump Boost", "build_id": _build(cdb), "licence": LICENCE, "tags": ["script", "rock"],
    }
    m = client.post("/api/assets", json=model).json()
    a = client.post("/api/assets", json=action).json()
    # make the order unambiguous (created_at has second granularity)
    with cdb.writer() as conn, conn:
        conn.execute("UPDATE assets SET created_at = '2020-01-01T00:00:00Z' WHERE id = ?", (m["id"],))

    assert [x["id"] for x in client.get("/api/assets").json()] == [a["id"], m["id"]]
    assert [x["id"] for x in client.get("/api/assets?kind=model").json()] == [m["id"]]
    assert [x["id"] for x in client.get("/api/assets?kind=action").json()] == [a["id"]]
    assert {x["id"] for x in client.get("/api/assets?tag=rock").json()} == {m["id"], a["id"]}
    assert [x["id"] for x in client.get("/api/assets?tag=prop&kind=model").json()] == [m["id"]]
    assert client.get("/api/assets?kind=sound").status_code == 422
    assert client.get("/api/assets?tag=").status_code == 422


def test_service_level_rejections_use_the_same_classes(cdb):
    failed = _build(cdb, "failed")
    with pytest.raises(assets.AssetConflict):
        assets.promote(cdb, kind="model", name="x", build_id=failed, licence=LICENCE)
    with pytest.raises(assets.AssetValidationError):
        assets.promote(cdb, kind="model", name="x", build_id=_build(cdb), licence="")
    with pytest.raises(NotFound):
        assets.get(cdb, "a-nope")


# ---------------------------------------------------------------------- MCP
def test_mcp_tools_are_registered_and_share_the_rest_gates(cdb):
    server = create_mcp_server(cdb)
    jid = _job(cdb)
    good = _promo(cdb, frame_job_id=jid, frame_artifact="frame.png")
    bad = _promo(cdb, build_id=_build(cdb, "failed"))

    async def main():
        async with Client(server) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            ok = await c.call_tool("asset_promote", good)
            listed = await c.call_tool("asset_list", {"kind": "model"})
            got = await c.call_tool("asset_get", {"asset_id": ok.structured_content["slug"]})
            rejected = await c.call_tool("asset_promote", bad)
            dup = await c.call_tool("asset_promote", {**good, "slug": None, "name": good["name"]})
            return tools, ok, listed, got, rejected, dup

    tools, ok, listed, got, rejected, dup = anyio.run(main)
    assert {"asset_promote", "asset_list", "asset_get"} <= tools
    assert not ok.is_error and ok.structured_content["slug"] == "hammered-rock-01"
    assert [a["id"] for a in listed.structured_content["result"]] == [ok.structured_content["id"]]
    assert got.structured_content["id"] == ok.structured_content["id"]
    # the same gate, the same message: MCP is not a second implementation
    assert rejected.is_error and "succeeded" in rejected.content[0].text
    assert dup.is_error and "already taken" in dup.content[0].text
