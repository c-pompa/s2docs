"""Start CS2 on a host over ssh, then wait for its netcon to answer.

Why a scheduled task (``references/live-cs2-control.md``): an ``ssh <host> cs2.exe`` process is spawned from
a service session, so the game has no desktop and never appears for the user. The proven recipe registers a
one-shot task with an **interactive** principal, starts it and deletes it again. The script is handed to
PowerShell on **stdin** (``-Command -``), never as an inline ``-Command "…"`` string: quoting a path like
``C:\\Program Files (x86)\\…`` through two shells is where launches used to break.

The map name is the only user-supplied text, and it is restricted to ``^de_[a-z0-9_]+$`` before it reaches
anything, so ``+map`` cannot carry a second command.
"""

from __future__ import annotations

import base64
import re
import subprocess
import time
from collections.abc import Callable

from s2docs import hosts
from s2docs.db.connection import Database
from s2docs.live import capabilities, service
from s2docs.live.netcon import NetconClient, NetconError

MAP_RE = re.compile(r"^de_[a-z0-9_]+$")
TASK_NAME = "hermes_cs2_launch"
DEFAULT_NETCON_PORT = 2121
# Same shape the iso-spectate launch used: retail, windowed, console log on, netcon for the actions.
LAUNCH_ARGS = "-steam -insecure -novid -windowed -w 1920 -h 1080 -netconport {port} -condebug"
LAUNCH_TIMEOUT_SEC = 120.0
POLL_SEC = 2.0
SSH_TIMEOUT_SEC = 60


class LaunchError(ValueError):
    """A request that cannot be launched at all (a bad map, no CS2 path on the host)."""


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def script(cs2_exe: str, map_name: str, *, port: int = DEFAULT_NETCON_PORT) -> str:
    """The PowerShell that starts the game as the logged-in user and removes the task again.

    Mirrors the recipe measured on pomps-pc (S0, P2): Interactive principal, no RunLevel (an elevated
    cs2.exe next to a non-elevated Steam is a different, unmeasured setup). Never assign a PowerShell
    automatic variable here: `$args = ...` silently became an array and broke -Argument (live 2026-10-25).
    """
    cs2_args = LAUNCH_ARGS.format(port=port) + f" +map {map_name}"
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$exe = {_ps_quote(cs2_exe)}",
        f"$cs2Args = {_ps_quote(cs2_args)}",
        f"$name = {_ps_quote(TASK_NAME)}",
        "$action = New-ScheduledTaskAction -Execute $exe -Argument $cs2Args"
        " -WorkingDirectory (Split-Path -LiteralPath $exe)",
        "$principal = New-ScheduledTaskPrincipal"
        " -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)"
        " -LogonType Interactive",
        "Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Force | Out-Null",
        "try { Start-ScheduledTask -TaskName $name; Start-Sleep -Seconds 2 }",
        "finally { Unregister-ScheduledTask -TaskName $name -Confirm:$false }",
        "Write-Output 'launched'",
        "",
    ])


def _cs2_exe(db: Database, host_id: str) -> str:
    host = hosts.get_host(db, host_id)
    try:
        cs2 = hosts.cs2_install_path(host)
    except hosts.HostError as exc:
        raise service.LiveError(str(exc)) from exc
    return cs2.rstrip("\\/") + "\\game\\bin\\win64\\cs2.exe"


def launch(
    db: Database,
    host_id: str,
    map_name: str,
    *,
    timeout_sec: float = LAUNCH_TIMEOUT_SEC,
    poll_sec: float = POLL_SEC,
    client_factory: Callable[[], NetconClient] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Launch ``map_name`` on ``host_id`` and poll netcon until it answers or ``timeout_sec`` runs out.

    Refuses (``LiveError``, a 409) while a test run owns the host, and refuses (``LaunchError``, a 400) a map
    that is not ``de_*`` or a host with no CS2 path — the last two before anything is sent anywhere.
    """
    if not isinstance(map_name, str) or not MAP_RE.fullmatch(map_name):
        raise LaunchError(f"map must match {MAP_RE.pattern}, got {map_name!r}")
    service._check_lease(db, host_id)  # noqa: SLF001 - the one lease rule lives in the service
    exe = _cs2_exe(db, host_id)
    host = hosts.get_host(db, host_id)

    # -EncodedCommand, never `-Command -` with the script on stdin: stdin is parsed line by line, so the
    # try/finally below fails to parse, the task is registered but never started or removed, and ssh still
    # exits 0 (live on pomps-pc 2026-10-25). The encoded form is parsed whole, like hosts.probe_host's.
    encoded = base64.b64encode(script(exe, map_name).encode("utf-16-le")).decode()
    argv = hosts.ssh_argv(host) + [f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=SSH_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        raise service.LiveError(f"launching CS2 on {host_id} timed out after {SSH_TIMEOUT_SEC}s") from exc
    if proc.returncode != 0:
        raise service.LiveError(f"launching CS2 on {host_id} failed (rc={proc.returncode}): "
                                f"{(proc.stderr or proc.stdout).strip()[:300]}")
    if "launched" not in (proc.stdout or ""):
        raise service.LiveError(f"the launch script on {host_id} did not confirm it started CS2: "
                                f"{(proc.stderr or proc.stdout or 'no output').strip()[:300]}")

    deadline = time.monotonic() + max(0.0, timeout_sec)
    client = (client_factory or (lambda: service.client_for(db, host_id)))()
    while True:
        try:
            reply = client.run(["status"], drain=0.6, settle=0.3)
        except NetconError:
            reply = ""
        if reply:
            try:
                build: str | None = capabilities.cs2_version(reply)
            except capabilities.CapabilityError:
                build = None
            return {"host_id": host_id, "map": map_name, "netcon": True, "cs2_build": build,
                    "detail": "netcon answered"}
        if time.monotonic() >= deadline:
            raise service.LiveError(f"CS2 is not answering on {host_id}'s netcon after {timeout_sec:.0f}s; "
                                    "check that the game window is up and -netconport was accepted")
        sleep(poll_sec)
