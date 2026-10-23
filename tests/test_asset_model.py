"""A generated ``.vmdl`` must be the document the compiler accepts, not a plausible one.

The structure below is pinned against a real CS2 document (``modeldoc28`` from a live addon), and the
class/field names are asserted literally: an invented field or a renamed class compiles to nothing.
The end-to-end proof (run ``resourcecompiler`` on the generated file) lives outside the unit suite
because it needs a Windows host with the game installed.
"""

from __future__ import annotations

import pytest

from s2docs.asset.model import (
    MODEL_DOC_HEADER,
    ModelDocError,
    mesh_reference,
    render_model_doc,
    write_model_doc,
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
