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


def render_vmat(name: str, *, shader: str = DEFAULT_SHADER) -> str:
    """The material document: only the shader and the keys proven by the decompiled references."""
    if shader not in VMAT_SHADERS:
        raise MaterialError(f"shader {shader!r} is not in the evidenced allow-list {list(VMAT_SHADERS)}")
    pairs = [
        ("shader", shader),
        ("TextureMetalness", _METALNESS),
        ("TextureAmbientOcclusion", _DEFAULT_AO),
        ("TextureColor", color_texture_path(name)),
        ("TextureNormal", _DEFAULT_NORMAL),
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


def write_vmat(
    addon_root: Path,
    name: str,
    color_png: Path,
    *,
    shader: str = DEFAULT_SHADER,
    force: bool = False,
) -> Path:
    """Write ``<addon>/materials/hammered/<name>/<name>.vmat`` and copy the PNG beside it.

    Refuses: a missing addon root, a source that is not a PNG, a shader outside ``VMAT_SHADERS``,
    a target outside the addon, and replacing either existing file unless ``force=True``.
    Returns the written ``.vmat`` path.
    """
    doc = render_vmat(name, shader=shader)
    root = Path(addon_root)
    if not root.is_dir():
        raise MaterialError(f"addon root {root} does not exist")
    src = Path(color_png)
    if not src.is_file():
        raise MaterialError(f"colour texture {src} not found")
    with src.open("rb") as fh:
        if fh.read(len(_PNG_MAGIC)) != _PNG_MAGIC:
            raise MaterialError(f"colour texture {src} is not a PNG (bad signature)")
    vmat = _inside(root, material_path(name))
    tex = _inside(root, color_texture_path(name))
    if not force:
        existing = [p for p in (vmat, tex) if p.exists()]
        if existing:
            raise MaterialError(f"{existing[0]} already exists (pass force=True to replace it)")
    vmat.parent.mkdir(parents=True, exist_ok=True)
    _inside(root, material_path(name))  # re-check after mkdir: a symlink created meanwhile still fails
    shutil.copyfile(src, tex)
    vmat.write_bytes(doc.encode("utf-8"))
    return vmat
