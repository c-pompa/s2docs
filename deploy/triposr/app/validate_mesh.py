"""Parse produced meshes and report what is actually in them (runs inside the container).

  docker exec triposr-worker python /app/validate_mesh.py /outputs/<task_id>/mesh.glb [/outputs/<task_id>/mesh.obj ...]

Two independent readers per GLB: trimesh (the container's 4.0.5) and the raw glTF JSON
chunk (accessor counts, attributes, materials/textures/images). Exit 1 if any file is
empty or unreadable.
"""
import hashlib
import json
import os
import struct
import sys

import numpy as np
import trimesh


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _gltf(path):
    with open(path, "rb") as fh:
        head = fh.read(20)
        magic, version, length = struct.unpack("<4sII", head[:12])
        if magic != b"glTF":
            raise ValueError("not a GLB (bad magic)")
        jlen, jtype = struct.unpack("<II", head[12:20])
        if jtype != 0x4E4F534A:
            raise ValueError("first GLB chunk is not JSON")
        doc = json.loads(fh.read(jlen))
    prims = [p for m in doc.get("meshes", []) for p in m.get("primitives", [])]
    acc = doc.get("accessors", [])
    idx = sum(acc[p["indices"]]["count"] for p in prims if "indices" in p)
    return {
        "glb_version": version,
        "glb_length": length,
        "meshes": len(doc.get("meshes", [])),
        "primitives": len(prims),
        "modes": sorted({p.get("mode", 4) for p in prims}),  # 4 = TRIANGLES
        "attributes": sorted({a for p in prims for a in p.get("attributes", {})}),
        "position_count": sum(acc[p["attributes"]["POSITION"]]["count"] for p in prims),
        "index_count": idx,
        "triangles_from_indices": idx // 3,
        "has_uv_TEXCOORD_0": any("TEXCOORD_0" in p.get("attributes", {}) for p in prims),
        "has_COLOR_0": any("COLOR_0" in p.get("attributes", {}) for p in prims),
        "has_NORMAL": any("NORMAL" in p.get("attributes", {}) for p in prims),
        "materials": len(doc.get("materials", [])),
        "primitive_material_refs": [p.get("material") for p in prims],
        "textures": len(doc.get("textures", [])),
        "images": len(doc.get("images", [])),
        "asset_generator": doc.get("asset", {}).get("generator"),
    }


def _obj_records(path):
    counts = {"v": 0, "vt": 0, "vn": 0, "f": 0, "mtllib": 0, "usemtl": 0}
    v_cols = set()
    with open(path, errors="replace") as fh:
        for line in fh:
            key = line.split(" ", 1)[0]
            if key in counts:
                counts[key] += 1
                if key == "v":
                    v_cols.add(len(line.split()) - 1)
    counts["v_columns"] = sorted(v_cols)  # 6 = xyz + rgb vertex colours
    return counts


def report(path):
    out = {"file": path, "bytes": os.path.getsize(path), "sha256": _sha256(path)}
    loaded = trimesh.load(path, process=False)
    geoms = list(loaded.geometry.values()) if isinstance(loaded, trimesh.Scene) else [loaded]
    verts = sum(len(g.vertices) for g in geoms)
    tris = sum(len(g.faces) for g in geoms)
    lo = np.min([g.bounds[0] for g in geoms], axis=0)
    hi = np.max([g.bounds[1] for g in geoms], axis=0)
    out["trimesh"] = {
        "version": trimesh.__version__,
        "geometries": len(geoms),
        "vertices": int(verts),
        "triangles": int(tris),
        "visual_kinds": sorted({str(g.visual.kind) for g in geoms}),
        "has_uv": any(getattr(g.visual, "uv", None) is not None for g in geoms),
        "has_material": any(getattr(g.visual, "material", None) is not None for g in geoms),
        "vertex_colors": [list(g.visual.vertex_colors.shape) for g in geoms if g.visual.kind == "vertex"],
        "watertight": all(g.is_watertight for g in geoms),
        "bounds": [[round(float(x), 4) for x in lo], [round(float(x), 4) for x in hi]],
        "extents": [round(float(x), 4) for x in (hi - lo)],
    }
    if path.lower().endswith(".glb"):
        out["gltf_json"] = _gltf(path)
    elif path.lower().endswith(".obj"):
        out["obj_records"] = _obj_records(path)
    out["ok"] = verts > 0 and tris > 0
    return out


if __name__ == "__main__":
    reports = []
    for p in sys.argv[1:]:
        try:
            reports.append(report(p))
        except Exception as exc:  # noqa: BLE001
            reports.append({"file": p, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    print(json.dumps(reports, indent=2))
    sys.exit(0 if reports and all(r.get("ok") for r in reports) else 1)
