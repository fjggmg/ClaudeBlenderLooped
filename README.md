# blendahbot

An autonomous agent that **builds what you describe in a live Blender session and keeps
iterating until the result is genuinely good** — not just until something exists.

You give it a request in plain English. It then loops:

1. **Plan** the creation and the qualities a great result needs.
2. **Build** in Blender via the Blender MCP server (`bpy`, operators, materials,
   lighting, cameras), researching references and downloading assets as needed.
3. **Render** the scene to an image and look at it.
4. **Critique** — an *independent* reviewer agent opens the render and scores it
   against your request.
5. **Revise** to fix what the reviewer flagged… and repeat, until the reviewer is
   satisfied (or limits are reached).

The "brain" is Claude, driven through the **Claude Agent SDK**, reusing your existing
Claude Code login (no API key needed). The bot has full shell, web search/fetch, and
file tools, so it can do *whatever* maximises the request — fetch reference images,
download free HDRIs/textures/models, `pip install` helpers, write and run scripts.

---

## Prerequisites

1. **Claude Code** installed (the bot spawns the `claude` CLI and auto-discovers it,
   including the Claude Desktop managed copy).
2. **A one-time subscription login.** The Claude *Desktop app* keeps its auth to
   itself — an external process can't borrow it. And `claude setup-token` alone isn't
   enough: it prints a long-lived token to stdout (soft-wrapped across lines) but never
   saves it anywhere blendahbot can find. So blendahbot does the capture for you:

   ```powershell
   start.bat auth        # or:  blendahbot --auth
   ```

   A browser opens — click **Approve**. blendahbot reconstructs the ~1-year token, saves
   it to `~/.blendahbot/oauth_token`, and passes it to every run via
   `CLAUDE_CODE_OAUTH_TOKEN`. After this, every run is hands-off. Confirm with:

   ```powershell
   blendahbot --selftest
   ```

   (You can also just run a build — it triggers the same one-time approve automatically.
   Or set `ANTHROPIC_API_KEY` to use the API directly, pay-per-token.)
3. **Blender** running, with the **Blender MCP add-on** enabled and its server started
   (listening on `localhost:9876`). Install per
   <https://www.blender.org/lab/mcp-server/>.
4. The **blender-mcp** stdio server. If you installed the Blender connector in Claude,
   the bot reuses it automatically; otherwise `pip install blender-mcp` or set
   `BLENDER_MCP_SERVER_CMD`.

## Install

```powershell
cd "C:\Users\grayb\python projects\blendahbot"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## Use

### Easiest: `start.bat`

Double-click **`start.bat`** (or run it from a terminal). It shows a menu:

```
[1] Build something
[2] Settings  (API key, budget, model, quality...)
[3] Log in    (Claude subscription)
[Q] Quit
```

**Build** asks what to make and runs it. **Settings** opens an editor for your saved
defaults (below). **Log in** does the one-time subscription approve. While a build runs,
type more instructions any time and press Enter to steer; type **`/stop`** to finish early.

Shortcuts skip the menu: `start.bat settings`, `start.bat auth`, or pass a request and
flags straight through:

```powershell
start.bat "a red sports car on a turntable" --budget 5
```

### Settings (saved defaults)

Run **Settings** from the menu (or `blendahbot --settings`) to set, once, things you'd
otherwise pass as flags every time — saved to `~/.blendahbot/settings.json`:

- **Anthropic API key** — switch to pay-per-token API auth instead of your subscription
  (leave blank to keep using the subscription login).
- **Budget per build (USD)**, **model**, **quality threshold**, **max rounds**,
  **patience**, **reference count**, and toggles for the **critic** and **steering**.

CLI flags still override saved settings for a single run.

### Reference images

To stop the agent building from a vague mental image (the #1 cause of bad output), each
run first downloads real reference photos of your subject (key-free, via Wikimedia
Commons + Openverse) into `runs\<…>\reference\`. The builder is required to **look at
them and match the real proportions, materials and colours**, and the critic **compares
your render against them**. The agent can also pull more itself mid-build:

```powershell
python -m blendahbot.refs "wooden cabin forest" --out reference -n 6
```

Tune with `--refs N` or turn it off with `--no-refs`.

### 3D-asset generation (local, on your GPU)

For organic / detail-dense props the bot can **generate a fully textured mesh from a text
prompt** instead of hand-modelling (its weak spot), via a local Hunyuan3D-2 server on your GPU —
free and offline. With texture enabled (below) the GLB ships a baked 2048² PBR map, so the bot
imports it ready-to-use (no PolyHaven material needed):

```powershell
python -m blendahbot.gen3d "a weathered wooden barrel" --out assets/barrel.glb
```

`gen3d` auto-detects the local server on `:8081`. Image→3D also works (`--image clean.png`) but
needs a *clean single-object* photo.

**Hosted backends** (no GPU needed, cost credits) kick in as fallbacks when their key is set, or
on demand with `--backend`:

- **Hunyuan3D 3.1** — Tencent's v3 model (newer geometry + PBR, text+image), via Replicate. Set
  `REPLICATE_API_TOKEN` and run `python -m blendahbot.gen3d "a barrel" --backend hunyuan3`. The
  model defaults to `tencent/hunyuan-3d-3.1`; override with `BLENDAHBOT_HUNYUAN3D_MODEL`. (No open
  weights exist for v3, so this is API-only — the local Hunyuan3D-2.1 server stays the free default.)
- **Tripo** — set `TRIPO_API_KEY` (`--backend tripo`).

**One-time local setup** (≈16 GB VRAM; tested on an RTX 5080): grab the
[Hunyuan3D-2 WinPortable](https://github.com/YanWenKun/Hunyuan3D-2-WinPortable) build, extract to a
short path like `C:\AI\Hunyuan3D2_WinPortable`, then (the scripts live in `WinScripts-Legacy\en-us`):

1. **Models** — run `2-download-models.bat`; for text→3D also pull the text-to-image model:
   `python_standalone\Scripts\hf.exe download Tencent-Hunyuan/HunyuanDiT-v1.1-Diffusers-Distilled`.
2. **Pin the hub** — `python_standalone\python.exe -m pip install "huggingface-hub>=0.34,<1.0"`
   (the download script otherwise installs a hub ≥1.0 that breaks `transformers` at import).
3. **Enable text→3D** — in `Hunyuan3D-2\api_server.py` uncomment the `self.pipeline_t2i =
   HunyuanDiTPipeline(...)` init, and in `hy3dgen\text2image.py` drop `torch.set_default_device('cpu')`
   and add `self.pipe.enable_model_cpu_offload()` after the pipe loads (runs it on the GPU and
   fixes a cuda-generator/cpu-pipe crash).
4. **Run** — `5-start-api-server.bat` (serves on `:8081`). Keep it running while the bot builds.

**Textured output (heavier one-time setup, worth it).** Geometry-only is the default; to get the
native paint model (baked PBR, the `--enable_tex` server) you must compile three CUDA/C++
extensions. On Windows + Blackwell (RTX 50-series) that needs:

1. **VS Build Tools 2022** with the C++ workload (`Microsoft.VisualStudio.2022.BuildTools`,
   `--add Microsoft.VisualStudio.Workload.VCTools`) — provides `cl.exe`.
2. **CUDA Toolkit 12.9** to match the portable's `torch …+cu129` (the major version must match).
   Install the **toolkit components only, NOT the bundled driver** (it ships 576.57; a 50-series
   card needs its newer driver). The math libraries are required too —
   `cublas/cublas_dev/cusparse/cusparse_dev/cusolver/cusolver_dev/curand/curand_dev/cufft/cufft_dev`
   — or `custom_rasterizer` fails with `Cannot open include file: 'cusparse.h'`.
3. **Compile** in a VS+CUDA env (`vcvarsall x64`, `CUDA_PATH`→v12.9 on `PATH` first,
   `TORCH_CUDA_ARCH_LIST=12.0`, `DISTUTILS_USE_SDK=1`): `pip install` the `diso`,
   `custom_rasterizer`, and `differentiable_renderer` source dirs (the portable's
   `1-compile-install-texture-gen.bat` does this once the env is set).
4. **Two source patches** the portable ships broken:
   - `api_server.py` never enabled offload, so the delight + multiview pipelines ran on **CPU**
     (50 fp16 SD steps ≈ a 10-minute stall). Add
     `self.pipeline_tex.enable_model_cpu_offload(device=device)` right after the
     `Hunyuan3DPaintPipeline.from_pretrained(...)` line.
   - Don't launch via `6-start-api-server-with-texture.bat` — its first-run block reinstalls
     `huggingface-hub` to ≥1.0 and breaks `transformers`. Start the server directly with
     `--enable_tex` (touch `python_standalone\Scripts\.hf-reinstalled` to neuter that block), or
     keep the pinned `huggingface-hub<1.0`.

   With this, `gen3d` (without `--no-texture`) returns a textured GLB in ~20–60 s.

### Live steering (any launch method)

Steering is on by default for every build. Lines you type are injected as authoritative
updates: if the agent is mid-task it's **interrupted immediately** and your note is
applied; if typed between rounds it's folded into the next round. `/stop` finishes after
the current step (the scene is still saved and reported). Disable with `--no-steer`.

### Manual / scripted

Check that everything is wired up (does not spend anything):

```powershell
.\.venv\Scripts\blendahbot.exe --check
```

Then build:

```powershell
.\.venv\Scripts\blendahbot.exe "a cozy low-poly cabin in a pine forest at dusk, warm windows"
```

…or via the module:

```powershell
.\.venv\Scripts\python.exe -m blendahbot "a glossy red sports car on a studio turntable"
```

### Useful flags

| Flag | Meaning | Default |
|------|---------|---------|
| `--max-rounds N` | Hard cap on rounds | unlimited (runs until done) |
| `--patience N` | Stop after N rounds with no score gain (0 = never) | 3 |
| `--max-turns N` | Max agent turns per round | 80 |
| `--budget USD` | Hard spend cap for the whole build | none |
| `--threshold N` | Critic score (0–100) needed to finish | 80 |
| `--model ID` | e.g. `claude-opus-4-8` | CLI default |
| `--no-critic` | Trust the builder's own self-assessment | off |
| `--no-steer` | Disable typing instructions mid-build | off (steering on) |
| `--refs N` | Reference photos fetched up front to ground the build | 6 |
| `--no-refs` | Don't fetch reference images | off |
| `--allow-no-blender` | Start even if Blender is unreachable | off |
| `--out DIR` | Output root | `./runs` |
| `--plain` / `--verbose` | Output style | off |

## What you get

Each run writes a timestamped folder under `runs/`:

```
runs/20260625-153000_a-cozy-low-poly-cabin/
├─ request.txt
├─ transcript.jsonl          # every message from builder + critic
├─ claude_stderr.log
├─ round_01/  render.png, verdict.json
├─ round_02/  ...
└─ final/     scene.blend, final_render.png
└─ report.md                 # what was built, per-round scores, cost
```

## How completion is decided

The builder calls a `declare_done` tool when it believes it's finished — but that is
**not** the final word. The orchestrator then renders the scene (using the builder's
render, or an out-of-band fallback render so there's always an image) and hands it to a
separate, skeptical **critic** agent that opens the image and returns a strict JSON
verdict. The run only finishes when `satisfied` is true and the score clears
`--threshold`. This independent gate is what stops the bot declaring victory too early.

By default there is **no fixed round count** — it keeps revising until the critic is
satisfied. The other ways a run ends: the score **plateaus** (no improvement for
`--patience` rounds — i.e. it's done getting better), the **`--budget`** is reached, you
type **`/stop`**, or a high safety backstop (60 rounds) trips. The run returns the
**best‑scoring** render, not necessarily the last one.

## Configuration via environment

| Variable | Purpose |
|----------|---------|
| `BLENDER_MCP_HOST` / `BLENDER_MCP_PORT` | Blender add-on socket (default `localhost:9876`) |
| `BLENDER_MCP_SERVER_CMD` | Override the blender-mcp launch command |
| `BLENDAHBOT_CLAUDE_CLI` | Override the path to the `claude` CLI |
| `BLENDAHBOT_MODEL` | Default model id |
| `BLENDAHBOT_OUT` | Default output root |

## Troubleshooting

- **"Blender is not reachable"** — open Blender, enable the MCP add-on, start its
  server. Confirm with `blendahbot --check`.
- **"Could not locate the `claude` CLI"** — install Claude Code or set
  `BLENDAHBOT_CLAUDE_CLI` to the full path of `claude.exe`.
- **`authentication failed` / HTTP 401** — the standalone CLI isn't logged in. Run
  `claude setup-token` once, then `blendahbot --selftest`. The Desktop app's login is
  **not** shared with external processes, so this step is required even though Claude
  Desktop works.
- Only one client should drive the Blender add-on at a time; don't run a build while
  another tool is actively controlling the same Blender instance.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```
