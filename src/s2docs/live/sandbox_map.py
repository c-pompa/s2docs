"""Writes the hermes_sandbox addon sources: an entity-only helper map (point_script + one point_template per
spawnable) and its cs_script. Entity-only on purpose: hand-authored WORLD meshes crash resourcecompiler
(docs/HAMMERED.md), entity-only helper maps compile in seconds (iso_spectate_ui).

The two `sandbox/` sources are templates, not shipped code: `hermes_sandbox.vmap.in` carries one
`{{ENTITIES}}` line where the entities go, and `runtime.js` carries `/*FORMATION*/` (inlined from
`formation.mjs`) and `/*HEIGHTS*/{}` (filled from the spawnables). Rendering is byte-stable so a deploy's
MD5 only changes when the content does.
"""

from __future__ import annotations

import json
import uuid
from importlib import resources
from pathlib import Path

from s2docs.live import sandbox_user
from s2docs.live.spawnables import Spawnable

_NS = uuid.UUID("6f3c2a10-5b1e-4c55-9d0a-48e5c2b1a001")
_SRC = resources.files("s2docs.live") / "sandbox"
SCRIPT_PATH = "maps/scripts/hermes_sandbox.vjs"  # what point_script.cs_script names


def _uid(*parts: object) -> str:
    return str(uuid.uuid5(_NS, ":".join(map(str, parts))))


def _entity(node: int, props: dict[str, str], origin: str = "0 0 0") -> str:
    """One `"CMapEntity"` block, shaped like iso_spectate_ui.vmap's (Hammer reads the same keys)."""
    kv = "\n".join(f'\t\t\t\t\t"{k}" "string" "{v}"' for k, v in props.items())
    ref = int(uuid.UUID(_uid(node, "ref")).hex[:15], 16)
    return f"""\t\t\t"CMapEntity"
\t\t\t{{
\t\t\t\t"id" "elementid" "{_uid(node, 'id')}"
\t\t\t\t"nodeID" "int" "{node}"
\t\t\t\t"referenceID" "uint64" "0x{ref:016x}"
\t\t\t\t"children" "element_array"
\t\t\t\t[
\t\t\t\t]
\t\t\t\t"variableTargetKeys" "string_array"
\t\t\t\t[
\t\t\t\t]
\t\t\t\t"variableNames" "string_array"
\t\t\t\t[
\t\t\t\t]
\t\t\t\t"relayPlugData" "DmePlugList"
\t\t\t\t{{
\t\t\t\t\t"id" "elementid" "{_uid(node, 'plug')}"
\t\t\t\t\t"names" "string_array"
\t\t\t\t\t[
\t\t\t\t\t]
\t\t\t\t\t"dataTypes" "int_array"
\t\t\t\t\t[
\t\t\t\t\t]
\t\t\t\t\t"plugTypes" "int_array"
\t\t\t\t\t[
\t\t\t\t\t]
\t\t\t\t\t"descriptions" "string_array"
\t\t\t\t\t[
\t\t\t\t\t]
\t\t\t\t}}

\t\t\t\t"connectionsData" "element_array"
\t\t\t\t[
\t\t\t\t]
\t\t\t\t"entity_properties" "EditGameClassProps"
\t\t\t\t{{
\t\t\t\t\t"id" "elementid" "{_uid(node, 'props')}"
{kv}
\t\t\t\t}}

\t\t\t\t"hitNormal" "vector3" "0 0 1"
\t\t\t\t"isProceduralEntity" "bool" "0"
\t\t\t\t"origin" "vector3" "{origin}"
\t\t\t\t"angles" "qangle" "0 0 0"
\t\t\t\t"scales" "vector3" "1 1 1"
\t\t\t\t"transformLocked" "bool" "0"
\t\t\t\t"force_hidden" "bool" "0"
\t\t\t\t"editorOnly" "bool" "0"
\t\t\t\t"customVisGroup" "string" ""
\t\t\t\t"randomSeed" "int" "{1000 + node}"
\t\t\t}}"""


def render_vmap(spawnables: dict[str, Spawnable]) -> str:
    """The helper map: one point_script, and per spawnable a point_template plus its child entity.

    The template puts the model in the map's resource manifest (that is what `ent_create` never did),
    so `PointTemplate.ForceSpawn hs_tpl_<id>` brings a precached entity into the world.
    """
    blocks = [_entity(2, {"classname": "point_script", "targetname": "hermes_sandbox_script",
                          "cs_script": SCRIPT_PATH}, "0 0 96")]
    node = 3
    for s in sorted(spawnables.values(), key=lambda s: s.id):
        child = {"classname": s.classname, "targetname": f"hs_src_{s.id}"}
        if s.model:
            child |= {"model": s.model, "solid": "6"}
        blocks.append(_entity(node, {"classname": "point_template", "targetname": s.template,
                                     "Template01": f"hs_src_{s.id}", "spawnflags": "0"}, "0 0 128"))
        blocks.append(_entity(node + 1, child, "0 0 160"))
        node += 2
    template = (_SRC / "hermes_sandbox.vmap.in").read_text(encoding="utf-8")
    return template.replace("{{ENTITIES}}", ",\n".join(blocks))


def user_block(user: sandbox_user.UserModule) -> str:
    """The bundled module: wrapped in its own function scope and invoked with the runtime's `api`.

    It is a function scope, not a module: the runtime already owns `Instance`, the single `OnPlayerChat`
    registration and the think loop, so the module gets `start(api)` and nothing else (the contract in
    :mod:`s2docs.live.sandbox_user`). It reports on load either way — `ready <nonce>` after `start()`
    returns, `error <message>` when it throws — so an in-game readback can prove this build is the one the
    engine loaded, and a throwing module is a visible failure instead of a silent no-op.
    """
    tag = json.dumps(user.name)
    nonce = json.dumps(user.nonce)
    lines = [
        f"// ---- user module {tag} (bundled by s2docs.live.sandbox_map; it is not a point_script of its own) ----",
        "(function () {",
        user.code.rstrip(),
        "",
        f"  const __hsApi = hsUserApi({tag}, {nonce});",
        "  try { start(__hsApi); }",
        f'  catch (e) {{ Instance.Msg("[user:" + {tag} + "] error " + hsUserText(e)); return; }}',
        f'  Instance.Msg("[user:" + {tag} + "] ready " + {nonce});',
    ]
    # `stop` is wired only when the module DECLARES it. A runtime `typeof stop === "function"` guard is not
    # enough: tsc resolves a bare `stop` to the lib's global `stop()` (0 parameters) when the module has
    # none, and `stop(__hsApi)` then fails the bundle's type-check (TS2554, measured 2026-10-27 with a
    # start-only module).
    if sandbox_user.STOP.search(sandbox_user._blank(user.code)[0]):
        lines += [
            '  Instance.RegisterCheatCommand("hu_stop", () => {',
            "    try { stop(__hsApi); }",
            f'    catch (e) {{ Instance.Msg("[user:" + {tag} + "] error " + hsUserText(e)); }}',
            "  });",
        ]
    lines.append("})();")
    return "\n".join(lines)


def render_script(spawnables: dict[str, Spawnable], user: sandbox_user.UserModule | None = None) -> str:
    """The cs_script: formation.mjs inlined (no imports, no exports) + the spawnable heights.

    ``user`` appends the bundled user module (``user_block``) at ``/*USER*/``; with ``None`` the marker is
    removed and the result is the stock sandbox runtime, so the two differ only by the module's own bytes.
    """
    formation = (_SRC / "formation.mjs").read_text(encoding="utf-8").replace("export function", "function")
    runtime = (_SRC / "runtime.js").read_text(encoding="utf-8")
    heights = json.dumps({s.id: s.height for s in sorted(spawnables.values(), key=lambda s: s.id)},
                         separators=(", ", ": "))
    rendered = runtime.replace("/*FORMATION*/", formation).replace("/*HEIGHTS*/{}", heights)
    return rendered.replace("/*USER*/", user_block(user) if user is not None else "")


def write_addon(content_root: Path, spawnables: dict[str, Spawnable],
                user: sandbox_user.UserModule | None = None) -> list[Path]:
    """content_root = <CS2>/content/csgo_addons/hermes_sandbox (or a local staging copy)."""
    maps, scripts = content_root / "maps", content_root / "maps" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    out = [maps / "hermes_sandbox.vmap", scripts / "hermes_sandbox.js"]
    out[0].write_bytes(render_vmap(spawnables).encode("utf-8"))
    out[1].write_bytes(render_script(spawnables, user).encode("utf-8"))
    return out
