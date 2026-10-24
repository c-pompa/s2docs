"""Source 2 model documents (``.vmdl``) — generated from a mesh file, not from memory.

The format is KV3 with a versioned header and node classes the ModelDoc compiler validates, so a
plausible-looking document is not a compiling one. This module copies the shape of a real, in-use
document instead: ``content/csgo_addons/iso_spectate/maps/de_dust2_iso/worldnodes/
n0_lr0_agg_merge_agave_plant_01_0_draw0.vmdl`` (851 bytes, ``modeldoc28``), which the CS2 compiler in
the user's install produced and accepts. ``tests/test_asset_model.py`` pins the structure, and the
generated output is compiled for real on a host before it is trusted.

Scope: the geometry half (``RenderMeshList`` → ``RenderMeshFile``) and, on request, the physics half
(``PhysicsShapeList`` → ``PhysicsHullFile``). Materials come from the mesh's own assignment; a
``.vmat`` generator needs a standard-shader example to copy and is not invented here.

Physics, derived from shipped documents rather than guessed (CS2 build 2000930, decompiled on
desktop-pc with Source2Viewer-CLI 20.1 into a reference directory outside every addon and this repo —
reference only, per docs/HAMMERED.md §6):

- ``models/props/ar_dizzy/dizzy_cinderblock/dizzy_cinderblock.vmdl_c`` — one
  ``PhysicsHullFile`` child under ``PhysicsShapeList``, keys in this order: ``filename``,
  ``parent_bone`` (``""``), ``surface_prop`` (``"rock"``), ``collision_tags`` (``""``), ``name``.
- ``models/props/containers/barrels/steel_drum_barrel_01a.vmdl_c`` — same five keys, sibling class
  ``PhysicsMeshFile`` (a concave mesh instead of a hull), ``surface_prop = "metal_barrel"``.
- ``models/props/containers/barrels/steel_drum_barrel_01b.vmdl_c`` and
  ``…/ar_dizzy/dizzy_generator/dizzy_generator_full.vmdl_c`` — several ``PhysicsHullFile`` children
  in one ``PhysicsShapeList`` (3 and 26), ``surface_prop = "metal"``.

None of the sampled documents carries a ``collision_group`` key, and no physics node carries anything
but the five keys above: the modeldoc surface/collision vocabulary is ``surface_prop`` (a
``surfaceproperties`` entry name) and ``collision_tags``. ``name`` is the mesh's name *inside* the
referenced file — the shipped documents name it after the source mesh (e.g.
``dizzy_cinderblock_physbox_0_mesh``) while their decompiled ``.dmx`` holds exactly that one mesh.

Collision contract for generated props (the Blender side owns producing the mesh): the collision is a
**separate mesh named ``<name>_collision`` inside the same FBX** as the render mesh, authored in
Source units (1 unit = 1 inch), Z-up, origin at the base centre of the prop. That is why the physics
node below points at the render mesh file and selects the collision mesh by name instead of naming a
second file.

Checked on the real compiler (desktop-pc, CS2 build 2000930, ``resourcecompiler -i probe.vmdl -nop4``):
a document with this physics node compiles (``OK: 1 compiled, 0 failed``) and the artifact carries a
real ``PHYS`` block (``physics_shape_count = 1``, ``physics_shape_hull_count = 1``). With a 500-vertex
sphere as the render mesh and an 8-vertex box as ``probe_collision``, the compiled hull stayed an
8-vertex box, so ``name`` does select the mesh inside the FBX. Two things the compiler does *not*
catch, which the Blender side must therefore get right: a ``name`` that matches no mesh in the file
still compiles with 0 failed (a typo there is not a compile error), and every mesh in the FBX needs
the ``.vmat`` material assigned — an unassigned mesh makes the importer synthesise ``<mesh>.vmat``,
which fails the compile with ``referencing missing material`` under gameinfo's
``content_consider_missing_materials_fatal``.
"""

from __future__ import annotations

import re
from pathlib import Path

# Copied byte-for-byte from the observed document: the guid pair is the format identity, not a value
# to "choose". modeldoc28 is what CS2's compiler wrote for a live addon.
MODEL_DOC_HEADER = (
    "<!-- kv3 encoding:text:version{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d} "
    "format:modeldoc28:version{fb63b6ca-f435-4aa0-a2c7-c66ddc651dca} -->"
)

MESH_SUFFIXES = (".dmx", ".fbx")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
# A surfaceproperties entry name (``rock``, ``metal``, ``metal_barrel``, ``default``): an identifier,
# never a path. The shipped documents use exactly this spelling style.
_SURFACE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# The collision mesh's name inside the model's FBX (contract with the Blender side).
COLLISION_MESH_SUFFIX = "_collision"
# Blender truncates ID names (bpy Object.name) beyond 63 characters, so a longer mesh name would
# silently become a different name than the one this document selects.
MAX_MESH_NAME = 63


class ModelDocError(ValueError):
    """A request that cannot produce a valid model document (bad name, path or mesh reference)."""


def check_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name or ""):
        raise ModelDocError(f"model name {name!r} must match {_NAME_RE.pattern}")
    return name


def addon_relative(rel: str, suffixes: tuple[str, ...], *, what: str = "reference") -> str:
    """Normalize an addon-relative asset path: forward slashes, inside the addon, a known suffix.

    The one path guard every generated document uses (mesh ``filename``, material and texture
    paths). An absolute path, a ``..`` segment or a drive letter is rejected rather than passed to
    the compiler to fail on.
    """
    raw = (rel or "").strip().replace("\\", "/")
    if not raw:
        raise ModelDocError(f"{what} is empty")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ModelDocError(f"{what} must be addon-relative, got {rel!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ModelDocError(f"{what} may not escape the addon: {rel!r}")
    if not parts or not parts[-1].lower().endswith(suffixes):
        raise ModelDocError(f"{what} must end in {' or '.join(suffixes)}: {rel!r}")
    return "/".join(parts)


def mesh_reference(mesh_rel: str) -> str:
    """Normalize an addon-relative mesh path (see ``addon_relative``).

    Model docs resolve ``filename`` relative to the addon's content root (the observed document
    references ``maps/de_dust2_iso/worldnodes/<mesh>.dmx``).
    """
    return addon_relative(mesh_rel, MESH_SUFFIXES, what="mesh reference")


def collision_mesh_name(name: str) -> str:
    """The collision mesh's name inside the model's FBX: ``<name>_collision``.

    Shared with the Blender side of the pipeline, which must create the mesh under exactly this name
    for the generated document to find it.
    """
    check_name(name)
    mesh_name = f"{name}{COLLISION_MESH_SUFFIX}"
    if len(mesh_name) > MAX_MESH_NAME:
        raise ModelDocError(
            f"collision mesh name {mesh_name!r} is {len(mesh_name)} chars; Blender truncates ID names "
            f"over {MAX_MESH_NAME}, so the model would point at a mesh that does not exist "
            f"(use a name of at most {MAX_MESH_NAME - len(COLLISION_MESH_SUFFIX)} chars for collision)"
        )
    return mesh_name


def check_surface_prop(surface_prop: str) -> str:
    """A ``surfaceproperties`` entry name (``rock``, ``metal``, ``metal_barrel``), not a path."""
    if not _SURFACE_RE.fullmatch(surface_prop or ""):
        raise ModelDocError(
            f"surface prop {surface_prop!r} must name a surfaceproperties entry "
            f"(matching {_SURFACE_RE.pattern})"
        )
    return surface_prop


def render_model_doc(
    name: str,
    mesh_rel: str,
    *,
    translation: tuple[float, float, float] | None = None,
    import_filter_exceptions: list[str] | None = None,
    collision: str | None = None,
    collision_mesh: str | None = None,
) -> str:
    """Render a modeldoc28 document for one mesh file.

    Mirrors the observed structure exactly — ``RootNode`` with ``BoneMarkupList``,
    ``RenderMeshList``, then (only when asked) ``PhysicsShapeList`` and ``ModelModifierList``. Tabs,
    trailing commas and the ``[  ]`` empty-array spelling are the real file's, because the compiler
    reads this text, not a normalised version of it.

    ``collision`` asks for the physics section and names the surface property the prop is given
    (``"metal"``, ``"wood"``, ``"rock"``…). Omitted — the default — no physics node is written at all,
    so every pre-existing caller's document is byte-identical to before. ``collision_mesh`` optionally
    points the hull at a different addon-relative mesh file; it defaults to the render mesh, which is
    the shipped pipeline's shape: one FBX holding the render mesh and ``<name>_collision``, both in
    Source units with Z up and the origin at the base centre.
    """
    check_name(name)
    mesh = mesh_reference(mesh_rel)
    if collision is None:
        if collision_mesh is not None:
            raise ModelDocError("collision_mesh was given without collision (pass the surface prop)")
        physics: tuple[str, str, str] | None = None
    else:
        # Same guard as the render mesh: addon-relative, inside the addon, a mesh suffix.
        hull_mesh = mesh_reference(collision_mesh) if collision_mesh is not None else mesh
        physics = (hull_mesh, check_surface_prop(collision), collision_mesh_name(name))
    lines = [
        MODEL_DOC_HEADER,
        "{",
        "\trootNode = ",
        "\t{",
        '\t\t_class = "RootNode"',
        "\t\tchildren = ",
        "\t\t[",
        "\t\t\t{",
        '\t\t\t\t_class = "BoneMarkupList"',
        '\t\t\t\tchildren = [  ]',
        '\t\t\t\tbone_cull_type = "None"',
        "\t\t\t},",
        "\t\t\t{",
        '\t\t\t\t_class = "RenderMeshList"',
        "\t\t\t\tchildren = ",
        "\t\t\t\t[",
        "\t\t\t\t\t{",
        '\t\t\t\t\t\t_class = "RenderMeshFile"',
        f'\t\t\t\t\t\tname = "{name}"',
        f'\t\t\t\t\t\tfilename = "{mesh}"',
    ]
    if import_filter_exceptions:
        lines.extend([
            "\t\t\t\t\t\timport_filter = ",
            "\t\t\t\t\t\t{",
            "\t\t\t\t\t\t\texclude_by_default = true",
            "\t\t\t\t\t\t\texception_list = ",
            "\t\t\t\t\t\t\t[",
            *(f'\t\t\t\t\t\t\t\t"{item}",' for item in import_filter_exceptions),
            "\t\t\t\t\t\t\t]",
            "\t\t\t\t\t\t}",
        ])
    lines.extend([
        "\t\t\t\t\t},",
        "\t\t\t\t]",
        "\t\t\t},",
    ])
    if physics is not None:
        hull_mesh, surface, hull = physics
        # Field order, class names and the empty-string defaults are the shipped documents' — the
        # compiler reads this node by name, so nothing here is optional-by-guess.
        lines.extend([
            "\t\t\t{",
            '\t\t\t\t_class = "PhysicsShapeList"',
            "\t\t\t\tchildren = ",
            "\t\t\t\t[",
            "\t\t\t\t\t{",
            '\t\t\t\t\t\t_class = "PhysicsHullFile"',
            f'\t\t\t\t\t\tfilename = "{hull_mesh}"',
            '\t\t\t\t\t\tparent_bone = ""',
            f'\t\t\t\t\t\tsurface_prop = "{surface}"',
            '\t\t\t\t\t\tcollision_tags = ""',
            f'\t\t\t\t\t\tname = "{hull}"',
            "\t\t\t\t\t},",
            "\t\t\t\t]",
            "\t\t\t},",
        ])
    if translation is not None:
        x, y, z = (float(v) for v in translation)
        lines.extend([
            "\t\t\t{",
            '\t\t\t\t_class = "ModelModifierList"',
            "\t\t\t\tchildren = ",
            "\t\t\t\t[",
            "\t\t\t\t\t{",
            '\t\t\t\t\t\t_class = "ModelModifier_Translate"',
            f"\t\t\t\t\t\ttranslation = [ {x:g}, {y:g}, {z:g} ]",
            "\t\t\t\t\t},",
            "\t\t\t\t]",
            "\t\t\t},",
        ])
    lines.extend(["\t\t]", "\t}", "}"])
    return "\r\n".join(lines) + "\r\n"


def write_model_doc(path: Path, text: str, *, overwrite: bool = False) -> Path:
    """Write the document, refusing to clobber an existing one unless asked.

    A generated file that silently replaces a hand-edited one is how a working asset disappears;
    the caller says ``overwrite=True`` deliberately.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise ModelDocError(f"{path} already exists (pass overwrite=True to replace it)")
    path.write_bytes(text.encode("utf-8"))
    return path
