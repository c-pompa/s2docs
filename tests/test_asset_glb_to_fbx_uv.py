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
