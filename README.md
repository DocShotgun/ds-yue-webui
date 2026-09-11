# ds-yue-webui

A web UI for the [YuE2](https://huggingface.co/m-a-p/YuE2-3B) music model: song generation,
cover generation, and editing.

## Features

| Tab | What it does |
|---|---|
| **Generate** | Full YuE2 songs from style + lyrics. Conditioning: `cot=full` (melody + chord plan, editable), `cot=melody`, `cot=off`. Solid model plans the score; you can also paste/edit an ABC score or reuse a saved plan. Per-model sampling overrides (temperature/top-p/top-k/rep penalty/window, token caps) and seed/CFG scale. |
| **Cover** | 3-step wizard. (1) Upload a reference recording → SheetSage2 transcribes it (task: full / melody-full / melody-vocal, optional max-seconds crop). (2) Review the melody text + abcjs preview + transcriber warnings. (3) Strip chords (recommended melodic adaptation, `cot=melody`) or keep chords (same harmony, `cot=full`), set style + lyrics, generate. |
| **Edit** | Pick a completed song → edit the ABC score in the browser → validate → compare with the original (invariant check, from the official skill's `compare`) → regenerate with the edited score. Editing produces a complete new recording; the source singer's timbre is not preserved. |
| **Library** | Songs, transcripts, and plans. Play FLAC/MP3, view request/config/result JSON and artifact manifests, re-decode cached latents with the legacy VAE, delete items. |
| **Diagnostics** | yue2 doctor, GPU status, tool versions, disk, queue + resident worker states, smoke-test report, and a residency toggle that persists to config.yaml. |

## Architecture

```
┌──────────────┐   NDJSON over stdin/stdout   ┌──────────────────────┐
│ FastAPI      │ ───────────────────────────► │ resident YuE2 worker │ (model stays warm)
│ + job queue  │   run / release / shutdown   └──────────────────────┘
│ (SQLite)     │ ───────────────────────────► ┌──────────────────────┐
│ + web UI     │                              │ resident SheetSage2  │ (worker warm,
└──────────────┘                              │       worker         │  model unloadable)
                                              └──────────────────────┘
```

- **No other repository checkouts are required.** `abc_tools.py` is vendored inside
  this repo (`vendor/yue2/abc_tools.py` — see `vendor/NOTICE.md`), the YuE2 runtime
  installs from GitHub at install time, and SheetSage2 code+weights resolve from the
  HF Hub. Local checkouts are strictly opt-in: `--yue-dir` installs the runtime from
  a local checkout and writes `yue2.dir` into config.yaml (always used at runtime);
  `--sheetsage-dir` does the same for the mel-frontend code source (`sheetsage2.dir`).
  The vendored `abc_tools` copy is refreshed whenever `--yue-dir` is passed
  (re-vendor it manually if you update the checkout without reinstalling).

- **Job queue** (SQLite): `generate`, `plan`, `transcribe`, `decode` — one GPU job at
  a time across both worker families. Jobs survive server restarts (pending/running
  are marked failed at startup), progress via SSE (`GET /api/jobs/{id}/events`).
  Pending jobs expose their 1-based queue position (`job.queue.position/total` —
  the UI shows "queued · position 2 of 3").
- **Resident workers** speak a small NDJSON protocol (`worker/protocol.py`). The model
  loads once at boot and stays alive across jobs. `residency` controls when the model
  is unloaded:
  - `on-demand` (default): the GPU is released *before* the other model family needs it,
    and after the idle timeout (`release_idle_minutes`, default 10). Reload happens on
    demand at the next job. On-demand also offloads a model component whenever it is
    no longer needed mid-run: the YuE2 AR model moves to CPU during NAR synthesis
    (covers have the longest prefix, and a resident AR alongside the NAR OOMs 24 GB).
  - `always`: models stay resident after every job and mid-run (both families fit in
    24 GB, but tight).
  - Toggling between "always"/"on-demand" at runtime is immediate; changing model/vae/device
    restarts that worker on the next job.
- **ABC validation/compare/strip-chords** run in-process in the server against the
  official [yue2-music](skills) skill's `abc_tools` module — no model load needed.
- **Cancel** semantics: pending → cancelled immediately; running → SIGTERM to the worker
  (graceful for YuE2: the pipeline honors the cancel hook), with a 180 s force-stop fallback.
  SheetSage2's transcriber has no cancel hook — a running transcription can only be
  stopped by killing the worker.
- **Config**: `config.yaml` (created by install.sh if missing; edit freely). Resolution:
  defaults < config.yaml < environment (`YUE_WEBUI_*`) < CLI flags.

## Quick start (Linux server)

```bash
git clone <this-repo> ds-yue-webui     # the only repo you need
cd ds-yue-webui

# 1. Install (uv venv, YuE2 runtime from GitHub, webui deps, writes config.yaml)
bash deploy/install.sh

# 2. Predownload model checkpoints (optional but recommended, multi-GB)
bash deploy/download-models.sh

# 3. Start the server (wrap in tmux for persistence)
tmux new -s yue-webui 'bash deploy/run.sh'

# 4. Open the UI
#    http://<server-ip>:8765/   (LAN or Tailscale)
```

The installer is idempotent: re-running preserves your `config.yaml` edits and only
updates specific keys. The first Generate/Cover job downloads any missing checkpoints
automatically if you skipped step 2.

### Smoke test (opt-in)

```bash
bash deploy/install.sh --smoke
```

Runs a small real YuE2 generate (token-capped sampling, ~1–2 min on 24 GB) + a
SheetSage2 transcribe of its output, writes `data/smoke-result.json`, and shows
the result on the Diagnostics page. If the relaxed shared env fails, re-run with
`--strict-pins`.

### Installer flags

| Flag | Meaning |
|---|---|
| `--strict-pins` | Two venvs exactly per the upstream READMEs: `.venv` (server + YuE2 with its pinned deps) and `.venv-sheetsage2` (torch 2.8 / transformers stack). Sets `worker.python_sheetsage2` in config.yaml. |
| `--latest-torch` | Float torch beyond the upstream pin. torchaudio 2.11+ follows un-pinned and works with every future torch release. On Windows the cu130 PyTorch index is used (overridable with `--torch-index`). |
| `--vendored-mel` | Apply the vendored mel-frontend patch (only needed when torchaudio cannot be installed for your torch build). Creates a local model at `data/sheetsage2-model/` (patched code + weights — code from the Hub, or from a local checkout with `--sheetsage-dir`) and points `sheetsage2.model` at it. |
| `--smoke` | Run the smoke test after installation. |
| `--data-dir`, `--venv-dir`, `--port`, `--revision`, `--skip-models` | Path/port overrides. |
| `--yue-dir DIR` | **Opt-in** local YuE checkout: installs the runtime from it, refreshes the vendored `abc_tools` copy, and writes `yue2.dir` into config.yaml. |
| `--sheetsage-dir DIR` | **Opt-in** local SheetSage2 checkout: mel-frontend code source for `--vendored-mel` and the `--strict-pins` requirements; writes `sheetsage2.dir` into config.yaml. |
| `--torch-index URL` | **Windows:** override the PyTorch CUDA index (default `https://download.pytorch.org/whl/cu128`, or cu130 with `--latest-torch`). |
| `--yue-source SRC` | YuE2 runtime source: `fa-fix` (DocShotgun/YuE @ flash-attention-fix) or `upstream`. Default: fa-fix on Windows (install.ps1), upstream elsewhere; a `--yue-rev` pin implies upstream. |

By default (no flags) the installer installs torch at YuE2's exact pin and then
**torchaudio pinned to the same version** — e.g. `torch==2.10.0` gets
`torchaudio==2.10.0`. If that match is unavailable for your platform, the
installer warns and points at `--vendored-mel` as the fallback.

## Quick start (Windows)

```bat
git clone <this-repo> ds-yue-webui     :: the only repo you need
cd ds-yue-webui

:: 1. Install (uv venv, YuE2 runtime, webui deps, writes config.yaml)
deploy\install.bat

:: 2. Predownload model checkpoints (optional but recommended, multi-GB)
deploy\download-models.bat

:: 3. Start the server
deploy\run.bat

:: 4. Open the UI
::    http://<server-ip>:8765/   (LAN or Tailscale)
```

Two Windows caveats are handled automatically:

- **CUDA torch wheels.** PyPI's Windows torch wheels are CPU-only when no index
  URL is given, so the installer preinstalls `torch==2.10.0` +
  `torchaudio==2.10.0` from the PyTorch CUDA index
  (`https://download.pytorch.org/whl/cu128` — the default CUDA for torch
  2.10.0) *before* installing the YuE2 runtime, so its torch dependency
  resolves against the CUDA wheel. Override the index with `--torch-index`, or
  float to the latest torch with `--latest-torch` (cu130 index by default).
- **Flash attention.** The Windows torch wheels are not compiled with flash
  attention, and upstream YuE's internal availability check misfires on
  Windows. The installer therefore installs the YuE2 runtime from the
  `flash-attention-fix` branch of DocShotgun/YuE by default — a temporary
  workaround; revisit upstream occasionally. Override with `--yue-source
  upstream`, pin a different revision with `--yue-rev`, or use a local
  checkout with `--yue-dir`.

Note: `DELETE /api/jobs/{id}` cancels pending jobs instantly; a running job is
killed without graceful cleanup on Windows.

## config.yaml reference

```yaml
host: 0.0.0.0
port: 8765
data_dir: data                 # relative to the project root, or absolute
residency: on-demand           # on-demand: offload whenever unused (incl. mid-run) | always: keep resident
release_idle_minutes: 10
offline: false                 # never reach the HF Hub (models must be cached)
memory_budget_gib: 24.0        # YuE2 memory budget
yue2:
  # dir: ../YuE                # opt-in: local YuE checkout (no local checkout is
  #                            #   used otherwise; install.sh writes this with --yue-dir)
  model: m-a-p/YuE2-3B
  vae: m-a-p/YuE2-Vae          # used for generation and decode=standard
  vae_legacy: m-a-p/YuE2-Vae-legacy
  device: auto                 # auto | cuda | cpu
  backend: torch               # torch | torch-eager | vllm
  quantization: none           # none | fp8
sheetsage2:
  # dir: ../SheetSage2         # opt-in: mel-frontend code source (written by
  #                            #   --sheetsage-dir; used by --vendored-mel)
  model: auto                  # auto: local dir with weights, else m-a-p/SheetSage2
  device: cuda
  dtype: bf16
worker:
  python: auto                 # auto: the venv the server runs in
  python_yue2: auto            # auto: worker.python
  python_sheetsage2: auto      # auto: worker.python (set by --strict-pins)
```

`residency` and `release_idle_minutes` are also editable at runtime on the
Diagnostics page (persisted back to this file).

## API summary

| Endpoint | Purpose |
|---|---|
| `POST /api/jobs` | `{kind, name?, params}` — submit `generate` / `plan` / `transcribe` / `decode`. Server-side validation mirrors the YuE2 runtime (cot modes, chord rule for melody-mode scores, sampling ranges). Returns `{job, warnings}`; warnings are advisory (e.g. an un-parseable ABC dialect is passed to the model as-is). |
| `GET /api/jobs?limit=` / `GET /api/jobs/{id}` | Recent jobs / one job. |
| `DELETE /api/jobs/{id}` | Cancel a pending/running job. |
| `GET /api/jobs/{id}/events` | SSE stream of job updates (progress, step, result). |
| `POST /api/abc/inspect` / `POST /api/abc/compare` / `POST /api/abc/strip` | ABC services (no model load). |
| `GET /api/library` + `/library/{songs\|transcripts\|plans}/...` | Library listing, details, audio (`?format=flac\|mp3`), artifacts, delete. |
| `POST /api/uploads` | Multipart audio upload for the Cover wizard. |
| `GET/POST /api/config` | Config snapshot / update residency + idle minutes. |
| `GET /api/diagnostics` | Full diagnostics report. |

## Troubleshooting

- **Out of memory**: switch residency to `on-demand` on the Diagnostics page; check the
  GPU table. `residency=always` keeps everything resident — covers (longest prefix) can
  OOM 24 GB during NAR synthesis; on-demand offloads the AR mid-run instead.
- **Audio doesn't play in the browser**: the FLAC format is not supported by Safari —
  switch the Library player to MP3 (converted on demand with ffmpeg; requires ffmpeg
  on PATH).
- **SheetSage2 job fails with torchaudio errors**: re-run `install.sh --vendored-mel`
  (vendors the three mel transforms SheetSage2 needs). The "paper" transcription preset
  is not supported with the patch — use "default" (FFmpeg decode, which is the UI default).
- **mp3 delivery 503**: ffmpeg is missing; install it and re-run a check on Diagnostics.
- **A job is stuck "running"**: see `data/logs/worker-<kind>.log` (worker stderr,
  including YuE2's own progress). Cancel from the job panel, or Diagnostics shows
  worker states.
- **First job is slow**: the resident model loads at first use (and downloads the model
  if you skipped `download-models.sh`). Subsequent jobs reuse the warm process.

## Assumptions & scope

- Single 24 GB NVIDIA GPU server (Linux, target ML server); the server binds
  `0.0.0.0` — protect it with your network (LAN/Tailscale), there is no auth.
- No repository checkouts besides this repo are required: the YuE2 runtime
  installs from GitHub (`git+https://github.com/multimodal-art-projection/YuE`,
  pin with `--yue-rev`) and SheetSage2 resolves from the HF Hub.
- Lyrics are always provided by you — no ASR.
- Uploads/songs/transcripts/plans live under `data/` (gitignored).
- A 12 GB budget caps `vae_core_frames` at 512 (mirrors the CLI's budget heuristic).
