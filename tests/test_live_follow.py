"""The generated hermes_sandbox.js, run in node against a stub Instance: followers walk the player's path.

Live on pomps-pc 2026-10-25 the follow line was placed from the VIEW yaw, so it turned with the camera and
the user saw nothing behind them in first person (a third-person camera showed the line). This drives the
real generated script: walk +x, then turn the camera 180 degrees without moving.
"""

import json
import shutil
import subprocess

import pytest

from s2docs.live import sandbox_map, spawnables

STUB = """
const cmds = {}, msgs = []; let think = null; let t = 0;
const pos = { x: 0, y: 0, z: 0 }; const ang = { pitch: 0, yaw: 0, roll: 0 };
const pawn = { IsValid: () => true, GetAbsOrigin: () => ({ ...pos }), GetEyeAngles: () => ({ ...ang }) };
const ctl = { IsBot: () => false, GetPlayerPawn: () => pawn, GetPlayerSlot: () => 0 };
const made = [];
function ent() {
  const e = { p: null, IsValid: () => true, Teleport: (o) => { e.p = o.position; } };
  made.push(e); return e;
}
const tpl = { ForceSpawn: () => [ent()] };
const Instance = {
  Msg: (s) => msgs.push(String(s)), ServerCommand: () => {}, GetGameTime: () => t,
  RegisterCheatCommand: (n, f) => { cmds[n] = f; }, SetThink: (f) => { think = f; }, SetNextThink: () => {},
  OnActivate: () => {}, OnPlayerChat: () => {}, GetAllPlayerControllers: () => [ctl],
  FindEntityByName: (n) => (n === "hs_tpl_chicken" ? tpl : undefined), FindEntitiesByName: () => [],
  Precache: () => null,
};
"""

DRIVER = """
cmds.hs_spawn("chicken 4 line 48 1");
for (let i = 0; i < 60; i++) { pos.x += 10; t += 0.1; think(); }     // walk 600 u along +x
const walked = made.map(e => [e.p.x, e.p.y]);
for (let i = 0; i < 18; i++) { ang.yaw += 10; t += 0.1; think(); }   // turn the camera 180, standing still
const turned = made.map(e => [e.p.x, e.p.y]);
console.log(JSON.stringify({ walked, turned, player: [pos.x, pos.y], msgs }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_followers_walk_the_path_and_ignore_camera_turns(tmp_path):
    js = sandbox_map.render_script(spawnables.stock())
    src = STUB + js.replace('import { Instance } from "cs_script/point_script";', "") + DRIVER
    (tmp_path / "run.mjs").write_text(src)
    out = subprocess.run(["node", str(tmp_path / "run.mjs")], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert any(m.startswith("[hs] spawned 4 chicken") for m in got["msgs"]), got["msgs"]
    px = got["player"][0]
    # on the walked path (y = 0), behind the player, 48 u apart, in order
    for i, (x, y) in enumerate(got["walked"]):
        assert abs(y) < 0.5 and abs((px - x) - 48 * (i + 1)) < 9, (i, x, y, px)
    # a camera turn moves nobody: the line is now IN FRONT of the camera, i.e. visible
    for (x0, y0), (x1, y1) in zip(got["walked"], got["turned"], strict=True):
        assert abs(x0 - x1) < 0.01 and abs(y0 - y1) < 0.01
