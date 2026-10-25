"""Per-CS2-build record of which experimental capabilities were measured to work (§BP rule 5).

The key is the version string netcon's `status` already prints ('1.41.9.2/14192'), not a steam.inf
integer: registered hosts have no `facts.cs2_path`, and a host's sshd can flap while netcon keeps
answering. Nothing here talks to a host — the caller passes the `status` text it already has.
"""

from __future__ import annotations

import re

from s2docs.db.connection import Database

NAMES = {
    "bots_team_switch": "S0: bot_add_t/ct + jointeam put the human on T with 4 T + 5 CT bots",
    "console_chicken": "S1: ent_create chicken from the console renders on stock de_dust2",
    "runtime_precache": "S2 (Task 4.9): Instance.Precache + SetModel renders a model the map did not ship",
    "script_reload": "S3 (Task 9.1): script_reload picks up a redeployed .vjs_c in a running game",
    "world_text": "S4 (Task 10.6): a point_worldtext spawned from a template shows its SetMessage text",
    "hud_clicks": "S5 (Task 10.7): a real mouse click on a custom_hud_layout Button fires OnCustomHudClicked",
    "offscreen_panel": "S6 (deferred): info_offscreen_panorama_texture renders a layout on a surface",
}
_VERSION = re.compile(r"^version\s*:\s*(\S+)", re.M)


class CapabilityError(ValueError):
    """A capability is not proven for this CS2 build, or the build cannot be read."""


def cs2_version(status_text: str) -> str:
    """The build key from netcon `status` output, e.g. '1.41.9.2/14192'."""
    m = _VERSION.search(status_text)
    if not m:
        raise CapabilityError("no 'version :' line in status output (is CS2 in a map?)")
    return m.group(1)


def record(db: Database, build: str, name: str, status: str, evidence: str) -> None:
    if name not in NAMES:
        raise ValueError(f"unknown capability {name!r}; known: {', '.join(NAMES)}")
    if status not in ("pass", "fail"):
        raise ValueError("status must be pass or fail")
    with db.writer() as conn, conn:
        conn.execute("INSERT INTO live_capabilities (cs2_build, name, status, evidence) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT(cs2_build, name) DO UPDATE SET status = excluded.status,"
                     " evidence = excluded.evidence, checked_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')",
                     (build, name, status, evidence[:2000]))


def table(db: Database, build: str) -> dict[str, dict]:
    with db.reader() as conn:
        rows = conn.execute("SELECT name, status, evidence, checked_at FROM live_capabilities WHERE cs2_build = ?",
                            (build,)).fetchall()
    have = {r["name"]: dict(r) for r in rows}
    return {n: have.get(n, {"name": n, "status": "unknown", "evidence": None, "checked_at": None}) | {"what": w}
            for n, w in NAMES.items()}


def require(db: Database, build: str, name: str) -> None:
    status = table(db, build)[name]["status"]
    if status != "pass":
        raise CapabilityError(f"{name} is {status} on build {build}: {NAMES[name]} — run that spike first")
