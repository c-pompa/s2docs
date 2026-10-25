"""The `s2studio` Hermes Desktop plugin that ships in this repo (hermes-desktop/s2studio/).

Hermes loads a plugin as FILES, not as an importable module: plugin.yaml names the entry points and
dashboard/plugin_api.py is mounted at /api/plugins/<id>/. So every test here loads the files by path,
exactly as Hermes does, and drives the backend against a real s2docs app (contract test) with only
the model call faked — the routes and response shapes are the ones the page will really see.
"""

import importlib.util
import json
from pathlib import Path

import yaml

PLUGIN = Path(__file__).resolve().parents[1] / "hermes-desktop" / "s2studio"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- Task 3.1: manifest + Python half


def test_manifest_and_python_half():
    """The manifest Hermes reads, the hidden web-dashboard tab, and the auxiliary task registration."""
    manifest = yaml.safe_load((PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "s2studio" == PLUGIN.name  # the folder name IS the plugin id
    assert manifest["manifest_version"] == 2
    assert set(manifest["config_schema"]) == {"s2docs_url", "default_host", "chat_auto_run"}
    assert manifest["config_schema"]["default_host"]["default"] == "pomps-pc"

    dash = json.loads((PLUGIN / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert dash["name"] == "s2studio" and dash["api"] == "plugin_api.py"
    assert dash["entry"] == "dist/index.js" and dash["tab"]["hidden"] is True
    assert (PLUGIN / "dashboard" / "dist" / "index.js").is_file()

    calls = {}

    class Ctx:
        def register_auxiliary_task(self, task, **kw):
            calls[task] = kw

    _load("s2studio_pkg", PLUGIN / "__init__.py").register(Ctx())
    assert calls["s2studio_intent"]["display_name"] == "Source 2 Studio intents"
