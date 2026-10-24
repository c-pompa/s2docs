"""A generated ``.vmdl`` must be the document the compiler accepts, not a plausible one.

The structure below is pinned against a real CS2 document (``modeldoc28`` from a live addon), and the
class/field names are asserted literally: an invented field or a renamed class compiles to nothing.

The physics half is pinned the same way, against shipped props decompiled from ``pak01_dir.vpk`` with
Source2Viewer-CLI 20.1 (reference only, never copied into this repo): ``dizzy_cinderblock`` (one
``PhysicsHullFile``, ``surface_prop = "rock"``), ``steel_drum_barrel_01a`` (``PhysicsMeshFile``,
``metal_barrel``) and ``dizzy_generator_full`` (26 hulls, ``metal``). Every one of them carries exactly
five keys — ``filename``, ``parent_bone``, ``surface_prop``, ``collision_tags``, ``name`` — and none
carries a ``collision_group``; that key belongs to the entity, not the modeldoc.

The end-to-end proof (run ``resourcecompiler`` on the generated file) lives outside the unit suite
because it needs a Windows host with the game installed.
"""

from __future__ import annotations

import pytest

from s2docs.asset.model import (
    COLLISION_MESH_SUFFIX,
    MAX_MESH_NAME,
    MODEL_DOC_HEADER,
    ModelDocError,
    collision_mesh_name,
    mesh_reference,
    render_model_doc,
    write_model_doc,
)


def _crlf(*lines: str) -> str:
    """The documents use CRLF; the literals below stay readable with plain newlines."""
    return "\r\n".join(lines) + "\r\n"


# The whole no-collision document, byte for byte. This is the shape every pre-existing caller gets:
# if a change to the physics plumbing moves one tab or adds a key here, this test says so.
NO_COLLISION_DOC = _crlf(
    MODEL_DOC_HEADER,
    "{",
    "\trootNode = ",
    "\t{",
    '\t\t_class = "RootNode"',
    "\t\tchildren = ",
    "\t\t[",
    "\t\t\t{",
    '\t\t\t\t_class = "BoneMarkupList"',
    "\t\t\t\tchildren = [  ]",
    '\t\t\t\tbone_cull_type = "None"',
    "\t\t\t},",
    "\t\t\t{",
    '\t\t\t\t_class = "RenderMeshList"',
    "\t\t\t\tchildren = ",
    "\t\t\t\t[",
    "\t\t\t\t\t{",
    '\t\t\t\t\t\t_class = "RenderMeshFile"',
    '\t\t\t\t\t\tname = "probe"',
    '\t\t\t\t\t\tfilename = "models/probe/probe.fbx"',
    "\t\t\t\t\t},",
    "\t\t\t\t]",
    "\t\t\t},",
    "\t\t]",
    "\t}",
    "}",
)

# The physics node exactly as the shipped documents spell it, with the generated prop's names.
COLLISION_BLOCK = _crlf(
    "\t\t\t{",
    '\t\t\t\t_class = "PhysicsShapeList"',
    "\t\t\t\tchildren = ",
    "\t\t\t\t[",
    "\t\t\t\t\t{",
    '\t\t\t\t\t\t_class = "PhysicsHullFile"',
    '\t\t\t\t\t\tfilename = "models/probe/probe.fbx"',
    '\t\t\t\t\t\tparent_bone = ""',
    '\t\t\t\t\t\tsurface_prop = "metal"',
    '\t\t\t\t\t\tcollision_tags = ""',
    '\t\t\t\t\t\tname = "probe_collision"',
    "\t\t\t\t\t},",
    "\t\t\t\t]",
    "\t\t\t},",
)


def test_header_is_the_observed_format_identity():
    assert MODEL_DOC_HEADER.startswith("<!-- kv3 encoding:text:version{")
    assert "format:modeldoc28:version{" in MODEL_DOC_HEADER


def test_document_carries_the_real_node_classes():
    doc = render_model_doc("probe", "models/probe/probe.dmx")
    for fragment in (
        '_class = "RootNode"',
        '_class = "BoneMarkupList"',
        '_class = "RenderMeshList"',
        '_class = "RenderMeshFile"',
        'name = "probe"',
        'filename = "models/probe/probe.dmx"',
    ):
        assert fragment in doc, f"missing {fragment!r}"
    # no invented modifier list when none was asked for
    assert "ModelModifierList" not in doc


def test_document_without_collision_is_byte_identical_to_the_pinned_document():
    """The default must not drift: existing callers keep producing exactly this file."""
    assert render_model_doc("probe", "models/probe/probe.fbx") == NO_COLLISION_DOC
    # and nothing physics-shaped sneaks in without being asked for
    assert "Physics" not in NO_COLLISION_DOC
    assert render_model_doc("probe", "models/probe/probe.fbx", translation=None,
                            import_filter_exceptions=None) == NO_COLLISION_DOC


def test_collision_emits_the_observed_physics_node_verbatim():
    doc = render_model_doc("probe", "models/probe/probe.fbx", collision="metal")
    assert COLLISION_BLOCK in doc
    # same five keys, in the shipped order, and nothing invented
    keys = ["filename", "parent_bone", "surface_prop", "collision_tags", "name"]
    start = doc.index('_class = "PhysicsHullFile"')
    block = doc[start:doc.index("\t\t\t\t\t},", start)]
    written = [line.split(" = ")[0].strip() for line in block.splitlines()
               if " = " in line and not line.strip().startswith("_class")]
    assert written == keys, "physics keys are not the shipped five, in the shipped order"
    for invented in ("collision_group", "mass =", "PhysicsSphere", "PhysicsBox", "surface_property"):
        assert invented not in doc


def test_collision_document_is_the_no_collision_document_plus_the_physics_node():
    plain = render_model_doc("probe", "models/probe/probe.fbx")
    doc = render_model_doc("probe", "models/probe/probe.fbx", collision="metal")
    assert doc.replace(COLLISION_BLOCK, "") == plain
    assert doc.count("{") == doc.count("}")
    assert doc.count("[") == doc.count("]")


def test_physics_node_precedes_the_model_modifier_list():
    doc = render_model_doc("probe", "models/probe/probe.fbx", collision="rock", translation=(0, 0, 12))
    assert doc.index('_class = "PhysicsShapeList"') < doc.index('_class = "ModelModifierList"')
    assert doc.index('_class = "RenderMeshList"') < doc.index('_class = "PhysicsShapeList"')
    assert 'surface_prop = "rock"' in doc
    assert "translation = [ 0, 0, 12 ]" in doc
    assert doc.count("{") == doc.count("}")


def test_collision_mesh_defaults_to_the_render_mesh_and_can_point_elsewhere():
    """One FBX holding mesh + ``<name>_collision``: the node selects the mesh by name."""
    same = render_model_doc("probe", "models/probe/probe.fbx", collision="metal")
    assert same.count('filename = "models/probe/probe.fbx"') == 2   # render mesh + hull
    assert f'name = "probe{COLLISION_MESH_SUFFIX}"' in same

    other = render_model_doc("probe", "models\\probe\\probe.fbx", collision="wood",
                             collision_mesh="models\\probe\\probe_hull.dmx")
    assert 'filename = "models/probe/probe_hull.dmx"' in other
    assert 'filename = "models/probe/probe.fbx"' in other
    assert 'surface_prop = "wood"' in other
    assert f'name = "probe{COLLISION_MESH_SUFFIX}"' in other


def test_collision_mesh_name_is_the_contract_the_blender_side_writes():
    assert collision_mesh_name("probe") == "probe_collision"
    assert len("probe" + COLLISION_MESH_SUFFIX) <= MAX_MESH_NAME
    with pytest.raises(ModelDocError):
        collision_mesh_name("Probe")


def test_a_collision_name_blender_would_truncate_is_rejected():
    fits = "a" * (MAX_MESH_NAME - len(COLLISION_MESH_SUFFIX))
    assert collision_mesh_name(fits) == fits + COLLISION_MESH_SUFFIX
    assert f'name = "{fits}_collision"' in render_model_doc(fits, "models/probe/probe.fbx",
                                                            collision="metal")
    too_long = "a" * (MAX_MESH_NAME - len(COLLISION_MESH_SUFFIX) + 1)
    with pytest.raises(ModelDocError):
        render_model_doc(too_long, "models/probe/probe.fbx", collision="metal")
    # …while the same long name is still fine for a visual-only model
    assert "Physics" not in render_model_doc(too_long, "models/probe/probe.fbx")


def test_collision_mesh_without_a_surface_prop_is_rejected():
    with pytest.raises(ModelDocError):
        render_model_doc("probe", "models/probe/probe.fbx", collision_mesh="models/probe/probe_hull.dmx")


@pytest.mark.parametrize("bad", ["Metal", "metal bar", "", "../metal", "metal/rock", "met al", "9metal"])
def test_surface_props_must_name_a_surfaceproperties_entry(bad):
    with pytest.raises(ModelDocError):
        render_model_doc("probe", "models/probe/probe.fbx", collision=bad)


@pytest.mark.parametrize("bad", [
    "/abs/models/probe_hull.dmx",      # absolute
    "C:/models/probe_hull.dmx",        # drive letter
    "../escape/probe_hull.dmx",        # escapes the addon
    "models/../../probe_hull.dmx",     # escapes the addon from the inside
    "models/probe/probe_hull.obj",     # suffix the compiler will not import as mesh
    "",                                # empty
])
def test_unusable_collision_mesh_references_are_rejected(bad):
    with pytest.raises(ModelDocError):
        render_model_doc("probe", "models/probe/probe.fbx", collision="metal", collision_mesh=bad)


def test_document_is_balanced_kv3():
    doc = render_model_doc("probe", "models/probe/probe.dmx")
    assert doc.count("{") == doc.count("}")
    assert doc.count("[") == doc.count("]")
    assert doc.startswith(MODEL_DOC_HEADER)
    assert doc.endswith("}\r\n")  # one newline after the closing brace, like the real files
    assert not doc.endswith("\n\n")


def test_translation_and_import_filter_are_optional_blocks():
    doc = render_model_doc("probe", "models/probe/probe.dmx", translation=(1.5, -2, 3),
                           import_filter_exceptions=["draw0"])
    assert '_class = "ModelModifier_Translate"' in doc
    assert "translation = [ 1.5, -2, 3 ]" in doc
    assert "exclude_by_default = true" in doc
    assert '"draw0",' in doc
    assert doc.count("{") == doc.count("}")


def test_windows_separators_are_normalised():
    assert mesh_reference("models\\probe\\probe.dmx") == "models/probe/probe.dmx"


@pytest.mark.parametrize("bad", [
    "/abs/models/probe.dmx",          # absolute
    "C:/models/probe.dmx",            # drive letter
    "../escape/probe.dmx",            # escapes the addon
    "models/probe/probe.obj",         # suffix the compiler will not import as mesh
    "",                               # empty
])
def test_unusable_mesh_references_are_rejected(bad):
    with pytest.raises(ModelDocError):
        mesh_reference(bad)


@pytest.mark.parametrize("bad", ["Probe", "1probe", "pro-be", "", "x" * 80])
def test_model_names_must_be_addon_safe(bad):
    with pytest.raises(ModelDocError):
        render_model_doc(bad, "models/probe/probe.dmx")


def test_writing_refuses_to_clobber(tmp_path):
    target = tmp_path / "models" / "probe" / "probe.vmdl"
    write_model_doc(target, render_model_doc("probe", "models/probe/probe.dmx"))
    with pytest.raises(ModelDocError):
        write_model_doc(target, "different")
    write_model_doc(target, "replaced", overwrite=True)
    assert target.read_text(encoding="utf-8") == "replaced"


# ------------------------------------------------------------------ CLI: the collision flags


def _addon(tmp_path, monkeypatch, name="probe_addon"):
    from s2docs.build.workspace import addon_root
    from s2docs.config import get_settings

    monkeypatch.setenv("S2DOCS_WORKSPACE", str(tmp_path / "ws"))
    get_settings.cache_clear()
    root = addon_root(get_settings().workspace_dir, "cs2", name)
    root.mkdir(parents=True)
    return root


def test_cli_collision_surface_emits_the_physics_node(tmp_path, monkeypatch):
    from s2docs import cli

    root = _addon(tmp_path, monkeypatch)
    code = cli.main(["--json", "asset", "--addon", "probe_addon", "--name", "probe",
                     "--mesh", "models/probe/probe.fbx", "--collision-surface", "rock"])
    assert code == 0
    doc = (root / "models" / "probe" / "probe.vmdl").read_bytes().decode("utf-8")
    # the flag reaches the pinned emitter unchanged: same bytes as the library call
    assert doc == render_model_doc("probe", "models/probe/probe.fbx", collision="rock")
    assert '_class = "PhysicsShapeList"' in doc
    assert 'surface_prop = "rock"' in doc
    assert f'name = "probe{COLLISION_MESH_SUFFIX}"' in doc


def test_cli_collision_mesh_points_the_hull_at_another_file(tmp_path, monkeypatch):
    from s2docs import cli

    root = _addon(tmp_path, monkeypatch)
    code = cli.main(["--json", "asset", "model", "--addon", "probe_addon", "--name", "probe",
                     "--mesh", "models/probe/probe.fbx", "--collision-surface", "metal_barrel",
                     "--collision-mesh", "models/probe/probe_hull.fbx"])
    assert code == 0
    doc = (root / "models" / "probe" / "probe.vmdl").read_bytes().decode("utf-8")
    assert doc == render_model_doc("probe", "models/probe/probe.fbx", collision="metal_barrel",
                                   collision_mesh="models/probe/probe_hull.fbx")
    assert 'filename = "models/probe/probe_hull.fbx"' in doc
    assert 'filename = "models/probe/probe.fbx"' in doc      # render mesh still the render mesh
    assert 'surface_prop = "metal_barrel"' in doc


def test_cli_without_collision_flags_keeps_todays_document(tmp_path, monkeypatch):
    from s2docs import cli

    root = _addon(tmp_path, monkeypatch)
    assert cli.main(["--json", "asset", "--addon", "probe_addon", "--name", "probe",
                     "--mesh", "models/probe/probe.fbx"]) == 0
    doc = (root / "models" / "probe" / "probe.vmdl").read_bytes().decode("utf-8")
    assert doc == render_model_doc("probe", "models/probe/probe.fbx")
    assert "Physics" not in doc


def test_cli_collision_mesh_without_a_surface_is_refused(tmp_path, monkeypatch, capsys):
    from s2docs import cli

    root = _addon(tmp_path, monkeypatch)
    code = cli.main(["--json", "asset", "--addon", "probe_addon", "--name", "probe",
                     "--mesh", "models/probe/probe.fbx", "--collision-mesh", "models/probe/hull.fbx"])
    assert code == 2
    assert not (root / "models" / "probe" / "probe.vmdl").exists()


@pytest.mark.parametrize("bad", ["Rock", "rock/../metal", "materials/rock", "rock prop"])
def test_cli_rejects_a_surface_that_is_not_a_surfaceproperties_name(tmp_path, monkeypatch, bad):
    from s2docs import cli

    root = _addon(tmp_path, monkeypatch)
    code = cli.main(["--json", "asset", "--addon", "probe_addon", "--name", "probe",
                     "--mesh", "models/probe/probe.fbx", "--collision-surface", bad])
    assert code == 2
    assert not (root / "models" / "probe" / "probe.vmdl").exists()
