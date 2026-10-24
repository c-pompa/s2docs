# scripts/asset — Source 2 asset-stage tools

## `glb_to_fbx_uv.py` — GLB (vertex colour) → FBX with UVs, a baked colour texture and a material

Runs inside headless Blender. Usage, outputs, exit codes, measurements and known limits are in the
module docstring. The script stands alone: `bpy` is the only import outside the standard library.

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
  --python glb_to_fbx_uv.py -- --glb <mesh.glb> --out-dir <dir> --name <model_name> --res 2048 --force
```

Name the FBX material by its Source 2 path: `--material-path` (default
`materials/hammered/<name>/<name>.vmat`, ≤63 chars). The script re-imports the FBX and exits 2 unless
the material name came back verbatim; Blender 4.5.15 keeps the `/` (verified 2026-10-23). Then
`s2docs asset material --addon <addon> --name <name> --color <name>.fbm/<name>_color.png` writes the
`.vmat` at that path.

Run long bakes as a background job that writes a `.done` marker, and poll that marker. Never
block a session on the bake.

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
