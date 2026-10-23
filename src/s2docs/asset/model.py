"""Source 2 model documents (``.vmdl``) — generated from a mesh file, not from memory.

The format is KV3 with a versioned header and node classes the ModelDoc compiler validates, so a
plausible-looking document is not a compiling one. This module copies the shape of a real, in-use
document instead: ``content/csgo_addons/iso_spectate/maps/de_dust2_iso/worldnodes/
n0_lr0_agg_merge_agave_plant_01_0_draw0.vmdl`` (851 bytes, ``modeldoc28``), which the CS2 compiler in
the user's install produced and accepts. ``tests/test_asset_model.py`` pins the structure, and the
generated output is compiled for real on a host before it is trusted.

Scope: the geometry half (``RenderMeshList`` → ``RenderMeshFile``). Materials come from the mesh's
own assignment; a ``.vmat`` generator needs a standard-shader example to copy and is not invented
here.
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


class ModelDocError(ValueError):
    """A request that cannot produce a valid model document (bad name, path or mesh reference)."""


def check_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name or ""):
        raise ModelDocError(f"model name {name!r} must match {_NAME_RE.pattern}")
    return name


def mesh_reference(mesh_rel: str) -> str:
    """Normalize an addon-relative mesh path: forward slashes, inside the addon, a known suffix.

    Model docs resolve ``filename`` relative to the addon's content root (the observed document
    references ``maps/de_dust2_iso/worldnodes/<mesh>.dmx``). An absolute path, a ``..`` segment or a
    drive letter is rejected rather than passed to the compiler to fail on.
    """
    raw = (mesh_rel or "").strip().replace("\\", "/")
    if not raw:
        raise ModelDocError("mesh reference is empty")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ModelDocError(f"mesh reference must be addon-relative, got {mesh_rel!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ModelDocError(f"mesh reference may not escape the addon: {mesh_rel!r}")
    if not parts[-1].lower().endswith(MESH_SUFFIXES):
        raise ModelDocError(f"mesh reference must end in {' or '.join(MESH_SUFFIXES)}: {mesh_rel!r}")
    return "/".join(parts)


def render_model_doc(
    name: str,
    mesh_rel: str,
    *,
    translation: tuple[float, float, float] | None = None,
    import_filter_exceptions: list[str] | None = None,
) -> str:
    """Render a modeldoc28 document for one mesh file.

    Mirrors the observed structure exactly — ``RootNode`` with ``BoneMarkupList``,
    ``RenderMeshList`` and (only when asked) ``ModelModifierList``. Tabs, trailing commas and the
    ``[  ]`` empty-array spelling are the real file's, because the compiler reads this text, not a
    normalised version of it.
    """
    check_name(name)
    mesh = mesh_reference(mesh_rel)
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
