"""Compile one cs_script ``.js`` into the ``.vjs_c`` the engine loads — and nothing else.

resourcecompiler is a single binary with two very different modes, and they cost different things:

* a **map** (``.vmap``) compile needs a display adapter and an active session, and it hard-froze pomps-pc
  (see :mod:`s2docs.live.sandbox_build`), so ``sandbox_build.COMPILE_HOSTS`` stays desktop-pc only;
* a **cs_script** (``.js``) compile takes about a second, needs no display, and works from ssh's session 0
  on both hosts — measured on pomps-pc 2026-10-27.

So this module has its own, deliberately narrow host policy (``SCRIPT_COMPILE_HOSTS``) and refuses any
input that is not exactly one ``.js`` file: a ``.vmap``, ``.vmdl`` or a directory handed to this path
would run the expensive mode on a host that must never run it.

The flow (the same one the map build proves):

1. push the ``.js`` to ``<CS2>/content/csgo_addons/<addon>/scripts/<name>.js`` (MD5-verified by the
   deploy service — the compile host reads only what is on the host);
2. start a server-owned run (:func:`s2docs.builds.start`) whose PowerShell runs resourcecompiler on that
   ONE file. A run is only a success when the compiler exits 0 **and** the ``.vjs_c`` exists with a
   ``LastWriteTime`` at or after this run's start: rc can exit 0 after a crash, and a leftover file from
   an earlier compile must never be reported as fresh;
3. pull the ``.vjs_c`` back to the data dir, checking it against the host's ``Get-FileHash`` MD5 (the
   deploy service pushes FROM this machine, so the artifact has to be here), and attach it to the run
   with :func:`s2docs.builds.record_artifacts`.

Every ssh/scp argv comes from :mod:`s2docs.hosts` (``ssh_argv``, ``scp_pull_argv``) and every PowerShell
command from ``hosts.powershell_command`` — the ``cmd.exe`` line limit on pomps-pc is one of the reasons
that builder exists.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from s2docs import builds, deploys, hosts
from s2docs import models as m
from s2docs.builds import md5_file
from s2docs.config import get_settings
from s2docs.db.connection import Database

#: Hosts where a cs_script ``.js`` compile is allowed. Both of them: the script pass is ~1 s, needs no
#: display adapter and runs from ssh's session 0 (measured on pomps-pc 2026-10-27). This is NOT the map
#: policy — ``sandbox_build.COMPILE_HOSTS`` (vmap) stays desktop-pc only, and this module refuses to
#: compile anything that is not a single ``.js`` so the two can never be confused.
SCRIPT_COMPILE_HOSTS = frozenset({"desktop-pc", "pomps-pc"})

#: The compiler's output for ``scripts/<name>.js``, relative to the addon's ``game/csgo_addons/<addon>``.
SCRIPT_SUFFIX = ".vjs_c"

#: A script name this path accepts: addon-safe, no dot, no separator — so ``scripts/<name>.js`` is the
#: only file the compiler can ever be pointed at.
SCRIPT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: Printed by the remote PowerShell only when rc exited 0 AND the ``.vjs_c`` is fresh. Exit status alone
#: is not proof: a crashed compiler can still exit 0.
SUCCESS_TOKEN = "SCRIPT COMPILE OK"

SSH_TIMEOUT_SEC = 120
DATA_SUBDIR = "scripts"


class ScriptCompileError(RuntimeError):
    """The script compile path was asked to do something it does not do (host, input, artifact).

    ``run_id`` is set when a build run existed (so the caller can still link/read it), even though the
    run did not produce a fresh artifact.
    """

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class ScriptNotFresh(ScriptCompileError):
    """The run finished without producing a ``.vjs_c`` written by *this* run (stale or missing)."""


@dataclass(frozen=True)
class CompiledScript:
    """A compiled cs_script: the run that made it, the local copy, and its identity."""

    run: m.BuildRun
    path: Path
    md5: str
    size: int
    remote: str


# --------------------------------------------------------------------- policy
def check_script_source(source: Path) -> Path:
    """Refuse anything that is not exactly one ``.js`` file — a single file, not a directory.

    This is the guard that keeps the cheap script pass from ever being used as a back door to the
    expensive map pass: ``.vmap``/``.vmdl`` compiles need a display adapter and froze pomps-pc.
    """
    path = Path(source)
    if path.is_dir():
        raise ScriptCompileError(
            f"refusing to compile {path}: a directory is not a script; this path compiles ONE .js file"
        )
    if path.suffix.lower() != ".js":
        raise ScriptCompileError(
            f"refusing to compile {path.name!r}: only a cs_script .js is compiled here "
            "(a .vmap/.vmdl compile needs a display adapter and runs on desktop-pc only, "
            "via live.sandbox_build)"
        )
    if not path.is_file():
        raise ScriptCompileError(f"script source {path} does not exist")
    return path


def check_host(host_id: str) -> str:
    """The host policy: a ``.js`` compile is allowed on desktop-pc and pomps-pc, nowhere else."""
    if host_id not in SCRIPT_COMPILE_HOSTS:
        raise ScriptCompileError(
            f"cs_script compiles run on {', '.join(sorted(SCRIPT_COMPILE_HOSTS))} only (got {host_id!r}); "
            "vmap compiles are a different path (live.sandbox_build, desktop-pc only)"
        )
    return host_id


def check_name(name: str) -> str:
    """The script name must be addon-safe so it can only ever name ``scripts/<name>.js``."""
    if not isinstance(name, str) or not SCRIPT_NAME_RE.match(name):
        raise ScriptCompileError(
            f"invalid script name {name!r}: lowercase [a-z][a-z0-9_]*, i.e. one .js file under scripts/"
        )
    return name


# ---------------------------------------------------------------------- paths
def _posix(path: str) -> str:
    return path.replace("\\", "/").rstrip("/")


def source_rel(name: str) -> str:
    return f"scripts/{name}.js"


def compiled_rel(name: str) -> str:
    return f"scripts/{name}{SCRIPT_SUFFIX}"


def content_dir(cs2: str, addon: str) -> str:
    """Where the *source* is pushed on the compile host."""
    return f"{_posix(cs2)}/content/csgo_addons/{addon}"


def addon_game_dir(cs2: str, addon: str) -> str:
    """Where resourcecompiler writes the compiled file on the compile host."""
    return f"{_posix(cs2)}/game/csgo_addons/{addon}"


def loose_root(cs2: str) -> str:
    """The *target* host's loose tree (``game/csgo``), where the engine loads a script from."""
    return f"{_posix(cs2)}/game/csgo"


def pull_dir(addon: str) -> Path:
    """Server-derived local dir for pulled artifacts, inside the data dir (never a caller's path)."""
    return get_settings().data_dir / DATA_SUBDIR / addon


def _ps_literal(value: str) -> str:
    """A PowerShell single-quoted literal (no expansion; ' doubled)."""
    return "'" + value.replace("'", "''") + "'"


# -------------------------------------------------------------------- compile
def compile_script(cs2: str, addon: str, name: str) -> str:
    """The PowerShell the compile host runs: one ``rc -nop4 -i <the .js>`` plus a freshness gate.

    No scheduled task and no display check: a script compile needs neither (measured on pomps-pc
    2026-10-27, from ssh's session 0). What it does need is proof the file is NEW — rc exits 0 even
    after an access violation, and an unchanged ``.vjs_c`` from an earlier compile would otherwise be
    pulled back as if this run produced it.
    """
    base = cs2.rstrip("\\/")
    rc = base + "\\game\\bin\\win64\\resourcecompiler.exe"
    js = f"{base}\\content\\csgo_addons\\{addon}\\scripts\\{name}.js"
    out = f"{base}\\game\\csgo_addons\\{addon}\\scripts\\{name}{SCRIPT_SUFFIX}"
    log = f"{base}\\game\\csgo_addons\\{addon}\\scripts\\{name}.rc.log"
    return "\n".join([
        "$ErrorActionPreference = 'Continue'",
        f"$rc = {_ps_literal(rc)}",
        f"$src = {_ps_literal(js)}",
        f"$out = {_ps_literal(out)}",
        f"$log = {_ps_literal(log)}",
        "if (-not (Test-Path -LiteralPath $src)) { Write-Output ('source missing: ' + $src); exit 2 }",
        # The output directory is created by the compiler only on some paths; a redirect into a missing
        # directory fails BEFORE rc runs (measured 2026-10-27), so create it explicitly.
        "$outDir = Split-Path -Parent $out",
        "[void][System.IO.Directory]::CreateDirectory($outDir)",
        # The run's start time: anything older is a leftover, not this run's output.
        "$started = Get-Date",
        "& $rc -nop4 -i $src *> $log",
        "$code = $LASTEXITCODE",
        # rc writes its log as UTF-16 (with or without a BOM); decode what actually landed.
        "if (Test-Path -LiteralPath $log) {",
        "  $bytes = [IO.File]::ReadAllBytes($log)",
        "  $text = if ($bytes.Length -ge 2 -and ($bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE)) {",
        "    [Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2)",
        "  } elseif ($bytes.Length -ge 2 -and $bytes[1] -eq 0) {",
        "    [Text.Encoding]::Unicode.GetString($bytes)",
        "  } else { [Text.Encoding]::UTF8.GetString($bytes) }",
        "  Write-Output $text",
        "}",
        # No exit code at all means the compiler never ran (a bad invocation, not a failed compile).
        "if ($null -eq $code) { Write-Output 'resourcecompiler did not run (no exit code)'; exit 1 }",
        "Write-Output ('resourcecompiler exit ' + $code)",
        "if ($code -ne 0) { exit 1 }",
        f"if (-not (Test-Path -LiteralPath $out)) {{ Write-Output 'NO {SCRIPT_SUFFIX} WAS WRITTEN'; exit 3 }}",
        "if ((Get-Item -LiteralPath $out).LastWriteTime -lt $started) {",
        f"  Write-Output 'STALE {SCRIPT_SUFFIX}: it predates this run (rc exited 0 without compiling)'; exit 3 }}",
        f"Write-Output ('{SUCCESS_TOKEN} ' + $out)",
        "exit 0",
    ])


def compile_command(cs2: str, addon: str, name: str) -> str:
    """``compile_script`` as one ``-EncodedCommand`` argument (the only PowerShell builder)."""
    return hosts.powershell_command(compile_script(cs2, addon, name))


def _cs2(host: m.Host) -> str:
    try:
        return hosts.cs2_install_path(host)
    except hosts.HostError as exc:
        raise ScriptCompileError(str(exc)) from exc


def _log_text(db: Database, run: m.BuildRun) -> str:
    """The run's log text through the build service (it decodes resourcecompiler's UTF-16)."""
    return "\n".join(builds.log_tail(db, run.id, lines=0).lines)


def push_source(db: Database, host_id: str, *, addon: str, name: str, source: Path,
                owner: str | None = None) -> m.Deployment:
    """Copy the ``.js`` to the compile host's content tree, MD5-verified before anything compiles."""
    source = check_script_source(source)
    host = hosts.get_host(db, host_id)
    cs2 = _cs2(host)
    try:
        deployment = deploys.push(
            db, host_id, content_dir(cs2, addon),
            [m.DeployFile(path=source_rel(name), local=str(source.resolve()))],
            addon=addon, notes=f"cs_script source for {addon}/scripts/{name}.js", owner=owner,
        )
    except (deploys.DeployError, hosts.HostError) as exc:
        raise ScriptCompileError(f"pushing {name}.js to {host_id} failed: {exc}") from exc
    if deployment.status != "verified":
        raise ScriptCompileError(
            f"pushing {name}.js to {host_id} did not verify (status {deployment.status!r}); "
            "the host does not have the source this run would compile"
        )
    return deployment


def pull_compiled(db: Database, host_id: str, *, addon: str, name: str,
                  dest: Path | None = None) -> tuple[Path, str, str]:
    """Bring the ``.vjs_c`` back, checking it against the host's MD5: ``(local, md5, remote)``."""
    host = hosts.get_host(db, host_id)
    remote = f"{addon_game_dir(_cs2(host), addon)}/{compiled_rel(name)}"
    on_host = deploys.remote_md5(host, [remote])
    want = on_host.get(remote, "missing")
    if want == "missing":
        raise ScriptNotFresh(
            f"{compiled_rel(name)} is missing on {host_id}: the compile produced no artifact (read the build log)"
        )
    out_dir = Path(dest) if dest else pull_dir(addon)
    out_dir.mkdir(parents=True, exist_ok=True)
    local = out_dir / f"{name}{SCRIPT_SUFFIX}"
    argv = hosts.scp_pull_argv(host, remote, local)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=SSH_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        raise ScriptCompileError(f"scp pull of {compiled_rel(name)} from {host_id} timed out") from exc
    if proc.returncode != 0:
        raise ScriptCompileError(
            f"scp pull of {compiled_rel(name)} from {host_id} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:300]}"
        )
    if not local.is_file():
        raise ScriptCompileError(f"scp pull of {compiled_rel(name)} reported success but wrote no file")
    got = md5_file(local)
    if got != want:
        raise ScriptCompileError(
            f"{compiled_rel(name)}: pulled md5 {got} != the host's {want} (a truncated copy is not a build)"
        )
    return local, got, remote


def compile_js(
    db: Database,
    host_id: str,
    *,
    addon: str,
    name: str,
    source: Path,
    timeout_sec: int = 120,
    poll_sec: float = 1.0,
    owner: str | None = None,
) -> CompiledScript:
    """Push, compile, prove fresh, pull and record one ``.js`` — the build half of the script chain.

    Raises :class:`ScriptCompileError` (or :class:`ScriptNotFresh`) instead of returning a run that did
    not produce a fresh artifact; the caller records what happened, it does not have to guess.
    """
    source = check_script_source(source)
    check_name(name)
    check_host(host_id)
    host = hosts.get_host(db, host_id)
    cs2 = _cs2(host)

    push_source(db, host_id, addon=addon, name=name, source=source, owner=owner)

    try:
        run = builds.start(
            db, host_id, compile_command(cs2, addon, name), kind="compile", timeout_sec=timeout_sec,
            notes=f"script={addon}/{source_rel(name)}",
        )
    except (builds.BuildError, hosts.HostError) as exc:
        raise ScriptCompileError(f"the compile run could not start on {host_id}: {exc}") from exc

    try:
        run = builds.wait(db, run.id, timeout_sec=timeout_sec, poll_sec=poll_sec)
    except builds.BuildTimeout:
        run = builds.get_run(db, run.id)
        raise ScriptCompileError(
            f"the compile run {run.id} on {host_id} is still {run.status} after {timeout_sec}s; "
            "nothing is claimed about its output"
        ) from None

    if run.status != "succeeded":
        raise ScriptCompileError(
            f"resourcecompiler on {host_id} failed for scripts/{name}.js (run {run.id}, "
            f"exit {run.exit_code}); log: {run.log_path}",
            run_id=run.id,
        )
    if SUCCESS_TOKEN not in _log_text(db, run):
        # exit 0 without the token: no fresh .vjs_c, so this run proved nothing
        raise ScriptNotFresh(
            f"run {run.id} exited 0 but printed no '{SUCCESS_TOKEN}' line on {host_id}: "
            f"no fresh {SCRIPT_SUFFIX} was proven (log: {run.log_path})",
            run_id=run.id,
        )

    try:
        local, digest, remote = pull_compiled(db, host_id, addon=addon, name=name)
    except ScriptCompileError as exc:
        raise type(exc)(str(exc), run_id=run.id) from exc
    builds.record_artifacts(db, run.id, [str(local)])
    return CompiledScript(run=builds.get_run(db, run.id), path=local, md5=digest,
                          size=local.stat().st_size, remote=remote)


def deploy_loose(
    db: Database,
    host_id: str,
    *,
    addon: str,
    name: str,
    compiled: Path,
    owner: str | None = None,
) -> m.Deployment:
    """Copy the ``.vjs_c`` LOOSE to ``<CS2>/game/csgo/scripts/<name>.vjs_c`` and MD5-verify it.

    Loose, not addon-mounted: an addon-mounted script did not load in iso-spectate, and the engine's
    script path is ``scripts/<name>.vjs`` under ``game/csgo``.
    """
    check_name(name)
    path = Path(compiled)
    if not path.is_file():
        raise ScriptCompileError(f"no compiled {name}{SCRIPT_SUFFIX} at {path}: compile and pull it first")
    host = hosts.get_host(db, host_id)
    cs2 = _cs2(host)
    try:
        return deploys.push(
            db, host_id, loose_root(cs2),
            [m.DeployFile(path=compiled_rel(name), local=str(path.resolve()))],
            addon=addon, notes=f"cs_script {name}{SCRIPT_SUFFIX} (loose: game/csgo/scripts)", owner=owner,
        )
    except (deploys.DeployError, hosts.HostError) as exc:
        raise ScriptCompileError(f"deploying {name}{SCRIPT_SUFFIX} to {host_id} failed: {exc}") from exc
