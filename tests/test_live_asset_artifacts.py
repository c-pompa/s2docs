"""Collecting a model compile's artifacts off the compile host, and putting them loose on a playing host.

Both halves are about bytes nobody would otherwise check: before 2026-10-27 a succeeded `resourcecompiler`
run wrote 6 files and the build record still said `artifacts: []`, and a pull that quietly truncated a
`.vmat_c` would have been indistinguishable from a good one. Every ssh/scp call is faked here; the live
readback is in the task report, not in the suite.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from s2docs import builds, hosts
from s2docs.db.connection import Database
from s2docs.live import asset_artifacts
from s2docs.live.service import LiveError
from s2docs.models import DeployStatus

CS2 = r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive"
ADDON = "probe_addon"
NAME = "tsr_eefa8331"
# The six files a real model compile wrote on desktop-pc (2026-10-27), sizes made up.
FILES: dict[str, bytes] = {
    f"models/{NAME}/{NAME}.vmdl_c": b"vmdl-compiled",
    f"materials/hammered/{NAME}/{NAME}.vmat_c": b"vmat-compiled",
    f"materials/hammered/{NAME}/{NAME}_color_png_c39ecddf.vtex_c": b"vtex-color",
    f"materials/hammered/{NAME}/{NAME}_normal_png_57f19f4d.vtex_c": b"vtex-normal",
    f"materials/hammered/{NAME}/{NAME}_ao_png_43e04446.vtex_c": b"vtex-ao",
    f"materials/hammered/{NAME}/{NAME}_vmat_g_tmetalness_ee87d4b4.vtex_c": b"vtex-metal",
}


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _set_facts(db: Database, host_id: str, facts: dict) -> None:
    with db.writer() as conn, conn:
        conn.execute("UPDATE hosts SET facts = ? WHERE id = ?", (json.dumps(facts), host_id))


@pytest.fixture
def db(tmp_path, monkeypatch) -> Database:
    monkeypatch.setenv("S2DOCS_DATA_DIR", str(tmp_path / "data"))
    from s2docs.config import get_settings

    get_settings.cache_clear()
    d = Database(tmp_path / "data" / "s2docs.db")
    hosts.register_host(d, "desktop-pc", "desktop-pc", shell="powershell")
    hosts.register_host(d, "pomps-pc", "pomps-pc", shell="powershell")
    for host_id in ("desktop-pc", "pomps-pc"):
        _set_facts(d, host_id, {"probe": {"cs2": {"path": CS2}}})
    with d.writer() as conn, conn:
        conn.execute(
            "INSERT INTO build_runs (id, host_id, kind, command, status, owner) VALUES (?,?,?,?,'succeeded',?)",
            ("b-test", "desktop-pc", "compile", "resourcecompiler -i probe.vmdl -nop4", "build:b-test"),
        )
    yield d
    get_settings.cache_clear()


def _remote(rel: str) -> str:
    """The path as the host's own tooling writes it (backslashes) — the listing and hash pass use these."""
    return f"{CS2}/game/csgo_addons/{ADDON}/{rel}".replace("/", "\\")


def _remote_posix(rel: str) -> str:
    """What the pull asks scp for: the same path, normalised — the compare is against the host's md5."""
    return f"{CS2}/game/csgo_addons/{ADDON}/{rel}".replace("\\", "/")


def _digest(payload: bytes) -> str:
    return hashlib.md5(payload).hexdigest()


def _fake_host(monkeypatch, files: dict[str, bytes] = FILES, *, missing: bool = False,
               corrupt: set[str] | None = None, scp_calls: list | None = None,
               truncate: set[str] | None = None):
    """Fake the three host calls: list, hash, and the scp pull that writes the bytes locally."""
    monkeypatch.setattr(
        asset_artifacts.deploys, "remote_files",
        lambda host, roots: {_remote(rel): len(payload) for rel, payload in files.items()},
    )

    def fake_md5(_host, paths):
        out = {}
        for path in paths:
            rel = next(r for r in files if path.replace("\\", "/").endswith(r))
            if missing:
                out[path] = "missing"
            elif corrupt and rel in corrupt:
                out[path] = _digest(files[rel] + b"-tampered")
            else:
                out[path] = _digest(files[rel])
        return out

    monkeypatch.setattr(asset_artifacts.deploys, "remote_md5", fake_md5)

    def fake_run(argv, **_kw):
        if scp_calls is not None:
            scp_calls.append(argv)
        remote = argv[-2].split(":", 1)[1].replace("\\", "/")
        rel = next(r for r in files if remote.endswith(r))
        payload = files[rel]
        Path(argv[-1]).write_bytes(payload[:2] if truncate and rel in truncate else payload)
        return _Result()

    monkeypatch.setattr(asset_artifacts.subprocess, "run", fake_run)


def test_collect_pulls_every_compiled_file_and_records_it_on_the_run(db, tmp_path, monkeypatch):
    calls: list = []
    _fake_host(monkeypatch, scp_calls=calls)

    collected = asset_artifacts.collect_model_artifacts(
        db, host_id="desktop-pc", addon=ADDON, name=NAME, run_id="b-test")

    # the six files, at their addon-relative paths — the vmat path is baked into the vmdl_c
    assert sorted(collected.files) == sorted(FILES)
    assert collected.root == tmp_path / "data" / "artifacts" / "b-test"
    for rel, payload in FILES.items():
        assert collected.files[rel] == collected.root / rel
        assert collected.files[rel].read_bytes() == payload

    # one scp per file, all from the addon's own tree, all back to this machine
    host = hosts.get_host(db, "desktop-pc")
    assert calls == [hosts.scp_pull_argv(host, _remote_posix(rel), collected.files[rel]) for rel in sorted(FILES)]

    # the run carries path + md5 + size, so `s2docs build show` can name what the compile produced
    run = builds.get_run(db, "b-test")
    recorded = {a.path: a for a in run.artifacts}
    assert set(recorded) == {collected.files[rel].as_posix() for rel in FILES}
    for rel, payload in FILES.items():
        assert recorded[collected.files[rel].as_posix()].md5 == _digest(payload)
        assert recorded[collected.files[rel].as_posix()].size == len(payload)


def test_collect_refuses_a_pull_whose_md5_does_not_match_the_host(db, tmp_path, monkeypatch):
    _fake_host(monkeypatch, truncate={f"materials/hammered/{NAME}/{NAME}.vmat_c"})

    with pytest.raises(LiveError, match="pulled md5"):
        asset_artifacts.collect_model_artifacts(db, host_id="desktop-pc", addon=ADDON, name=NAME,
                                                run_id="b-test")
    # a truncated artifact is not a build: nothing is recorded against the run
    assert builds.get_run(db, "b-test").artifacts == []


def test_collect_refuses_when_the_compile_produced_no_vmdl_c(db, monkeypatch):
    files = {rel: payload for rel, payload in FILES.items() if rel.endswith(".vtex_c")}
    calls: list = []
    _fake_host(monkeypatch, files, scp_calls=calls)

    with pytest.raises(LiveError, match="did not produce it"):
        asset_artifacts.collect_model_artifacts(db, host_id="desktop-pc", addon=ADDON, name=NAME,
                                                run_id="b-test")
    assert calls == [], "fail before copying anything"


def test_collect_refuses_a_file_that_vanished_between_listing_and_hashing(db, monkeypatch):
    calls: list = []
    _fake_host(monkeypatch, missing=True, scp_calls=calls)

    with pytest.raises(LiveError, match="vanished"):
        asset_artifacts.collect_model_artifacts(db, host_id="desktop-pc", addon=ADDON, name=NAME,
                                                run_id="b-test")
    assert calls == []


def test_collect_needs_a_probed_cs2_path(db, monkeypatch):
    _set_facts(db, "desktop-pc", {})
    with pytest.raises(LiveError, match="facts.probe.cs2.path"):
        asset_artifacts.collect_model_artifacts(db, host_id="desktop-pc", addon=ADDON, name=NAME,
                                                run_id="b-test")


def _collected(tmp_path) -> asset_artifacts.Collected:
    root = tmp_path / "artifacts" / "b-test"
    files = {}
    for rel, payload in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        files[rel] = path
    return asset_artifacts.Collected(run_id="b-test", root=root, files=files)


def _deployment(host_id="pomps-pc", status: DeployStatus = "verified", entries: int = 6):
    from s2docs.models import DeployEntry, Deployment

    return Deployment(id="d-loose", host_id=host_id, dest_root="x", status=status,
                      entries=[DeployEntry(path=f"p{i}", verified=status == "verified") for i in range(entries)],
                      created_at="2026-10-27T00:00:00Z", updated_at="2026-10-27T00:00:00Z")


def test_deploy_loose_pushes_the_same_relative_paths_into_game_csgo(db, tmp_path, monkeypatch):
    pushed: dict = {}
    verified: dict = {}

    def fake_push(db_, host_id, dest_root, files, **kw):
        pushed.update(host_id=host_id, dest_root=dest_root, files=[dict(f) for f in files], **kw)
        return _deployment(status="pushed")

    def fake_verify(db_, deploy_id):
        verified.update(id=deploy_id)
        return _deployment(status="verified")

    monkeypatch.setattr(asset_artifacts.deploys, "push", fake_push)
    monkeypatch.setattr(asset_artifacts.deploys, "verify", fake_verify)
    collected = _collected(tmp_path)

    deployment, failure = asset_artifacts.deploy_loose(
        db, collected, host_id="pomps-pc", addon=ADDON, owner="live:pipeline:j-1")

    assert failure is None and deployment.status == "verified" and verified["id"] == "d-loose"
    assert pushed["host_id"] == "pomps-pc"
    assert pushed["dest_root"] == CS2.replace("\\", "/") + "/game/csgo"
    # LOOSE at the addon's own paths: a renamed or flattened file would break the vmat path inside the vmdl_c
    assert [f["path"] for f in pushed["files"]] == sorted(FILES)
    assert [f["local"] for f in pushed["files"]] == [str(collected.files[rel].resolve()) for rel in sorted(FILES)]
    assert pushed["verify"] is False, "the readback is its own call, so a failure still leaves an id"
    assert pushed["owner"] == "live:pipeline:j-1"


def test_deploy_loose_reports_a_partial_copy_without_hiding_the_record(db, tmp_path, monkeypatch):
    monkeypatch.setattr(asset_artifacts.deploys, "push",
                        lambda *a, **kw: _deployment(status="failed", entries=3))
    monkeypatch.setattr(asset_artifacts.deploys, "verify",
                        lambda *a, **kw: pytest.fail("no readback for a copy that already failed"))

    deployment, failure = asset_artifacts.deploy_loose(db, _collected(tmp_path), host_id="pomps-pc")

    assert deployment is not None and deployment.id == "d-loose"
    assert "did not copy" in failure


def test_deploy_loose_keeps_the_id_when_the_readback_cannot_run(db, tmp_path, monkeypatch):
    from s2docs.deploys import DeployError

    monkeypatch.setattr(asset_artifacts.deploys, "push", lambda *a, **kw: _deployment(status="pushed"))
    monkeypatch.setattr(asset_artifacts.deploys, "verify",
                        lambda *a, **kw: (_ for _ in ()).throw(DeployError("Get-FileHash timed out")))

    deployment, failure = asset_artifacts.deploy_loose(db, _collected(tmp_path), host_id="pomps-pc")

    assert deployment is not None and "MD5 readback could not run" in failure


def test_deploy_loose_refuses_an_empty_collection(db):
    with pytest.raises(LiveError, match="no collected artifacts"):
        asset_artifacts.deploy_loose(db, asset_artifacts.Collected(run_id="b-test", root=Path("/tmp")),
                                     host_id="pomps-pc")
