"""A generated ``.vmat`` must name a shader and keys that shipped CS2 prop materials actually use.

The shape below is pinned against three shipped prop materials decompiled from the game's own
``pak01_dir.vpk`` (see ``s2docs.asset.material``): ``csgo_vertexlitgeneric.vfx`` with
``TextureColor`` plus the four texture keys every one of them sets. The end-to-end proof
(resourcecompiler turns the .vmdl + this .vmat + the PNG into .vmdl_c/.vmat_c/.vtex_c) needs the
Windows host and lives in docs/HAMMERED.md, not in the unit suite.
"""

from __future__ import annotations

import json

import pytest

from s2docs.asset.material import (
    DEFAULT_SHADER,
    MAX_ID_NAME,
    VMAT_SHADERS,
    MaterialError,
    color_texture_path,
    material_path,
    render_vmat,
    write_vmat,
)
from s2docs.asset.model import ModelDocError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # magic + filler: enough for the signature check


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "src" / "baked_color.png"
    p.parent.mkdir(parents=True)
    p.write_bytes(PNG)
    return p


def test_material_path_is_the_hammered_layout():
    assert material_path("tsr_eefa8331") == "materials/hammered/tsr_eefa8331/tsr_eefa8331.vmat"
    assert color_texture_path("tsr_eefa8331") == "materials/hammered/tsr_eefa8331/tsr_eefa8331_color.png"


def test_material_path_fits_the_blender_id_name_limit():
    # 25 + 2n characters: n = 19 is the longest name whose path survives Blender's 63-char ID names
    assert len(material_path("a" * 19)) == MAX_ID_NAME == 63
    with pytest.raises(MaterialError, match="63"):
        material_path("a" * 20)


@pytest.mark.parametrize("bad", ["Probe", "1probe", "pro-be", "", "x", "../up", "a/b"])
def test_material_names_must_be_addon_safe(bad):
    with pytest.raises(MaterialError):
        material_path(bad)


def test_material_error_is_a_model_doc_error():
    # one except clause in the CLI/MCP layer covers both generators
    assert issubclass(MaterialError, ModelDocError)


def test_shader_allow_list_is_the_evidenced_prop_shader():
    assert DEFAULT_SHADER == "csgo_vertexlitgeneric.vfx"
    assert DEFAULT_SHADER in VMAT_SHADERS
    # the weapon-skin shader is never a prop shader
    assert "csgo_customweapon.vfx" not in VMAT_SHADERS


def test_document_carries_only_the_evidenced_keys():
    doc = render_vmat("probe")
    assert doc.startswith('"Layer0"\n{\n')
    assert doc.endswith("}\n")
    assert '\t"shader"\t"csgo_vertexlitgeneric.vfx"\n' in doc
    assert '\t"TextureColor"\t"materials/hammered/probe/probe_color.png"\n' in doc
    keys = [line.split("\t")[1].strip('"') for line in doc.splitlines() if line.startswith("\t\"")]
    assert keys == [
        "shader",
        "TextureMetalness",
        "TextureAmbientOcclusion",
        "TextureColor",
        "TextureNormal",
        "TextureRoughness",
    ]
    # decompiler-only output is never authored
    assert "Compiled Textures" not in doc
    assert "g_t" not in doc
    assert "\r" not in doc
    assert doc.count("{") == doc.count("}") == 1


def test_unknown_shader_is_refused():
    with pytest.raises(MaterialError, match="allow-list"):
        render_vmat("probe", shader="csgo_customweapon.vfx")
    with pytest.raises(MaterialError):
        render_vmat("probe", shader="csgo_complex.vfx")


def test_write_vmat_places_material_and_texture(tmp_path, png):
    addon = tmp_path / "addon"
    addon.mkdir()
    out = write_vmat(addon, "probe", png)
    assert out == addon / "materials" / "hammered" / "probe" / "probe.vmat"
    assert out.read_text(encoding="utf-8") == render_vmat("probe")
    copied = addon / "materials" / "hammered" / "probe" / "probe_color.png"
    assert copied.read_bytes() == PNG


def test_write_vmat_refuses_to_clobber(tmp_path, png):
    addon = tmp_path / "addon"
    addon.mkdir()
    write_vmat(addon, "probe", png)
    with pytest.raises(MaterialError, match="force"):
        write_vmat(addon, "probe", png)
    png.write_bytes(PNG + b"new")
    write_vmat(addon, "probe", png, force=True)
    assert (addon / "materials/hammered/probe/probe_color.png").read_bytes() == PNG + b"new"


def test_write_vmat_refuses_clobbering_the_texture_alone(tmp_path, png):
    addon = tmp_path / "addon"
    tex = addon / "materials" / "hammered" / "probe" / "probe_color.png"
    tex.parent.mkdir(parents=True)
    tex.write_bytes(b"hand-made")
    with pytest.raises(MaterialError):
        write_vmat(addon, "probe", png)
    assert tex.read_bytes() == b"hand-made"
    assert not (tex.parent / "probe.vmat").exists()


def test_write_vmat_needs_a_real_png(tmp_path):
    addon = tmp_path / "addon"
    addon.mkdir()
    with pytest.raises(MaterialError, match="not found"):
        write_vmat(addon, "probe", tmp_path / "missing.png")
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"GIF89a....")
    with pytest.raises(MaterialError, match="PNG"):
        write_vmat(addon, "probe", fake)


def test_write_vmat_needs_an_existing_addon_root(tmp_path, png):
    with pytest.raises(MaterialError, match="addon"):
        write_vmat(tmp_path / "nope", "probe", png)


def test_write_vmat_refuses_a_root_that_escapes_through_a_symlink(tmp_path, png):
    addon = tmp_path / "addon"
    (addon / "materials").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (addon / "materials" / "hammered").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MaterialError, match="escapes"):
        write_vmat(addon, "probe", png)
    assert list(outside.iterdir()) == []


# ------------------------------------------------------------------ CLI


def _addon(tmp_path, monkeypatch, name="probe_addon"):
    from s2docs.build.workspace import addon_root
    from s2docs.config import get_settings

    monkeypatch.setenv("S2DOCS_WORKSPACE", str(tmp_path / "ws"))
    get_settings.cache_clear()
    root = addon_root(get_settings().workspace_dir, "cs2", name)
    root.mkdir(parents=True)
    return root


def test_cli_asset_material_writes_vmat(tmp_path, monkeypatch, png, capsys):
    from s2docs import cli

    root = _addon(tmp_path, monkeypatch)
    code = cli.main(["--json", "asset", "material", "--addon", "probe_addon", "--name", "probe",
                     "--color", str(png)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["material"] == "materials/hammered/probe/probe.vmat"
    assert payload["shader"] == DEFAULT_SHADER
    assert (root / "materials/hammered/probe/probe.vmat").is_file()
    assert (root / "materials/hammered/probe/probe_color.png").read_bytes() == PNG
    # second run without --force is a bad request (exit 2), with --force it replaces
    assert cli.main(["asset", "material", "--addon", "probe_addon", "--name", "probe", "--color", str(png)]) == 2
    assert cli.main(["asset", "material", "--addon", "probe_addon", "--name", "probe", "--color", str(png),
                     "--force"]) == 0


def test_cli_asset_material_rejects_bad_shader_and_missing_addon(tmp_path, monkeypatch, png):
    from s2docs import cli

    _addon(tmp_path, monkeypatch)
    assert cli.main(["asset", "material", "--addon", "probe_addon", "--name", "probe", "--color", str(png),
                     "--shader", "csgo_customweapon.vfx"]) == 2
    assert cli.main(["asset", "material", "--addon", "other_addon", "--name", "probe",
                     "--color", str(png)]) == 3


def test_cli_asset_model_form_still_works(tmp_path, monkeypatch):
    from s2docs import cli

    root = _addon(tmp_path, monkeypatch)
    assert cli.main(["asset", "--addon", "probe_addon", "--name", "probe", "--mesh", "models/probe/probe.fbx"]) == 0
    assert (root / "models/probe/probe.vmdl").is_file()
    assert cli.main(["asset", "model", "--addon", "probe_addon", "--name", "probe2",
                     "--mesh", "models/probe2/probe2.fbx"]) == 0
    # the model form still needs --mesh
    assert cli.main(["asset", "--addon", "probe_addon", "--name", "probe3"]) == 2


# ------------------------------------------------ baked normal/AO (the Blender bridge's output)
# glb_to_fbx_uv.py bakes <name>_normal.png and <name>_ao.png beside <name>_color.png; material.py
# consumes them when they are there and keeps the engine defaults when they are not.
NORMAL_PNG = b"\x89PNG\r\n\x1a\n" + b"tangent-space normal" * 2
AO_PNG = b"\x89PNG\r\n\x1a\n" + b"occlusion" * 2


@pytest.fixture
def baked(tmp_path):
    """A bridge output dir: colour + normal + AO, exactly as the .fbm folder looks after a bake."""
    d = tmp_path / "baked" / "probe.fbm"
    d.mkdir(parents=True)
    (d / "probe_color.png").write_bytes(PNG)
    (d / "probe_normal.png").write_bytes(NORMAL_PNG)
    (d / "probe_ao.png").write_bytes(AO_PNG)
    return d


def test_find_baked_maps_reports_only_the_maps_that_exist(baked):
    from s2docs.asset.material import find_baked_maps

    assert find_baked_maps(baked / "probe_color.png") == {
        "normal": baked / "probe_normal.png",
        "ao": baked / "probe_ao.png",
    }
    (baked / "probe_ao.png").unlink()
    assert set(find_baked_maps(baked / "probe_color.png")) == {"normal"}


def test_baked_maps_beside_the_colour_texture_are_wired_and_copied(tmp_path, baked):
    addon = tmp_path / "addon"
    addon.mkdir()
    out = write_vmat(addon, "probe", baked / "probe_color.png")
    doc = out.read_text(encoding="utf-8")
    assert '\t"TextureNormal"\t"materials/hammered/probe/probe_normal.png"\n' in doc
    assert '\t"TextureAmbientOcclusion"\t"materials/hammered/probe/probe_ao.png"\n' in doc
    # the keys point at files that really landed beside the .vmat, byte for byte
    assert (out.parent / "probe_normal.png").read_bytes() == NORMAL_PNG
    assert (out.parent / "probe_ao.png").read_bytes() == AO_PNG
    # the engine defaults are gone for the two wired keys and kept for the one that was not baked
    assert "materials/default/default_normal.tga" not in doc
    assert "materials/default/default_ao.tga" not in doc
    assert '\t"TextureRoughness"\t"materials/default/default_rough.tga"\n' in doc
    # LF endings and one KV block, as before
    assert "\r" not in doc
    assert doc.count("{") == doc.count("}") == 1


def test_colour_only_is_still_the_document_it_always_was(tmp_path, png):
    addon = tmp_path / "addon"
    addon.mkdir()
    out = write_vmat(addon, "probe", png)
    assert out.read_text(encoding="utf-8") == render_vmat("probe")
    doc = out.read_text(encoding="utf-8")
    assert '\t"TextureNormal"\t"materials/default/default_normal.tga"\n' in doc
    assert '\t"TextureAmbientOcclusion"\t"materials/default/default_ao.tga"\n' in doc
    assert not (out.parent / "probe_normal.png").exists()
    assert not (out.parent / "probe_ao.png").exists()


def test_one_map_alone_leaves_the_other_key_on_its_default(tmp_path, baked):
    (baked / "probe_ao.png").unlink()
    addon = tmp_path / "addon"
    addon.mkdir()
    out = write_vmat(addon, "probe", baked / "probe_color.png")
    doc = out.read_text(encoding="utf-8")
    assert '\t"TextureNormal"\t"materials/hammered/probe/probe_normal.png"\n' in doc
    assert '\t"TextureAmbientOcclusion"\t"materials/default/default_ao.tga"\n' in doc
    assert not (out.parent / "probe_ao.png").exists()


def test_a_map_belonging_to_another_base_name_is_not_picked_up(tmp_path, png):
    # probe_color.png with someone else's normal map beside it: wiring that would be a guess
    (png.parent / "other_normal.png").write_bytes(NORMAL_PNG)
    addon = tmp_path / "addon"
    addon.mkdir()
    out = write_vmat(addon, "probe", png)
    assert out.read_text(encoding="utf-8") == render_vmat("probe")


def test_a_baked_map_that_is_not_a_png_is_refused(tmp_path, baked):
    (baked / "probe_normal.png").write_bytes(b"GIF89a....")
    addon = tmp_path / "addon"
    addon.mkdir()
    with pytest.raises(MaterialError, match="PNG"):
        write_vmat(addon, "probe", baked / "probe_color.png")
    assert not (addon / "materials" / "hammered" / "probe" / "probe.vmat").exists()


def test_a_baked_map_is_not_clobbered_without_force(tmp_path, baked):
    addon = tmp_path / "addon"
    dest = addon / "materials" / "hammered" / "probe" / "probe_normal.png"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"hand-made")
    with pytest.raises(MaterialError, match="force"):
        write_vmat(addon, "probe", baked / "probe_color.png")
    assert dest.read_bytes() == b"hand-made"
    write_vmat(addon, "probe", baked / "probe_color.png", force=True)
    assert dest.read_bytes() == NORMAL_PNG


def test_render_vmat_refuses_a_baked_value_that_is_not_an_addon_png():
    with pytest.raises(MaterialError):
        render_vmat("probe", baked={"normal": "../evil.png"})
    with pytest.raises(MaterialError):
        render_vmat("probe", baked={"ao": "materials/hammered/probe/probe_ao.tga"})
    with pytest.raises(MaterialError):
        render_vmat("probe", baked={"rough": "materials/hammered/probe/probe_rough.png"})


def test_cli_asset_material_wires_the_baked_maps(tmp_path, monkeypatch, baked, capsys):
    from s2docs import cli

    root = _addon(tmp_path, monkeypatch)
    code = cli.main(["--json", "asset", "material", "--addon", "probe_addon", "--name", "probe",
                     "--color", str(baked / "probe_color.png")])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["textures"] == {
        "TextureColor": "materials/hammered/probe/probe_color.png",
        "TextureNormal": "materials/hammered/probe/probe_normal.png",
        "TextureAmbientOcclusion": "materials/hammered/probe/probe_ao.png",
    }
    doc = (root / "materials" / "hammered" / "probe" / "probe.vmat").read_text(encoding="utf-8")
    assert "materials/default/default_normal.tga" not in doc
    assert (root / "materials/hammered/probe/probe_normal.png").read_bytes() == NORMAL_PNG
