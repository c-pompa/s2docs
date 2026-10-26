"""Build the `hermes_sandbox` helper map, then put the compiled files where CS2 loads them.

**Compiles run on desktop-pc only.** A de_dust2-scale compile hard-froze pomps-pc once already, and it is
the machine the user plays on; ``COMPILE_HOSTS`` makes that a refusal in code rather than a line in a doc.

The flow (Tasks 4.5 + 4.6):

1. render the addon sources into a staging dir on THIS machine (``sandbox_map.write_addon``);
2. push them to ``<CS2>/content/csgo_addons/hermes_sandbox`` on desktop-pc (MD5-verified by the deploy
   service), and start a server-owned ``resourcecompiler`` run over ssh (``builds.start``);
3. ``pull_compiled`` brings the two compiled files back to the Mac, checking each against the host's
   ``Get-FileHash`` — the deploy service pushes FROM the Mac, so the artifacts have to be here;
4. ``deploy_loose`` copies them to the *target* host's ``game/csgo`` tree. Loose files, not the addon:
   an addon-mounted script did not load in iso-spectate (``iso-spectate-view/docs/BACKLOG.md``).

Every ssh/scp argv comes from :mod:`s2docs.hosts` (``ssh_argv`` / ``scp_pull_argv``, and ``scp_argv``
inside :mod:`s2docs.deploys`), so the destination can never become an option.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from s2docs import builds, deploys, hosts
from s2docs import models as m
from s2docs.builds import md5_file
from s2docs.config import get_settings
from s2docs.db.connection import Database
from s2docs.live import sandbox_map, spawnables
from s2docs.live.service import LiveError

ADDON = "hermes_sandbox"
# pomps-pc hard-froze on a compile: nothing else may ever compile here.
COMPILE_HOSTS = frozenset({"desktop-pc"})
# What resourcecompiler writes, relative to <CS2>/game/csgo_addons/hermes_sandbox.
COMPILED = ("maps/hermes_sandbox.vpk", "maps/scripts/hermes_sandbox.vjs_c")
# ... and where the target host needs them, relative to <CS2>/game/csgo (loose, not addon-mounted).
LOOSE_ROOT = "game/csgo"
OWNER = f"live:{ADDON}"
SSH_TIMEOUT_SEC = 120
COMPILE_TIMEOUT_SEC = 900
DISPLAY_QUERY = "nvidia-smi --query-gpu=display_active --format=csv,noheader"


# ------------------------------------------------------------------ paths
def _posix(path: str) -> str:
    return path.replace("\\", "/").rstrip("/")


def _cs2_path(host: m.Host) -> str:
    """The host's CS2 install root (``hosts.cs2_install_path``: the probe's ``facts.probe.cs2.path``)."""
    try:
        return hosts.cs2_install_path(host)
    except hosts.HostError as exc:
        raise LiveError(str(exc)) from exc


def content_root(cs2: str) -> str:
    """Where the addon's *sources* go on the compile host (the deploy service's dest_root)."""
    return f"{_posix(cs2)}/content/csgo_addons/{ADDON}"


def addon_dir(cs2: str) -> str:
    """Where resourcecompiler writes the compiled files on the compile host."""
    return f"{_posix(cs2)}/game/csgo_addons/{ADDON}"


def game_root(cs2: str) -> str:
    return f"{_posix(cs2)}/game"


def staging_dir() -> Path:
    """Server-derived staging dir inside the data dir (mirrors the build log confinement)."""
    return get_settings().data_dir / "live_sandbox"


def _ps_literal(value: str) -> str:
    """A PowerShell single-quoted literal (no expansion; ' doubled)."""
    return "'" + value.replace("'", "''") + "'"


TASK_NAME = "hermes_sandbox_rc"
WORK_DIR = "C:\\iso_build"


def compile_script(cs2: str) -> str:
    """The PowerShell the compile host runs: both rc passes from a one-shot **Interactive** task.

    In ssh's session 0 resourcecompiler sees no display adapter ("nAdapterCount=0 ... software
    rasterizer") and the vmap pass access-violates — measured on desktop-pc 2026-10-25; the same run from
    an Interactive-principal task found the RTX 3080 and compiled 14/0. So this registers a hidden task
    running a generated .ps1 (log + done marker), deletes the task at once, waits for the marker, prints
    the log for the build record and exits with resourcecompiler's exit code. iso-spectate's
    compile_map.py uses the same session rule ('/IT').
    """
    base = cs2.rstrip("\\/")
    rc = base + "\\game\\bin\\win64\\resourcecompiler.exe"
    content = f"{base}\\content\\csgo_addons\\{ADDON}"
    js = content + "\\maps\\scripts\\hermes_sandbox.js"
    vmap = content + "\\maps\\hermes_sandbox.vmap"
    log, done, ps1 = (f"{WORK_DIR}\\{TASK_NAME}.{ext}" for ext in ("log", "done", "ps1"))
    inner = (
        "$ErrorActionPreference = 'Continue'\n"
        f"& {_ps_literal(rc)} -nop4 -i {_ps_literal(js)} *> {_ps_literal(log)}\n"
        "$a = $LASTEXITCODE\n"
        f"& {_ps_literal(rc)} -nop4 -i {_ps_literal(vmap)} *>> {_ps_literal(log)}\n"
        "$b = $LASTEXITCODE\n"
        f"\"exit $([Math]::Max([int]$a, [int]$b))\" | Set-Content -LiteralPath {_ps_literal(done)}\n"
    )
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"New-Item -ItemType Directory -Force -Path {_ps_literal(WORK_DIR)} | Out-Null",
        f"Remove-Item -LiteralPath {_ps_literal(done)},{_ps_literal(log)} -ErrorAction SilentlyContinue",
        f"Set-Content -LiteralPath {_ps_literal(ps1)} -Encoding UTF8 -Value {_ps_literal(inner)}",
        "$act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
        + _ps_literal(f"-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{ps1}\""),
        "$who = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive",
        f"Register-ScheduledTask -TaskName {_ps_literal(TASK_NAME)} -Action $act -Principal $who -Force | Out-Null",
        f"Start-ScheduledTask -TaskName {_ps_literal(TASK_NAME)}",
        "Start-Sleep -Seconds 3",
        f"Unregister-ScheduledTask -TaskName {_ps_literal(TASK_NAME)} -Confirm:$false",
        f"$deadline = (Get-Date).AddSeconds({COMPILE_TIMEOUT_SEC - 60})",
        f"while (-not (Test-Path -LiteralPath {_ps_literal(done)})) {{",
        "  if ((Get-Date) -gt $deadline) { Write-Output 'TIMEOUT waiting for the compile task'; exit 124 }",
        "  Start-Sleep -Seconds 2 }",
        f"$bytes = [IO.File]::ReadAllBytes({_ps_literal(log)})",
        "Write-Output ([Text.Encoding]::Unicode.GetString($bytes))",
        f"$code = [int](((Get-Content -LiteralPath {_ps_literal(done)}) -join '') -replace '[^0-9]', '')",
        "exit $code",
    ])


def compile_command(cs2: str) -> str:
    """``compile_script`` as one ``-EncodedCommand`` argument: no quoting survives to be re-split by ssh."""
    encoded = base64.b64encode(compile_script(cs2).encode("utf-16-le")).decode()
    return f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"


# ------------------------------------------------------------------ preflight
def display_gate(db: Database, host_id: str) -> bool:
    """True when the host has a display adapter (`display_active: Enabled`).

    resourcecompiler fails on an RDP-only session (no display adapter) — that was desktop-pc's earlier
    blocker — so the live run checks this before it starts anything.
    """
    host = hosts.get_host(db, host_id)
    argv = hosts.ssh_argv(host) + [DISPLAY_QUERY]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise LiveError(f"nvidia-smi on {host_id} timed out after 60s") from exc
    out = proc.stdout.replace("\r", "").strip()
    if proc.returncode != 0 or "Enabled" not in out:
        raise LiveError(
            f"{host_id} cannot compile: nvidia-smi display_active is {out[:80] or 'unreadable'} "
            f"(rc={proc.returncode}); compile from a console session, not RDP"
        )
    return True


# ------------------------------------------------------------------ 4.5 build
def build(db: Database, host_id: str, *, catalog: dict[str, spawnables.Spawnable] | None = None) -> m.BuildRun:
    """Render the addon, push its sources to the compile host and start the resourcecompiler run."""
    if host_id not in COMPILE_HOSTS:
        raise LiveError("compiles run on desktop-pc only (pomps-pc hard-froze on a compile)")
    host = hosts.get_host(db, host_id)
    cs2 = _cs2_path(host)

    staging = staging_dir()
    written = sandbox_map.write_addon(staging, catalog or spawnables.stock())
    deploys.push(
        db, host_id, content_root(cs2),
        [{"path": p.relative_to(staging).as_posix(), "local": str(p)} for p in written],
        addon=ADDON, notes="hermes_sandbox sources (generated by s2docs.live.sandbox_map)", owner=OWNER,
    )
    return builds.start(
        db, host_id, compile_command(cs2), kind="compile", timeout_sec=COMPILE_TIMEOUT_SEC,
        notes=f"map={ADDON}; addon={ADDON}; sources={','.join(p.name for p in written)}",
    )


def pull_compiled(db: Database, host_id: str, *, dest: Path | None = None) -> list[Path]:
    """Bring the two compiled files back to the Mac, each verified against the host's MD5."""
    host = hosts.get_host(db, host_id)
    cs2 = _cs2_path(host)
    out_dir = Path(dest) if dest else staging_dir() / "compiled"
    out_dir.mkdir(parents=True, exist_ok=True)
    remotes = [f"{addon_dir(cs2)}/{rel}" for rel in COMPILED]
    on_host = deploys.remote_md5(host, remotes)
    pulled: list[Path] = []
    for remote, rel in zip(remotes, COMPILED, strict=True):
        want = on_host.get(remote, "missing")
        if want == "missing":
            raise LiveError(f"{rel} is missing on {host_id}: the compile did not produce it (read the build log)")
        local = out_dir / Path(rel).name
        argv = hosts.scp_pull_argv(host, remote, local)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=SSH_TIMEOUT_SEC)
        except subprocess.TimeoutExpired as exc:
            raise LiveError(f"scp pull of {rel} from {host_id} timed out after {SSH_TIMEOUT_SEC}s") from exc
        if proc.returncode != 0:
            raise LiveError(
                f"scp pull of {rel} from {host_id} failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}"
            )
        if not local.is_file():
            raise LiveError(f"scp pull of {rel} from {host_id} reported success but wrote no file")
        got = md5_file(local)
        if got != want:
            raise LiveError(f"{rel}: pulled md5 {got} != the host's {want} (a truncated copy is not a build)")
        pulled.append(local)
    return pulled


# ------------------------------------------------------------------ 4.6 deploy loose
def deploy_loose(db: Database, host_id: str, *, pulled: list[Path] | None = None,
                 owner: str | None = OWNER) -> m.Deployment:
    """Copy the compiled files to the target host's loose ``game/csgo`` paths (MD5 both ends)."""
    host = hosts.get_host(db, host_id)
    cs2 = _cs2_path(host)
    files = [Path(p) for p in (pulled or [staging_dir() / "compiled" / Path(rel).name for rel in COMPILED])]
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise LiveError("no compiled hermes_sandbox files on this machine: run build + pull_compiled first ("
                        + ", ".join(missing) + ")")
    return deploys.push(
        db, host_id, f"{_posix(cs2)}/{LOOSE_ROOT}",
        [{"path": rel, "local": str(p.resolve())} for rel, p in zip(COMPILED, files, strict=True)],
        addon=ADDON, notes="hermes_sandbox loose files (an addon-mounted script does not load)", owner=owner,
    )
