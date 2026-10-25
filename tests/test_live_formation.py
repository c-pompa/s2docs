import json
from pathlib import Path

import pytest

from s2docs.live import spawnables
from s2docs.live.formation import positions

VECTORS = json.loads((Path(__file__).parent / "fixtures/live/formation_vectors.json").read_text())


@pytest.mark.parametrize("v", VECTORS, ids=lambda v: f"{v['formation']}-{v['count']}-yaw{v['yaw']}")
def test_vectors(v):
    got = positions(tuple(v["origin"]), v["yaw"], v["count"], v["formation"], v["spacing"], v["height"])
    assert len(got) == len(v["expect"])
    for g, e in zip(got, v["expect"], strict=True):
        assert all(abs(a - b) < 0.05 for a, b in zip(g, e, strict=True)), (g, e)


def test_stock_has_chicken_with_template_name():
    chicken = spawnables.stock()["chicken"]
    assert chicken.template == "hs_tpl_chicken" and chicken.classname == "chicken"
