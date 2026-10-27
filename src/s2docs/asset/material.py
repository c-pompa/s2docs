"""Source 2 material documents (``.vmat``) for a baked colour texture — keys copied, not invented.

Evidence (2026-10-23, desktop-pc, CS2 build 2000930): three shipped prop materials were decompiled
from ``game/csgo/pak01_dir.vpk`` with Source2Viewer-CLI 20.1 (reference copies only, kept outside
every addon and this repo):

- ``materials/models/props/ar_dizzy/dizzy_cinderblock/dizzy_cinderblock_color.vmat_c``
- ``materials/models/props/coop_cementplant/coop_military_crate/coop_military_crate_color.vmat_c``
- ``materials/models/props/coop_cementplant/furniture/coop_wooden_table_color.vmat_c``

All three use ``csgo_vertexlitgeneric.vfx``; the base colour is ``TextureColor``; and every one sets
``TextureMetalness`` (``[0.000000 0.000000 0.000000 0.000000]``), ``TextureAmbientOcclusion``,
``TextureNormal`` and ``TextureRoughness``. The defaults written here are the ones those references
point at and that exist as sources in ``content/core/materials/default/`` (``default_ao.tga``,
``default_normal.tga``, ``default_rough.tga``). The decompiler's ``Compiled Textures`` block is
compiler output and is never authored. Format: the decompiled text's quoted KV — ``"Layer0"``, a
brace block, one tab-separated ``"key"\t"value"`` pair per line, LF endings.

The material lives at ``materials/hammered/<name>/<name>.vmat`` because CS2's FBX importer reads the
FBX material NAME as the ``.vmat`` path (``GetFbxMaterialPath Failed!`` otherwise); the Blender
bridge names its material with ``material_path(name)``, so that path must fit Blender's 63-character
ID-name limit.

Baked maps. ``scripts/asset/glb_to_fbx_uv.py`` bakes a tangent-space normal map (``<name>_normal.png``)
and an AO map (``<name>_ao.png``) from the original high-poly and writes them beside
``<name>_color.png`` in the same ``.fbm`` folder. When those two files sit next to the colour texture
this module copies them into the addon and points ``TextureNormal`` / ``TextureAmbientOcclusion`` at
them, which replaces the engine defaults (``materials/default/default_normal.tga``,
``default_ao.tga``) with the prop's own geometry. When they do not exist, the keys keep those
defaults: the no-bake path is byte-identical to before. The map that is present is wired on its own,
so a bake that produced only one of the two still compiles with the other defaulted.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from s2docs.asset.model import ModelDocError, addon_relative, check_name

MATERIAL_ROOT = "materials/hammered"
MAX_ID_NAME = 63  # Blender ID names (bpy Material.name) are truncated beyond 63 characters

# Allow-list of shaders a generated .vmat may name. csgo_vertexlitgeneric.vfx is what all three
# decompiled shipped prop materials use (see the module docstring); add a shader here only with the
# same kind of evidence — a decompiled shipped material that uses it for the same purpose.
VMAT_SHADERS: tuple[str, ...] = ("csgo_vertexlitgeneric.vfx",)
DEFAULT_SHADER = "csgo_vertexlitgeneric.vfx"

# The keys every decompiled reference sets besides shader/TextureColor, in the references' order,
# with the defaults they point at (sources under content/core/materials/default/).
_METALNESS = "[0.000000 0.000000 0.000000 0.000000]"
_DEFAULT_AO = "materials/default/default_ao.tga"
_DEFAULT_NORMAL = "materials/default/default_normal.tga"
_DEFAULT_ROUGH = "materials/default/default_rough.tga"

# The two maps the Blender bridge bakes beside the colour texture, the .vmat key each one feeds, the
# engine default that key keeps when the map is absent, and the file suffix the bridge writes.
BAKED_MAPS: tuple[str, ...] = ("normal", "ao")
_MAP_SUFFIX = {"normal": "_normal.png", "ao": "_ao.png"}
BAKED_MAP_VMAT_KEY = {"normal": "TextureNormal", "ao": "TextureAmbientOcclusion"}
_MAP_DEFAULT = {"normal": _DEFAULT_NORMAL, "ao": _DEFAULT_AO}
# The bridge's colour map is <name>_color.png; the base name is the colour stem without this suffix.
_COLOR_SUFFIX = "_color"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class MaterialError(ModelDocError):
    """A request that cannot produce a valid material (bad name, shader, path or texture)."""


def _name(name: str) -> str:
    try:
        return check_name(name)
    except ModelDocError as exc:
        raise MaterialError(str(exc)) from exc


def _guard(rel: str, suffix: str, what: str) -> str:
    try:
        return addon_relative(rel, (suffix,), what=what)
    except ModelDocError as exc:
        raise MaterialError(str(exc)) from exc


def material_path(name: str) -> str:
    """``materials/hammered/<name>/<name>.vmat`` — also the FBX material name the bridge writes."""
    path = _guard(f"{MATERIAL_ROOT}/{_name(name)}/{name}.vmat", ".vmat", "material path")
    if len(path) > MAX_ID_NAME:
        raise MaterialError(
            f"material path {path!r} is {len(path)} chars; Blender truncates ID names over {MAX_ID_NAME} "
            f"(use a name of at most {(MAX_ID_NAME - 25) // 2} chars)"
        )
    return path


def color_texture_path(name: str) -> str:
    """Addon-relative path of the colour texture beside the material: ``<dir>/<name>_color.png``."""
    vmat = material_path(name)
    return _guard(f"{vmat.rsplit('/', 1)[0]}/{name}_color.png", ".png", "colour texture path")


def baked_texture_path(name: str, kind: str) -> str:
    """Addon-relative path a baked map is copied to: ``<dir>/<name>_normal.png`` / ``<name>_ao.png``."""
    if kind not in BAKED_MAPS:
        raise MaterialError(f"baked map {kind!r} is not one of {list(BAKED_MAPS)}")
    vmat = material_path(name)
    return _guard(f"{vmat.rsplit('/', 1)[0]}/{name}{_MAP_SUFFIX[kind]}", ".png", f"baked {kind} texture path")


def find_baked_maps(color_png: Path) -> dict[str, Path]:
    """The baked maps that exist **beside the colour texture**: ``{kind: path}``, absent ones omitted.

    The bridge writes ``<name>_color.png``, ``<name>_normal.png`` and ``<name>_ao.png`` into one
    ``.fbm`` folder, so the base name comes from the colour file (a trailing ``_color`` stripped) and
    each map must be a file in the colour texture's own directory. Nothing is guessed: a missing map is
    not in the result and its ``.vmat`` key keeps the engine default.
    """
    src = Path(color_png)
    base = src.stem[: -len(_COLOR_SUFFIX)] if src.stem.endswith(_COLOR_SUFFIX) else src.stem
    return {kind: p for kind in BAKED_MAPS if (p := src.parent / f"{base}{_MAP_SUFFIX[kind]}").is_file()}


def render_vmat(name: str, *, shader: str = DEFAULT_SHADER, baked: dict[str, str] | None = None) -> str:
    """The material document: only the shader and the keys proven by the decompiled references.

    ``baked`` maps a kind from ``BAKED_MAPS`` to the addon-relative PNG that key should name. A kind
    that is not in ``baked`` (the default: no baked maps at all) keeps the engine default the
    decompiled references point at, so the document is what it always was.
    """
    if shader not in VMAT_SHADERS:
        raise MaterialError(f"shader {shader!r} is not in the evidenced allow-list {list(VMAT_SHADERS)}")
    values = dict(_MAP_DEFAULT)
    for kind, rel in (baked or {}).items():
        if kind not in BAKED_MAPS:
            raise MaterialError(f"baked map {kind!r} is not one of {list(BAKED_MAPS)}")
        values[kind] = _guard(rel, ".png", what=f"baked {kind} texture path")
    pairs = [
        ("shader", shader),
        ("TextureMetalness", _METALNESS),
        (BAKED_MAP_VMAT_KEY["ao"], values["ao"]),
        ("TextureColor", color_texture_path(name)),
        (BAKED_MAP_VMAT_KEY["normal"], values["normal"]),
        ("TextureRoughness", _DEFAULT_ROUGH),
    ]
    lines = ['"Layer0"', "{", *(f'\t"{k}"\t"{v}"' for k, v in pairs), "}"]
    return "\n".join(lines) + "\n"


def _inside(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and refuse anything (e.g. a symlinked dir) that lands outside."""
    base = root.resolve()
    target = (base / rel).resolve()
    if base not in target.parents:
        raise MaterialError(f"{rel!r} escapes the addon root {root}")
    return target


def _check_png(path: Path, what: str) -> None:
    with path.open("rb") as fh:
        if fh.read(len(_PNG_MAGIC)) != _PNG_MAGIC:
            raise MaterialError(f"{what} {path} is not a PNG (bad signature)")


def write_vmat(
    addon_root: Path,
    name: str,
    color_png: Path,
    *,
    shader: str = DEFAULT_SHADER,
    force: bool = False,
) -> Path:
    """Write ``<addon>/materials/hammered/<name>/<name>.vmat`` and copy the PNGs beside it.

    The colour texture's own baked ``<name>_normal.png`` / ``<name>_ao.png`` siblings (see
    ``find_baked_maps``) are copied too and the matching keys name them; a map that is absent leaves
    its key on the engine default, so a colour-only call writes exactly the document it always wrote.

    Refuses: a missing addon root, a source that is not a PNG (colour or baked map), a shader outside
    ``VMAT_SHADERS``, a target outside the addon, and replacing any existing file unless ``force=True``.
    Returns the written ``.vmat`` path.
    """
    src = Path(color_png)
    baked_src = find_baked_maps(src)
    baked_rel = {kind: baked_texture_path(name, kind) for kind in baked_src}
    doc = render_vmat(name, shader=shader, baked=baked_rel)
    root = Path(addon_root)
    if not root.is_dir():
        raise MaterialError(f"addon root {root} does not exist")
    if not src.is_file():
        raise MaterialError(f"colour texture {src} not found")
    _check_png(src, "colour texture")
    for kind, path in baked_src.items():
        _check_png(path, f"baked {kind} texture")
    vmat = _inside(root, material_path(name))
    tex = _inside(root, color_texture_path(name))
    baked_dst = {kind: _inside(root, rel) for kind, rel in baked_rel.items()}
    if not force:
        existing = [p for p in (vmat, tex, *baked_dst.values()) if p.exists()]
        if existing:
            raise MaterialError(f"{existing[0]} already exists (pass force=True to replace it)")
    vmat.parent.mkdir(parents=True, exist_ok=True)
    # re-check after mkdir: a symlink created meanwhile still fails
    for rel in (material_path(name), color_texture_path(name), *baked_rel.values()):
        _inside(root, rel)
    # The caller may point at the texture already in the addon (a re-run of the same asset): copying a
    # file onto itself raises SameFileError, and there is nothing to copy anyway.
    for source, dst in ((src, tex), *((baked_src[k], d) for k, d in baked_dst.items())):
        if not (dst.exists() and source.resolve() == dst.resolve()):
            shutil.copyfile(source, dst)
    vmat.write_bytes(doc.encode("utf-8"))
    return vmat
