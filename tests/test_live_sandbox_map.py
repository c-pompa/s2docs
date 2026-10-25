"""The helper map + script the live actions stream onto stock de_dust2 (Tasks 4.1-4.3).

Offline only: these tests read the templates and render text; nothing is compiled or deployed here.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from s2docs.live import sandbox_map, spawnables

NODE = shutil.which("node")


def test_entities_and_determinism():
    text = sandbox_map.render_vmap(spawnables.stock())
    # point_script + (point_template + child) per spawnable: chicken, the three cars, the generic prop
    assert text.count('"CMapEntity"') == 11
    assert '"classname" "string" "point_template"' in text and '"Template01" "string" "hs_src_chicken"' in text
    assert '"cs_script" "string" "maps/scripts/hermes_sandbox.vjs"' in text
    assert text.count("{") == text.count("}") and "{{ENTITIES}}" not in text
    assert text == sandbox_map.render_vmap(spawnables.stock())  # byte-stable -> stable md5 for deploys


def test_script_is_one_file_without_exports():
    js = sandbox_map.render_script(spawnables.stock())
    assert "export " not in js and "function positions(" in js and '"chicken": 24' in js
    # stack spacing comes from each spawnable's own height
    for height in ('"car_hatch": 64', '"car_van": 96', '"car_wreck": 64', '"prop": 32'):
        assert height in js


CARS = {
    "car_wreck": "models/props/de_dust/hr_dust/dust_vehicles/dust_car_wreck_body.vmdl",
    "car_hatch": "models/props_vehicles/cara_82hatchback.vmdl",
    "car_van": "models/props_vehicles/van.vmdl",
}


def test_stock_cars_are_single_model_props():
    """dust2's own cars ship as 4-part models; these single-model vehicles are the ones that spawn as ONE entity."""
    stock = spawnables.stock()
    for sid, model in CARS.items():
        car = stock[sid]
        assert car.template == f"hs_tpl_{sid}" and car.classname == "prop_dynamic" and car.model == model
        assert car.console_ok is False  # nothing proved ent_create renders them; the template precaches them
        assert car.height > 0
    text = sandbox_map.render_vmap(stock)
    for sid, model in CARS.items():
        assert f'"targetname" "string" "hs_tpl_{sid}"' in text and f'"model" "string" "{model}"' in text


def test_prop_template_is_the_runtime_precache_placeholder():
    """`hs_model` swaps a real model onto an entity of this template (spike S2)."""
    prop = spawnables.stock()["prop"]
    assert prop.template == "hs_tpl_prop" and prop.classname == "prop_dynamic"
    assert prop.model == "models/chicken/chicken_roasted.vmdl"  # VPK-listed; also the TELL if SetModel failed
    assert prop.console_ok is False and prop.height == 32.0


def test_script_registers_hs_model_and_pumps_it_first_in_the_think():
    js = sandbox_map.render_script(spawnables.stock())
    assert 'Instance.RegisterCheatCommand("hs_model"' in js and "Instance.Precache({ resources: [vmdl] })" in js
    assert "function pumpModels()" in js
    think = js.split("Instance.SetThink(() => {", 1)[1]
    assert think.lstrip().startswith("pumpModels();")  # a pending precache is serviced every think


def test_vmap_keeps_the_iso_entity_shape():
    """The generated block must look like the hand-authored one in iso_spectate_ui.vmap (lines 32-84)."""
    text = sandbox_map.render_vmap(spawnables.stock())
    block = text.split('\t\t\t"CMapEntity"\n')[1].split("\t\t\t},\n")[0]
    for key in ('"id" "elementid" ', '"nodeID" "int" "', '"referenceID" "uint64" "0x', '"children" "element_array"',
                '"variableTargetKeys" "string_array"', '"variableNames" "string_array"',
                '"relayPlugData" "DmePlugList"', '"connectionsData" "element_array"',
                '"entity_properties" "EditGameClassProps"', '"hitNormal" "vector3" "0 0 1"',
                '"isProceduralEntity" "bool" "0"', '"origin" "vector3" "0 0 96"', '"angles" "qangle" "0 0 0"',
                '"scales" "vector3" "1 1 1"', '"transformLocked" "bool" "0"', '"force_hidden" "bool" "0"',
                '"editorOnly" "bool" "0"', '"customVisGroup" "string" ""', '"randomSeed" "int" "'):
        assert key in block, key


def test_write_addon_writes_both_sources(tmp_path):
    out = sandbox_map.write_addon(tmp_path / "hermes_sandbox", spawnables.stock())
    assert [p.name for p in out] == ["hermes_sandbox.vmap", "hermes_sandbox.js"]
    assert out[0].read_text(encoding="utf-8") == sandbox_map.render_vmap(spawnables.stock())
    assert out[1].read_text(encoding="utf-8") == sandbox_map.render_script(spawnables.stock())


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_formation_js_matches_the_python_vectors():
    """The node half of the shared vectors runs in CI too, not only by hand."""
    proc = subprocess.run([NODE, "--test", "tests/js/formation.test.mjs"], capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[1], timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
