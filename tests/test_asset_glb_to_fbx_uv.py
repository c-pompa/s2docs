"""The asset bridge's pure logic: triangle budget, pivot, Source units, texture naming, collision seam.

``scripts/asset/glb_to_fbx_uv.py`` runs inside Blender, so ``bpy`` is stubbed out here and only the
parts that do not touch the scene are exercised: the arithmetic that decides the decimate ratio and
the recorded report fields, the base-centre pivot, the metre -> Source-unit conversion, the PNG names
and ``.vmat`` keys (pinned against ``s2docs.asset.material``, which owns the other side of that
seam), and argument validation. The Blender half — decimate, the normal/AO bakes, the FBX round trip
— is proven by a real run on the Windows host; see docs/HAMMERED.md and
docs/evidence/hammered/tsr_eefa8331_meshq_bridge.json.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
import types
from pathlib import Path

import pytest

from s2docs.asset.material import MAX_ID_NAME, material_path, render_vmat

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "asset" / "glb_to_fbx_uv.py"


@pytest.fixture(scope="module")
def bridge():
    """The script as a module, with ``bpy`` stubbed (no Blender, no numpy in the test env)."""
    stub = types.ModuleType("bpy")
    stub.types = types.SimpleNamespace(Object=object, Mesh=object, Image=object)
    stub.app = types.SimpleNamespace(version_string="stub")
    sys.modules.setdefault("bpy", stub)
    spec = importlib.util.spec_from_file_location("glb_to_fbx_uv", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def args(bridge, *argv):
    return bridge.parse_args(["--glb", "mesh.glb", "--out-dir", "out", "--name", "tsr_probe", *argv])


# ------------------------------------------------------------------ argument validation


def test_defaults_are_a_game_budget_run(bridge):
    a = args(bridge)
    assert a.target_tris == 5000
    assert a.res == 2048
    assert a.unit_scale == pytest.approx(39.3700787)
    assert a.material_path == "materials/hammered/tsr_probe/tsr_probe.vmat"
    assert a.verify_source is True
    assert a.cage_pct == 2.0 and a.ao_distance_pct == 50.0
    assert a.force is False


def test_target_tris_can_be_switched_off_but_not_silly(bridge):
    assert args(bridge, "--target-tris", "0").target_tris == 0
    assert args(bridge, "--target-tris", "12000").target_tris == 12000
    for bad in ("-1", "3", "10000001"):
        with pytest.raises(bridge.BridgeError, match="target-tris"):
            args(bridge, "--target-tris", bad)


def test_unit_scale_and_percentages_are_validated(bridge):
    assert args(bridge, "--unit-scale", "100").unit_scale == 100.0
    for flag, bad in (("--unit-scale", "0"), ("--unit-scale", "-39"), ("--cage-pct", "-1"),
                      ("--cage-pct", "101"), ("--ao-distance-pct", "0"), ("--ao-distance-pct", "1001"),
                      ("--samples", "0"), ("--ao-samples", "0"), ("--bake-margin", "-1")):
        with pytest.raises(bridge.BridgeError):
            args(bridge, flag, bad)


def test_res_bounds_are_the_bake_bounds(bridge):
    assert args(bridge, "--res", "64").res == 64
    assert args(bridge, "--res", "8192").res == 8192
    with pytest.raises(bridge.BridgeError, match="res"):
        args(bridge, "--res", "32")


def test_names_and_material_paths_keep_the_blender_limit(bridge):
    with pytest.raises(bridge.BridgeError, match="name"):
        bridge.parse_args(["--glb", "m.glb", "--out-dir", "o", "--name", "Probe"])
    with pytest.raises(bridge.BridgeError, match="material-path"):
        args(bridge, "--material-path", "Materials/Hammered/x/x.vmat")
    with pytest.raises(bridge.BridgeError, match="63"):
        args(bridge, "--material-path", "materials/hammered/" + "a" * 30 + "/" + "a" * 30 + ".vmat")
    # 25 + 2n characters is the longest path Blender keeps as an ID name
    longest = "a" * 19
    assert len(material_path(longest)) == MAX_ID_NAME
    assert args(bridge, "--name", longest).material_path == material_path(longest)


# ------------------------------------------------------------------ triangle budget


def test_decimate_ratio_lands_under_the_budget(bridge):
    tris_in, target = 58696, 5000
    ratio = bridge.decimate_ratio(tris_in, target)
    assert 0.0 < ratio < 1.0
    assert ratio * tris_in <= target  # the safety factor is what keeps the first pass under budget
    assert ratio == pytest.approx(target / tris_in * bridge.DECIMATE_SAFETY)


def test_decimate_ratio_is_a_no_op_at_or_under_budget(bridge):
    assert bridge.decimate_ratio(4000, 5000) == 1.0
    assert bridge.decimate_ratio(5000, 5000) == 1.0
    assert bridge.decimate_ratio(58696, 0) == 1.0  # --target-tris 0 turns decimation off


def test_decimate_report_records_in_out_and_ratio(bridge):
    report = bridge.decimate_report(58696, 4904, 5000, [{"ratio": 0.083493, "tris": 4904}])
    assert report["tris_in"] == 58696 and report["tris_out"] == 4904
    assert report["ratio"] == round(4904 / 58696, 6)
    assert report["target_ratio"] == round(5000 / 58696, 6)
    assert report["within_budget"] is True
    assert report["target_tris"] == 5000 and report["enabled"] is True
    assert report["passes"] == [{"ratio": 0.083493, "tris": 4904}]


def test_decimate_report_flags_a_missed_budget(bridge):
    report = bridge.decimate_report(10000, 5100, 5000, [])
    assert report["within_budget"] is False
    off = bridge.decimate_report(58696, 58696, 0, [])
    assert off["within_budget"] is True and off["ratio"] == 1.0 and off["enabled"] is False


# ------------------------------------------------------------------ pivot and units


def test_pivot_is_the_base_centre(bridge):
    # x/y centre, min z: a prop stands on its origin, it is not centred vertically
    assert bridge.base_centre_pivot([-0.4, -0.1, 0.2], [0.4, 0.6, 1.0]) == [0.0, -0.25, -0.2]
    assert bridge.base_centre_pivot([1.0, 2.0, 3.0], [3.0, 6.0, 4.0]) == [-2.0, -4.0, -3.0]
    # already centred at the base: nothing to do
    assert bridge.base_centre_pivot([-1.0, -1.0, 0.0], [1.0, 1.0, 2.0]) == [0.0, 0.0, 0.0]


def test_source_units_are_inches(bridge):
    # 39.3700787 inches to the metre, so a 1 m box is 39.37 Source units on a side
    assert bridge.to_source_units([1.0, 1.0, 1.0], bridge.SOURCE_UNITS_PER_BU) == [39.37, 39.37, 39.37]
    # the measured TripoSR prop: 0.8377 x 0.2964 x 1.0021 m is a ~33 x 12 x 39 inch prop
    assert bridge.to_source_units([0.8377, 0.2964, 1.0021], bridge.SOURCE_UNITS_PER_BU) == [32.98, 11.67, 39.45]


def test_normalise_report_carries_units_and_pivot(bridge):
    report = bridge.normalise_report([0.84, 0.30, 1.00], [0.0, 0.15, -0.02], 39.3700787,
                                    [-0.42, -0.15, 0.0])
    assert report["bbox_units"] == [33.07, 11.81, 39.37]
    assert report["bbox_bu"] == [0.84, 0.3, 1.0]
    assert report["pivot_units"] == [0.0, 5.91, -0.79]
    assert report["unit_scale"] == pytest.approx(39.3700787)
    assert "inch" in report["unit"]
    assert "min z" in report["pivot_rule"]
    assert report["z_up"] is True
    assert report["bbox_min_after_bu"] == [-0.42, -0.15, 0.0]
    # a bbox min after normalisation must sit on the ground plane
    assert report["bbox_min_after_bu"][2] == 0.0


# ------------------------------------------------------------------ texture naming and .vmat seam


def test_texture_names_follow_the_material_convention(bridge):
    assert bridge.texture_name("tsr_probe", "color") == "tsr_probe_color.png"
    assert bridge.texture_name("tsr_probe", "normal") == "tsr_probe_normal.png"
    assert bridge.texture_name("tsr_probe", "ao") == "tsr_probe_ao.png"
    with pytest.raises(bridge.BridgeError):
        bridge.texture_name("tsr_probe", "roughness")


def test_texture_paths_sit_beside_the_vmat_material_py_writes(bridge):
    name = "tsr_probe"
    vmat = material_path(name)
    assert bridge.vmat_dir(name) == vmat.rsplit("/", 1)[0]
    paths = bridge.vmat_textures(name)
    # the colour map is exactly what s2docs.asset.material.color_texture_path() expects
    from s2docs.asset.material import color_texture_path

    assert paths["TextureColor"] == color_texture_path(name)
    assert paths["TextureNormal"] == "materials/hammered/tsr_probe/tsr_probe_normal.png"
    assert paths["TextureAmbientOcclusion"] == "materials/hammered/tsr_probe/tsr_probe_ao.png"
    assert set(paths) == {"TextureColor", "TextureNormal", "TextureAmbientOcclusion"}


def test_vmat_layer0_is_material_pys_document_with_our_textures(bridge):
    name = "tsr_probe"
    pairs = bridge.vmat_layer0(name, material_path(name))
    keys = [k for k, _ in pairs]
    # same keys, same order, same non-texture values as s2docs.asset.material.render_vmat
    reference = [line.split("\t")[1].strip('"') for line in render_vmat(name).splitlines() if line.startswith('\t"')]
    assert keys == reference == ["shader", "TextureMetalness", "TextureAmbientOcclusion", "TextureColor",
                                 "TextureNormal", "TextureRoughness"]
    reference_values = dict(line.strip().strip('"').split('"\t"') for line in render_vmat(name).splitlines()
                            if line.startswith('\t"'))
    ours = dict(pairs)
    assert ours["shader"] == reference_values["shader"]
    assert ours["TextureMetalness"] == reference_values["TextureMetalness"]
    assert ours["TextureRoughness"] == reference_values["TextureRoughness"]
    # and the three texture keys move off the engine defaults onto the maps this bridge bakes
    assert ours["TextureColor"] == reference_values["TextureColor"]
    assert ours["TextureNormal"] == "materials/hammered/tsr_probe/tsr_probe_normal.png"
    assert ours["TextureAmbientOcclusion"] == "materials/hammered/tsr_probe/tsr_probe_ao.png"
    assert "materials/default/default_normal.tga" not in ours.values()
    assert "materials/default/default_ao.tga" not in ours.values()


def test_vmat_layer0_refuses_a_material_path_away_from_the_textures(bridge):
    with pytest.raises(bridge.BridgeError, match="beside"):
        bridge.vmat_layer0("tsr_probe", "materials/other/tsr_probe/tsr_probe.vmat")


# ------------------------------------------------------------------ collision seam


def test_collision_name_matches_the_vmdl_contract(bridge):
    assert bridge.collision_mesh_name("tsr_probe") == "tsr_probe_collision"


def test_collision_is_picked_up_only_on_an_exact_name(bridge):
    name = "tsr_probe"
    assert bridge.pick_collision(["tsr_probe_collision"], name) == "tsr_probe_collision"
    assert bridge.pick_collision(["body", "tsr_probe_collision"], name) == "tsr_probe_collision"
    # near misses are not a collision mesh: the bridge never guesses and never invents geometry
    for near in ("tsr_probe_collision_01", "collision", "tsr_probe_coll", "tsr_probe_Collision",
                 "TSR_PROBE_COLLISION", "tsr_probe_collision.001"):
        assert bridge.pick_collision([near], name) is None
    assert bridge.pick_collision([], name) is None


def test_collision_name_follows_the_model_name(bridge):
    assert bridge.pick_collision(["crate_collision"], "crate") == "crate_collision"
    assert bridge.pick_collision(["crate_collision"], "crate_2") is None


# ------------------------------------------------------------------ FBX units: the numbers are inches
#
# The measured bug: the bridge normalised the mesh to inches (bbox_units 32.98 x 11.67 x 39.45) but the
# FBX wrote centimetres (83.467 x 29.656 x 100.027) and declared no UnitScaleFactor, so Source read them
# as inches and a compiled prop came out 2.53x too big. These tests pin the factor and the reader that
# proves it in the report without a compile.


def _fbx_header(version: int = 7500) -> bytes:
    return b"Kaydara FBX Binary  \x00" + b"\x1a\x00" + struct.pack("<I", version)


def _fbx_scalar(kind: str, fmt: str, value):
    return kind.encode() + struct.pack(fmt, value)


def _fbx_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return b"S" + struct.pack("<I", len(raw)) + raw


def _fbx_double_array(values: list[float]) -> bytes:
    raw = struct.pack(f"<{len(values)}d", *values)
    return b"d" + struct.pack("<III", len(values), 0, len(raw)) + raw


def _fbx_record(name: str, props: tuple = (), children: tuple = (), *, start: int = 0,
                wide: bool = True) -> bytes:
    """One FBX binary record (64-bit headers at version 7500), children laid out after the props."""
    header = 24 if wide else 12
    name_raw = name.encode("utf-8")
    props_raw = b"".join(props)
    child_raw = b""
    offset = start + header + 1 + len(name_raw) + len(props_raw)
    for child in children:
        chunk = _fbx_record(*child, start=offset, wide=wide)
        child_raw += chunk
        offset += len(chunk)
    terminator = b"\x00" * (13 if wide else 9)
    end = start + header + 1 + len(name_raw) + len(props_raw) + len(child_raw) + len(terminator)
    head = struct.pack("<QQQ" if wide else "<III", end, len(props), len(props_raw))
    return head + bytes([len(name_raw)]) + name_raw + props_raw + child_raw + terminator


def _write_probe_fbx(path: Path, *, unit_scale_factor: float = 2.54, extent_bu=(1.0, 2.0, 3.0),
                     unit_scale: float = 39.3700787, name: str = "probe") -> Path:
    """A minimal binary FBX shaped like Blender's: GlobalSettings/USF, Model, Geometry, Connections.

    ``extent_bu`` is in Blender's Z-up axes; the file carries ``(x, z, -y)`` because the exporter bakes
    ``axis_forward='-Z'``/``axis_up='Y'`` into the vertices (measured on 4.5.15).
    """
    x, y, z = (v * unit_scale for v in extent_bu)
    vertices = [0.0, 0.0, 0.0, x, 0.0, 0.0, 0.0, z, 0.0, 0.0, 0.0, -y]
    settings = ("GlobalSettings", (), (
        ("Properties70", (), (
            ("P", (_fbx_string("UnitScaleFactor"), _fbx_string("Number"), _fbx_string(""),
                   _fbx_string("A"), _fbx_scalar("D", "<d", unit_scale_factor)), ()),
        )),
    ))
    model = ("Model", (_fbx_scalar("L", "<q", 1), _fbx_string(f"{name}\x00\x01Model"),
                       _fbx_string("Mesh")), ())
    geometry = ("Geometry", (_fbx_scalar("L", "<q", 2), _fbx_string(f"{name}\x00\x01Geometry")),
                (("Vertices", (_fbx_double_array(vertices),), ()),))
    objects = ("Objects", (), (model, geometry))
    connections = ("Connections", (), (
        ("C", (_fbx_string("OO"), _fbx_scalar("L", "<q", 2), _fbx_scalar("L", "<q", 1)), ()),
    ))
    start = len(_fbx_header())
    body = b""
    for node in (settings, objects, connections):
        body += _fbx_record(*node, start=start + len(body))
    path.write_bytes(_fbx_header() + body + b"\x00" * 13)
    return path


def test_export_scale_settings_write_source_units(bridge):
    """The 4.5.15 recipe: global_scale = Source units per BU, and USF = 100/units (2.54 = one inch)."""
    s = bridge.export_scale_settings(bridge.SOURCE_UNITS_PER_BU)
    assert s["global_scale"] == pytest.approx(39.3700787)
    assert s["apply_unit_scale"] is True
    assert s["apply_scale_options"] == "FBX_SCALE_UNITS"
    assert s["scene_scale_length"] == pytest.approx(0.0254)
    assert s["expected_unit_scale_factor"] == pytest.approx(2.54)
    # --unit-scale counts Source units per Blender unit, so a centimetre run declares USF 1.0 instead
    cm = bridge.export_scale_settings(100.0)
    assert cm["global_scale"] == 100.0
    assert cm["scene_scale_length"] == pytest.approx(0.01)
    assert cm["expected_unit_scale_factor"] == pytest.approx(1.0)
    for bad in (0.0, -39.37):
        with pytest.raises(bridge.BridgeError):
            bridge.export_scale_settings(bad)


def test_unit_check_catches_the_centimetre_error(bridge):
    intended = [32.98, 11.67, 39.45]
    ok = bridge.unit_check(intended, [32.86, 11.68, 39.40], bridge.EXPORT_UNIT_TOL_INTENDED)
    assert ok["ok"] is True
    assert ok["ratio"] == [0.99636, 1.00086, 0.99873]
    assert ok["max_rel_err"] == 0.00364
    # the same box written in centimetres and read as inches is 2.53x on x — nowhere near the tolerance
    bad = bridge.unit_check(intended, [83.47, 29.66, 100.03], bridge.EXPORT_UNIT_TOL_INTENDED)
    assert bad["ok"] is False and bad["ratio"][0] > 2.5
    # the mesh tolerance is tighter than the body one, on purpose: 0.6 % off fails the mesh check while
    # the same numbers pass the body check (which allows decimation's shrink)
    off = bridge.unit_check(intended, [32.9, 11.6, 39.2], bridge.EXPORT_UNIT_TOL_MESH)
    assert off["ok"] is False and off["max_rel_err"] == 0.00634
    assert bridge.unit_check(intended, [32.9, 11.6, 39.2], bridge.EXPORT_UNIT_TOL_INTENDED)["ok"] is True
    with pytest.raises(bridge.BridgeError, match="axes"):
        bridge.unit_check([1.0, 2.0, 3.0], [1.0, 2.0], 0.05)


def test_fbx_measure_reads_the_unit_factor_and_the_extent(bridge, tmp_path):
    m = bridge.fbx_measure(str(_write_probe_fbx(tmp_path / "probe.fbx")))
    assert m["unit_scale_factor"] == pytest.approx(2.54)
    assert m["unit_scale_factor_declared"] is True
    assert m["unit_inches"] == pytest.approx(1.0)
    obj = m["objects"][0]
    assert obj["name"] == "probe" and obj["vertices"] == 4
    # the file's own axes: (x, z, -y) of a 1 x 2 x 3 BU box, times 39.3700787
    assert obj["extent"] == [39.3701, 118.1102, 78.7402]
    # and mapped back to the Z-up frame bbox_units is measured in: (X, Z, Y)
    assert obj["extent_z_up"] == [39.3701, 78.7402, 118.1102]


def test_fbx_measure_does_not_raise_on_a_file_it_cannot_read(bridge, tmp_path):
    bad = tmp_path / "bad.fbx"
    bad.write_bytes(b"not an FBX at all")
    m = bridge.fbx_measure(str(bad))
    assert "error" in m and m["objects"] == []


def test_export_units_report_passes_when_the_file_carries_inches(bridge, tmp_path):
    settings = bridge.export_scale_settings(bridge.SOURCE_UNITS_PER_BU)
    measured = bridge.fbx_measure(str(_write_probe_fbx(tmp_path / "probe.fbx")))
    mesh_units = [39.3701, 78.7402, 118.1102]  # a 1 x 2 x 3 BU mesh in Source units, Z-up
    report = bridge.export_units_report(39.3700787, settings, measured, mesh_units, mesh_units, "probe")
    assert report["exported_units"] == [39.3701, 78.7402, 118.1102]
    assert report["check_vs_mesh"]["ok"] is True and report["check_vs_mesh"]["ratio"] == [1.0, 1.0, 1.0]
    assert report["check_vs_intended"]["ok"] is True
    assert report["unit_scale_factor_matches"] is True
    assert report["ok"] is True
    assert report["intended_units"] == mesh_units and report["mesh_units"] == mesh_units


def test_export_units_report_fails_a_centimetre_file(bridge, tmp_path):
    """USF 1.0 + centimetre numbers is the shipped bug: the report must say so without a compile."""
    settings = bridge.export_scale_settings(bridge.SOURCE_UNITS_PER_BU)
    measured = bridge.fbx_measure(str(_write_probe_fbx(
        tmp_path / "cm.fbx", unit_scale_factor=1.0, extent_bu=(2.12, 0.753, 2.54), unit_scale=39.3700787)))
    report = bridge.export_units_report(39.3700787, settings, measured, [32.98, 11.67, 39.45],
                                        [32.98, 11.67, 39.45], "probe")
    assert report["unit_scale_factor_matches"] is False
    assert report["check_vs_mesh"]["ok"] is False and report["check_vs_intended"]["ok"] is False
    assert report["ok"] is False


def test_export_units_report_says_why_when_the_body_is_missing(bridge, tmp_path):
    settings = bridge.export_scale_settings(bridge.SOURCE_UNITS_PER_BU)
    measured = bridge.fbx_measure(str(_write_probe_fbx(tmp_path / "probe.fbx", name="other")))
    report = bridge.export_units_report(39.3700787, settings, measured, [32.98], [32.98], "probe")
    assert report["ok"] is False and "no geometry named" in report["why"]
    # an unreadable file reports the reader's error instead of a traceback
    broken = bridge.export_units_report(39.3700787, settings, {"error": "ValueError: nope", "objects": []},
                                        [32.98], [32.98], "probe")
    assert broken["ok"] is False and "no geometry named" in broken["why"]


# ------------------------------------------------------------------ report hygiene


def test_bridge_json_is_written_with_lf_endings():
    """The report is committed from a Windows Blender run; CRLF fails the CHC-COMMITS gate."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'newline="\\n"' in source
    assert b"\r\n" not in SCRIPT.read_bytes()


def test_docstring_documents_the_collision_contract():
    doc = SCRIPT.read_text(encoding="utf-8").split('"""')[1]
    assert "COLLISION CONTRACT" in doc
    assert "<name>_collision" in doc
    assert "invents no geometry" in doc
