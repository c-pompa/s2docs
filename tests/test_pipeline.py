"""The asset-chain slice: one job, one event per stage, honest stubs and blocks."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from s2docs import pipeline
from s2docs.models import BuildRun, BuildStatus

# 1x1 transparent PNG — the material writer validates the signature, not the pixels.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_run(
    *,
    id: str = "b-test",
    host_id: str = "desktop-pc",
    command: str = "rc -i bridge.vmdl -nop4",
    status: BuildStatus = "running",
    exit_code: int | None = None,
    summary: dict | None = None,
) -> BuildRun:
    """A BuildRun with every required field (created_at/updated_at have no defaults)."""
    return BuildRun(
        id=id, host_id=host_id, command=command, status=status,
        owner=f"build:{id}", exit_code=exit_code, summary=summary or {},
        created_at=_now(), updated_at=_now(),
    )


@pytest.fixture(autouse=True)
def _settings_are_not_cached():
    from s2docs.config import get_settings

    yield
    get_settings.cache_clear()


def _host(db, host_id="desktop-pc"):
    """`enqueue` validates the host against the registry, so a job needs a registered one."""
    from s2docs import hosts

    try:
        return hosts.get_host(db, host_id)
    except hosts.HostError:
        return hosts.register_host(db, host_id, "pompa@example.invalid", shell="powershell")


def _addon(tmp_path, monkeypatch, name="probe_addon"):
    from s2docs.build.workspace import addon_root
    from s2docs.config import get_settings

    monkeypatch.setenv("S2DOCS_WORKSPACE", str(tmp_path / "ws"))
    get_settings.cache_clear()
    root = addon_root(get_settings().workspace_dir, "cs2", name)
    root.mkdir(parents=True)
    return root


def _run(db, *, prompt="a stone bridge prop", addon="probe_addon", name="bridge",
         mesh="models/bridge/bridge.fbx", host_id="desktop-pc", material_color=None,
         build_command=None, poll_sec=0.0, build_timeout_sec=300):
    return pipeline.run_asset_build_slice(
        db,
        prompt=prompt,
        addon=addon,
        name=name,
        mesh=mesh,
        host_id=host_id,
        material_color=material_color,
        build_command=build_command,
        poll_sec=poll_sec,
        build_timeout_sec=build_timeout_sec,
    )


def test_slice_records_every_stage_in_order_and_stubs_what_it_cannot_run(db, tmp_path, monkeypatch):
    root = _addon(tmp_path, monkeypatch)
    _host(db)

    job = _run(db)

    # one event per stage, in chain order — the timeline IS the chain
    assert [e.phase for e in job.events if e.phase] == list(pipeline.STAGES)
    assert job.status == "inconclusive", "deploy/ingame are blocked, so the chain cannot claim a pass"

    # the real stage wrote the document
    assert (root / "models" / "bridge" / "bridge.vmdl").is_file()

    # the stages that do not exist yet say so
    for stage in ("concept", "mesh"):
        event = next(e for e in job.events if e.phase == stage)
        assert event.data["stubbed"] is True
        assert "generator" in event.data["reason"]

    # the blocked stages say why, so a reader is not left guessing
    for stage in ("deploy", "ingame"):
        event = next(e for e in job.events if e.phase == stage)
        assert event.data["blocked"] is True
        assert event.data["reason"]

    build = next(e for e in job.events if e.phase == "build")
    assert build.data["skipped"] is True


def test_slice_writes_the_material_when_a_colour_map_is_given(db, tmp_path, monkeypatch):
    root = _addon(tmp_path, monkeypatch)
    _host(db)
    colour = tmp_path / "bridge_color.png"
    colour.write_bytes(PNG)

    job = _run(db, material_color=colour)

    vmat = root / "materials" / "hammered" / "bridge" / "bridge.vmat"
    assert vmat.is_file()
    asset = next(e for e in job.events if e.phase == "asset")
    assert asset.data["vmat"] == "materials/hammered/bridge/bridge.vmat"
    assert "vmat" in job.summary.learned


def test_slice_links_a_real_build_and_a_failed_build_fails_the_job(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    seen = {}

    def fake_start(_db, host_id, command, **kw):
        seen["start"] = {"host_id": host_id, "command": command, **kw}
        return _build_run(host_id=host_id, command=command)

    def fake_wait(_db, run_id, **_kw):
        return _build_run(id=run_id, status="failed", exit_code=1,
                          summary={"compiled": 0, "failed": 1})

    monkeypatch.setattr(pipeline.builds, "start", fake_start)
    monkeypatch.setattr(pipeline.builds, "wait", fake_wait)

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", build_timeout_sec=5)

    assert seen["start"]["host_id"] == "desktop-pc"
    assert seen["start"]["command"] == "rc -i bridge.vmdl -nop4"
    assert seen["start"]["kind"] == "compile"
    # the artifact is linked, not pasted into a message
    assert any(link.kind == "build" and link.ref_id == "b-test" for link in job.links)
    build = next(e for e in job.events if e.phase == "build")
    assert build.data["build"] == "b-test"
    assert build.data["status"] == "failed"
    assert build.level == "error"
    assert job.status == "failed"


def test_slice_stops_at_the_asset_stage_when_the_addon_is_missing(db, tmp_path, monkeypatch):
    from s2docs.config import get_settings

    monkeypatch.setenv("S2DOCS_WORKSPACE", str(tmp_path / "empty-workspace"))
    get_settings.cache_clear()
    _host(db)

    job = _run(db)

    assert job.status == "error"
    error = next(e for e in job.events if e.level == "error")
    assert error.phase == "asset"
    assert "not in the workspace" in error.message
    assert job.summary.next.startswith("Run `s2docs scaffold")
    # nothing pretends the later stages ran
    assert not [e for e in job.events if e.phase in ("build", "deploy", "ingame")]


def test_cli_runs_the_chain_and_emits_the_job(tmp_path, monkeypatch, capsys):
    import json

    from s2docs import cli
    from s2docs.config import get_settings
    from s2docs.db.connection import Database

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("S2DOCS_DATA_DIR", str(data))
    workspace = _addon(tmp_path, monkeypatch)
    get_settings.cache_clear()
    db = Database(get_settings().db_path)
    with db.writer():  # a writer applies the migrations; a reader refuses an uninitialised file
        pass
    _host(db)

    code = cli.main([
        "--json", "pipeline", "run",
        "--prompt", "a stone bridge prop",
        "--addon", "probe_addon",
        "--name", "bridge",
        "--mesh", "models/bridge/bridge.fbx",
        "--host", "desktop-pc",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "inconclusive"
    assert [e["phase"] for e in payload["events"] if e["phase"]] == list(pipeline.STAGES)
    assert (workspace / "models" / "bridge" / "bridge.vmdl").is_file()
