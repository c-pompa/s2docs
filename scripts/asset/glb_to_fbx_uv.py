"""GLB (per-vertex colour only) -> FBX with a UV map, a baked colour texture and a material.

Headless Blender bridge for the Hammered local mesh route. TripoSR (through trimesh) writes a GLB
with POSITION + COLOR_0 and nothing else: no UVs, no normals, no materials, no images. A ``.vmdl``
built from that compiles but cannot take a material. This script adds the missing half:

1. import the GLB (Blender's glTF importer), drop non-mesh objects, join meshes, apply transforms;
2. smart-UV-project a fresh UV map;
3. bake the vertex colour into ``<name>_color.png`` (Cycles EMIT bake, CPU only);
4. build the material, named by its Source 2 path (``--material-path``, default
   ``materials/hammered/<name>/<name>.vmat``): Principled BSDF, Base Color <- the baked PNG.
   CS2's FBX importer reads the FBX material NAME as the ``.vmat`` path
   (``GetFbxMaterialPath Failed!`` otherwise), so the name must survive export verbatim;
5. export ``<out-dir>/<name>.fbx`` with ``path_mode='COPY'``, ``embed_textures=False``,
   ``bake_space_transform=True`` (after ``transform_apply``), ``use_selection=False``, MESH only;
6. re-import the FBX into an empty scene, report what is really in it, and require the re-imported
   material name to equal ``--material-path`` (Blender truncates ID names over 63 characters, and an
   exporter may sanitize ``/``).

Run with Blender 4.5 LTS (verified on 4.5.15, build-pc ``D:\\Tools\\blender-4.5.15-windows-x64``)::

    blender.exe -b --factory-startup --python-exit-code 1 --python glb_to_fbx_uv.py -- \\
        --glb <mesh.glb> --out-dir <dir> --name <model_name> [--res 2048] [--force] \\
        [--material-path materials/hammered/<name>/<name>.vmat]

Output in ``<out-dir>``: ``<name>.fbx``; ``<name>.fbm/<name>_color.png`` (``path_mode='COPY'``
copies media into the FBX convention's ``.fbm`` folder and the FBX references it relatively, so
move the two together); ``<name>_bridge.json`` (the report, also printed as one stdout line
``GLB_TO_FBX_UV_RESULT {json}``). Exit codes: 0 = baked texture shipped; 3 = the bake failed or
missed its gate and the documented fallback shipped (UV map + a plain base-colour material, no
texture); 2 = bad input, or the re-imported FBX material name is not ``--material-path``
(``verdict.material_path_survived`` false; the FBX is still written for inspection);
1 = uncaught error.

Source 2 next step: copy ``<name>.fbx`` + ``<name>.fbm/`` to ``<addon>/models/<name>/``, then
``s2docs asset --addon <addon> --name <name> --mesh models/<name>/<name>.fbx``, write the material
with ``s2docs asset material --addon <addon> --name <name> --color <name>.fbm/<name>_color.png``
(it lands at the same ``materials/hammered/<name>/<name>.vmat`` path), and compile the ``.vmdl``
with ``resourcecompiler -i <abs .vmdl> -nop4``.

Measured on TripoSR job eefa8331d9cf4056 (29,401 verts / 58,696 tris, 2048 px, CPU, 5.2 s total):
2,197 UV islands covering 21% of the texture; centroid texels match the vertex colour to 0.5/255
on average, but 0.36% of triangles (sub-texel islands) carry a wrong colour (>8/255).

Colour space: the bake reads COLOR_0 straight from the GLB bytes, so the result does not depend on
how a given glTF importer converts it. glTF defines COLOR_0 as linear, but TripoSR's colours are
sRGB-encoded values written as-is: on job eefa8331d9cf4056, projecting the front-facing vertices
onto ``input_processed.png`` gave a mean abs error of 0.107 reading the bytes as sRGB, against
0.177 reading them as linear (0.30 with R/B swapped). So the default ``--vertex-color-space srgb``
makes each texel equal the COLOR_0 byte; ``linear`` follows the spec instead and comes out brighter.

What this does NOT give you: the normals are computed by Blender from the geometry
(``--shade smooth`` by default), not authored, and there is no normal map. Smart-UV seams on an
organic marching-cubes surface are many small islands. ``<name>_color.png`` is only an input for a
``.vmat``; no Source 2 material is written here (``s2docs asset material`` writes it).
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

import bpy
import numpy as np

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")  # same rule as s2docs.asset.model.check_name
# addon-relative, lowercase, forward slashes, ends in .vmat — what s2docs.asset.material writes
MATERIAL_PATH_RE = re.compile(r"^[a-z0-9_]+(/[a-z0-9_]+)*\.vmat$")
MAX_ID_NAME = 63  # Blender truncates ID names (Material.name) beyond 63 characters
RESULT_TAG = "GLB_TO_FBX_UV_RESULT"
BAKE_ATTR = "bake_color"
_GLB_MAGIC, _CHUNK_JSON, _CHUNK_BIN = 0x46546C67, 0x4E4F534A, 0x004E4942
_DTYPES = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


class BridgeError(Exception):
    """Bad input: reported with exit code 2."""


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="glb_to_fbx_uv.py", description=__doc__.splitlines()[0])
    p.add_argument("--glb", required=True, help="input .glb (per-vertex colour in COLOR_0)")
    p.add_argument("--out-dir", required=True, help="directory for <name>.fbx and <name>_color.png")
    p.add_argument("--name", required=True, help="model/material name, [a-z][a-z0-9_]{1,62}")
    p.add_argument("--res", type=int, default=2048, help="baked texture size in px (square)")
    p.add_argument("--angle-limit", type=float, default=66.0, help="smart UV project angle limit, degrees")
    p.add_argument("--island-margin-px", type=float, default=8.0, help="gap between UV islands, in texels")
    p.add_argument("--bake-margin", type=int, default=16, help="bake margin (gutter fill), in texels")
    p.add_argument("--samples", type=int, default=4, help="Cycles samples per texel for the bake")
    p.add_argument("--shade", choices=("smooth", "flat", "keep"), default="smooth",
                   help="normals Blender computes for the export (the GLB has none)")
    p.add_argument("--vertex-color-space", choices=("srgb", "linear"), default="srgb",
                   help="how to read COLOR_0: srgb = TripoSR's real encoding (default), linear = glTF spec")
    p.add_argument("--export-vertex-colors", action="store_true", help="also write the colours as an FBX layer")
    p.add_argument("--material-path", default=None,
                   help="FBX material name = the Source 2 .vmat path (default materials/hammered/<name>/<name>.vmat)")
    p.add_argument("--force", action="store_true", help="replace an existing <name>.fbx")
    args = p.parse_args(argv)
    if not NAME_RE.fullmatch(args.name):
        raise BridgeError(f"--name {args.name!r} must match {NAME_RE.pattern}")
    if args.material_path is None:
        args.material_path = f"materials/hammered/{args.name}/{args.name}.vmat"
    if not MATERIAL_PATH_RE.fullmatch(args.material_path) or ".." in args.material_path.split("/"):
        raise BridgeError(f"--material-path {args.material_path!r} must match {MATERIAL_PATH_RE.pattern}")
    if len(args.material_path) > MAX_ID_NAME:
        raise BridgeError(f"--material-path is {len(args.material_path)} chars; Blender truncates ID names "
                          f"over {MAX_ID_NAME}")
    if not 64 <= args.res <= 8192:
        raise BridgeError("--res must be within 64..8192")
    return args


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


def world_extent(obj: bpy.types.Object) -> list[float]:
    """Bounding-box size in world space (``Object.dimensions`` ignores rotation)."""
    me = obj.data
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", co)
    mw = np.array(obj.matrix_world)
    world = co.reshape(-1, 3).astype(np.float64) @ mw[:3, :3].T + mw[:3, 3]
    return [round(float(v), 4) for v in world.max(0) - world.min(0)]


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


def fbx_scan(path: str, png_name: str) -> dict:
    with open(path, "rb") as fh:
        raw = fh.read()
    return {
        "binary": raw.startswith(b"Kaydara FBX Binary"),
        "LayerElementUV": raw.count(b"LayerElementUV"),
        "LayerElementNormal": raw.count(b"LayerElementNormal"),
        "LayerElementMaterial": raw.count(b"LayerElementMaterial"),
        "LayerElementColor": raw.count(b"LayerElementColor"),
        "texture_refs": raw.count(png_name.encode()),
    }


# --- pipeline stages -----------------------------------------------------------------------------


def import_glb(path: str, shade: str) -> bpy.types.Object:
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
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


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
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(args.angle_limit), margin_method="FRACTION",
                             island_margin=args.island_margin_px / args.res, area_weight=0.0,
                             correct_aspect=True, scale_to_bounds=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    return uv.name


def bake_colour(obj, mat, args, uv_name: str) -> bpy.types.Image:
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = max(1, args.samples)
    if hasattr(scene.cycles, "use_denoising"):
        scene.cycles.use_denoising = False
    res = args.res
    img = bpy.data.images.new(f"{args.name}_bake", res, res, alpha=False, float_buffer=False)
    # srgb: the shader carries the COLOR_0 values untouched, so write them as-is (Non-Color target);
    # linear: they are scene-linear, so let the bake encode them to sRGB.
    img.colorspace_settings.name = "Non-Color" if args.vertex_color_space == "srgb" else "sRGB"
    img.pixels.foreach_set(np.tile(np.array([1.0, 0.0, 1.0, 1.0], np.float32), res * res))  # magenta = unbaked
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    attr = nt.nodes.new("ShaderNodeVertexColor")
    attr.layer_name = BAKE_ATTR
    emit = nt.nodes.new("ShaderNodeEmission")
    emit.inputs["Strength"].default_value = 1.0
    nt.links.new(attr.outputs["Color"], emit.inputs["Color"])
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    target = nt.nodes.new("ShaderNodeTexImage")
    target.image = img
    for node in nt.nodes:
        node.select = False
    target.select = True
    nt.nodes.active = target
    bake = scene.render.bake
    bake.margin, bake.margin_type = args.bake_margin, "ADJACENT_FACES"
    bake.use_clear, bake.target = False, "IMAGE_TEXTURES"
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.bake(type="EMIT", margin=args.bake_margin, margin_type="ADJACENT_FACES", use_clear=False,
                        target="IMAGE_TEXTURES", uv_layer=uv_name)
    return img


def fill_unbaked(px: np.ndarray, unbaked: np.ndarray, iterations: int = 8) -> None:
    """Grow baked texels into the still-empty ones (4-neighbour average), then the mean for the rest.

    The bake margin already extended every island; this covers sub-texel triangles the rasteriser
    skipped and keeps low mips from averaging in a sentinel colour.
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
    worst = err.max(axis=1)
    return {
        f"{prefix}abs_err_mean_255": round(float(err.mean()), 3),
        f"{prefix}abs_err_p95_255": round(float(np.percentile(err, 95)), 3),
        f"{prefix}abs_err_p999_255": round(float(np.percentile(worst, 99.9)), 3),
        f"{prefix}abs_err_max_255": round(float(err.max()), 3),
        f"{prefix}frac_err_gt_8": round(float((worst > 8).mean()), 5),
    }


def check_bake(img, uvs: np.ndarray, tris: np.ndarray, loop_expected: np.ndarray) -> tuple[dict, np.ndarray]:
    """Compare each triangle's centroid texel with the colour the shader had there, before and after
    the empty texels are filled."""
    res = img.size[0]
    px = np.empty(res * res * 4, np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(res, res, 4)
    unbaked = (px[:, :, 0] == 1.0) & (px[:, :, 1] == 0.0) & (px[:, :, 2] == 1.0)
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


def final_material(mat, image):
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.inputs["Roughness"].default_value = 1.0
    bsdf.inputs["Metallic"].default_value = 0.0
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if image is not None:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = image
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    return bsdf


def reimport_report(fbx_path: str) -> list[dict]:
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
        report.append({
            "name": o.name, "type": "MESH", "vertices": len(me.vertices), "faces": len(me.polygons),
            "uv_layers": [u.name for u in me.uv_layers], "materials": mats,
            "color_attributes": [c.name for c in me.color_attributes],
            "smooth_face_frac": round(float(smooth.mean()) if len(smooth) else 0.0, 4),
            "world_extent_bu": world_extent(o),
            "rotation_deg": [round(math.degrees(v), 2) for v in o.rotation_euler],
        })
    return report


def run(args: argparse.Namespace) -> tuple[dict, int]:
    t_all = time.perf_counter()
    timings: dict[str, float] = {}
    glb = os.path.abspath(args.glb)
    if not os.path.isfile(glb):
        raise BridgeError(f"GLB not found: {glb}")
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    fbx_path = os.path.join(out_dir, f"{args.name}.fbx")
    png_name = f"{args.name}_color.png"
    if os.path.exists(fbx_path) and not args.force:
        raise BridgeError(f"{fbx_path} exists (pass --force to replace it)")
    result: dict = {
        "tool": "glb_to_fbx_uv",
        "script_sha256": sha256(os.path.abspath(__file__)) if os.path.isfile(__file__) else None,
        "blender": bpy.app.version_string,
        "args": dict(vars(args)),
    }

    t = time.perf_counter()
    doc, blob = read_glb(glb)
    facts, prims = glb_facts(doc)
    result["input"] = {"glb": glb, "bytes": os.path.getsize(glb), "sha256": sha256(glb), **facts}
    empty_scene()
    obj = import_glb(glb, args.shade)
    obj.name = obj.data.name = args.name
    me = obj.data
    colours, colour_source = vertex_colours(obj, doc, blob, prims)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    while me.color_attributes:
        me.color_attributes.remove(me.color_attributes[0])
    arrays = mesh_arrays(me)
    loop_col = np.clip(colours[arrays["loop_vert"]], 0.0, 1.0)
    attr = me.color_attributes.new(BAKE_ATTR, "FLOAT_COLOR", "CORNER")
    rgba = np.concatenate([loop_col, np.ones((len(loop_col), 1))], 1).astype(np.float32)
    attr.data.foreach_set("color", rgba.ravel())
    result["mesh"] = {
        "vertices": len(me.vertices), "faces": len(me.polygons), "triangles": len(arrays["tris"]),
        "colour_source": colour_source, "vertex_color_space": args.vertex_color_space,
        "colour_mean_255": [round(float(v) * 255, 2) for v in colours.mean(0)],
        "world_extent_bu": world_extent(obj),
    }
    timings["import_s"] = round(time.perf_counter() - t, 2)

    t = time.perf_counter()
    uv_name = smart_uv(obj, args)
    uvs = loop_uvs(me)
    tri = arrays["tris"]
    a, b, c = uvs[tri[:, 0]], uvs[tri[:, 1]], uvs[tri[:, 2]]
    uv_area = 0.5 * np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    result["uv"] = {
        "layer": uv_name, "method": "smart_project", "angle_limit_deg": args.angle_limit,
        "island_margin": round(args.island_margin_px / args.res, 6),
        "islands": count_uv_islands(len(me.polygons), arrays["loop_vert"], arrays["loop_poly"], uvs),
        "uv_area_frac": round(float(uv_area.sum()), 4),
        "zero_area_triangles": int((uv_area <= 0).sum()),
        "in_unit_square": bool(((uvs >= 0.0) & (uvs <= 1.0)).all()),
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
    tmp_png = os.path.join(tmp_dir, png_name)
    expected = loop_col if args.vertex_color_space == "srgb" else linear_to_srgb(loop_col)
    t = time.perf_counter()
    bake_ok, image = False, None
    try:
        img = bake_colour(obj, mat, args, uv_name)
        stats, px = check_bake(img, uvs, tri, expected)
        img.pixels.foreach_set(px.ravel())
        img.filepath_raw = tmp_png
        img.file_format = "PNG"
        img.save()
        bpy.data.images.remove(img)
        image = bpy.data.images.load(tmp_png, check_existing=False)
        image.name = png_name
        image.colorspace_settings.name = "sRGB"
        # Gate: <=1% of triangles may be sub-texel (their centroid texel is filled, not baked) and the
        # baked texels must reproduce the vertex colour to within 4/255 on average.
        unbaked_ok = stats["centroid_texel_unbaked"] <= stats["triangles_checked"] // 100
        bake_ok = unbaked_ok and stats["baked_abs_err_mean_255"] < 4.0
        result["bake"] = {"ok": bake_ok, "type": "EMIT", "engine": "CYCLES/CPU", "res": args.res,
                          "samples": args.samples, "margin_px": args.bake_margin, "margin_type": "ADJACENT_FACES",
                          **stats}
        if not bake_ok:
            image = None
            result["bake"]["why_rejected"] = "unbaked centroid texels > 1% or baked mean error >= 4/255"
    except Exception as exc:  # the documented fallback: UV map + plain base colour, no texture
        result["bake"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    timings["bake_s"] = round(time.perf_counter() - t, 2)
    bsdf = final_material(mat, image)
    if image is None:
        mean_srgb = expected.mean(0)
        bsdf.inputs["Base Color"].default_value = (*srgb_to_linear(mean_srgb).tolist(), 1.0)
        result["material"] = {"name": mat.name, "kind": "plain base colour (fallback)",
                              "base_color_srgb_255": [round(float(v) * 255, 1) for v in mean_srgb]}
    else:
        result["material"] = {"name": mat.name, "kind": "Principled BSDF, Base Color <- baked texture",
                              "texture": png_name}
    if not args.export_vertex_colors:
        while me.color_attributes:
            me.color_attributes.remove(me.color_attributes[0])

    t = time.perf_counter()
    # Vertex colours (opt-in) hold the COLOR_0 values untouched: LINEAR = write them as stored.
    colors_type = ("LINEAR" if args.vertex_color_space == "srgb" else "SRGB") if args.export_vertex_colors else "NONE"
    bpy.ops.export_scene.fbx(
        filepath=fbx_path, check_existing=False, use_selection=False, object_types={"MESH"},
        global_scale=1.0, apply_unit_scale=True, apply_scale_options="FBX_SCALE_NONE",
        use_space_transform=True, bake_space_transform=True, axis_forward="-Z", axis_up="Y",
        use_mesh_modifiers=True, mesh_smooth_type="OFF", colors_type=colors_type, use_tspace=False,
        use_triangles=False, use_custom_props=False, add_leaf_bones=False, bake_anim=False,
        path_mode="COPY", embed_textures=False, batch_mode="OFF",
    )
    timings["export_s"] = round(time.perf_counter() - t, 2)
    result["fbx"] = {"path": fbx_path, "bytes": os.path.getsize(fbx_path), "sha256": sha256(fbx_path),
                     "scan": fbx_scan(fbx_path, png_name)}
    if image is not None:
        # COPY mode puts media in the FBX convention's "<name>.fbm" folder beside the .fbx.
        found = [p for p in (os.path.join(out_dir, f"{args.name}.fbm", png_name), os.path.join(out_dir, png_name))
                 if os.path.isfile(p)]
        png_path = found[0] if found else None
        result["texture"] = {
            "path": png_path, "relpath": os.path.relpath(png_path, out_dir).replace(os.sep, "/") if png_path else None,
            "copied_by_exporter": png_path is not None,
            "bytes": os.path.getsize(png_path) if png_path else None,
            "sha256": sha256(png_path) if png_path else None,
        }
    shutil.rmtree(tmp_dir, ignore_errors=True)

    t = time.perf_counter()
    result["reimport"] = reimport_report(fbx_path)
    timings["reimport_s"] = round(time.perf_counter() - t, 2)
    timings["total_s"] = round(time.perf_counter() - t_all, 2)
    result["timings"] = timings
    meshes = [r for r in result["reimport"] if r.get("type") == "MESH"]
    has_uv = bool(meshes) and all(r["uv_layers"] for r in meshes)
    has_mat = bool(meshes) and all(r["materials"] and r["materials"][0]["name"] for r in meshes)
    has_tex = bool(meshes) and all(any(i["exists"] for m in r["materials"] for i in m["images"]) for r in meshes)
    # The compile resolves the FBX material NAME as the .vmat path, so the name must come back verbatim.
    reimported = [m.name for m in bpy.data.materials]
    survived = bool(reimported) and reimported[0] == args.material_path
    result["material_path_check"] = {"expected": args.material_path, "reimported": reimported,
                                     "survived": survived}
    result["verdict"] = {"fbx_has_uv": has_uv, "fbx_has_material": has_mat, "fbx_texture_resolves": has_tex,
                         "baked_texture": bool(image is not None and has_tex),
                         "material_path_survived": survived}
    code = 0 if result["verdict"]["baked_texture"] and has_uv and has_mat else 3
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
