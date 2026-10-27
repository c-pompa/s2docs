"""Keep a model compile's outputs, then put them where a running CS2 can load them.

Measured 2026-10-27 (desktop-pc -> pomps-pc, CS2 1.41.9.2/14192): one

    resourcecompiler -i <CS2>/content/csgo_addons/<addon>/models/<name>/<name>.vmdl -nop4

wrote six files under ``<CS2>/game/csgo_addons/<addon>/`` — ``models/<name>/<name>.vmdl_c``,
``materials/hammered/<name>/<name>.vmat_c`` and four ``.vtex_c`` (``_color_png_*``, ``_normal_png_*``,
``_ao_png_*``, ``_vmat_g_tmetalness_*``) — and the build record still said ``artifacts: []``. Nothing
collected them, so a succeeded compile was indistinguishable from a compile that wrote nothing.

The ``_c`` files bake their sibling **relative** paths (the ``vmdl_c`` names
``materials/hammered/<name>/<name>.vmat_c``), so artifacts are copied and deployed at the same relative
paths, never renamed and never flattened.

Two hops, both MD5-checked, because each one is a place bytes can silently disappear:

1. **host -> Mac** (:func:`collect_model_artifacts`): list the two trees with ``deploys.remote_files``,
   hash every file with one ``deploys.remote_md5`` pass, pull each with ``hosts.scp_pull_argv`` into
   ``<data_dir>/artifacts/<build_id>/<rel path>``, and compare the local MD5 to the host's. A mismatch
   is an error, not a retry: a truncated artifact is not a build. The collected files are attached to
   the run with ``builds.record_artifacts`` (path + md5 + size).
2. **Mac -> playing host** (:func:`deploy_loose`): ``deploys.push`` copies them under
   ``<CS2>/game/csgo`` (loose, not addon-mounted — measured: loose files load, an addon mount did not)
   and ``deploys.verify`` re-reads every MD5 over SSH.

The playing host then needs nothing but ``hs_model models/<name>/<name>.vmdl`` over netcon
(``s2docs.live.service.show_model``) — the same route the 2026-10-27 spike proved with a pixel diff.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from s2docs import builds, deploys, hosts
from s2docs import models as m
from s2docs.builds import md5_file
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.live.service import LiveError

#: Where resourcecompiler writes a compiled addon, relative to the CS2 install root.
ADDON_TREE = "game/csgo_addons"
#: Where the PLAYING host loads loose files from (same constant as ``sandbox_build.LOOSE_ROOT``).
LOOSE_ROOT = "game/csgo"
SSH_TIMEOUT_SEC = 120


def _posix(path: str) -> str:
    return path.replace("\\", "/").rstrip("/")


def _cs2_path(host: m.Host) -> str:
    """The host's CS2 install root (``hosts.cs2_install_path``), as a LiveError when it was never probed."""
    try:
        return hosts.cs2_install_path(host)
    except hosts.HostError as exc:
        raise LiveError(str(exc)) from exc


def addon_dir(cs2: str, addon: str) -> str:
    """Where the compile host writes this addon's compiled output."""
    return f"{_posix(cs2)}/{ADDON_TREE}/{addon}"


def artifact_roots(addon: str, name: str) -> tuple[str, ...]:
    """The two trees a model compile writes into, relative to the compiled addon dir.

    ``materials/hammered/<name>`` is where ``s2docs.asset.material`` puts the ``.vmat``, so it is derived
    from the asset stage rather than guessed from the ``.vmdl``: the compile only writes a material tree
    if the model references one.
    """
    return (f"models/{name}", f"materials/hammered/{name}")


def artifacts_dir(build_id: str) -> Path:
    """Where this build's artifacts are kept locally: inside the data dir, one folder per build."""
    return get_settings().data_dir / "artifacts" / build_id


@dataclass(frozen=True)
class Collected:
    """The compiled files of one model build: addon-relative path -> local copy on this machine."""

    run_id: str
    root: Path
    files: dict[str, Path] = field(default_factory=dict)

    def paths(self) -> list[str]:
        return [self.files[rel].as_posix() for rel in sorted(self.files)]


def collect_model_artifacts(
    db: Database,
    *,
    host_id: str,
    addon: str,
    name: str,
    run_id: str,
    dest: Path | None = None,
) -> Collected:
    """Pull a succeeded model build's compiled files off ``host_id`` and attach them to ``run_id``.

    Raises ``LiveError`` when the compile host has no ``<name>.vmdl_c`` (the compile wrote nothing, or
    it wrote into another addon), when a file disappears between listing and hashing, when scp fails,
    or when a pulled file's MD5 differs from the host's — every one of those means "these bytes are not
    the build", so none of them is retried silently.
    """
    host = hosts.get_host(db, host_id)
    cs2 = _cs2_path(host)
    prefix = addon_dir(cs2, addon)
    roots = [f"{prefix}/{rel}" for rel in artifact_roots(addon, name)]
    try:
        listing = deploys.remote_files(host, roots)
    except deploys.DeployError as exc:
        raise LiveError(str(exc)) from exc

    files_rel: dict[str, str] = {}
    for raw in sorted(listing):
        path = _posix(raw)
        if not path.startswith(prefix + "/"):
            raise LiveError(f"{host_id} listed {path!r}, which is outside the addon dir {prefix!r}")
        files_rel[path[len(prefix) + 1 :]] = path

    required = f"models/{name}/{name}.vmdl_c"
    if required not in files_rel:
        raise LiveError(
            f"{required} is not on {host_id} under {prefix}: the compile did not produce it "
            f"(found {len(files_rel)} other file(s); read the build log)"
        )

    # One Get-FileHash pass over everything, before a single scp: a file the hash pass calls 'missing'
    # would otherwise be copied and re-read for nothing.
    try:
        on_host = deploys.remote_md5(host, [files_rel[rel] for rel in sorted(files_rel)])
    except deploys.DeployError as exc:
        raise LiveError(str(exc)) from exc

    root = Path(dest) if dest else artifacts_dir(run_id)
    local_files: dict[str, Path] = {}
    for rel in sorted(files_rel):
        remote = files_rel[rel]
        want = on_host.get(remote, "missing")
        if want == "missing":
            raise LiveError(f"{rel} vanished from {host_id} between the listing and the hash pass")
        local = root / rel
        local.parent.mkdir(parents=True, exist_ok=True)
        argv = hosts.scp_pull_argv(host, remote, local)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=SSH_TIMEOUT_SEC)
        except subprocess.TimeoutExpired as exc:
            raise LiveError(f"scp pull of {rel} from {host_id} timed out after {SSH_TIMEOUT_SEC}s") from exc
        if proc.returncode != 0:
            raise LiveError(f"scp pull of {rel} from {host_id} failed (rc={proc.returncode}): "
                            f"{proc.stderr.strip()[:300]}")
        if not local.is_file():
            raise LiveError(f"scp pull of {rel} from {host_id} reported success but wrote no file")
        got = md5_file(local)
        if got != want:
            raise LiveError(
                f"{rel}: pulled md5 {got} != the host's {want} (a truncated artifact is not a build)"
            )
        local_files[rel] = local

    collected = Collected(run_id=run_id, root=root, files=local_files)
    builds.record_artifacts(db, run_id, collected.paths())
    return collected


def deploy_loose(
    db: Database,
    collected: Collected,
    *,
    host_id: str,
    addon: str | None = None,
    owner: str | None = None,
) -> tuple[m.Deployment | None, str | None]:
    """Copy collected artifacts to ``host_id``'s loose ``game/csgo`` tree and verify: ``(deployment, failure)``.

    Same shape as the pipeline's addon deploy: ``push(verify=False)`` then a separate ``deploys.verify``,
    so a failed readback still leaves one deployment id to link instead of only an exception string.
    """
    if not collected.files:
        raise LiveError("no collected artifacts to deploy: run collect_model_artifacts first")
    host = hosts.get_host(db, host_id)
    cs2 = _cs2_path(host)
    files = [{"path": rel, "local": str(collected.files[rel].resolve())} for rel in sorted(collected.files)]
    try:
        deployment = deploys.push(
            db, host_id, f"{_posix(cs2)}/{LOOSE_ROOT}", files, addon=addon, verify=False, owner=owner,
            notes="compiled model + material artifacts, loose (the vmat path is baked into the vmdl_c)",
        )
    except (deploys.DeployError, hosts.HostError) as exc:
        return None, str(exc)
    if deployment.status == "failed":  # partial copy: the record carries the per-file errors
        return deployment, "some files did not copy; see the deployment record for the per-file errors"
    try:
        deployment = deploys.verify(db, deployment.id)
    except deploys.DeployError as exc:  # no verdict, but the id still stands
        return deployment, f"MD5 readback could not run: {exc}"
    return deployment, None
