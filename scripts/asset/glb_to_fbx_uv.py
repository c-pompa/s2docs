"""GLB (per-vertex colour only) -> game-budget FBX: decimated mesh, UVs, colour + normal + AO bakes.

Headless Blender bridge for the Hammered local mesh route. TripoSR (through trimesh) writes a GLB
with POSITION + COLOR_0 and nothing else: no UVs, no normals, no materials, no images. A ``.vmdl``
built from that compiles but cannot take a material, and its 58,696-triangle / 29,401-vertex output
is 10-50x a prop's triangle budget. This script turns that into a shippable prop:

1. import the GLB (Blender's glTF importer), drop non-mesh objects, join the body mesh, apply
   transforms, orient Z-up, and move the origin to the **base centre** of the body's bounding box
   (x/y centre, min z). The export therefore carries no scale node and the prop stands on its pivot;
2. duplicate the body as the render mesh and decimate it (Decimate modifier, COLLAPSE) to
   ``--target-tris`` triangles or fewer;
3. sample the high-poly's per-vertex colour onto the render mesh (nearest point on the original
   surface, barycentric), then smart-UV-project the render mesh;
4. bake three 2048x2048 maps onto the render mesh's UVs:
   ``<name>_color.png`` (EMIT, the transferred vertex colour),
   ``<name>_normal.png`` (Cycles NORMAL, tangent space, from the **original high-poly**),
   ``<name>_ao.png`` (Cycles AO, occluded by the **original high-poly**);
5. build the material, named by its Source 2 path (``--material-path``, default
   ``materials/hammered/<name>/<name>.vmat``): Principled BSDF, Base Color <- the colour PNG,
   Normal <- Normal Map <- the normal PNG. CS2's FBX importer reads the FBX material NAME as the
   ``.vmat`` path (``GetFbxMaterialPath Failed!`` otherwise), so the name must survive export
   verbatim;
6. export ``<out-dir>/<name>.fbx`` with ``path_mode='COPY'``, ``embed_textures=False``,
   ``bake_space_transform=True`` (after ``transform_apply``), ``use_selection=False``, MESH only, and
   the unit settings that make the written numbers Source units (see **Units**);
7. re-import the FBX into an empty scene, report what is really in it, and require the re-imported
   material name to equal ``--material-path`` (Blender truncates ID names over 63 characters, and an
   exporter may sanitize ``/``).

Run with Blender 4.5 LTS (verified on 4.5.15, build-pc ``D:\\Tools\\blender-4.5.15-windows-x64``)::

    blender.exe -b --factory-startup --python-exit-code 1 --python glb_to_fbx_uv.py -- \\
        --glb <mesh.glb> --out-dir <dir> --name <model_name> [--target-tris 5000] [--res 2048] \\
        [--force] [--material-path materials/hammered/<name>/<name>.vmat]

Output in ``<out-dir>``: ``<name>.fbx``; ``<name>.fbm/<name>_color.png`` + ``<name>_normal.png`` +
``<name>_ao.png`` (``path_mode='COPY'`` copies media into the FBX convention's ``.fbm`` folder and the
FBX references it relatively, so move them together); ``<name>_bridge.json`` (the report, also
printed as one stdout line ``GLB_TO_FBX_UV_RESULT {json}``). Exit codes: 0 = colour + normal + AO
baked and every gate passed; 3 = the colour map shipped but a map or gate failed (the documented
fallback is a UV map + plain base-colour material); 2 = bad input, or the re-imported FBX material
name is not ``--material-path`` (``verdict.material_path_survived`` false; the FBX is still written
for inspection); 1 = uncaught error. ``--target-tris 0`` turns decimation off.

Units. Blender's unit is the metre (glTF's), Source's is the inch, and **the numbers in the FBX are
Source units**. ``--unit-scale`` (default ``39.3700787``, i.e. 1 Blender unit = 1 m = 39.3700787 in)
says how many Source units one Blender unit is, and the export is driven so the written vertices are
exactly that: ``global_scale=--unit-scale``, ``apply_scale_options='FBX_SCALE_UNITS'`` and
``scene.unit_settings.scale_length = 1/--unit-scale`` (probed on Blender 4.5.15, see
``export_scale_settings``). A 1 x 1 x 1 Blender-unit cube therefore lands in the file as
39.3701 x 39.3701 x 39.3701 with ``UnitScaleFactor 2.54`` — "1 unit = 2.54 cm = 1 inch", numbers in
inches. Source reads those numbers as inches, so a prop compiles at the size the bridge measured; a
metric importer divides by 2.54 x 100 and lands on the same metres. The old setting,
``apply_scale_options='FBX_SCALE_NONE'``, wrote *centimetres* with ``UnitScaleFactor 1.0``: Source
read them as inches and the compiled prop came out **2.53x** too big (measured 2026-10-23 on the
compiled artifact). The geometry's applied scale stays 1 (``normalise.object_scale``) and the axis
conversion is baked into the vertices, so no node in the export carries a scale; the ``export_units``
report block carries ``normalise.bbox_units`` (the intended size), the extent read back out of the
written FBX, and a per-axis check that they agree.

Axis note. ``axis_forward='-Z'``/``axis_up='Y'`` with ``bake_space_transform=True`` writes the
vertices as ``(x, z, -y)`` of Blender's Z-up frame (measured on 4.5.15 with a 1 x 2 x 3 cube), so
``export_units.exported_units`` maps the raw FBX extent back to ``(X, Z, Y)`` — the frame
``bbox_units`` is in.

COLLISION CONTRACT (shared with the task that writes the ``.vmdl``). The ``.vmdl`` emits its
``collision`` block from a mesh named ``<name>_collision`` in this FBX. Rules here:
- if the input GLB contains a mesh whose object name *or* mesh-data name is exactly
  ``<name>_collision``, it is exported as its own object with that exact name: never joined into the
  body, never decimated, never UV-unwrapped, never baked onto, and it gets the same applied transform
  and pivot as the body so both sit in one frame (``collision.outside_body_bbox`` says whether it
  reaches past the body);
- if there is no such mesh, the FBX has no collision object and ``collision.present`` is false;
- the bridge invents no geometry, no name variants and no proxies: an input without the mesh is an
  input without a collision mesh, and the ``.vmdl`` side must handle that (it compiles a
  ``collision`` block whose mesh is missing into ``ERROR``, so the two sides agree on absence).
Exact match only: ``<name>_collision_01``, ``collision`` and ``<name>_coll`` are *not* picked up.

Material keys. ``src/s2docs/asset/material.py`` writes the ``.vmat``: shader
``csgo_vertexlitgeneric.vfx`` and the keys ``TextureMetalness``, ``TextureAmbientOcclusion``,
``TextureColor``, ``TextureNormal``, ``TextureRoughness``. Its naming convention is
``<dir>/<name>_color.png`` beside the ``.vmat`` at ``materials/hammered/<name>/<name>.vmat``; this
script adds ``<name>_normal.png`` and ``<name>_ao.png`` in the same place and reports the resulting
``vmat.layer0`` pairs (``TextureNormal`` -> the normal PNG, ``TextureAmbientOcclusion`` -> the AO PNG)
so that side can consume them instead of the engine defaults
(``materials/default/default_normal.tga``, ``materials/default/default_ao.tga``). The AO map is not
wired into the Blender node tree: Principled BSDF has no AO input, and multiplying AO into Base Color
would bake the occlusion into the diffuse the ``.vmat`` shades separately.

Colour space: the colour bake reads COLOR_0 straight from the GLB bytes, so the result does not depend
on how a given glTF importer converts it. glTF defines COLOR_0 as linear, but TripoSR's colours are
sRGB-encoded values written as-is: on job eefa8331d9cf4056, projecting the front-facing vertices
onto ``input_processed.png`` gave a mean abs error of 0.107 reading the bytes as sRGB, against
0.177 reading them as linear (0.30 with R/B swapped). So the default ``--vertex-color-space srgb``
makes each texel equal the COLOR_0 byte; ``linear`` follows the spec instead and comes out brighter.
Normal and AO are geometry, not colour: they are written Non-Color (tangent-space data, no transform)
and sRGB (an 8-bit occlusion mask, which is how a PNG source reaches a ``.vmat``) respectively.

Verification without a render: the normal and AO bakes each get a control bake that must differ —
normal against a self-bake of the decimated mesh (``normal_bake.control``), AO against a bake with the
high-poly moved ten body diagonals away (``ao_bake.control``). A control that matches means the map
was not sampled from the original high-poly. ``--no-verify-source`` skips the pair (cheaper, less
evidence).

Source 2 next step: copy ``<name>.fbx`` + ``<name>.fbm/`` to ``<addon>/models/<name>/``, then
``s2docs asset --addon <addon> --name <name> --mesh models/<name>/<name>.fbx``, write the material
with ``s2docs asset material --addon <addon> --name <name> --color <name>.fbm/<name>_color.png``
(it lands at the same ``materials/hammered/<name>/<name>.vmat`` path), and compile the ``.vmdl``
with ``resourcecompiler -i <abs .vmdl> -nop4``.

Measured on TripoSR job eefa8331d9cf4056 (29,401 verts / 58,696 tris; ``--target-tris 5000``, 2048 px,
CPU, Blender 4.5.15, 18.9 s total): 58,696 -> **4,900 triangles** (ratio 0.0835, one Decimate pass);
432 UV islands covering 29% of the texture, no zero-area triangle; colour 2,526,746 B, normal
3,110,940 B, AO 2,731,642 B, FBX **185,724 B** (the un-decimated bridge wrote 2,212,140 B for the same
job, so the budget cut the mesh payload 11.9x). Bbox 0.8377 x 0.2964 x 1.0020 BU = **32.98 x 11.67 x
39.45 Source units**; pivot (-0.0139, 0.0588, 0.5023) BU, bbox min after (-0.4189, -0.1482, 0.0).
Colour bake matched the vertex colour to 0.46/255 on average (2 of 4,900 centroids unbaked). Normal
map: B mean 247/255, R/G spread 33/27/255, mean tilt 17.3 deg from the surface normal, and 98% of
texels differ by >4/255 from a self-bake of the decimated mesh — the detail is the high-poly's. AO:
linear mean 0.894 (p5 0.624, median 0.939), and 99.7% of texels change when the high-poly is moved out
of ray range. The collision seam was exercised on a two-mesh fixture: a 12-triangle mesh named
``probe_collision`` came back out of the FBX as its own 12-triangle object with no UVs, no material
and the body's pivot. Reports: ``docs/evidence/hammered/tsr_eefa8331_meshq_bridge.json``.

The un-decimated bridge's colour-only numbers, for comparison: 2,197 UV islands over 21% of the
texture, centroid texels matching the vertex colour to 0.5/255 on average, 0.36% of triangles
(sub-texel islands) carrying a wrong colour (>8/255).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import sys
import time
import zlib

import bpy

try:  # Blender always ships numpy; the pure helpers stay importable without it (unit tests).
    import numpy as np
except ImportError:  # pragma: no cover - only on a stripped interpreter
    np = None

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")  # same rule as s2docs.asset.model.check_name
# addon-relative, lowercase, forward slashes, ends in .vmat — what s2docs.asset.material writes
MATERIAL_PATH_RE = re.compile(r"^[a-z0-9_]+(/[a-z0-9_]+)*\.vmat$")
MAX_ID_NAME = 63  # Blender truncates ID names (Material.name) beyond 63 characters
RESULT_TAG = "GLB_TO_FBX_UV_RESULT"
BAKE_ATTR = "bake_color"
DEFAULT_TARGET_TRIS = 5000
# 1 Blender unit is the metre (glTF's unit); a Source unit is the inch.
SOURCE_UNITS_PER_BU = 39.3700787
MATERIAL_ROOT = "materials/hammered"  # s2docs.asset.material.MATERIAL_ROOT
# The `.vmat` keys each map lands on, in s2docs.asset.material.render_vmat's order.
VMAT_TEXTURE_KEYS = {"color": "TextureColor", "normal": "TextureNormal", "ao": "TextureAmbientOcclusion"}
TEXTURE_KINDS = ("color", "normal", "ao")
UNBAKED_RGB = (1.0, 0.0, 1.0)  # magenta: no unit-colour and no unit-normal can encode to it
DECIMATE_SAFETY = 0.98  # aim just under the budget: Decimate's ratio is a face-count estimate
# An FBX UnitScaleFactor of 2.54 means "one file unit is 2.54 cm" = one inch.
CM_PER_INCH = 2.54
# The exported numbers are the proof the single unit factor was right. Against the render mesh that was
# actually exported, 0.5 % (float32 vertices, a baked axis conversion); against ``bbox_units`` — the
# HIGH-POLY body — 5 %, because decimation may shrink the box a little while a units mistake is 2.5x.
EXPORT_UNIT_TOL_MESH = 0.005
EXPORT_UNIT_TOL_INTENDED = 0.05
# The re-import measures the file in a metre scene, so the only conversion left is the file's own unit
# declaration: an honest file comes back at the bridge's extent, a centimetre file declared as inches
# comes back 2.54x too big.
REIMPORT_SCENE_SCALE_LENGTH = 1.0
_GLB_MAGIC, _CHUNK_JSON, _CHUNK_BIN = 0x46546C67, 0x4E4F534A, 0x004E4942
_DTYPES = {5120: "int8", 5121: "uint8", 5122: "int16", 5123: "uint16", 5125: "uint32", 5126: "float32"}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


class BridgeError(Exception):
    """Bad input: reported with exit code 2."""


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="glb_to_fbx_uv.py", description=__doc__.splitlines()[0])
    p.add_argument("--glb", required=True, help="input .glb (per-vertex colour in COLOR_0)")
    p.add_argument("--out-dir", required=True, help="directory for <name>.fbx and the baked PNGs")
    p.add_argument("--name", required=True, help="model/material name, [a-z][a-z0-9_]{1,62}")
    p.add_argument("--res", type=int, default=2048, help="baked texture size in px (square), all maps")
    p.add_argument("--target-tris", type=int, default=DEFAULT_TARGET_TRIS,
                   help=f"triangle budget for the exported mesh (default {DEFAULT_TARGET_TRIS}); 0 = no decimation")
    p.add_argument("--angle-limit", type=float, default=66.0, help="smart UV project angle limit, degrees")
    p.add_argument("--island-margin-px", type=float, default=8.0, help="gap between UV islands, in texels")
    p.add_argument("--bake-margin", type=int, default=16, help="bake margin (gutter fill), in texels")
    p.add_argument("--samples", type=int, default=4, help="Cycles samples per texel for the colour bake")
    p.add_argument("--ao-samples", type=int, default=16, help="Cycles samples per texel for the AO bake")
    p.add_argument("--cage-pct", type=float, default=2.0,
                   help="selected-to-active extrusion, percent of the body bbox diagonal")
    p.add_argument("--ao-distance-pct", type=float, default=50.0,
                   help="AO ray distance, percent of the body bbox diagonal")
    p.add_argument("--unit-scale", type=float, default=SOURCE_UNITS_PER_BU,
                   help="Source units per Blender unit for reporting (default 39.3700787 = m -> in)")
    p.add_argument("--shade", choices=("smooth", "flat", "keep"), default="smooth",
                   help="normals Blender computes for the export (the GLB has none)")
    p.add_argument("--vertex-color-space", choices=("srgb", "linear"), default="srgb",
                   help="how to read COLOR_0: srgb = TripoSR's real encoding (default), linear = glTF spec")
    p.add_argument("--export-vertex-colors", action="store_true", help="also write the colours as an FBX layer")
    p.add_argument("--verify-source", action=argparse.BooleanOptionalAction, default=True,
                   help="bake the normal/AO control maps that prove the high-poly was the source (default on)")
    p.add_argument("--material-path", default=None,
                   help="FBX material name = the Source 2 .vmat path (default materials/hammered/<name>/<name>.vmat)")
    p.add_argument("--force", action="store_true", help="replace an existing <name>.fbx")
    args = p.parse_args(argv)
    if not NAME_RE.fullmatch(args.name):
        raise BridgeError(f"--name {args.name!r} must match {NAME_RE.pattern}")
    if args.material_path is None:
        args.material_path = f"{MATERIAL_ROOT}/{args.name}/{args.name}.vmat"
    if not MATERIAL_PATH_RE.fullmatch(args.material_path) or ".." in args.material_path.split("/"):
        raise BridgeError(f"--material-path {args.material_path!r} must match {MATERIAL_PATH_RE.pattern}")
    if len(args.material_path) > MAX_ID_NAME:
        raise BridgeError(f"--material-path is {len(args.material_path)} chars; Blender truncates ID names "
                          f"over {MAX_ID_NAME}")
    if not 64 <= args.res <= 8192:
        raise BridgeError("--res must be within 64..8192")
    # 0 = off; below 4 no triangle budget is meaningful (a tetrahedron is the smallest closed mesh),
    # and a budget the mesh already meets is a no-op, so huge values are allowed but bounded.
    if not 0 <= args.target_tris <= 10_000_000:
        raise BridgeError("--target-tris must be within 0..10000000 (0 = no decimation)")
    if args.target_tris and args.target_tris < 4:
        raise BridgeError("--target-tris must be 0 (off) or at least 4 (one closed tetrahedron)")
    if not 0.0 < args.unit_scale <= 10_000.0:
        raise BridgeError("--unit-scale must be within 0..10000 Source units per Blender unit")
    if not 0.0 <= args.cage_pct <= 100.0:
        raise BridgeError("--cage-pct must be within 0..100 (percent of the bbox diagonal)")
    if not 0.0 < args.ao_distance_pct <= 1000.0:
        raise BridgeError("--ao-distance-pct must be within 0..1000 (percent of the bbox diagonal)")
    if args.ao_samples < 1 or args.samples < 1:
        raise BridgeError("--samples and --ao-samples must be at least 1")
    if args.bake_margin < 0 or args.island_margin_px < 0:
        raise BridgeError("--bake-margin and --island-margin-px must not be negative")
    return args


# --- naming and units (pure; mirrors src/s2docs/asset/material.py) -------------------------------


def texture_name(name: str, kind: str) -> str:
    """``<name>_color.png`` / ``<name>_normal.png`` / ``<name>_ao.png`` — material.py's convention."""
    if kind not in TEXTURE_KINDS:
        raise BridgeError(f"texture kind {kind!r} is not one of {list(TEXTURE_KINDS)}")
    return f"{name}_{kind}.png"


def vmat_dir(name: str) -> str:
    """Addon-relative directory of the material: ``materials/hammered/<name>``."""
    return f"{MATERIAL_ROOT}/{name}"


def vmat_textures(name: str) -> dict[str, str]:
    """``{vmat key: addon-relative PNG path}`` for the three maps this bridge writes."""
    return {VMAT_TEXTURE_KEYS[kind]: f"{vmat_dir(name)}/{texture_name(name, kind)}" for kind in TEXTURE_KINDS}


def vmat_layer0(name: str, material_path: str) -> list[list[str]]:
    """The ``.vmat`` pairs this bridge would write, in ``material.render_vmat``'s order and spelling.

    Shader, metalness and roughness are the values that module pins; only the three texture paths move
    off the engine defaults (``materials/default/default_normal.tga`` / ``default_ao.tga``) to the maps
    baked here. ``src/s2docs/asset/material.py`` owns the rendering; this is the seam it consumes.
    """
    if material_path != f"{vmat_dir(name)}/{name}.vmat":
        raise BridgeError(f"material path {material_path!r} is not beside the textures of {name!r}")
    paths = vmat_textures(name)
    return [
        ["shader", "csgo_vertexlitgeneric.vfx"],
        ["TextureMetalness", "[0.000000 0.000000 0.000000 0.000000]"],
        [VMAT_TEXTURE_KEYS["ao"], paths[VMAT_TEXTURE_KEYS["ao"]]],
        [VMAT_TEXTURE_KEYS["color"], paths[VMAT_TEXTURE_KEYS["color"]]],
        [VMAT_TEXTURE_KEYS["normal"], paths[VMAT_TEXTURE_KEYS["normal"]]],
        ["TextureRoughness", "materials/default/default_rough.tga"],
    ]


def collision_mesh_name(name: str) -> str:
    """The collision mesh the ``.vmdl`` task sources its ``collision`` block from."""
    return f"{name}_collision"


def pick_collision(names: list[str], name: str) -> str | None:
    """The collision mesh name if the input really has it, else ``None``. Exact match, no guessing."""
    want = collision_mesh_name(name)
    return want if want in set(names) else None


def decimate_ratio(tris_now: int, target: int) -> float:
    """Decimate COLLAPSE ratio to reach ``target`` triangles, or 1.0 when already there / off."""
    if target <= 0 or tris_now <= target:
        return 1.0
    return max(0.0, min(1.0, target / tris_now * DECIMATE_SAFETY))


def base_centre_pivot(bb_min: list[float], bb_max: list[float]) -> list[float]:
    """Translation putting the bbox x/y centre and min z at the origin (Source's prop pivot)."""
    return [round(-(bb_min[0] + bb_max[0]) / 2.0, 9),
            round(-(bb_min[1] + bb_max[1]) / 2.0, 9),
            round(-bb_min[2], 9)]


def to_source_units(vec: list[float], unit_scale: float) -> list[float]:
    """Blender units (metres) -> Source units (inches), rounded to 0.01 unit."""
    return [round(float(v) * unit_scale, 2) for v in vec]


def decimate_report(tris_in: int, tris_out: int, target: int, passes: list[dict]) -> dict:
    """The ``decimate`` block: what went in, what came out, and the achieved ratio."""
    enabled = target > 0
    return {
        "enabled": enabled,
        "target_tris": target,
        "tris_in": tris_in,
        "tris_out": tris_out,
        "ratio": round(tris_out / tris_in, 6) if tris_in else None,
        "target_ratio": round(min(1.0, target / tris_in), 6) if enabled and tris_in else 1.0,
        "passes": passes,
        "within_budget": (tris_out <= target) if enabled else True,
    }


def normalise_report(bbox_bu: list[float], pivot_bu: list[float], unit_scale: float,
                     bbox_min_after_bu: list[float] | None = None) -> dict:
    """The ``normalise`` block: box and pivot in Blender units and in Source units (inches)."""
    return {
        "unit": "Source unit (1 unit = 1 inch)",
        "unit_scale": round(float(unit_scale), 7),
        "unit_scale_basis": "1 Blender unit = 1 m = 39.3700787 in",
        "bbox_bu": [round(float(v), 6) for v in bbox_bu],
        "bbox_units": to_source_units(bbox_bu, unit_scale),
        "pivot_bu": [round(float(v), 6) for v in pivot_bu],
        "pivot_units": to_source_units(pivot_bu, unit_scale),
        "pivot_rule": "bbox x/y centre and min z -> origin (0,0,0); object transform identity, no scale node",
        "z_up": True,
        "bbox_min_after_bu": [round(float(v), 6) for v in bbox_min_after_bu] if bbox_min_after_bu else None,
    }


def export_scale_settings(unit_scale: float) -> dict:
    """The exporter settings that make the written FBX numbers equal Source units.

    Measured on Blender 4.5.15 (1 x 1 x 1 Blender-unit cube, ``bake_space_transform=True``):

    ==============================  ===========================================  ==========================
    ``apply_scale_options``         written vertex extent                        ``UnitScaleFactor``
    ==============================  ===========================================  ==========================
    ``FBX_SCALE_NONE`` (old)        100 x 100 x 100  (centimetres)               1.0  (reads as cm)
    ``FBX_SCALE_UNITS`` + sl 0.0254 39.3701 x 39.3701 x 39.3701  (inches)        2.54 (= one inch)
    ==============================  ===========================================  ==========================

    so ``FBX_SCALE_UNITS`` with ``scene.unit_settings.scale_length = 1/unit_scale`` is what makes a
    Source importer — which reads the numbers as inches — get the authored size. The scene's unit
    scale is only what the exporter reads to compute the declared factor (``USF = 100 /
    unit_scale``); the geometry keeps applied scale 1, and ``bake_space_transform`` puts the axis
    conversion (and this scale) into the vertices, so no node carries a scale.
    """
    unit_scale = float(unit_scale)
    if not unit_scale > 0.0:
        raise BridgeError(f"unit scale {unit_scale!r} must be positive")
    return {
        "global_scale": round(unit_scale, 7),
        "apply_unit_scale": True,
        "apply_scale_options": "FBX_SCALE_UNITS",
        "scene_scale_length": round(1.0 / unit_scale, 9),
        "expected_unit_scale_factor": round(100.0 / unit_scale, 6),
    }


def unit_check(expected: list[float], measured: list[float], tolerance: float) -> dict:
    """Per-axis ``measured / expected`` and the worst relative error, against ``tolerance``."""
    if len(expected) != len(measured):
        raise BridgeError(f"cannot compare {len(expected)} expected axes against {len(measured)} measured")
    ratios = [round(float(m) / float(e), 5) if e else None for e, m in zip(expected, measured, strict=True)]
    errors = [abs(r - 1.0) for r in ratios if r is not None]
    return {
        "expected": [round(float(v), 4) for v in expected],
        "measured": [round(float(v), 4) for v in measured],
        "ratio": ratios,
        "max_rel_err": round(max(errors), 5) if errors else None,
        "tolerance": tolerance,
        "ok": bool(errors) and max(errors) <= tolerance,
    }


def export_units_report(unit_scale: float, settings: dict, measured: dict,
                        intended_units: list[float], mesh_units: list[float], body_name: str,
                        tol_mesh: float = EXPORT_UNIT_TOL_MESH,
                        tol_intended: float = EXPORT_UNIT_TOL_INTENDED) -> dict:
    """The ``export_units`` block: what the FBX really contains, next to what the bridge intended.

    ``intended_units`` is ``normalise.bbox_units`` (the high-poly body, inches) and ``mesh_units`` is
    the render mesh that went into the file (also inches). Both are compared per axis against the
    extent read back out of the written FBX, so a units error is visible in the report without a
    compile: centimetres read as inches shows up as a ratio of ~2.54, not as a plausible number.
    """
    exported = None
    if isinstance(measured.get("objects"), list):
        body = next((o for o in measured["objects"] if o.get("name") == body_name), None)
        exported = body.get("extent_z_up") if body else None
    declared = measured.get("unit_scale_factor")
    expected_declared = settings.get("expected_unit_scale_factor")
    report: dict = {
        "unit": "Source unit (1 unit = 1 inch)",
        "unit_scale": round(float(unit_scale), 7),
        "exporter": settings,
        "file": measured,
        "body_name": body_name,
        "intended_units": [round(float(v), 4) for v in intended_units],
        "mesh_units": [round(float(v), 4) for v in mesh_units],
        "exported_units": exported,
    }
    if exported is None:
        report["ok"] = False
        report["why"] = (f"the written FBX has no geometry named {body_name!r}, so its numeric extent "
                         f"cannot be checked against bbox_units")
        return report
    report["check_vs_mesh"] = unit_check(mesh_units, exported, tol_mesh)
    report["check_vs_intended"] = unit_check(intended_units, exported, tol_intended)
    report["unit_scale_factor_matches"] = bool(
        declared is not None and expected_declared
        and abs(float(declared) - float(expected_declared)) <= 1e-4 * float(expected_declared)
    )
    report["ok"] = bool(report["check_vs_mesh"]["ok"] and report["check_vs_intended"]["ok"]
                        and report["unit_scale_factor_matches"])
    return report


# --- GLB reading (only what the colour bake needs) ----------------------------------------------


def read_glb(path: str) -> tuple[dict, bytes]:
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 20:
        raise BridgeError(f"{path} is too small to be a GLB")
    magic, _version, length = struct.unpack_from("<III", data, 0)
    if magic != _GLB_MAGIC:
        raise BridgeError(f"{path} is not a GLB (bad magic)")
    off, doc, blob = 12, None, b""
    while off + 8 <= min(length, len(data)):
        clen, ctype = struct.unpack_from("<II", data, off)
        off += 8
        chunk = data[off:off + clen]
        off += clen
        if ctype == _CHUNK_JSON:
            doc = json.loads(chunk.decode("utf-8"))
        elif ctype == _CHUNK_BIN:
            blob = chunk
    if doc is None:
        raise BridgeError(f"{path} has no JSON chunk")
    return doc, blob


def read_accessor(doc: dict, blob: bytes, index: int) -> np.ndarray:
    acc = doc["accessors"][index]
    if "bufferView" not in acc or "sparse" in acc:
        raise BridgeError("sparse or bufferView-less accessors are not supported")
    view = doc["bufferViews"][acc["bufferView"]]
    if view.get("buffer", 0) != 0:
        raise BridgeError("only the GLB's own binary chunk is supported")
    dtype = np.dtype(_DTYPES[acc["componentType"]])
    ncomp = _NCOMP[acc["type"]]
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride") or dtype.itemsize * ncomp
    count = acc["count"]
    if stride == dtype.itemsize * ncomp:
        arr = np.frombuffer(blob, dtype=dtype, count=count * ncomp, offset=start).reshape(count, ncomp)
    else:
        raw = np.frombuffer(blob, dtype=np.uint8, count=stride * (count - 1) + dtype.itemsize * ncomp, offset=start)
        rows = np.lib.stride_tricks.as_strided(raw, shape=(count, dtype.itemsize * ncomp), strides=(stride, 1))
        arr = rows.copy().view(dtype).reshape(count, ncomp)
    if acc.get("normalized") and dtype.kind == "u":
        return arr.astype(np.float64) / float(np.iinfo(dtype).max)
    return arr.astype(np.float64)


def glb_facts(doc: dict) -> tuple[dict, list[dict]]:
    prims = [p for m in doc.get("meshes", []) for p in m.get("primitives", [])]
    facts = {
        "generator": doc.get("asset", {}).get("generator"),
        "primitives": len(prims),
        "attributes": sorted({k for p in prims for k in p.get("attributes", {})}),
        "materials": len(doc.get("materials", [])),
        "textures": len(doc.get("textures", [])),
        "images": len(doc.get("images", [])),
        "vertices": sum(doc["accessors"][p["attributes"]["POSITION"]]["count"] for p in prims),
        "mesh_names": [m.get("name") for m in doc.get("meshes", [])],
        "node_names": [n.get("name") for n in doc.get("nodes", [])],
    }
    return facts, prims


# --- helpers -------------------------------------------------------------------------------------


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1.0 / 2.4) - 0.055)


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.04045, x / 12.92, np.power((x + 0.055) / 1.055, 2.4))


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def empty_scene() -> None:
    for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.materials, bpy.data.images,
                 bpy.data.cameras, bpy.data.lights):
        for item in list(coll):
            coll.remove(item)


def world_vertices(obj: bpy.types.Object) -> np.ndarray:
    me = obj.data
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", co)
    mw = np.array(obj.matrix_world)
    return co.reshape(-1, 3).astype(np.float64) @ mw[:3, :3].T + mw[:3, 3]


def world_extent(obj: bpy.types.Object) -> list[float]:
    """Bounding-box size in world space (``Object.dimensions`` ignores rotation)."""
    w = world_vertices(obj)
    return [round(float(v), 4) for v in w.max(0) - w.min(0)]


def world_bbox(objs: list[bpy.types.Object]) -> tuple[np.ndarray, np.ndarray]:
    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    for o in objs:
        w = world_vertices(o)
        if len(w):
            lo = np.minimum(lo, w.min(0))
            hi = np.maximum(hi, w.max(0))
    return lo, hi


def mesh_arrays(me: bpy.types.Mesh) -> dict:
    n_loops = len(me.loops)
    loop_vert = np.empty(n_loops, np.int32)
    me.loops.foreach_get("vertex_index", loop_vert)
    me.calc_loop_triangles()
    tris = np.empty(len(me.loop_triangles) * 3, np.int32)
    me.loop_triangles.foreach_get("loops", tris)
    starts = np.empty(len(me.polygons), np.int32)
    totals = np.empty(len(me.polygons), np.int32)
    me.polygons.foreach_get("loop_start", starts)
    me.polygons.foreach_get("loop_total", totals)
    loop_poly = np.empty(n_loops, np.int64)
    for pi, (s, t) in enumerate(zip(starts.tolist(), totals.tolist(), strict=True)):
        loop_poly[s:s + t] = pi
    return {"loop_vert": loop_vert, "tris": tris.reshape(-1, 3), "loop_poly": loop_poly}


def loop_uvs(me: bpy.types.Mesh) -> np.ndarray:
    uvs = np.empty(len(me.loops) * 2, np.float32)
    me.uv_layers.active.data.foreach_get("uv", uvs)
    return uvs.reshape(-1, 2).astype(np.float64)


def count_uv_islands(n_polys: int, loop_vert: np.ndarray, loop_poly: np.ndarray, uvs: np.ndarray) -> int:
    """Polygons sharing a vertex with the same UV are one island (union-find over loop groups)."""
    q = np.round(uvs * (1 << 20)).astype(np.int64)
    _, group = np.unique(np.stack([loop_vert.astype(np.int64), q[:, 0], q[:, 1]], 1), axis=0, return_inverse=True)
    group = group.ravel()
    order = np.argsort(group, kind="stable")
    parent = list(range(n_polys))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    prev, first = -1, -1
    for g, p in zip(group[order].tolist(), loop_poly[order].tolist(), strict=True):
        if g != prev:
            prev, first = g, p
            continue
        ra, rb = find(first), find(p)
        if ra != rb:
            parent[rb] = ra
    return len({find(i) for i in range(n_polys)})


def fbx_scan(path: str, png_names: list[str]) -> dict:
    with open(path, "rb") as fh:
        raw = fh.read()
    return {
        "binary": raw.startswith(b"Kaydara FBX Binary"),
        "LayerElementUV": raw.count(b"LayerElementUV"),
        "LayerElementNormal": raw.count(b"LayerElementNormal"),
        "LayerElementMaterial": raw.count(b"LayerElementMaterial"),
        "LayerElementColor": raw.count(b"LayerElementColor"),
        "texture_refs": {n: raw.count(n.encode()) for n in png_names},
    }


# --- what the written FBX says (pure: a binary reader, no bpy) -----------------------------------
#
# The only honest measure of the unit factor is the file itself: Blender's importer applies its own
# cm/inch conversion, so a re-import cannot tell "inches in the file" from "centimetres declared as
# inches". Reading the bytes reports the numbers Source will read. The format is the FBX binary
# record tree; only what is needed here is decoded (GlobalSettings/UnitScaleFactor, Objects/Geometry/
# Vertices, Connections for the geometry -> model names).

_FBX_MAGIC = b"Kaydara FBX Binary  \x00"
_FBX_SCALARS = {"Y": ("<h", 2), "C": ("<B", 1), "I": ("<i", 4), "F": ("<f", 4), "D": ("<d", 8),
                "L": ("<q", 8)}
_FBX_ARRAYS = {"f": "f", "d": "d", "l": "q", "i": "i", "b": "b"}


def _fbx_properties(raw: bytes, offset: int, count: int) -> tuple[list, int]:
    """Decode ``count`` FBX property records; returns them and the offset just after them."""
    props: list = []
    for _ in range(count):
        kind = chr(raw[offset])
        offset += 1
        if kind in _FBX_SCALARS:
            fmt, size = _FBX_SCALARS[kind]
            props.append(struct.unpack_from(fmt, raw, offset)[0])
            offset += size
        elif kind in _FBX_ARRAYS:
            n, encoding, length = struct.unpack_from("<III", raw, offset)
            offset += 12
            data = raw[offset:offset + length]
            offset += length
            if encoding == 1:  # zlib-deflated array (what Blender writes for vertices)
                data = zlib.decompress(data)
            props.append(list(struct.unpack_from(f"<{n}{_FBX_ARRAYS[kind]}", data, 0)))
        elif kind in ("S", "R"):
            length = struct.unpack_from("<I", raw, offset)[0]
            offset += 4
            data = raw[offset:offset + length]
            offset += length
            props.append(data.decode("utf-8", "replace") if kind == "S" else data)
        else:
            raise ValueError(f"FBX property type {kind!r} at offset {offset - 1} is not one this "
                             f"reader handles")
    return props, offset


def _fbx_node(raw: bytes, offset: int, wide: bool) -> tuple[dict | None, int]:
    """One record. A null record (end offset 0) ends a record list and comes back as ``None``."""
    fmt = "<QQQ" if wide else "<III"
    end, count, _props_len = struct.unpack_from(fmt, raw, offset)
    offset += struct.calcsize(fmt)
    if end == 0:
        return None, offset
    name_len = raw[offset]
    offset += 1
    name = raw[offset:offset + name_len].decode("utf-8", "replace")
    offset += name_len
    props, offset = _fbx_properties(raw, offset, count)
    children = []
    while offset < end:
        child, offset = _fbx_node(raw, offset, wide)
        if child is None:
            offset = end
            break
        children.append(child)
    return {"name": name, "props": props, "children": children}, end


def _fbx_tree(path: str) -> tuple[int, list[dict]]:
    """``(version, top-level records)`` of a binary FBX."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if len(raw) < 27 or not raw.startswith(_FBX_MAGIC):
        raise ValueError(f"{path} is not a binary FBX")
    version = struct.unpack_from("<I", raw, 23)[0]
    wide = version >= 7500
    header = struct.calcsize("<QQQ" if wide else "<III")
    offset, nodes = 27, []
    while offset + header <= len(raw):
        node, offset = _fbx_node(raw, offset, wide)
        if node is None:
            break
        nodes.append(node)
    return version, nodes


def _fbx_props70(node: dict) -> dict:
    """``{property name: last value}`` of a record's ``Properties70`` block."""
    out: dict = {}
    for block in node["children"]:
        if block["name"] != "Properties70":
            continue
        for rec in block["children"]:
            if rec["name"] == "P" and rec["props"] and isinstance(rec["props"][0], str):
                out[rec["props"][0]] = rec["props"][-1]
    return out


def fbx_measure(path: str) -> dict:
    """The unit factor and the raw vertex extent of every mesh object in a binary FBX.

    ``extent`` is in the file's own axes and ``extent_z_up`` is the same box mapped back to Blender's
    Z-up frame — ``(X, Z, Y)`` for this exporter's ``axis_forward='-Z'``/``axis_up='Y'`` with
    ``bake_space_transform=True`` (measured on 4.5.15 with a 1 x 2 x 3 cube) — which is the frame
    ``normalise.bbox_units`` is in. Never raises: a file this reader cannot decode comes back as
    ``{"error": ...}`` so the run still ships the FBX and reports a failed gate instead of dying.
    """
    try:
        version, nodes = _fbx_tree(path)
        unit_scale, declared = 1.0, False
        model_names: dict[int, str] = {}
        links: dict[int, int] = {}
        objects: list[dict] = []
        for node in nodes:
            if node["name"] == "GlobalSettings":
                props = _fbx_props70(node)
                if "UnitScaleFactor" in props:
                    unit_scale, declared = float(props["UnitScaleFactor"]), True
            elif node["name"] == "Objects":
                for obj in node["children"]:
                    uid = int(obj["props"][0]) if obj["props"] else 0
                    if obj["name"] == "Model":
                        # The exporter appends "\x00\x01<kind>" to a Model's name (Blender's importer
                        # splits it off again), so the object name is the part before the NUL.
                        raw_name = str(obj["props"][1]) if len(obj["props"]) > 1 else ""
                        model_names[uid] = raw_name.split("\x00", 1)[0] or f"uid:{uid}"
                    elif obj["name"] == "Geometry":
                        verts = next((c["props"][0] for c in obj["children"]
                                      if c["name"] == "Vertices" and c["props"]), None)
                        if not verts:
                            continue
                        xs, ys, zs = verts[0::3], verts[1::3], verts[2::3]
                        extent = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]
                        objects.append({
                            "uid": uid, "name": None, "vertices": len(verts) // 3,
                            "extent": [round(v, 4) for v in extent],
                            "extent_z_up": [round(extent[0], 4), round(extent[2], 4), round(extent[1], 4)],
                        })
            elif node["name"] == "Connections":
                for conn in node["children"]:
                    if conn["name"] == "C" and len(conn["props"]) >= 3 and conn["props"][0] == "OO":
                        links[int(conn["props"][1])] = int(conn["props"][2])
        for obj in objects:  # a Geometry is connected to the Model that owns it
            parent = links.get(obj["uid"])
            obj["name"] = (model_names.get(parent) if parent is not None else None) or f"uid:{obj['uid']}"
        return {
            "path": path,
            "version": version,
            "unit_scale_factor": round(unit_scale, 6),
            "unit_scale_factor_declared": declared,
            "unit_inches": round(unit_scale / CM_PER_INCH, 6),
            "unit_note": "FBX UnitScaleFactor is cm per file unit: 1.0 = centimetre, 2.54 = inch",
            "axis_order": "extent is raw (X, Y, Z); extent_z_up is (X, Z, Y), the frame bbox_units uses",
            "objects": objects,
        }
    except (OSError, ValueError, struct.error, zlib.error) as exc:
        return {"path": path, "error": f"{type(exc).__name__}: {exc}", "objects": []}


# --- pipeline stages -----------------------------------------------------------------------------


def join_objects(objs: list[bpy.types.Object], new_name: str) -> bpy.types.Object:
    """Join ``objs`` into one object (identity-free: transforms are already applied) and name it."""
    if len(objs) > 1:
        bpy.ops.object.select_all(action="DESELECT")
        for o in objs:
            o.select_set(True)
        bpy.context.view_layer.objects.active = objs[0]
        bpy.ops.object.join()
        obj = bpy.context.view_layer.objects.active
    else:
        obj = objs[0]
    obj.name = obj.data.name = new_name
    return obj


def import_glb(path: str, shade: str, name: str) -> tuple[bpy.types.Object, bpy.types.Object | None, dict]:
    """Import, drop non-meshes, split off the collision mesh, join the rest into the body."""
    shading = {"smooth": "SMOOTH", "flat": "FLAT", "keep": "NORMALS"}[shade]
    bpy.ops.import_scene.gltf(filepath=path, import_shading=shading, merge_vertices=False)
    scene = bpy.context.scene
    meshes = [o for o in scene.objects if o.type == "MESH"]
    if not meshes:
        raise BridgeError("the GLB produced no mesh objects")
    for o in meshes:
        if o.parent is not None:
            world = o.matrix_world.copy()
            o.parent = None
            o.matrix_world = world
    for o in [o for o in scene.objects if o.type != "MESH"]:
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.ops.object.select_all(action="DESELECT")

    # The collision seam: an exact "<name>_collision" object/data name is authored input geometry.
    names = [o.name for o in meshes] + [o.data.name for o in meshes]
    coll_name = pick_collision(names, name)
    coll_objs = [o for o in meshes if o.name == coll_name or o.data.name == coll_name] if coll_name else []
    body = [o for o in meshes if o not in coll_objs]
    if not body:
        raise BridgeError(f"the GLB has only {coll_name!r}; there is no body mesh to export")
    info = {
        "objects": [o.name for o in meshes],
        "body_objects_joined": len(body),
        "collision_objects": [o.name for o in coll_objs],
    }
    coll = join_objects(coll_objs, coll_name) if coll_objs else None
    if coll is not None and coll.name != coll_name:
        raise BridgeError(f"could not name the collision object {coll_name!r} (Blender kept {coll.name!r})")
    body_obj = join_objects(body, f"{name}_high")
    return body_obj, coll, info


def apply_transforms(objs: list[bpy.types.Object]) -> None:
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def normalise(objs: list[bpy.types.Object], body: bpy.types.Object, unit_scale: float) -> dict:
    """Z-up (Blender's own frame), identity transform, origin at the body bbox's base centre.

    The pivot comes from the *body* only, so adding or dropping a collision mesh cannot move the
    prop's origin; every object gets the same translation, so they stay in one frame.
    """
    apply_transforms(objs)
    lo, hi = world_bbox([body])
    bbox_bu = (hi - lo).tolist()
    pivot = base_centre_pivot(lo.tolist(), hi.tolist())
    for o in objs:
        o.location = pivot
    apply_transforms(objs)
    lo2, _hi2 = world_bbox([body])
    scale = [round(float(v), 6) for v in body.scale]
    rotation = [round(math.degrees(v), 4) for v in body.rotation_euler]
    if any(abs(s - 1.0) > 1e-5 for s in scale) or any(abs(r) > 1e-4 for r in rotation):
        raise BridgeError(f"transform_apply left scale {scale} / rotation {rotation} on the body")
    report = normalise_report(bbox_bu, pivot, unit_scale, lo2.tolist())
    report["object_scale"] = scale
    report["object_rotation_deg"] = rotation
    return report


def apply_shading(obj: bpy.types.Object, shade: str) -> None:
    if shade == "keep":
        return
    me = obj.data
    me.polygons.foreach_set("use_smooth", np.full(len(me.polygons), shade == "smooth", bool))
    me.update()


def duplicate_object(src: bpy.types.Object, name: str) -> bpy.types.Object:
    dup = src.copy()
    dup.data = src.data.copy()
    dup.name = dup.data.name = name
    bpy.context.scene.collection.objects.link(dup)
    dup.parent = None
    dup.matrix_world = src.matrix_world.copy()
    if dup.name != name:
        raise BridgeError(f"could not name the render mesh {name!r} (Blender kept {dup.name!r})")
    return dup


def decimate_to(obj: bpy.types.Object, target: int, max_passes: int = 3) -> dict:
    """Decimate COLLAPSE to at or under ``target`` triangles (retries: the ratio is an estimate)."""
    tris_in = len(mesh_arrays(obj.data)["tris"])
    tris_now = tris_in
    passes: list[dict] = []
    if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    while target > 0 and tris_now > target and len(passes) < max_passes:
        ratio = decimate_ratio(tris_now, target)
        mod = obj.modifiers.new(f"meshq_decimate_{len(passes)}", "DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = ratio
        mod.use_collapse_triangulate = True
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.modifier_apply(modifier=mod.name)
        tris_now = len(mesh_arrays(obj.data)["tris"])
        passes.append({"ratio": round(ratio, 6), "tris": tris_now})
    return decimate_report(tris_in, tris_now, target, passes)


def nearest_colours(bvh, src_world: np.ndarray, tri_verts: np.ndarray, tri_loops: np.ndarray,
                    src_loop_rgb: np.ndarray, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Colour of the nearest point on the source surface, barycentric over that triangle's corners.

    ``tri_verts`` index ``src_world`` (the BVH's geometry); ``tri_loops`` index the per-corner colours
    of the same triangles — a joined Blender mesh has more loops than vertices.
    """
    from mathutils import Vector  # Blender-only; imported here so the pure helpers stay testable

    out = np.zeros((len(points), 3))
    dist = np.full(len(points), np.inf)
    for i, p in enumerate(points):
        hit = bvh.find_nearest(Vector(p))
        if hit[0] is None:
            continue
        loc, index = np.asarray(hit[0]), hit[2]
        verts, tri = tri_verts[index], tri_loops[index]
        a, b, c = src_world[verts[0]], src_world[verts[1]], src_world[verts[2]]
        v0, v1, v2 = b - a, c - a, loc - a
        d00, d01, d11 = v0 @ v0, v0 @ v1, v1 @ v1
        d20, d21 = v2 @ v0, v2 @ v1
        den = d00 * d11 - d01 * d01
        if abs(den) < 1e-20:  # degenerate triangle: fall back to its first corner
            out[i] = src_loop_rgb[tri[0]]
        else:
            u = (d11 * d20 - d01 * d21) / den
            v = (d00 * d21 - d01 * d20) / den
            w = 1.0 - u - v
            out[i] = (w * src_loop_rgb[tri[0]] + u * src_loop_rgb[tri[1]] + v * src_loop_rgb[tri[2]])
        dist[i] = float(hit[3])
    return out, dist


def transfer_colours(src: bpy.types.Object, src_loop_rgb: np.ndarray, dst: bpy.types.Object,
                     bbox_diag_bu: float) -> tuple[np.ndarray, dict]:
    """Sample the high-poly's colour onto the decimated mesh's vertices (nearest surface point)."""
    from mathutils.bvhtree import BVHTree

    sa = mesh_arrays(src.data)
    src_world = world_vertices(src)
    # BVHTree wants vertex indices; sa["tris"] holds loop indices (that is what indexes the colours).
    tri_verts = sa["loop_vert"][sa["tris"]]
    bvh = BVHTree.FromPolygons([tuple(v) for v in src_world],
                               [tuple(int(i) for i in t) for t in tri_verts], all_triangles=True)
    da = mesh_arrays(dst.data)
    cols, dist = nearest_colours(bvh, src_world, tri_verts, sa["tris"], src_loop_rgb, world_vertices(dst))
    ok = np.isfinite(dist)
    report = {
        "method": "nearest point on the high-poly surface, barycentric over the hit triangle",
        "vertices_sampled": int(ok.sum()),
        "vertices_unmatched": int((~ok).sum()),
        "distance_bu_mean": round(float(dist[ok].mean()), 6) if ok.any() else None,
        "distance_bu_max": round(float(dist[ok].max()), 6) if ok.any() else None,
        "distance_frac_of_bbox_max": round(float(dist[ok].max()) / bbox_diag_bu, 5)
        if ok.any() and bbox_diag_bu else None,
    }
    return cols[da["loop_vert"]], report


def set_corner_colour(me: bpy.types.Mesh, per_loop_rgb: np.ndarray, name: str = BAKE_ATTR) -> None:
    while me.color_attributes:
        me.color_attributes.remove(me.color_attributes[0])
    attr = me.color_attributes.new(name, "FLOAT_COLOR", "CORNER")
    rgb = np.clip(per_loop_rgb, 0.0, 1.0)
    rgba = np.concatenate([rgb, np.ones((len(rgb), 1))], 1).astype(np.float32)
    attr.data.foreach_set("color", rgba.ravel())


def vertex_colours(obj: bpy.types.Object, doc: dict, blob: bytes, prims: list[dict]) -> tuple[np.ndarray, str]:
    """Per-vertex colour (n_verts x 3, as stored in COLOR_0) in Blender's vertex order."""
    me = obj.data
    if len(prims) == 1 and "COLOR_0" in prims[0]["attributes"]:
        pos = read_accessor(doc, blob, prims[0]["attributes"]["POSITION"])
        col = read_accessor(doc, blob, prims[0]["attributes"]["COLOR_0"])[:, :3]
        if len(me.vertices) == len(pos):
            co = np.empty(len(me.vertices) * 3, np.float32)
            me.vertices.foreach_get("co", co)
            co = co.reshape(-1, 3).astype(np.float64)
            gltf_to_blender = np.stack([pos[:, 0], -pos[:, 2], pos[:, 1]], 1)  # glTF +Y up -> Blender +Z up
            mw = np.array(obj.matrix_world)
            co_world = co @ mw[:3, :3].T + mw[:3, 3]
            if np.abs(co_world - gltf_to_blender).max() < 1e-4:
                return col, "glb COLOR_0 bytes (vertex order verified against POSITION)"
    if me.color_attributes:
        ca = me.color_attributes.active_color or me.color_attributes[0]
        buf = np.empty(len(ca.data) * 4, np.float32)
        ca.data.foreach_get("color", buf)
        buf = buf.reshape(-1, 4)[:, :3].astype(np.float64)
        if ca.domain == "CORNER":  # average the corners of each vertex
            lv = mesh_arrays(me)["loop_vert"]
            acc = np.zeros((len(me.vertices), 3))
            np.add.at(acc, lv, buf)
            buf = acc / np.maximum(np.bincount(lv, minlength=len(me.vertices)), 1)[:, None]
        return buf, f"importer colour attribute {ca.name!r} ({ca.data_type}/{ca.domain}, read as linear)"
    raise BridgeError("no vertex colour: the GLB has no COLOR_0 the bridge can map")


def smart_uv(obj: bpy.types.Object, args: argparse.Namespace) -> str:
    me = obj.data
    while me.uv_layers:
        me.uv_layers.remove(me.uv_layers[0])
    uv = me.uv_layers.new(name="UVMap")
    me.uv_layers.active = uv
    uv.active_render = True
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(args.angle_limit), margin_method="FRACTION",
                             island_margin=args.island_margin_px / args.res, area_weight=0.0,
                             correct_aspect=True, scale_to_bounds=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    return uv.name


def bake_rig(mat, image, emission_attr: str | None) -> None:
    """The bake's node tree: EMIT reads the attribute, other types only need the target image node."""
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    if emission_attr:
        attr = nt.nodes.new("ShaderNodeVertexColor")
        attr.layer_name = emission_attr
        emit = nt.nodes.new("ShaderNodeEmission")
        emit.inputs["Strength"].default_value = 1.0
        nt.links.new(attr.outputs["Color"], emit.inputs["Color"])
        nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    else:
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    target = nt.nodes.new("ShaderNodeTexImage")
    target.image = image
    for node in nt.nodes:
        node.select = False
    target.select = True
    nt.nodes.active = target


def bake_map(obj: bpy.types.Object, mat, args: argparse.Namespace, uv_name: str, *, kind: str,
             image_name: str, colorspace: str, samples: int, source: bpy.types.Object | None = None,
             emission_attr: str | None = None, cage: float = 0.0,
             ao_distance: float | None = None) -> bpy.types.Image:
    """Bake one map onto ``obj``'s UVs; ``source`` (if given) is the high-poly the map is sampled from."""
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = max(1, samples)
    if hasattr(scene.cycles, "use_denoising"):
        scene.cycles.use_denoising = False
    if ao_distance is not None:
        if scene.world is None:
            scene.world = bpy.data.worlds.new("World")
        # Cycles' AO bake reads the ray distance from the world's light settings (verified 4.5.15:
        # the map changes when this changes, 0.2 vs 5.0 differ by 10/255 mean on a test torus).
        scene.world.light_settings.distance = float(ao_distance)
    res = args.res
    img = bpy.data.images.new(image_name, res, res, alpha=False, float_buffer=False)
    img.colorspace_settings.name = colorspace
    img.pixels.foreach_set(np.tile(np.array([*UNBAKED_RGB, 1.0], np.float32), res * res))
    bake_rig(mat, img, emission_attr)
    bpy.ops.object.select_all(action="DESELECT")
    if source is not None:
        source.select_set(True)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bake = scene.render.bake
    bake.margin, bake.margin_type = args.bake_margin, "ADJACENT_FACES"
    bake.use_clear, bake.target = False, "IMAGE_TEXTURES"
    bake.use_selected_to_active = source is not None
    kwargs = dict(type=kind, margin=args.bake_margin, margin_type="ADJACENT_FACES", use_clear=False,
                  target="IMAGE_TEXTURES", uv_layer=uv_name, use_selected_to_active=source is not None,
                  cage_extrusion=float(cage), max_ray_distance=0.0)
    if kind == "NORMAL":
        kwargs["normal_space"] = "TANGENT"  # a game normal map is tangent space, not object space
    bpy.ops.object.bake(**kwargs)
    return img


def image_pixels(img: bpy.types.Image) -> np.ndarray:
    res = img.size[0]
    px = np.empty(res * res * 4, np.float32)
    img.pixels.foreach_get(px)
    return px.reshape(res, res, 4)


def unbaked_mask(px: np.ndarray) -> np.ndarray:
    return (px[:, :, 0] == UNBAKED_RGB[0]) & (px[:, :, 1] == UNBAKED_RGB[1]) & (px[:, :, 2] == UNBAKED_RGB[2])


def fill_unbaked(px: np.ndarray, unbaked: np.ndarray, iterations: int = 8) -> None:
    """Grow baked texels into the still-empty ones (4-neighbour average), then the mean for the rest.

    The bake margin already extended every island; this covers sub-texel triangles the rasteriser
    skipped and keeps low mips from averaging in the sentinel colour.
    """
    rgb = px[:, :, :3]
    filled = ~unbaked
    for _ in range(iterations):
        if filled.all():
            break
        acc = np.zeros_like(rgb)
        cnt = np.zeros(filled.shape, np.float32)
        for shift in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nb = np.roll(filled, shift, axis=(0, 1))
            acc += np.roll(rgb, shift, axis=(0, 1)) * nb[:, :, None]
            cnt += nb
        grow = ~filled & (cnt > 0)
        rgb[grow] = acc[grow] / cnt[grow][:, None]
        filled |= grow
    if not filled.all():
        rgb[~filled] = rgb[filled].mean(0) if filled.any() else 0.5
    px[:, :, 3] = 1.0


def save_image(img: bpy.types.Image, path: str, colorspace: str) -> bpy.types.Image:
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)
    out = bpy.data.images.load(path, check_existing=False)
    out.name = os.path.basename(path)
    out.colorspace_settings.name = colorspace
    return out


def centroid_errors(px: np.ndarray, uvs: np.ndarray, tris: np.ndarray, loop_expected: np.ndarray):
    res = px.shape[0]
    a, b, c = uvs[tris[:, 0]], uvs[tris[:, 1]], uvs[tris[:, 2]]
    area = 0.5 * np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    keep = area > 0
    centre = ((a + b + c) / 3.0)[keep]
    expected = (loop_expected[tris[:, 0]] + loop_expected[tris[:, 1]] + loop_expected[tris[:, 2]])[keep] / 3.0
    x = np.clip((centre[:, 0] * res).astype(np.int64), 0, res - 1)
    y = np.clip((centre[:, 1] * res).astype(np.int64), 0, res - 1)
    return np.abs(px[y, x, :3] - expected) * 255.0, x, y, area[keep] * res * res


def err_stats(err: np.ndarray, prefix: str) -> dict:
    if not len(err):
        return {f"{prefix}abs_err_mean_255": None, f"{prefix}abs_err_p95_255": None,
                f"{prefix}abs_err_p999_255": None, f"{prefix}abs_err_max_255": None,
                f"{prefix}frac_err_gt_8": None}
    worst = err.max(axis=1)
    return {
        f"{prefix}abs_err_mean_255": round(float(err.mean()), 3),
        f"{prefix}abs_err_p95_255": round(float(np.percentile(err, 95)), 3),
        f"{prefix}abs_err_p999_255": round(float(np.percentile(worst, 99.9)), 3),
        f"{prefix}abs_err_max_255": round(float(err.max()), 3),
        f"{prefix}frac_err_gt_8": round(float((worst > 8).mean()), 5),
    }


def check_bake_pixels(px: np.ndarray, uvs: np.ndarray, tris: np.ndarray,
                      loop_expected: np.ndarray) -> tuple[dict, np.ndarray]:
    """Compare each triangle's centroid texel with the colour the shader had there, before and after
    the empty texels are filled."""
    unbaked = unbaked_mask(px)
    if unbaked.all():  # a collapse (degenerate UVs, wrong target) must be reported, not raised
        return {"texels_written_frac": 0.0, "triangles_checked": 0, "error": "no texels were baked",
                **err_stats(np.zeros(0), "baked_"), **err_stats(np.zeros(0), "final_")}, px
    err, x, y, area_texels = centroid_errors(px, uvs, tris, loop_expected)
    hit = ~unbaked[y, x]
    missed = ~hit
    written = px[~unbaked][:, :3]
    stats = {
        "texels_written_frac": round(float(1.0 - unbaked.mean()), 4),
        "triangles_checked": int(len(err)),
        "centroid_texel_unbaked": int(missed.sum()),
        "unbaked_tri_uv_area_texels_max": round(float(area_texels[missed].max()), 3) if missed.any() else None,
        "texel_mean_255": [round(float(v) * 255, 2) for v in written.mean(0)],
        "texel_std_255": [round(float(v) * 255, 2) for v in written.std(0)],
        **err_stats(err[hit], "baked_"),
    }
    fill_unbaked(px, unbaked)
    err_after, _, _, _ = centroid_errors(px, uvs, tris, loop_expected)
    stats.update(err_stats(err_after, "final_"))
    return stats, px


def centroid_coverage(unbaked: np.ndarray, uvs: np.ndarray, tris: np.ndarray) -> dict:
    """Are the triangles' centroid texels baked? Texels outside every UV island are not a failure —
    they are the gutter, and ``fill_unbaked`` dilates real values into them.
    """
    res = unbaked.shape[0]
    centre = (uvs[tris[:, 0]] + uvs[tris[:, 1]] + uvs[tris[:, 2]]) / 3.0
    x = np.clip((centre[:, 0] * res).astype(np.int64), 0, res - 1)
    y = np.clip((centre[:, 1] * res).astype(np.int64), 0, res - 1)
    miss = unbaked[y, x]
    return {"triangles_checked": int(len(miss)), "centroid_texel_unbaked": int(miss.sum()),
            "centroid_texel_unbaked_frac": round(float(miss.mean()) if len(miss) else 1.0, 5)}


def check_normal(px: np.ndarray, uvs: np.ndarray, tris: np.ndarray,
                 expect_detail: bool = True) -> tuple[dict, np.ndarray]:
    """Stats for a tangent-space normal map: how far each baked texel tilts from the surface normal.

    ``expect_detail`` is False when nothing was decimated: then the bake source *is* the mesh, the map
    is legitimately flat, and a zero spread must not be read as a failure.
    """
    unbaked = unbaked_mask(px)
    baked = px[~unbaked][:, :3]
    stats: dict = {"unbaked_frac": round(float(unbaked.mean()), 5), "texels_baked": int((~unbaked).sum()),
                   **centroid_coverage(unbaked, uvs, tris)}
    if len(baked):
        v = baked * 2.0 - 1.0
        norm = np.linalg.norm(v, axis=1)
        on_sphere = norm > 1e-6
        unit = v[on_sphere] / norm[on_sphere][:, None]
        ang = np.degrees(np.arccos(np.clip(unit[:, 2], -1.0, 1.0))) if on_sphere.any() else np.zeros(1)
        stats.update({
            "encoded_mean_255": [round(float(x) * 255, 2) for x in baked.mean(0)],
            "encoded_std_255": [round(float(x) * 255, 2) for x in baked.std(0)],
            "mean_angle_deg": round(float(ang.mean()), 3),
            "p95_angle_deg": round(float(np.percentile(ang, 95)), 3),
            "max_angle_deg": round(float(ang.max()), 3),
            "frac_within_2deg": round(float((ang < 2.0).mean()), 5),
            "frac_off_sphere": round(float(1.0 - on_sphere.mean()), 5),
        })
        # A tangent-space map baked off a real surface is blue-dominant and not constant, and every
        # triangle's centroid texel must have been written. An object-space bake, an empty target or
        # a self-bake of a smooth mesh fails one of these.
        spread = max(baked.std(0)[0], baked.std(0)[1]) * 255.0
        stats["ok"] = bool(baked.mean(0)[2] * 255 > 180.0 and (spread > 1.0 if expect_detail else True)
                           and stats["centroid_texel_unbaked_frac"] <= 0.01)
    else:
        stats["ok"] = False
    fill_unbaked(px, unbaked)
    return stats, px


def check_ao(px: np.ndarray, uvs: np.ndarray, tris: np.ndarray) -> tuple[dict, np.ndarray]:
    """Stats for an occlusion map (sRGB bytes in the PNG; reported linear)."""
    unbaked = unbaked_mask(px)
    enc = px[~unbaked][:, 0].astype(np.float64)
    lin = srgb_to_linear(enc) if len(enc) else enc
    stats: dict = {"unbaked_frac": round(float(unbaked.mean()), 5), "texels_baked": int((~unbaked).sum()),
                   **centroid_coverage(unbaked, uvs, tris)}
    if len(lin):
        stats.update({
            "mean": round(float(lin.mean()), 4),
            "min": round(float(lin.min()), 4),
            "p1": round(float(np.percentile(lin, 1)), 4),
            "p5": round(float(np.percentile(lin, 5)), 4),
            "p50": round(float(np.percentile(lin, 50)), 4),
            "frac_below_0_8": round(float((lin < 0.8).mean()), 4),
            "mean_encoded_255": round(float(enc.mean()) * 255, 2),
        })
        # Occlusion that is not constant and not zero: an all-white map is a failed bake, an
        # all-black one is a self-intersecting cage.
        stats["ok"] = bool(0.05 <= lin.mean() <= 0.999 and float(np.percentile(lin, 1)) < 0.95
                           and stats["centroid_texel_unbaked_frac"] <= 0.01)
    else:
        stats["ok"] = False
    fill_unbaked(px, unbaked)
    return stats, px


def diff_stats(ref: np.ndarray, other: np.ndarray, prefix: str) -> dict:
    """How different two baked maps are — the control that proves the high-poly was the source."""
    d = np.abs(ref[:, :, :3] - other[:, :, :3]) * 255.0
    worst = d.max(axis=2)
    return {
        f"{prefix}_mean_255": round(float(d.mean()), 3),
        f"{prefix}_p95_255": round(float(np.percentile(worst, 95)), 3),
        f"{prefix}_frac_gt4": round(float((worst > 4.0).mean()), 4),
    }


def final_material(mat, colour_image, normal_image):
    """Principled BSDF with the colour on Base Color and the normal map on Normal."""
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Roughness"].default_value = 1.0
    bsdf.inputs["Metallic"].default_value = 0.0
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if colour_image is not None:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = colour_image
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    if normal_image is not None:
        ntex = nt.nodes.new("ShaderNodeTexImage")
        ntex.image = normal_image
        ntex.image.colorspace_settings.name = "Non-Color"
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nmap.inputs["Strength"].default_value = 1.0
        nt.links.new(ntex.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    return bsdf


def reimport_report(fbx_path: str) -> list[dict]:
    """Re-import the written FBX and report what is really in it.

    Measured in a **metre** scene (``unit_settings.scale_length = 1.0``) on purpose: the importer then
    converts by the file's own unit declaration and nothing else, so an FBX that writes centimetres and
    calls them inches comes back 2.54x too big instead of looking plausible.
    """
    bpy.context.scene.unit_settings.system = "METRIC"
    bpy.context.scene.unit_settings.scale_length = REIMPORT_SCENE_SCALE_LENGTH
    empty_scene()
    bpy.ops.import_scene.fbx(filepath=fbx_path)
    report = []
    for o in bpy.context.scene.objects:
        if o.type != "MESH":
            report.append({"name": o.name, "type": o.type})
            continue
        me = o.data
        mats = []
        for m in me.materials:
            imgs = []
            if m is not None and m.node_tree is not None:
                for n in m.node_tree.nodes:
                    if n.type == "TEX_IMAGE" and n.image is not None:
                        p = bpy.path.abspath(n.image.filepath)
                        imgs.append({"file": os.path.basename(p), "exists": os.path.isfile(p),
                                     "size": list(n.image.size)})
            mats.append({"name": m.name if m else None, "images": imgs})
        smooth = np.empty(len(me.polygons), bool)
        me.polygons.foreach_get("use_smooth", smooth)
        me.calc_loop_triangles()
        report.append({
            "name": o.name, "type": "MESH", "vertices": len(me.vertices), "faces": len(me.polygons),
            "triangles": len(me.loop_triangles),
            "uv_layers": [u.name for u in me.uv_layers], "materials": mats,
            "color_attributes": [c.name for c in me.color_attributes],
            "smooth_face_frac": round(float(smooth.mean()) if len(smooth) else 0.0, 4),
            "world_extent_bu": world_extent(o),
            "world_bbox_min_bu": [round(float(v), 6) for v in world_vertices(o).min(0)],
            "rotation_deg": [round(math.degrees(v), 2) for v in o.rotation_euler],
            "scale": [round(float(v), 6) for v in o.scale],
        })
    return report


def place_textures(out_dir: str, tmp_dir: str, name: str) -> dict:
    """Make sure all three PNGs sit together in the FBX convention's ``<name>.fbm`` folder.

    The exporter copies what the material references (colour, normal); the AO map is not a Blender
    material input (Principled BSDF has none), so it is copied here rather than silently left behind.
    """
    fbm = os.path.join(out_dir, f"{name}.fbm")
    out = {}
    for kind in TEXTURE_KINDS:
        png = texture_name(name, kind)
        dst = os.path.join(fbm, png)
        src = os.path.join(tmp_dir, png)
        copied_by_exporter = os.path.isfile(dst)
        if not copied_by_exporter:
            if not os.path.isfile(src):
                out[kind] = {"path": None, "relpath": None, "bytes": None, "sha256": None,
                             "copied_by_exporter": False, "error": "missing after export"}
                continue
            os.makedirs(fbm, exist_ok=True)
            shutil.copyfile(src, dst)
        out[kind] = {
            "path": dst, "relpath": os.path.relpath(dst, out_dir).replace(os.sep, "/"),
            "bytes": os.path.getsize(dst), "sha256": sha256(dst),
            "copied_by_exporter": copied_by_exporter,
        }
    return out


def run(args: argparse.Namespace) -> tuple[dict, int]:
    if np is None:
        raise BridgeError("numpy is required (Blender bundles it)")
    t_all = time.perf_counter()
    timings: dict[str, float] = {}
    glb = os.path.abspath(args.glb)
    if not os.path.isfile(glb):
        raise BridgeError(f"GLB not found: {glb}")
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    fbx_path = os.path.join(out_dir, f"{args.name}.fbx")
    if os.path.exists(fbx_path) and not args.force:
        raise BridgeError(f"{fbx_path} exists (pass --force to replace it)")
    result: dict = {
        "tool": "glb_to_fbx_uv",
        "script_sha256": sha256(os.path.abspath(__file__)) if os.path.isfile(__file__) else None,
        "blender": bpy.app.version_string,
        "args": dict(vars(args)),
    }

    # 1. read the GLB, import it, and separate the collision mesh from the body.
    t = time.perf_counter()
    doc, blob = read_glb(glb)
    facts, prims = glb_facts(doc)
    result["input"] = {"glb": glb, "bytes": os.path.getsize(glb), "sha256": sha256(glb), **facts}
    empty_scene()
    body, coll, import_info = import_glb(glb, args.shade, args.name)
    result["input"].update(import_info)
    colours, colour_source = vertex_colours(body, doc, blob, prims)
    objs = [o for o in (body, coll) if o is not None]
    norm = normalise(objs, body, args.unit_scale)
    result["normalise"] = norm
    diag = float(np.linalg.norm(norm["bbox_bu"])) or 1.0
    timings["import_s"] = round(time.perf_counter() - t, 2)

    body_lo, body_hi = world_bbox([body])
    if coll is not None:
        c_lo, c_hi = world_bbox([coll])
        # A collision proxy usually *encloses* the visual mesh. Cycles' AO bake casts against render
        # geometry, so leaving it visible would blacken the map (measured on the seam fixture: AO mean
        # 0.07 with the proxy visible, 0.9 without). It is not render geometry — and hiding it from the
        # render does not keep the FBX exporter from writing it (use_visible is off).
        coll.hide_render = True
        result["collision"] = {
            "present": True, "name": coll.name, "triangles": len(mesh_arrays(coll.data)["tris"]),
            "bbox_bu": [round(float(v), 6) for v in (c_hi - c_lo)],
            "outside_body_bbox": bool(np.any(c_lo < body_lo - 1e-6) or np.any(c_hi > body_hi + 1e-6)),
            "decimated": False, "uv_unwrapped": False, "material": None, "renders": False,
        }
    else:
        result["collision"] = {"present": False, "name": collision_mesh_name(args.name),
                               "exported": False, "decimated": False, "uv_unwrapped": False}

    # 2. decimate a copy: the body stays intact as the bake source.
    t = time.perf_counter()
    render = duplicate_object(body, args.name)
    result["decimate"] = decimate_to(render, args.target_tris)
    apply_shading(render, args.shade)
    timings["decimate_s"] = round(time.perf_counter() - t, 2)

    # 3. the high-poly's per-vertex colour, sampled onto the decimated surface.
    t = time.perf_counter()
    src_loop_rgb = np.clip(colours[mesh_arrays(body.data)["loop_vert"]], 0.0, 1.0)
    render_colour, transfer = transfer_colours(body, src_loop_rgb, render, diag)
    set_corner_colour(render.data, render_colour)
    timings["transfer_s"] = round(time.perf_counter() - t, 2)

    t = time.perf_counter()
    uv_name = smart_uv(render, args)
    me = render.data
    arrays = mesh_arrays(me)
    uvs = loop_uvs(me)
    tri = arrays["tris"]
    a, b, c = uvs[tri[:, 0]], uvs[tri[:, 1]], uvs[tri[:, 2]]
    uv_area = 0.5 * np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    zero_area = int((uv_area <= 0).sum())
    uv_area_frac = round(float(uv_area.sum()), 4)
    result["uv"] = {
        "layer": uv_name, "method": "smart_project", "angle_limit_deg": args.angle_limit,
        "island_margin": round(args.island_margin_px / args.res, 6),
        "islands": count_uv_islands(len(me.polygons), arrays["loop_vert"], arrays["loop_poly"], uvs),
        "uv_area_frac": uv_area_frac,
        "zero_area_triangles": zero_area,
        "in_unit_square": bool(((uvs >= 0.0) & (uvs <= 1.0)).all()),
    }
    # A packed-out unwrap (huge island margin at a small --res) leaves nothing to bake onto: the maps
    # come out empty, so say so here instead of blaming the bake.
    uv_ok = uv_area_frac >= 0.01 and zero_area <= max(4, len(tri) // 100)
    result["uv"]["ok"] = bool(uv_ok)
    result["mesh"] = {
        "vertices": len(me.vertices), "faces": len(me.polygons), "triangles": len(tri),
        "colour_source": colour_source, "vertex_color_space": args.vertex_color_space,
        "colour_mean_255": [round(float(v) * 255, 2) for v in render_colour.mean(0)],
        "world_extent_bu": world_extent(render),
        "high_poly": {"vertices": len(body.data.vertices), "triangles": result["decimate"]["tris_in"]},
        "colour_transfer": transfer,
    }
    timings["uv_s"] = round(time.perf_counter() - t, 2)

    mat = bpy.data.materials.new(args.material_path)
    if mat.name != args.material_path:  # truncated, or uniquified (".001") against an existing ID
        raise BridgeError(f"Blender named the material {mat.name!r}, not {args.material_path!r}")
    mat.use_nodes = True
    me.materials.clear()
    me.materials.append(mat)
    tmp_dir = os.path.join(out_dir, f".{args.name}_bake_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    # 4a. colour: EMIT the transferred vertex colour (Non-Color = write the COLOR_0 bytes as-is).
    t = time.perf_counter()
    expected = render_colour if args.vertex_color_space == "srgb" else linear_to_srgb(render_colour)
    colour_img, stats, colour_ok = None, {}, False
    try:
        img = bake_map(render, mat, args, uv_name, kind="EMIT", image_name=f"{args.name}_bake",
                       colorspace="Non-Color" if args.vertex_color_space == "srgb" else "sRGB",
                       samples=args.samples, emission_attr=BAKE_ATTR)
        px = image_pixels(img)
        stats, px = check_bake_pixels(px, uvs, tri, expected)
        img.pixels.foreach_set(px.ravel())
        saved = save_image(img, os.path.join(tmp_dir, texture_name(args.name, "color")), "sRGB")
        # Gate: <=1% of triangles may be sub-texel (their centroid texel is filled, not baked) and the
        # baked texels must reproduce the vertex colour to within 4/255 on average.
        unbaked_ok = stats["centroid_texel_unbaked"] <= stats["triangles_checked"] // 100
        colour_ok = unbaked_ok and stats["baked_abs_err_mean_255"] < 4.0
        if colour_ok:
            colour_img = saved
        else:
            bpy.data.images.remove(saved)
            stats["why_rejected"] = "unbaked centroid texels > 1% or baked mean error >= 4/255"
    except Exception as exc:  # the documented fallback: UV map + plain base colour, no texture
        stats = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    stats["ok"] = colour_ok
    stats.update({"type": "EMIT", "engine": "CYCLES/CPU", "res": args.res, "samples": args.samples,
                  "margin_px": args.bake_margin, "margin_type": "ADJACENT_FACES"})
    result["bake"] = stats
    timings["bake_s"] = round(time.perf_counter() - t, 2)

    # 4b/4c. normal + AO, both sampled from the ORIGINAL high-poly through the decimated UVs. When the
    # mesh was already inside the budget there is no high-poly to sample: a coincident selected-to-active
    # bake only adds raycast noise, so that case bakes from the mesh itself.
    baked_from_high = result["decimate"]["tris_out"] < result["decimate"]["tris_in"]
    source = body if baked_from_high else None
    source_note = ("high-poly" if baked_from_high
                   else "self (mesh already within the triangle budget: nothing was decimated)")
    cage = args.cage_pct / 100.0 * diag
    normal_img, n_stats = None, {"ok": False}
    t = time.perf_counter()
    try:
        n_img = bake_map(render, mat, args, uv_name, kind="NORMAL", image_name=f"{args.name}_normal_bake",
                         colorspace="Non-Color", samples=1, source=source, cage=cage)
        n_px = image_pixels(n_img)
        n_stats, n_px = check_normal(n_px, uvs, tri, expect_detail=baked_from_high)
        n_img.pixels.foreach_set(n_px.ravel())
        normal_img = save_image(n_img, os.path.join(tmp_dir, texture_name(args.name, "normal")), "Non-Color")
    except Exception as exc:
        n_stats = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        n_px = None
    n_stats.update({"type": "NORMAL", "space": "TANGENT", "engine": "CYCLES/CPU", "res": args.res,
                    "samples": 1, "source": source_note, "cage_extrusion_bu": round(cage, 6),
                    "margin_px": args.bake_margin})
    timings["normal_bake_s"] = round(time.perf_counter() - t, 2)

    ao_img, a_stats = None, {"ok": False}
    t = time.perf_counter()
    ao_distance = args.ao_distance_pct / 100.0 * diag
    try:
        a_img = bake_map(render, mat, args, uv_name, kind="AO", image_name=f"{args.name}_ao_bake",
                         colorspace="sRGB", samples=args.ao_samples, source=source, cage=cage,
                         ao_distance=ao_distance)
        a_px = image_pixels(a_img)
        a_stats, a_px = check_ao(a_px, uvs, tri)
        a_img.pixels.foreach_set(a_px.ravel())
        ao_img = save_image(a_img, os.path.join(tmp_dir, texture_name(args.name, "ao")), "sRGB")
    except Exception as exc:
        a_stats = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        a_px = None
    a_stats.update({"type": "AO", "engine": "CYCLES/CPU", "res": args.res, "samples": args.ao_samples,
                    "source": source_note, "distance_bu": round(ao_distance, 6),
                    "distance_pct_of_bbox": args.ao_distance_pct})
    timings["ao_bake_s"] = round(time.perf_counter() - t, 2)

    # 4d. controls: each map must change when the high-poly stops being the source/occluder. They only
    # mean something when something *was* decimated — otherwise the reference and the control are the
    # same bake, and a zero difference would be the honest answer rather than a failure.
    if args.verify_source and not baked_from_high:
        n_stats["control"] = {"skipped": "no decimation: the bake source is the mesh itself"}
        a_stats["control"] = {"skipped": "no decimation: the bake source is the mesh itself"}
    if args.verify_source and baked_from_high and n_px is not None:
        t = time.perf_counter()
        try:
            ctrl = bake_map(render, mat, args, uv_name, kind="NORMAL", image_name=f"{args.name}_normal_ctrl",
                            colorspace="Non-Color", samples=1, source=None)
            c_px = image_pixels(ctrl)
            bpy.data.images.remove(ctrl)
            fill_unbaked(c_px, unbaked_mask(c_px))
            n_stats["control"] = {"kind": "self-bake of the decimated mesh (no high-poly)",
                                  **diff_stats(n_px, c_px, "diff")}
            n_stats["ok"] = bool(n_stats.get("ok") and n_stats["control"]["diff_mean_255"] >= 1.0)
        except Exception as exc:
            n_stats["control"] = {"error": f"{type(exc).__name__}: {exc}"}
        timings["normal_control_s"] = round(time.perf_counter() - t, 2)
    if args.verify_source and baked_from_high and a_px is not None:
        t = time.perf_counter()
        try:
            body.location = (0.0, 0.0, 10.0 * diag)  # out of ray range: an honest "no occluder" control
            bpy.context.view_layer.update()
            ctrl = bake_map(render, mat, args, uv_name, kind="AO", image_name=f"{args.name}_ao_ctrl",
                            colorspace="sRGB", samples=args.ao_samples, source=body, cage=cage,
                            ao_distance=ao_distance)
            body.location = (0.0, 0.0, 0.0)
            bpy.context.view_layer.update()
            c_px = image_pixels(ctrl)
            bpy.data.images.remove(ctrl)
            fill_unbaked(c_px, unbaked_mask(c_px))
            a_stats["control"] = {"kind": "high-poly moved 10 bbox diagonals away",
                                  **diff_stats(a_px, c_px, "diff")}
            a_stats["ok"] = bool(a_stats.get("ok") and a_stats["control"]["diff_mean_255"] >= 1.0)
        except Exception as exc:
            body.location = (0.0, 0.0, 0.0)
            a_stats["control"] = {"error": f"{type(exc).__name__}: {exc}"}
        timings["ao_control_s"] = round(time.perf_counter() - t, 2)
    result["normal_bake"] = n_stats
    result["ao_bake"] = a_stats

    # 5. the material CS2's importer reads by name.
    bsdf = final_material(mat, colour_img, normal_img)
    if colour_img is None:
        mean_srgb = expected.mean(0)
        bsdf.inputs["Base Color"].default_value = (*srgb_to_linear(mean_srgb).tolist(), 1.0)
        result["material"] = {"name": mat.name, "kind": "plain base colour (fallback)",
                              "base_color_srgb_255": [round(float(v) * 255, 1) for v in mean_srgb]}
    else:
        result["material"] = {"name": mat.name,
                              "kind": "Principled BSDF, Base Color <- colour PNG, Normal <- normal PNG",
                              "textures": {kind: texture_name(args.name, kind)
                                           for kind in TEXTURE_KINDS if kind != "ao"},
                              "unwired": {"ao": "Principled BSDF has no AO input; the .vmat key "
                                                f"{VMAT_TEXTURE_KEYS['ao']} points at {texture_name(args.name, 'ao')}"}}
    if not args.export_vertex_colors:
        while me.color_attributes:
            me.color_attributes.remove(me.color_attributes[0])

    # 6. export. The high-poly leaves the scene first: only the render mesh and any collision mesh go out.
    bpy.data.objects.remove(body, do_unlink=True)
    units = export_scale_settings(args.unit_scale)
    t = time.perf_counter()
    # Vertex colours (opt-in) hold the COLOR_0 values untouched: LINEAR = write them as stored.
    colors_type = ("LINEAR" if args.vertex_color_space == "srgb" else "SRGB") if args.export_vertex_colors else "NONE"
    # The scene's unit scale is the one knob the exporter reads to decide what a file unit means (it
    # becomes UnitScaleFactor = 100 * scale_length); the geometry itself keeps applied scale 1.
    scene_units = bpy.context.scene.unit_settings
    previous_units = (scene_units.system, scene_units.scale_length)
    scene_units.system = "METRIC"
    scene_units.scale_length = units["scene_scale_length"]
    try:
        bpy.ops.export_scene.fbx(
            filepath=fbx_path, check_existing=False, use_selection=False, object_types={"MESH"},
            global_scale=units["global_scale"], apply_unit_scale=units["apply_unit_scale"],
            apply_scale_options=units["apply_scale_options"],
            use_space_transform=True, bake_space_transform=True, axis_forward="-Z", axis_up="Y",
            use_mesh_modifiers=True, mesh_smooth_type="OFF", colors_type=colors_type, use_tspace=False,
            use_triangles=False, use_custom_props=False, add_leaf_bones=False, bake_anim=False,
            path_mode="COPY", embed_textures=False, batch_mode="OFF",
        )
    finally:
        scene_units.system, scene_units.scale_length = previous_units
    timings["export_s"] = round(time.perf_counter() - t, 2)
    result["textures"] = place_textures(out_dir, tmp_dir, args.name)
    result["texture"] = result["textures"]["color"]  # the colour map, as the earlier reports named it
    result["vmat"] = {"path": args.material_path, "dir": vmat_dir(args.name),
                      "layer0": vmat_layer0(args.name, args.material_path),
                      "textures": vmat_textures(args.name)}
    pngs = [texture_name(args.name, kind) for kind in TEXTURE_KINDS]
    result["fbx"] = {"path": fbx_path, "bytes": os.path.getsize(fbx_path), "sha256": sha256(fbx_path),
                     "scan": fbx_scan(fbx_path, pngs)}
    # What the file really contains vs the size the bridge measured: a units mistake (centimetres read
    # as inches) is a per-axis ratio of ~2.54 here, visible without compiling anything.
    result["export_units"] = export_units_report(
        args.unit_scale, units, fbx_measure(fbx_path),
        result["normalise"]["bbox_units"],
        [float(v) * args.unit_scale for v in result["mesh"]["world_extent_bu"]],
        body_name=args.name,
    )
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # 7. prove it by re-importing what was written.
    t = time.perf_counter()
    result["reimport"] = reimport_report(fbx_path)
    timings["reimport_s"] = round(time.perf_counter() - t, 2)
    timings["total_s"] = round(time.perf_counter() - t_all, 2)
    result["timings"] = timings
    meshes = [r for r in result["reimport"] if r.get("type") == "MESH"]
    by_name = {r["name"]: r for r in meshes}
    body_r = by_name.get(args.name)
    has_uv = bool(body_r) and bool(body_r["uv_layers"])
    has_mat = bool(body_r) and bool(body_r["materials"] and body_r["materials"][0]["name"])
    has_tex = bool(body_r) and any(i["exists"] for m in body_r["materials"] for i in m["images"])
    # The budget and the pivot must survive the round trip, and no scale factor may ride along.
    tris_ok = bool(body_r) and body_r["triangles"] == result["decimate"]["tris_out"]
    # The exported objects had scale 1 (normalise.object_scale, checked before export). Blender's own
    # FBX importer then hands them back with its uniform file-unit -> metre factor (0.0254 for the inch
    # file this writes), so what is checked here is that the factor is uniform (no squeezed or mirrored
    # axis) and that the extent survived; the unit factor itself is checked in ``export_units``.
    reimport_scale = body_r["scale"] if body_r else None
    scale_ok = bool(body_r) and len({round(s, 6) for s in body_r["scale"]}) == 1
    extent_ok = bool(body_r) and max(abs(x - y) for x, y in zip(body_r["world_extent_bu"],
                                                               result["mesh"]["world_extent_bu"], strict=True)) <= 1e-3
    coll_name = collision_mesh_name(args.name)
    coll_ok = (coll is None) or coll_name in by_name
    if coll is not None:
        result["collision"]["exported"] = coll_name in by_name
        result["collision"]["exported_triangles"] = by_name.get(coll_name, {}).get("triangles")
        result["collision"]["exported_scale"] = by_name.get(coll_name, {}).get("scale")
        result["collision"]["exported_uv_layers"] = by_name.get(coll_name, {}).get("uv_layers")
    # The compile resolves the FBX material NAME as the .vmat path, so the name must come back verbatim.
    # Membership, not position: a material-less collision mesh makes the importer add its own default.
    reimported = [m.name for m in bpy.data.materials]
    survived = args.material_path in reimported
    result["material_path_check"] = {
        "expected": args.material_path, "reimported": reimported,
        "body_materials": [m["name"] for m in (body_r or {}).get("materials", [])] if body_r else None,
        "survived": survived,
    }
    result["verdict"] = {
        "fbx_has_uv": has_uv, "fbx_has_material": has_mat, "fbx_texture_resolves": has_tex,
        "baked_texture": bool(colour_img is not None and has_tex),
        "normal_map_baked": bool(normal_img is not None and n_stats.get("ok")),
        "ao_map_baked": bool(ao_img is not None and a_stats.get("ok")),
        "within_triangle_budget": tris_ok, "no_scale_node": scale_ok, "extent_preserved": extent_ok,
        "uv_usable": bool(uv_ok), "collision_exported": coll_ok,
        "material_path_survived": survived,
        # The numbers in the FBX are Source units and match what the bridge measured (``export_units``).
        "export_units_ok": bool(result["export_units"]["ok"]),
    }
    result["verdict"]["reimport_scale"] = reimport_scale
    verdict = result["verdict"]
    maps_ok = verdict["normal_map_baked"] and verdict["ao_map_baked"]
    code = 0 if (verdict["baked_texture"] and maps_ok and has_uv and has_mat and tris_ok and scale_ok
                 and extent_ok and coll_ok and uv_ok and verdict["export_units_ok"]) else 3
    if not survived:
        code = 2
        result["error"] = (f"re-imported FBX material is {reimported[:1]!r}, not {args.material_path!r}: "
                           "the compiler would look for a different .vmat")
    # newline="\n" pins LF on every host: this script runs inside Blender on Windows, where a
    # text-mode write turns each "\n" into "\r\n", and the trailing CR then fails CHC-COMMITS
    # (`git diff --check` reads it as trailing whitespace) once the report is committed.
    with open(os.path.join(out_dir, f"{args.name}_bridge.json"), "w", encoding="utf-8",
              newline="\n") as fh:
        json.dump(result, fh, indent=2)
        fh.write("\n")
    return result, code


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    try:
        result, code = run(parse_args(argv))
    except BridgeError as exc:
        print(f"{RESULT_TAG} " + json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 2
    print(f"{RESULT_TAG} " + json.dumps(result, separators=(",", ":")), flush=True)
    if code == 2:
        print(f"ERROR: {result.get('error')}", file=sys.stderr, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
