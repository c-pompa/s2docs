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

### Known caveat: the FBX is written in centimetres, Source reads inches

Blender's unit is the metre and the exporter writes centimetres (`apply_unit_scale=True`,
`apply_scale_options='FBX_SCALE_NONE'`), so a 0.84 m bbox lands in the file as 84. Blender's own
importer divides by 100 on the way back — that is the uniform `0.01` in `verdict.reimport_scale`, which
is why the round-trip check compares world extents rather than absolute scale. A Source importer reads
those numbers as inches: the 2026-10-23 compile of the un-decimated asset measured 2.1 x 2.5 x 0.75 m
for the bridge's 0.84 x 0.30 x 1.00 m bbox — the same box, axis-permuted, 2.5x bigger.
`normalise.bbox_units` reports the honest inch size (32.98 x 11.67 x 39.45); the bridge deliberately
does **not** rescale geometry, because that changes the in-game size and needs its own
compile-and-render proof.

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
