# scripts/asset — Source 2 asset-stage tools

## `glb_to_fbx_uv.py` — GLB (vertex colour) → game-budget FBX with UVs, colour + normal + AO maps

Runs inside headless Blender. Usage, outputs, exit codes, measurements and known limits are in the
module docstring. The script stands alone: `bpy` (and the `numpy` Blender ships) is the only import
outside the standard library.

### What the game-budget pass adds

| stage | flag | report field | what it does |
|---|---|---|---|
| triangle budget | `--target-tris N` (default 5000, `0` = off) | `decimate.tris_in/tris_out/ratio` | Decimate COLLAPSE on a copy of the body, retried until at or under N; the original stays as the bake source |
| colour | — | `bake.*` | the high-poly's COLOR_0 sampled onto the render mesh, then EMIT-baked to `<name>_color.png` |
| normal | `--cage-pct` | `normal_bake.*` | Cycles NORMAL, tangent space, selected-to-active from the high-poly → `<name>_normal.png` |
| occlusion | `--ao-samples`, `--ao-distance-pct` | `ao_bake.*` | Cycles AO occluded by the high-poly → `<name>_ao.png` |
| normalise | `--unit-scale` | `normalise.*` | transforms applied (scale 1, Z-up), origin at the body bbox's base centre, bbox reported in Source units (1 = 1 in) |
| collision | — | `collision.*` | a mesh named `<name>_collision` in the input is exported as its own object, never decimated or unwrapped; nothing is ever generated for it |

The `normal_bake`/`ao_bake` blocks carry a `control`: the same map baked without the high-poly (normal)
or with the high-poly out of ray range (AO). A control that matches the reference means the map was not
sampled from the high-poly, and the run is not green. `--no-verify-source` skips the pair.

A collision proxy is hidden from the render during the bakes (an enclosing proxy would otherwise
blacken the AO map: measured AO mean 0.07 with it visible against 0.27 without on the seam fixture);
the FBX exporter still writes it.

Exit codes: 0 = every gate green (colour + normal + AO baked, budget kept, extent and material name
preserved); 3 = something shipped but a map or gate failed; 2 = bad input or the material name did not
survive. The `verdict` block names which.

### Pinned Blender (the build this script was verified on)

| field | value |
|---|---|
| version | Blender **4.5.15** (4.5 LTS line), Windows x64 portable |
| source | `https://download.blender.org/release/Blender4.5/blender-4.5.15-windows-x64.zip` |
| zip sha256 | `52DD02FD74877FA08E74B67D7854CF32468A232C02AAA19B6844C91285574F8D` (398,649,826 bytes) |
| install on the build host | `D:\Tools\blender-4.5.15-windows-x64\` (unzipped, no installer, no registry) |

A different Blender build is a re-verification event, not a drop-in. Re-run the proof job and
compare its `<name>_bridge.json` report before you trust it.

### Invocation (build host)

```bat
"D:\Tools\blender-4.5.15-windows-x64\blender.exe" -b --factory-startup --python-exit-code 1 ^
  --python glb_to_fbx_uv.py -- --glb <mesh.glb> --out-dir <dir> --name <model_name> ^
  --target-tris 5000 --res 2048 --force
```

The three maps land in `<dir>/<name>.fbm/` beside each other; the `.vmat` keys they belong on are in
`<name>_bridge.json` (`vmat.layer0`, `vmat.textures`): `TextureColor`, `TextureNormal`,
`TextureAmbientOcclusion` — the same names and `<name>_<kind>.png` files `s2docs.asset.material` uses,
so the material side can point at these instead of `materials/default/default_normal.tga` /
`default_ao.tga`.

Name the FBX material by its Source 2 path: `--material-path` (default
`materials/hammered/<name>/<name>.vmat`, ≤63 chars). The script re-imports the FBX and exits 2 unless
the material name came back verbatim; Blender 4.5.15 keeps the `/` (verified 2026-10-23). Then
`s2docs asset material --addon <addon> --name <name> --color <name>.fbm/<name>_color.png` writes the
`.vmat` at that path.

Run long bakes as a background job that writes a `.done` marker, and poll that marker. Never
block a session on the bake.

### Units: the FBX numbers are Source inches

Blender's unit is the metre, Source reads FBX numbers as inches, and the exporter's old setting
(`apply_scale_options='FBX_SCALE_NONE'`) wrote **centimetres** with `UnitScaleFactor 1.0` — a 0.84 m
bbox landed in the file as 84, and a compiled prop came out 2.53x too big (measured on the compiled
artifact, not by eye). The export now sets `global_scale=--unit-scale`,
`apply_scale_options='FBX_SCALE_UNITS'` and `scene.unit_settings.scale_length = 1/--unit-scale`, so a
1 x 1 x 1 Blender-unit cube lands as 39.3701^3 with `UnitScaleFactor 2.54` (one file unit = 2.54 cm =
one inch). The geometry's applied scale stays 1 and the axis conversion stays baked into the vertices,
so no node in the file carries a scale; Blender's own importer then returns a uniform `0.0254` in
`verdict.reimport_scale` (inch -> metre), which is why the round-trip check compares world extents
rather than absolute scale.

Proof on the real pipeline (job `eefa8331d9cf4056`, desktop-pc, 2026-10-24): the bridge reported
`normalise.bbox_units` 32.98 x 11.67 x 39.45 in and read `exported_units` 32.8609 x 11.6758 x 39.3807
in back out of the written FBX (0.36% under `bbox_units` — the decimated mesh against the high-poly
body — and 0.02% against the mesh that was exported). The compiled `.vmdl_c` of that FBX (build
`b-d1a37806`) reports `m_vMinBounds` `[-5.7987, -16.4322, -0.0053]` and `m_vMaxBounds`
`[5.8770, 16.4286, 39.3755]`, an extent of 11.6758 x 32.8609 x 39.3807 in. Source's FBX importer maps
the file's (X, Y, Z) onto (Source -Z, X, Y), so the horizontal axes arrive swapped relative to
`bbox_units` — a convention of the importer, not of the export — while the three sizes and the z-up
base (min z ~ 0) are the bridge's own. The pre-fix artifact compiled from the centimetre FBX measured
29.64 x 83.77 x 100.20: the same box, 2.54x bigger. Reports:
`docs/evidence/hammered/tsr_eefa8331_units_bridge.json` and
`docs/evidence/hammered/b-d1a37806_compile.log`.

## Pinned Source2Viewer-CLI (reference decompiles only)

Used to read shipped CS2 materials/models for parameter names (e.g. the prop shader and its texture
keys). Its output is reference material: keep it outside the game install, every addon and this repo.

| field | value |
|---|---|
| version | **20.1** (`20.1.7467+9e2e6c7f67e3c4e5a5f443a60c3a5d841ba8f304`), Windows x64 CLI |
| source | `https://github.com/ValveResourceFormat/ValveResourceFormat/releases/download/20.1/cli-windows-x64.zip` (MIT) |
| zip sha256 | `67AAF8A67A85CA60FD09FF343E62103A4CDF3A2D177FC656231D7D734C5B3AA3` (52,773,073 bytes) |
| install on the build host | `D:\Tools\s2v\cli-20.1\Source2Viewer-CLI.exe`; reference output in `D:\Tools\s2v\ref\` |

Flags at 20.1 (from `--help`): `-i <vpk|file>`, `-l` list, `-d` decompile, `-o <dir>`,
`-e <ext[,ext]>` extension filter, `-f <path[,path]>` path filter, `-b DATA` print a block,
`--gltf_export_format gltf|glb`.

```bat
Source2Viewer-CLI.exe -i "<cs2>\game\csgo\pak01_dir.vpk" -l -e vmat_c -f materials/models/props
Source2Viewer-CLI.exe -i "<cs2>\game\csgo\pak01_dir.vpk" -d -e vmat_c -f <path.vmat_c>[,<path>] -o D:\Tools\s2v\ref
```

`-d` on a `.vmat_c` also dumps every texture it references as PNG beside the `.vmat`; read only the
`.vmat` text. Its `"Compiled Textures"` block is compiler output, never something to author.
