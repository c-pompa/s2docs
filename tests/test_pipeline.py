"""The asset-chain slice: one job, one event per stage, honest stubs, skips and blocks."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from s2docs import pipeline
from s2docs.models import BuildRun, BuildStatus, DeployStatus

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


def _deployment(*, id="d-test", status: DeployStatus = "pushed", verified=False):
    """A Deployment as the deploy service returns one; the push/verify halves are what gets faked."""
    from s2docs.models import DeployEntry, Deployment

    entries = [DeployEntry(path="models/bridge/bridge.vmdl", size=11, md5="0" * 32, verified=verified)]
    return Deployment(
        id=id, host_id="desktop-pc", addon="probe_addon", dest_root="D:/addons", entries=entries,
        status=status, owner=f"deploy:{id}", created_at=_now(), updated_at=_now(),
    )


def _patch_build(monkeypatch, *, status: BuildStatus = "succeeded"):
    """Fake the server-owned build run: the pipeline sees a finished run, never SSH or a host."""
    def fake_start(_db, host_id, command, **_kw):
        return _build_run(host_id=host_id, command=command)

    def fake_wait(_db, run_id, **_kw):
        return _build_run(
            id=run_id, status=status, exit_code=0 if status == "succeeded" else 1,
            summary={"compiled": 1 if status == "succeeded" else 0},
        )

    monkeypatch.setattr(pipeline.builds, "start", fake_start)
    monkeypatch.setattr(pipeline.builds, "wait", fake_wait)


def _patch_deploy(monkeypatch, *, status: DeployStatus = "verified", push_raises=None, verify_raises=None, seen=None):
    """Fake both halves of the deploy service (push + MD5 readback) — no scp, no Get-FileHash."""
    def fake_push(_db, host_id, dest_root, files, **kw):
        if push_raises is not None:
            raise push_raises
        if seen is not None:
            seen.update({"host_id": host_id, "dest_root": dest_root,
                         "paths": [f.path for f in files], **kw})
        return _deployment(status="pushed")

    def fake_verify(_db, deploy_id):
        if verify_raises is not None:
            raise verify_raises
        return _deployment(id=deploy_id, status=status, verified=status == "verified")

    monkeypatch.setattr(pipeline.deploys, "push", fake_push)
    monkeypatch.setattr(pipeline.deploys, "verify", fake_verify)


def _run(db, *, prompt="a stone bridge prop", addon="probe_addon", name="bridge",
         mesh="models/bridge/bridge.fbx", host_id="desktop-pc", material_color=None,
         build_command=None, poll_sec=0.0, build_timeout_sec=300, dest=None):
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
        dest=dest,
    )


def test_slice_records_every_stage_in_order_and_stubs_what_it_cannot_run(db, tmp_path, monkeypatch):
    root = _addon(tmp_path, monkeypatch)
    _host(db)

    job = _run(db)

    # one event per stage, in chain order — the timeline IS the chain
    assert [e.phase for e in job.events if e.phase] == list(pipeline.STAGES)
    assert job.status == "inconclusive", "ingame is blocked, so the chain cannot claim a pass"

    # the real stage wrote the document
    assert (root / "models" / "bridge" / "bridge.vmdl").is_file()

    # the stages that do not exist yet say so
    for stage in ("concept", "mesh"):
        event = next(e for e in job.events if e.phase == stage)
        assert event.data["stubbed"] is True
        assert "generator" in event.data["reason"]

    # the blocked stage says why, so a reader is not left guessing
    ingame = next(e for e in job.events if e.phase == "ingame")
    assert ingame.data["blocked"] is True
    assert "display adapter" in ingame.data["reason"]

    # deploy is not blocked any more: with no destination it says so and copies nothing
    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.data["skipped"] is True
    assert "no destination was given" in deploy.data["reason"]
    assert not deploy.data.get("blocked")

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


def test_slice_deploys_to_a_destination_and_links_the_deploy(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_build(monkeypatch)
    seen: dict = {}
    _patch_deploy(monkeypatch, seen=seen)

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", dest="D:/addons")

    # the push got the addon's own manifest and asked for the separate MD5 readback
    assert seen["host_id"] == "desktop-pc"
    assert seen["dest_root"] == "D:/addons"
    assert seen["addon"] == "probe_addon"
    assert seen["verify"] is False
    assert seen["paths"] == ["models/bridge/bridge.vmdl"]

    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.level == "milestone"
    assert deploy.data["status"] == "verified"
    assert deploy.data["deploy"] == "d-test"
    assert deploy.data["files"] == 1
    # the deployment is linked exactly the way the build is, so the job alone reconstructs it
    assert any(link.kind == "deploy" and link.ref_id == "d-test" for link in job.links)
    assert any(link.kind == "build" for link in job.links)
    # deploy is real, but ingame is still blocked — the chain still cannot claim a pass
    assert job.status == "inconclusive"
    assert next(e for e in job.events if e.phase == "ingame").data["blocked"] is True


def test_slice_skips_deploy_without_a_destination(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_build(monkeypatch)

    def _never(*_a, **_kw):
        raise AssertionError("deploy must not run without a destination")

    monkeypatch.setattr(pipeline.deploys, "push", _never)

    job = _run(db, build_command="rc -i bridge.vmdl -nop4")

    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.data["skipped"] is True
    assert "no destination was given" in deploy.data["reason"]
    assert not deploy.data.get("blocked")
    assert not [link for link in job.links if link.kind == "deploy"]
    assert job.status == "inconclusive", "a skipped deploy is not a failure and not a pass"
    assert next(e for e in job.events if e.phase == "ingame").data["blocked"] is True


def test_slice_fails_the_job_when_the_deploy_readback_fails(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_build(monkeypatch)
    _patch_deploy(monkeypatch, status="failed")

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", dest="D:/addons")

    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.level == "error"
    assert deploy.data["status"] == "failed"
    # the record exists even when the bytes did not match, so the id is still linked
    assert deploy.data["deploy"] == "d-test"
    assert any(link.kind == "deploy" and link.ref_id == "d-test" for link in job.links)
    assert job.status == "failed"
    assert next(e for e in job.events if e.phase == "ingame").data["blocked"] is True


def test_slice_fails_the_job_when_the_deploy_cannot_start(db, tmp_path, monkeypatch):
    from s2docs.deploys import DeployError

    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_build(monkeypatch)
    _patch_deploy(monkeypatch, push_raises=DeployError("all copies failed: scp rc=1"))

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", dest="D:/addons")

    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.level == "error"
    assert deploy.data["status"] == "failed"
    assert "all copies failed" in deploy.data["error"]
    assert not [link for link in job.links if link.kind == "deploy"]
    assert job.status == "failed"


def test_slice_keeps_the_deploy_link_when_the_readback_cannot_run(db, tmp_path, monkeypatch):
    from s2docs.deploys import DeployError

    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_build(monkeypatch)
    _patch_deploy(monkeypatch, verify_raises=DeployError("Get-FileHash on desktop-pc timed out after 120s"))

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", dest="D:/addons")

    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.level == "error"
    assert deploy.data["status"] == "pushed", "no verdict was reached, and the stage says so"
    assert "could not run" in deploy.data["error"]
    # the record exists even without a verdict, so the id is still on the job
    assert any(link.kind == "deploy" and link.ref_id == "d-test" for link in job.links)
    assert job.status == "failed", "an unconfirmed copy is not an inconclusive chain"


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


def test_cli_passes_the_destination_to_the_deploy_stage(tmp_path, monkeypatch, capsys):
    import json

    from s2docs import cli
    from s2docs.config import get_settings
    from s2docs.db.connection import Database

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("S2DOCS_DATA_DIR", str(data))
    _addon(tmp_path, monkeypatch)
    get_settings.cache_clear()
    db = Database(get_settings().db_path)
    with db.writer():
        pass
    _host(db)
    _patch_build(monkeypatch)
    seen: dict = {}
    _patch_deploy(monkeypatch, seen=seen)

    code = cli.main([
        "--json", "pipeline", "run",
        "--prompt", "a stone bridge prop",
        "--addon", "probe_addon",
        "--name", "bridge",
        "--mesh", "models/bridge/bridge.fbx",
        "--host", "desktop-pc",
        "--build-command", "rc -i bridge.vmdl -nop4",
        "--dest", "D:/addons",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert seen["dest_root"] == "D:/addons"
    deploy = next(e for e in payload["events"] if e["phase"] == "deploy")
    assert "skipped" not in deploy["data"]
    assert deploy["data"]["status"] == "verified"
    assert deploy["data"]["deploy"] == "d-test"
    assert payload["status"] == "inconclusive"


# ------------------------------------------------------------------ text -> cs_script slice


def _fake_validation(*, ok: bool, errors: int = 0):
    from s2docs.models import ScriptValidation

    return ScriptValidation(ok=ok, errors=errors, warnings=0, diagnostics=[], checker="tsc (fake)")


def _patch_validate(monkeypatch, *, raises=None, result=None):
    import s2docs.build.validate_script as vs

    def fake(_repo, _code, *, game="cs2", filename="script.js"):
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(vs, "validate_cs_script", fake)


def test_script_slice_writes_validates_and_blocks_the_rest(db, tmp_path, monkeypatch):
    root = _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))

    job = pipeline.run_script_slice(
        db, prompt="a ping/pong script", addon="probe_addon", name="pinger",
        host_id="desktop-pc", script_text='import { Instance } from "cs_script/point_script";\n',
    )

    assert [e.phase for e in job.events if e.phase] == list(pipeline.SCRIPT_STAGES)
    assert job.status == "inconclusive", "build/deploy/verdict are blocked, so no pass"
    written = root / "scripts" / "pinger.js"
    assert written.is_file() and "cs_script/point_script" in written.read_text(encoding="utf-8")
    generate = next(e for e in job.events if e.phase == "generate")
    assert generate.data["source"] == "supplied"
    validate = next(e for e in job.events if e.phase == "validate")
    assert validate.level == "milestone"
    for stage in ("build", "deploy", "verdict"):
        assert next(e for e in job.events if e.phase == stage).data["blocked"] is True


def test_script_slice_uses_the_scaffold_skeleton_when_no_text_is_supplied(db, tmp_path, monkeypatch):
    root = _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))

    job = pipeline.run_script_slice(db, prompt="p", addon="probe_addon", name="pinger",
                                    host_id="desktop-pc")

    generate = next(e for e in job.events if e.phase == "generate")
    assert generate.data["source"] == "scaffold skeleton"
    assert "Instance.OnActivate" in (root / "scripts" / "pinger.js").read_text(encoding="utf-8")


def test_script_slice_fails_when_validation_reports_errors(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_validate(monkeypatch, result=_fake_validation(ok=False, errors=2))

    job = pipeline.run_script_slice(db, prompt="p", addon="probe_addon", name="pinger",
                                    host_id="desktop-pc", script_text="const x = ;\n")

    assert job.status == "failed"
    validate = next(e for e in job.events if e.phase == "validate")
    assert validate.level == "error"
    assert validate.data["errors"] == 2


def test_script_slice_blocks_validate_when_the_toolchain_is_missing(db, tmp_path, monkeypatch):
    from s2docs.build.validate_script import ToolchainMissing

    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_validate(monkeypatch, raises=ToolchainMissing("no tsc"))

    job = pipeline.run_script_slice(db, prompt="p", addon="probe_addon", name="pinger",
                                    host_id="desktop-pc")

    validate = next(e for e in job.events if e.phase == "validate")
    assert validate.data["blocked"] is True
    assert "tsc" in validate.data["reason"]
    assert job.status == "inconclusive", "a missing toolchain is not a validation failure"


def test_cli_runs_the_script_chain(tmp_path, monkeypatch, capsys):
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
    with db.writer():
        pass
    _host(db)
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))

    code = cli.main([
        "--json", "pipeline", "script",
        "--prompt", "a ping/pong script",
        "--addon", "probe_addon",
        "--name", "pinger",
        "--host", "desktop-pc",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "inconclusive"
    assert [e["phase"] for e in payload["events"] if e["phase"]] == list(pipeline.SCRIPT_STAGES)
    assert (workspace / "scripts" / "pinger.js").is_file()
