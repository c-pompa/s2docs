You are decompiling and rebuilding a **Counter-Strike 2 map** with the s2docs server (${build}).

## Task
$task

## Target map
$map

## Hard rules (learned the expensive way)
- **Reserved paths never compile into your vpk.** Assets under Valve's `_vrf/` subtree, and assets under
  **another map's** `maps/<other_map>/` subtree, are content-illegal for a rebuilt map. If your meshes or
  materials reference them, the rebuild will *finish* and still ship a broken map.
- **Your own map's nodraw physics-surface materials are a trap.** A decompile can point meshes at the map's
  own `maps/<map>/_vrf/physics_surfaces/*.vmat` (nodraw physics surfaces). Those are reserved: do not keep
  the reference and do not shadow it — remap the faces to a real, packaged material.
- **"compiled N, failed M" is not a clean compile unless M = 0.** A run with failures still reports a
  summary; read the counts, don't trust the exit code alone.
- **Red/black checkerboard = missing material.** That is what a missing/unpacked material renders as, not a
  lighting or vvis problem. Chasing it as anything else wastes a build cycle.

## Checklist
1. Inventory the decompiled content: `maps/<map>/`, `materials/`, `models/`, and any `_vrf/` directory.
   List every reference under `_vrf/` or under a `maps/` subtree that isn't this map.
2. Fix the references at the source (the `.vmt`/`.vmf`/mesh), then rebuild. Don't just delete files a
   material still points at.
3. Run the rebuild through the server, not a hand-rolled sleep-poll:
   `build_start` → `build_wait(run_id)` (or `build_status`). Read `summary.material_guard` on the result.
4. **Gate on the material guard, not the exit code.** `summary.material_guard` lists the counts and the
   first offending paths for `missing_material`, `failed_compile` and `reserved_path`. A `succeeded` run
   with non-empty warnings is **not** clean — confirm `failed == 0` and no `_vrf/` / foreign `maps/` paths.
   Use `--strict-materials` (CLI) or `strict_materials=True` (MCP `build_wait`) to make it a hard failure.
5. Verify visually, don't assume: load the map and **pixel-scan a frame for saturated red** (the
   checkerboard artifact) before declaring success.
6. Keep a **stock-map control frame** from the same viewpoint and compare — it separates "our material bug"
   from "the map normally renders like that".

## Output
- The list of offending references you found and how each was remapped.
- The build run id, its `summary` counts and the material-guard verdict.
- The pixel-scan result on your rebuilt frame and on the stock-map control frame.
- Anything still referencing a reserved path, and why.
