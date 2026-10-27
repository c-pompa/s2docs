"""The asset-chain slice: one job, one event per stage, honest stubs, skips and blocks."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

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
    root.mkdir(parents=True, exist_ok=True)
    return root


def _deployment(*, id="d-test", status: DeployStatus = "pushed", verified=False):
    """A Deployment as the deploy service returns one; the push/verify halves are what gets faked."""
    from s2docs.models import DeployEntry, Deployment

    entries = [DeployEntry(path="models/bridge/bridge.vmdl", size=11, md5="0" * 32, verified=verified)]
    return Deployment(
        id=id, host_id="desktop-pc", addon="probe_addon", dest_root="D:/addons", entries=entries,
        status=status, owner=f"deploy:{id}", created_at=_now(), updated_at=_now(),
    )


def _collected(tmp_path, *, run_id="b-test", name="bridge"):
    """What a real artifact pull leaves behind: one local file per addon-relative compiled path."""
    from s2docs.live.asset_artifacts import Collected

    root = tmp_path / "artifacts" / run_id
    files = {}
    for rel in (f"models/{name}/{name}.vmdl_c", f"materials/hammered/{name}/{name}.vmat_c"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"compiled")
        files[rel] = path
    return Collected(run_id=run_id, root=root, files=files)


def _patch_artifacts(monkeypatch, tmp_path, *, raises=None, seen=None):
    """Fake the artifact pull: no ssh, no scp — the collected files are written locally instead."""
    def fake_collect(_db, *, host_id, addon, name, run_id, dest=None):
        if raises is not None:
            raise raises
        if seen is not None:
            seen.update({"host_id": host_id, "addon": addon, "name": name, "run_id": run_id})
        return _collected(tmp_path, run_id=run_id, name=name)

    monkeypatch.setattr(pipeline.asset_artifacts, "collect_model_artifacts", fake_collect)


def _patch_build(monkeypatch, tmp_path, *, status: BuildStatus = "succeeded", artifact_raises=None,
                 collected_seen=None):
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
    _patch_artifacts(monkeypatch, tmp_path, raises=artifact_raises, seen=collected_seen)


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
         build_command=None, poll_sec=0.0, build_timeout_sec=300, dest=None,
         ingame_host=None, ingame_model=None, ingame_clear=False, verdict=None):
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
        ingame_host=ingame_host,
        ingame_model=ingame_model,
        ingame_clear=ingame_clear,
        verdict=verdict,
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

    # ingame with no target host is a missing argument: skipped, with the flag that fixes it
    ingame = next(e for e in job.events if e.phase == "ingame")
    assert ingame.data["skipped"] is True and not ingame.data.get("blocked")
    assert "--ingame-host" in ingame.data["reason"]

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
    _patch_build(monkeypatch, tmp_path)
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
    # deploy is real, but with no target host ingame is skipped — the chain still cannot claim a pass
    assert job.status == "inconclusive"
    assert next(e for e in job.events if e.phase == "ingame").data["skipped"] is True


def test_slice_skips_deploy_without_a_destination(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_build(monkeypatch, tmp_path)

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
    assert next(e for e in job.events if e.phase == "ingame").data["skipped"] is True


def test_slice_fails_the_job_when_the_deploy_readback_fails(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_build(monkeypatch, tmp_path)
    _patch_deploy(monkeypatch, status="failed")

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", dest="D:/addons")

    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.level == "error"
    assert deploy.data["status"] == "failed"
    # the record exists even when the bytes did not match, so the id is still linked
    assert deploy.data["deploy"] == "d-test"
    assert any(link.kind == "deploy" and link.ref_id == "d-test" for link in job.links)
    assert job.status == "failed"
    assert next(e for e in job.events if e.phase == "ingame").data["skipped"] is True


def test_slice_fails_the_job_when_the_deploy_cannot_start(db, tmp_path, monkeypatch):
    from s2docs.deploys import DeployError

    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_build(monkeypatch, tmp_path)
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
    _patch_build(monkeypatch, tmp_path)
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
    _patch_build(monkeypatch, tmp_path)
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


def test_script_slice_writes_validates_and_skips_build_without_a_compile(db, tmp_path, monkeypatch):
    root = _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))

    job = pipeline.run_script_slice(
        db, prompt="a ping/pong script", addon="probe_addon", name="pinger",
        host_id="desktop-pc", script_text='import { Instance } from "cs_script/point_script";\n',
    )

    assert [e.phase for e in job.events if e.phase] == list(pipeline.SCRIPT_STAGES)
    assert job.status == "inconclusive", "the verdict is blocked, so no pass"
    written = root / "scripts" / "pinger.js"
    assert written.is_file() and "cs_script/point_script" in written.read_text(encoding="utf-8")
    generate = next(e for e in job.events if e.phase == "generate")
    assert generate.data["source"] == "supplied"
    validate = next(e for e in job.events if e.phase == "validate")
    assert validate.level == "milestone"
    # build/deploy are real capabilities now: without a request they are SKIPPED with the reason
    build = next(e for e in job.events if e.phase == "build")
    assert build.data["skipped"] is True and "no compile was requested" in build.data["reason"]
    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.data["skipped"] is True
    assert not [link for link in job.links if link.kind in ("build", "deploy")]
    # ... and only the verdict is still blocked
    verdict = next(e for e in job.events if e.phase == "verdict")
    assert verdict.data["blocked"] is True
    assert "point_script" in verdict.data["reason"] and "Failed to load cs_script" in verdict.data["reason"]


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

    from s2docs.build import script_compile

    monkeypatch.setattr(script_compile, "compile_js",
                        lambda *a, **kw: pytest.fail("a script that does not type-check is not compiled"))

    job = pipeline.run_script_slice(db, prompt="p", addon="probe_addon", name="pinger",
                                    host_id="desktop-pc", script_text="const x = ;\n", compile=True)

    assert job.status == "failed"
    validate = next(e for e in job.events if e.phase == "validate")
    assert validate.level == "error"
    assert validate.data["errors"] == 2
    # compile was asked for, and still did not run: the honest record says why
    build = next(e for e in job.events if e.phase == "build")
    assert build.data["skipped"] is True and "validate reported 2 error(s)" in build.data["reason"]
    assert next(e for e in job.events if e.phase == "deploy").data["skipped"] is True


def _patch_script_compile(monkeypatch, seen: dict, *, error=None, deploy_error=None):
    """Fake the compile/pull and the loose deploy halves: no host, no scp, no resourcecompiler."""
    from s2docs.build import script_compile

    def fake_compile(_db, host_id, *, addon, name, source, **_kw):
        seen["compile"] = {"host_id": host_id, "addon": addon, "name": name, "source": str(source)}
        if error is not None:
            raise error
        artifact = Path(seen["data_dir"]) / f"{name}.vjs_c"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"vjs_c")
        seen["artifact"] = artifact
        return script_compile.CompiledScript(
            run=_build_run(id="b-vjs", host_id=host_id, status="succeeded", exit_code=0),
            path=artifact, md5="a" * 32, size=artifact.stat().st_size,
            remote=f"C:/cs2/game/csgo_addons/{addon}/scripts/{name}.vjs_c",
        )

    def fake_deploy(_db, host_id, *, addon, name, compiled, **_kw):
        seen["deploy"] = {"host_id": host_id, "addon": addon, "name": name, "compiled": str(compiled)}
        if deploy_error is not None:
            raise deploy_error
        return _deployment(id="d-vjs", status="verified", verified=True)

    monkeypatch.setattr(script_compile, "compile_js", fake_compile)
    monkeypatch.setattr(script_compile, "deploy_loose", fake_deploy)


def test_script_slice_compiles_and_deploys_when_asked(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db, "pomps-pc")
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))
    seen: dict = {"data_dir": str(tmp_path)}
    _patch_script_compile(monkeypatch, seen)

    job = pipeline.run_script_slice(
        db, prompt="p", addon="probe_addon", name="pinger", host_id="pomps-pc",
        script_text="export {};\n", compile=True, deploy_host_id="pomps-pc",
    )

    # one event per stage, in order — the timeline is still the chain
    assert [e.phase for e in job.events if e.phase] == list(pipeline.SCRIPT_STAGES)
    assert job.status == "inconclusive", "the verdict is blocked, so the chain cannot claim a pass"

    # the compile got the addon's own .js and the host it was told to use
    assert seen["compile"]["host_id"] == "pomps-pc"
    assert seen["compile"]["name"] == "pinger"
    assert seen["compile"]["source"].endswith("scripts/pinger.js")

    build = next(e for e in job.events if e.phase == "build")
    assert build.level == "milestone"
    assert build.data["build"] == "b-vjs" and build.data["status"] == "succeeded"
    assert build.data["md5"] == "a" * 32 and build.data["size"] == 5
    assert any(link.kind == "build" and link.ref_id == "b-vjs" for link in job.links)

    # the deploy copied the pulled artifact, and its record is linked like the build
    assert seen["deploy"]["host_id"] == "pomps-pc" and seen["deploy"]["name"] == "pinger"
    assert seen["deploy"]["compiled"] == str(seen["artifact"])
    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.data["status"] == "verified" and deploy.data["deploy"] == "d-vjs"
    assert deploy.data["path"] == "scripts/pinger.vjs_c"
    assert any(link.kind == "deploy" and link.ref_id == "d-vjs" for link in job.links)
    assert job.status != "failed"


def test_script_slice_records_a_compile_failure_and_skips_the_deploy(db, tmp_path, monkeypatch):
    from s2docs.build import script_compile

    _addon(tmp_path, monkeypatch)
    _host(db, "pomps-pc")
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))
    seen: dict = {"data_dir": str(tmp_path)}
    _patch_script_compile(
        monkeypatch, seen,
        error=script_compile.ScriptNotFresh("run b-x exited 0 but printed no 'SCRIPT COMPILE OK'", run_id="b-x"),
    )

    job = pipeline.run_script_slice(db, prompt="p", addon="probe_addon", name="pinger",
                                    host_id="pomps-pc", script_text="export {};\n",
                                    compile=True, deploy_host_id="pomps-pc")

    build = next(e for e in job.events if e.phase == "build")
    assert build.level == "error" and build.data["status"] == "failed"
    assert "SCRIPT COMPILE OK" in build.data["error"]
    assert build.data["build"] == "b-x" and any(
        link.kind == "build" and link.ref_id == "b-x" for link in job.links
    )
    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.data["skipped"] is True and "nothing was copied" in deploy.data["reason"]
    assert "deploy" not in seen
    assert job.status == "failed"


def test_script_slice_fails_when_the_loose_deploy_fails(db, tmp_path, monkeypatch):
    from s2docs.build import script_compile

    _addon(tmp_path, monkeypatch)
    _host(db, "pomps-pc")
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))
    seen: dict = {"data_dir": str(tmp_path)}
    _patch_script_compile(monkeypatch, seen,
                          deploy_error=script_compile.ScriptCompileError("scp rc=1"))

    job = pipeline.run_script_slice(db, prompt="p", addon="probe_addon", name="pinger",
                                    host_id="pomps-pc", script_text="export {};\n",
                                    compile=True, deploy_host_id="pomps-pc")

    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.level == "error" and deploy.data["status"] == "failed"
    assert "scp rc=1" in deploy.data["error"]
    assert job.status == "failed"


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


def test_script_slice_blocks_validate_when_no_script_api_is_ingested(db, tmp_path, monkeypatch):
    """An empty data dir has no .d.ts to check against: blocked (like a missing tsc), never a crash."""
    from s2docs.db.repository import NotFound

    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_validate(monkeypatch, raises=NotFound("game 'cs2' has no any ingested build"))

    job = pipeline.run_script_slice(db, prompt="p", addon="probe_addon", name="pinger",
                                    host_id="desktop-pc")

    validate = next(e for e in job.events if e.phase == "validate")
    assert validate.data["blocked"] is True
    assert "ingested build" in validate.data["reason"]
    assert job.status == "inconclusive"


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


# ------------------------------------------------------------------ ingame (real, judged by readback)
# The 2026-10-27 route: collect the model compile's artifacts, push them loose into the playing host's
# game/csgo tree, then `hs_model` over netcon and judge by the game's own `[hs] model ... spawned name=...`
# line plus a rising `hs_ping` counter. A console line is not a look at the screen, so the job only
# closes `passed` on the caller's verdict.

def _patch_ingame(monkeypatch, *, status="verified", detail="readback ok", show_raises=None,
                  loose_status: DeployStatus = "verified", loose_raises=None, seen=None):
    from s2docs.live.asset_artifacts import Collected

    def fake_loose(_db, collected, *, host_id, addon=None, owner=None):
        if loose_raises is not None:
            raise loose_raises
        if seen is not None:
            seen.update({"loose_host": host_id, "loose_addon": addon, "owner": owner,
                         "loose_paths": sorted(collected.files)})
        assert isinstance(collected, Collected)
        return _deployment(id="d-loose", status=loose_status, verified=loose_status == "verified"), None

    def fake_show(_db, host_id, model, *, client=None, clear=False):
        if show_raises is not None:
            raise show_raises
        if seen is not None:
            seen.update({"show_host": host_id, "model": model, "clear": clear})
        return {"host_id": host_id, "model": model, "status": status, "detail": detail,
                "lines": [f"hs_model {model}"], "readback": "…", "pong_before": 0, "pong_after": 1}

    monkeypatch.setattr(pipeline.asset_artifacts, "deploy_loose", fake_loose)
    monkeypatch.setattr(pipeline.service, "show_model", fake_show)


def test_slice_collects_the_build_artifacts_and_links_them_to_the_run(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    seen: dict = {}
    _patch_build(monkeypatch, tmp_path, collected_seen=seen)

    job = _run(db, build_command="rc -i bridge.vmdl -nop4")

    assert seen == {"host_id": "desktop-pc", "addon": "probe_addon", "name": "bridge", "run_id": "b-test"}
    build = next(e for e in job.events if e.phase == "build")
    assert build.data["artifacts"] == 2 and build.level == "milestone"
    assert "2 artifact(s) collected" in build.message
    assert job.status == "inconclusive", "a collected build is still not an in-game proof"


def test_slice_fails_the_job_when_an_artifact_pull_does_not_match(db, tmp_path, monkeypatch):
    from s2docs.live.service import LiveError

    _addon(tmp_path, monkeypatch)
    _host(db)
    _host(db, "pomps-pc")
    _patch_build(monkeypatch, tmp_path,
                 artifact_raises=LiveError("models/bridge/bridge.vmdl_c: pulled md5 a != the host's b"))

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", ingame_host="pomps-pc")

    build = next(e for e in job.events if e.phase == "build")
    assert build.level == "error"
    assert "not collected" in build.message and "md5" in build.data["artifact_error"]
    assert job.status == "failed", "bytes nobody could confirm are not a build we can hand to a game"
    ingame = next(e for e in job.events if e.phase == "ingame")
    assert ingame.data["skipped"] is True and "artifacts" in ingame.data["reason"]


def test_slice_ingame_verified_closes_inconclusive_and_asks_for_a_visual_verdict(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    _host(db, "pomps-pc")
    _patch_build(monkeypatch, tmp_path)
    seen: dict = {}
    _patch_ingame(monkeypatch, seen=seen)

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", ingame_host="pomps-pc", ingame_clear=True)

    # the artifacts went LOOSE to the playing host, at their own addon-relative paths
    assert seen["loose_host"] == "pomps-pc" and seen["loose_paths"] == [
        "materials/hammered/bridge/bridge.vmat_c", "models/bridge/bridge.vmdl_c"]
    assert seen["owner"].startswith("live:pipeline:")
    # …and the model the stage asked the game to spawn is the one the asset stage wrote
    assert seen["show_host"] == "pomps-pc" and seen["model"] == "models/bridge/bridge.vmdl"
    assert seen["clear"] is True

    ingame = next(e for e in job.events if e.phase == "ingame")
    assert ingame.data["status"] == "verified" and ingame.level == "milestone"
    assert ingame.data["loose_deploy"] == "d-loose"
    assert not ingame.data.get("blocked") and not ingame.data.get("skipped")
    # the loose push is a real deployment record, linked like any other artifact
    assert any(link.kind == "deploy" and link.ref_id == "d-loose" for link in job.links)
    # mechanical readback only: the job cannot claim a pass on it
    assert job.status == "inconclusive"
    assert job.summary.next.startswith("the model precached and spawned")


def test_slice_closes_passed_only_with_a_supplied_pass_verdict(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    _host(db, "pomps-pc")
    _patch_build(monkeypatch, tmp_path)
    _patch_ingame(monkeypatch)

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", ingame_host="pomps-pc", verdict="pass")

    assert job.status == "passed"
    ingame = next(e for e in job.events if e.phase == "ingame")
    assert ingame.data["verdict"] == "pass" and ingame.data["status"] == "verified"


def test_slice_refuses_a_verdict_it_does_not_understand(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    with pytest.raises(pipeline.PipelineError, match="verdict"):
        _run(db, verdict="looked fine")


def test_slice_fails_the_job_when_the_readback_does_not_match(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    _host(db, "pomps-pc")
    _patch_build(monkeypatch, tmp_path)
    _patch_ingame(monkeypatch, status="failed", detail="the runtime spawned name=models/chicken/chicken_roasted.vmdl")

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", ingame_host="pomps-pc", verdict="pass")

    ingame = next(e for e in job.events if e.phase == "ingame")
    assert ingame.level == "error" and ingame.data["status"] == "failed"
    assert "chicken_roasted" in ingame.data["detail"]
    assert job.status == "failed", "a pass verdict cannot rescue a readback that did not match"


def test_slice_fails_the_job_when_the_artifacts_do_not_reach_the_target_host(db, tmp_path, monkeypatch):
    from s2docs.deploys import DeployError

    _addon(tmp_path, monkeypatch)
    _host(db)
    _host(db, "pomps-pc")
    _patch_build(monkeypatch, tmp_path)
    _patch_ingame(monkeypatch, loose_raises=DeployError("all copies failed: scp rc=1"))

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", ingame_host="pomps-pc")

    ingame = next(e for e in job.events if e.phase == "ingame")
    assert ingame.level == "error" and "scp rc=1" in ingame.data["error"]
    assert job.status == "failed"


def test_slice_records_ingame_blocked_when_the_host_has_no_netcon(db, tmp_path, monkeypatch):
    """A host registered but never given a netcon address is a missing precondition, not a pass."""
    from s2docs.live.service import LiveError

    _addon(tmp_path, monkeypatch)
    _host(db)
    _host(db, "pomps-pc")
    _patch_build(monkeypatch, tmp_path)
    _patch_ingame(monkeypatch, show_raises=LiveError("no netcon address for 'pomps-pc'"))

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", ingame_host="pomps-pc")

    ingame = next(e for e in job.events if e.phase == "ingame")
    assert ingame.data["blocked"] is True and ingame.data["status"] == "blocked"
    assert "no netcon address" in ingame.data["detail"]
    assert "hermes_sandbox" in ingame.data["reason"]
    assert job.status == "inconclusive", "blocked is not failed and not passed"


def test_cli_passes_the_ingame_flags_through(tmp_path, monkeypatch, capsys):
    import json

    from s2docs import cli
    from s2docs.config import get_settings
    from s2docs.db.connection import Database

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    _addon(tmp_path, monkeypatch)
    get_settings.cache_clear()
    db = Database(get_settings().db_path)
    with db.writer():
        pass
    _host(db)
    _host(db, "pomps-pc")
    _patch_build(monkeypatch, tmp_path)
    seen: dict = {}
    _patch_ingame(monkeypatch, seen=seen)

    code = cli.main([
        "--json", "pipeline", "run",
        "--prompt", "a stone bridge prop",
        "--addon", "probe_addon",
        "--name", "bridge",
        "--mesh", "models/bridge/bridge.fbx",
        "--host", "desktop-pc",
        "--build-command", "rc -i bridge.vmdl -nop4",
        "--ingame-host", "pomps-pc",
        "--ingame-model", "models/bridge/bridge.vmdl",
        "--ingame-clear",
        "--verdict", "pass",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "passed"
    ingame = next(e for e in payload["events"] if e["phase"] == "ingame")
    assert ingame["data"]["verdict"] == "pass"
    assert seen["model"] == "models/bridge/bridge.vmdl" and seen["clear"] is True


# ------------------------------------------------- the look at the screen (`pipeline verdict`)
# The chain closes `inconclusive` the moment the readback verifies, because a console line is not a
# look at the screen. The look happens AFTER that, so the verdict is its own command on a closed job:
# `inconclusive -> passed|failed`, recorded as a human-confirm evidence row + a `verdict` stage event,
# never by re-running the chain (which would spawn a second model nobody judged).


def _verified_chain(db, tmp_path, monkeypatch, **kw):
    """A real asset-chain job that closed `inconclusive` on a verified readback (the measured case)."""
    _addon(tmp_path, monkeypatch)
    _host(db)
    _host(db, "pomps-pc")
    _patch_build(monkeypatch, tmp_path)
    _patch_ingame(monkeypatch, **kw)
    return _run(db, build_command="rc -i bridge.vmdl -nop4", ingame_host="pomps-pc")


def _human_confirms(db, job_id):
    from s2docs import evidence

    return [row for row in evidence.list_evidence(db, kind="human-confirm") if row.ref == job_id]


def test_verdict_pass_closes_a_verified_inconclusive_job_and_records_evidence(db, tmp_path, monkeypatch):
    job = _verified_chain(db, tmp_path, monkeypatch)
    assert job.status == "inconclusive"

    closed = pipeline.record_verdict(db, job.id, "pass", notes="the bridge spawned at the right scale")

    assert closed.status == "passed"
    # the chain's seven stages, then the one stage the human added — no re-run, no second spawn
    assert [e.phase for e in closed.events if e.phase] == [*pipeline.STAGES, pipeline.VERDICT_STAGE]
    verdict_event = closed.events[-1]
    assert verdict_event.phase == "verdict" and verdict_event.level == "milestone"
    assert verdict_event.data["verdict"] == "pass" and verdict_event.data["source"] == "coordinator"
    assert verdict_event.data["from"] == "inconclusive" and verdict_event.data["to"] == "passed"

    # durable, not just a status: a human-confirm row anchored to the job, linked from it
    rows = _human_confirms(db, job.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "human-confirm" and row.data["verdict"] == "pass"
    assert row.data["notes"] == "the bridge spawned at the right scale"
    assert row.data["job_id"] == job.id and row.data["stage"] == "ingame"
    assert row.id == verdict_event.data["evidence_id"]
    assert any(link.kind == "evidence" and link.ref_id == row.id for link in closed.links)

    # the close-out says what a human saw, and does not claim the console line was the proof
    assert "visual verdict pass" in closed.summary.learned
    assert "looked right" in closed.summary.learned
    assert "human look at the screen" in closed.summary.next
    # …and the confirm push channel fired, so a waiting agent sees it without polling the API
    from s2docs import confirms

    assert row.id in confirms.confirms_jsonl_path().read_text(encoding="utf-8")


def test_verdict_fail_closes_the_job_failed(db, tmp_path, monkeypatch):
    job = _verified_chain(db, tmp_path, monkeypatch)

    closed = pipeline.record_verdict(db, job.id, "fail", notes="the material was untextured grey")

    assert closed.status == "failed"
    assert "visual verdict fail" in closed.summary.learned
    assert "did NOT look right" in closed.summary.learned
    assert "fix what was seen" in closed.summary.next
    assert closed.events[-1].data["to"] == "failed"


def test_verdict_is_idempotent_for_the_same_call_and_a_conflict_for_a_different_one(db, tmp_path, monkeypatch):
    job = _verified_chain(db, tmp_path, monkeypatch)

    first = pipeline.record_verdict(db, job.id, "pass", notes="looks right")
    again = pipeline.record_verdict(db, job.id, "pass", notes="looks right")

    assert again.status == "passed"
    # a retried command is a no-op: one evidence row, one verdict event, not two
    assert len(_human_confirms(db, job.id)) == 1
    assert [e.phase for e in again.events if e.phase] == [*pipeline.STAGES, pipeline.VERDICT_STAGE]
    assert again.summary.learned == first.summary.learned

    with pytest.raises(pipeline.jobs.JobConflict, match="already passed"):
        pipeline.record_verdict(db, job.id, "fail", notes="changed my mind")
    assert len(_human_confirms(db, job.id)) == 1


def test_verdict_refuses_a_job_whose_readback_failed(db, tmp_path, monkeypatch):
    """A verified spawn that then failed is `failed`, and no verdict can rescue it."""
    _addon(tmp_path, monkeypatch)
    _host(db)
    _host(db, "pomps-pc")
    _patch_build(monkeypatch, tmp_path)
    _patch_ingame(monkeypatch, status="failed", detail="the runtime spawned name=models/chicken/chicken_roasted.vmdl")

    job = _run(db, build_command="rc -i bridge.vmdl -nop4", ingame_host="pomps-pc")
    assert job.status == "failed"

    with pytest.raises(pipeline.jobs.JobConflict, match="not verified"):
        pipeline.record_verdict(db, job.id, "pass", notes="it looked fine to me")
    assert pipeline.jobs.get(db, job.id).status == "failed"
    assert _human_confirms(db, job.id) == []


def test_verdict_refuses_a_job_with_no_ingame_stage(db, tmp_path, monkeypatch):
    """Not an asset-chain job: nothing was ever spawned, so there is nothing anybody could have seen."""
    from s2docs import jobs

    _addon(tmp_path, monkeypatch)
    _host(db)
    job = jobs.enqueue(
        db, kind="test", title="a plain job", target="probe_addon", host_id="desktop-pc",
        purpose="no chain here", expected="a closed job",
    )
    jobs.start(db, job.id, worker="test")
    jobs.finish(db, job.id, status="inconclusive", summary={
        "what": "nothing", "why": "test", "learned": "no ingame stage", "next": "none",
    })

    with pytest.raises(pipeline.jobs.JobConflict, match="no ingame stage"):
        pipeline.record_verdict(db, job.id, "pass")


def test_verdict_refuses_a_job_whose_ingame_stage_was_skipped_or_blocked(db, tmp_path, monkeypatch):
    """Skipped (no host) and blocked (no netcon) both mean nothing was on screen."""
    from s2docs.live.service import LiveError

    _addon(tmp_path, monkeypatch)
    _host(db)
    skipped = _run(db)  # no --ingame-host
    with pytest.raises(pipeline.jobs.JobConflict, match="ingame stage was skipped"):
        pipeline.record_verdict(db, skipped.id, "pass")

    _host(db, "pomps-pc")
    _patch_build(monkeypatch, tmp_path)
    _patch_ingame(monkeypatch, show_raises=LiveError("no netcon address for 'pomps-pc'"))
    blocked = _run(db, build_command="rc -i bridge.vmdl -nop4", ingame_host="pomps-pc")
    assert blocked.status == "inconclusive"
    with pytest.raises(pipeline.jobs.JobConflict, match="ingame stage was blocked"):
        pipeline.record_verdict(db, blocked.id, "fail")
    assert blocked.id and pipeline.jobs.get(db, blocked.id).status == "inconclusive"


def test_verdict_refuses_a_running_job(tmp_path, monkeypatch):
    """A running job's answer is `s2docs job verdict`; closing it here would skip the chain's close-out."""
    from s2docs import jobs
    from s2docs.config import get_settings
    from s2docs.db.connection import Database

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db = Database(get_settings().db_path)
    with db.writer():
        pass
    _host(db)
    job = jobs.enqueue(
        db, kind="test", title="still going", target="probe_addon", host_id="desktop-pc",
        purpose="running", expected="finish",
    )
    jobs.start(db, job.id, worker="test")
    # the readback can be recorded before the chain closes out — the status is what gates the verdict
    jobs.event(db, job.id, level="milestone", message="ingame verified", phase="ingame",
               data={"status": "verified", "host_id": "desktop-pc", "model": "models/bridge/bridge.vmdl"})

    with pytest.raises(pipeline.jobs.JobConflict, match="is running"):
        pipeline.record_verdict(db, job.id, "pass")
    assert jobs.get(db, job.id).status == "running"


def test_verdict_refuses_an_unknown_verdict_or_source(db, tmp_path, monkeypatch):
    job = _verified_chain(db, tmp_path, monkeypatch)

    with pytest.raises(pipeline.PipelineError, match="verdict must be one of pass, fail"):
        pipeline.record_verdict(db, job.id, "skip")  # `skip` is for steps, not for a look at the screen
    with pytest.raises(pipeline.PipelineError, match="verdict must be one of pass, fail"):
        pipeline.record_verdict(db, job.id, "looked fine")
    with pytest.raises(pipeline.PipelineError, match="source must be one of"):
        pipeline.record_verdict(db, job.id, "pass", source="vibes")
    assert pipeline.jobs.get(db, job.id).status == "inconclusive"


def test_cli_pipeline_verdict_closes_the_job_and_prints_the_chain(tmp_path, monkeypatch, capsys):
    import json as _json

    from s2docs import cli
    from s2docs.config import get_settings
    from s2docs.db.connection import Database

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    _addon(tmp_path, monkeypatch)
    get_settings.cache_clear()
    db = Database(get_settings().db_path)
    with db.writer():
        pass
    job = _verified_chain(db, tmp_path, monkeypatch)

    code = cli.main(["--json", "pipeline", "verdict", job.id, "pass", "--notes", "spawned, looks right"])

    assert code == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["status"] == "passed"
    assert [e["phase"] for e in payload["events"] if e["phase"]] == [*pipeline.STAGES, "verdict"]
    assert payload["events"][-1]["data"]["verdict"] == "pass"
    assert payload["events"][-1]["data"]["notes"] == "spawned, looks right"


def test_cli_pipeline_verdict_reports_a_conflict_and_an_unknown_job(tmp_path, monkeypatch, capsys):
    from s2docs import cli
    from s2docs.config import get_settings
    from s2docs.db.connection import Database

    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    _addon(tmp_path, monkeypatch)
    get_settings.cache_clear()
    db = Database(get_settings().db_path)
    with db.writer():
        pass
    job = _verified_chain(db, tmp_path, monkeypatch)
    pipeline.record_verdict(db, job.id, "pass")
    capsys.readouterr()

    conflict = cli.main(["pipeline", "verdict", job.id, "fail"])
    assert conflict == cli.EXIT_CONFLICT
    assert "already passed" in capsys.readouterr().err

    missing = cli.main(["pipeline", "verdict", "j-nope", "pass"])
    assert missing == cli.EXIT_NOT_FOUND


# ------------------------------------------------- text -> cs_script: the bundled verdict


USER_CODE = """function start(api) { api.msg("hello"); }\n"""


class _BundleClient:
    """The netcon half of the reload: canned console text per phase, and a record of what was sent."""

    def __init__(self, *, map_text: str = "@ Current  :  game\n1: de_dust2\n", load_text: str = ""):
        self.map_text, self.load_text = map_text, load_text
        self.sent: list[list[str]] = []

    def poll(self, lines, _probe, ready, *, timeout=90.0, **_kw):
        self.sent.append(list(lines))
        text = self.map_text if any(line.startswith("map ") for line in lines) else self.load_text
        return bool(ready(text)), text


def _patch_bundle(monkeypatch, seen: dict, *, deploy_status: str = "verified"):
    """Fake the two host halves of the bundle (compile + loose deploy); no ssh, no resourcecompiler."""
    from s2docs.build import script_compile

    def fake_compile(_db, host_id, *, addon, name, source, rel=None, **_kw):
        seen.setdefault("compile", []).append(
            {"host_id": host_id, "addon": addon, "name": name, "rel": rel,
             "source": Path(source).read_text(encoding="utf-8")}
        )
        artifact = Path(seen["dir"]) / f"bundle{len(seen['compile'])}.vjs_c"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"vjs_c")
        seen["artifact"] = artifact
        return script_compile.CompiledScript(
            run=_build_run(id=f"b-bundle{len(seen['compile'])}", host_id=host_id, status="succeeded",
                           exit_code=0),
            path=artifact, md5="b" * 32, size=5,
            remote="C:/cs2/game/csgo_addons/hermes_sandbox/maps/scripts/hermes_sandbox.vjs_c",
        )

    def fake_deploy(_db, host_id, *, addon, name, compiled, rel=None, **_kw):
        seen.setdefault("deploy", []).append({"host_id": host_id, "addon": addon, "rel": rel,
                                              "compiled": str(compiled)})
        return _deployment(id=f"d-bundle{len(seen['deploy'])}", status=deploy_status,
                           verified=deploy_status == "verified")

    monkeypatch.setattr(script_compile, "compile_js", fake_compile)
    monkeypatch.setattr(script_compile, "deploy_loose", fake_deploy)


def test_script_slice_verifies_the_verdict_by_readback_without_a_map_compile(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db, "pomps-pc")
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))
    seen: dict = {"dir": str(tmp_path)}
    _patch_bundle(monkeypatch, seen)
    client = _BundleClient(load_text="[user:pinger] ready n0nce\n[hs] pong 0\n")

    job = pipeline.run_script_slice(
        db, prompt="p", addon="probe_addon", name="pinger", host_id="pomps-pc",
        script_text=USER_CODE, run_host="pomps-pc", nonce="n0nce", client=client,
    )

    assert [e.phase for e in job.events if e.phase] == list(pipeline.SCRIPT_STAGES)
    # build/deploy are the BUNDLE (maps/scripts/hermes_sandbox), not the user's own scripts/pinger.js
    build = next(e for e in job.events if e.phase == "build")
    assert build.level == "milestone" and build.data["bundle"] is True
    assert build.data["target"] == "maps/scripts/hermes_sandbox"
    assert seen["compile"][0]["rel"] == "maps/scripts/hermes_sandbox"
    assert seen["compile"][0]["addon"] == "hermes_sandbox"
    assert '[user:" + "pinger" + "] ready " + "n0nce"' in seen["compile"][0]["source"]
    deploy = next(e for e in job.events if e.phase == "deploy")
    assert deploy.data["path"] == "maps/scripts/hermes_sandbox.vjs_c"
    assert deploy.data["status"] == "verified"
    verdict = next(e for e in job.events if e.phase == "verdict")
    assert verdict.level == "milestone" and verdict.data["status"] == "verified"
    assert verdict.data["nonce"] == "n0nce" and "[hs] pong" in verdict.data["readback"]
    # a readback is not a visual verdict: the job does not claim a pass
    assert job.status == "inconclusive" and "human" in job.summary.next


def test_script_slice_refuses_a_module_that_touches_instance_before_any_compile(db, tmp_path, monkeypatch):
    from s2docs.build import script_compile

    _addon(tmp_path, monkeypatch)
    _host(db, "pomps-pc")
    monkeypatch.setattr(script_compile, "compile_js", lambda *a, **kw: pytest.fail("must not compile"))

    job = pipeline.run_script_slice(
        db, prompt="p", addon="probe_addon", name="pinger", host_id="pomps-pc",
        script_text="function start(api) { Instance.Msg('x'); }\n", run_host="pomps-pc",
    )

    validate = next(e for e in job.events if e.phase == "validate")
    assert validate.level == "error" and validate.data["refused"]
    assert any("Instance" in r for r in validate.data["refused"])
    build = next(e for e in job.events if e.phase == "build")
    assert build.data["skipped"] is True and "validate reported" in build.data["reason"]
    verdict = next(e for e in job.events if e.phase == "verdict")
    assert verdict.data["status"] == "failed"
    assert job.status == "failed"


def test_script_slice_fails_the_verdict_when_no_ready_line_arrives(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db, "pomps-pc")
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))
    seen: dict = {"dir": str(tmp_path)}
    _patch_bundle(monkeypatch, seen)
    client = _BundleClient(load_text="[hs] pong 0\n")

    job = pipeline.run_script_slice(
        db, prompt="p", addon="probe_addon", name="pinger", host_id="pomps-pc",
        script_text=USER_CODE, run_host="pomps-pc", nonce="n0nce", client=client,
    )

    verdict = next(e for e in job.events if e.phase == "verdict")
    assert verdict.level == "error" and verdict.data["status"] == "failed"
    assert "bundled module did not" in verdict.data["detail"]
    assert job.status == "failed"


def test_script_slice_blocks_the_verdict_when_the_host_cannot_be_reached(db, tmp_path, monkeypatch):
    from s2docs.live import sandbox_run
    from s2docs.live.service import LiveError

    _addon(tmp_path, monkeypatch)
    _host(db, "pomps-pc")
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))
    seen: dict = {"dir": str(tmp_path)}
    _patch_bundle(monkeypatch, seen)

    def boom(*_a, **_kw):
        raise LiveError("no netcon address for 'pomps-pc'")

    monkeypatch.setattr(sandbox_run, "reload", boom)

    job = pipeline.run_script_slice(
        db, prompt="p", addon="probe_addon", name="pinger", host_id="pomps-pc",
        script_text=USER_CODE, run_host="pomps-pc", nonce="n0nce",
    )

    verdict = next(e for e in job.events if e.phase == "verdict")
    assert verdict.data["blocked"] is True and "netcon" in verdict.data["reason"]
    assert job.status == "inconclusive", "a block is not a failure: nothing was sent"


def test_script_slice_restores_the_stock_script_and_reports_it(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db, "pomps-pc")
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))
    seen: dict = {"dir": str(tmp_path)}
    _patch_bundle(monkeypatch, seen)

    # the restore's own reload sees the stock runtime: pong, no user line
    calls: list[str] = []

    class Client(_BundleClient):
        def poll(self, lines, probe, ready, *, timeout=90.0, **kw):
            calls.append("map" if any(line.startswith("map ") for line in lines) else "load")
            if len(calls) > 2:  # the restore's second phase: stock script only
                self.load_text = "[hs] pong 0\n"
            return super().poll(lines, probe, ready, timeout=timeout, **kw)

    job = pipeline.run_script_slice(
        db, prompt="p", addon="probe_addon", name="pinger", host_id="pomps-pc",
        script_text=USER_CODE, run_host="pomps-pc", nonce="n0nce", client=Client(
            load_text="[user:pinger] ready n0nce\n[hs] pong 1\n"),
        restore=True,
    )

    verdict = next(e for e in job.events if e.phase == "verdict")
    assert verdict.data["status"] == "verified"
    assert verdict.data["restore"]["status"] == "verified"
    # the restore compiled the STOCK script: a second build, and no user module in its source
    assert len(seen["compile"]) == 2
    assert 'hsUserApi("' not in seen["compile"][1]["source"]
    assert job.status == "inconclusive" and "restore verified" in job.summary.learned


def test_script_slice_without_a_run_host_still_blocks_the_verdict(db, tmp_path, monkeypatch):
    _addon(tmp_path, monkeypatch)
    _host(db)
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))

    job = pipeline.run_script_slice(db, prompt="p", addon="probe_addon", name="pinger",
                                    host_id="desktop-pc", script_text=USER_CODE)

    verdict = next(e for e in job.events if e.phase == "verdict")
    assert verdict.data["blocked"] is True
    assert "point_script" in verdict.data["reason"] and "Failed to load cs_script" in verdict.data["reason"]
    assert "--run-host" in verdict.data["reason"]
    assert job.status == "inconclusive"

def test_cli_runs_the_script_chain_against_a_run_host(tmp_path, monkeypatch, capsys):
    """The flags the coordinator uses: --run-host bundles, reloads and judges the verdict by readback."""
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
    _host(db, "pomps-pc")
    _patch_validate(monkeypatch, result=_fake_validation(ok=True))
    seen: dict = {"dir": str(tmp_path)}
    _patch_bundle(monkeypatch, seen)
    # the CLI builds its own client from live_hosts; a fake needs the reload seam, not netcon
    from s2docs.live import sandbox_run

    real_reload = sandbox_run.reload
    monkeypatch.setattr(
        sandbox_run, "reload",
        lambda db_, host, *, user, client=None, timeout_sec=90: real_reload(
            db_, host, user=user, timeout_sec=timeout_sec,
            client=_BundleClient(load_text=f"[user:pinger] ready {user.nonce}\n[hs] pong 0\n"),
        ),
    )
    script = tmp_path / "user.js"
    script.write_text(USER_CODE, encoding="utf-8")

    code = cli.main([
        "--json", "pipeline", "script",
        "--prompt", "a ping/pong script",
        "--addon", "probe_addon",
        "--name", "pinger",
        "--host", "pomps-pc",
        "--script", str(script),
        "--run-host", "pomps-pc",
        "--verdict-timeout", "5",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "inconclusive"
    verdict = next(e for e in payload["events"] if e["phase"] == "verdict")
    assert verdict["data"]["status"] == "verified" and verdict["data"]["nonce"]
