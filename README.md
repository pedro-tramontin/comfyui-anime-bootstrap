# ComfyUI Anime Bootstrap

A cloud-native, GPU-ready Docker image for running ComfyUI with anime diffusion models. Works on RunPod, Vast.ai, or any CUDA-capable host.

## Features

- **Base image**: built on a `pytorch/pytorch` tag — see [Base images & tags](#base-images--tags) below.
- **Pre-installed**: ComfyUI (in `/opt/ComfyUI`, volume-mount friendly) + ComfyUI-Manager + ComfyUI_GalleryManager + aria2 (fast resume downloads)
- **Model bootstrap**: `models.json` manifest for idempotent, resumable downloads
- **SSH ready**: Host keys generated at container start (secure, no baked keys)
- **Volume-mount safe**: ComfyUI source lives outside `/workspace`, so attaching a RunPod network volume doesn't shadow the installation
- **Multi-provider**: Tested on RunPod and Vast.ai

## Bundled Gallery

The image ships with **ComfyUI_GalleryManager** baked into `/opt/ComfyUI/custom_nodes/ComfyUI_GalleryManager/`. On first boot, ComfyUI auto-discovers it and registers a `/gallery` route — no scp, no first-boot install, no `__pycache__` traps.

Open the gallery in a browser at:

```
http://<pod-ip>:8188/gallery
```

(Or just `/gallery` — a 302 redirect serves the trailing-slash URL too.)

The gallery features:
- Browsable, searchable, recursive folder view of `/opt/ComfyUI/output`
- PNG metadata extraction (model, LoRA, seed, steps, CFG, sampler, prompts)
- Video support: MP4/WebM/MOV/AVI/MKV playback with ffmpeg-generated thumbnails
- Subfolder CRUD (create, rename, delete)
- JSONL metadata sidecar for fast search across thousands of files

> **Where output lives:** the gallery scans `/opt/ComfyUI/output` by default. If you mount a network volume that shadows `/opt/ComfyUI/output` (rare — most users mount at `/workspace`), the gallery auto-falls back to `/workspace/ComfyUI/output`.

## Base images & tags

The image is built from one `pytorch/pytorch` base per run. Each image carries OCI labels so consumers can `docker inspect` to find the right tag for their host's driver.

| Tag | PyTorch | CUDA | NVIDIA driver floor | Use when |
|---|---|---|---|---|
| `latest` (alias of `cuda13.0-pytorch2.12.0`) | 2.12.0 | 13.0 | 12080 | Most recent — default for new hosts with current NVIDIA drivers |
| `cuda13.0-pytorch2.12.0` | 2.12.0 | 13.0 | 12080 | Pin to this tag explicitly |

> **Older bases:** to publish a variant for an older driver, re-trigger the GitHub Actions workflow manually (Actions → Build and Push → Run workflow) and change the inputs. The image gets tagged `cuda<X>-pytorch<Y>` and (optionally) `:latest`.

**Programmatic selection:**

```bash
# List all available tags
docker manifest inspect ghcr.io/pedro-tramontin/comfyui-anime-bootstrap | jq -r '.manifests[].digest'

# Inspect a tag's driver floor
docker pull ghcr.io/pedro-tramontin/comfyui-anime-bootstrap:cuda13.0-pytorch2.12.0
docker inspect --format '{{ index .Config.Labels "com.pedro-tramontin.comfyui-anime-bootstrap.driver-floor" }}' \
  ghcr.io/pedro-tramontin/comfyui-anime-bootstrap:cuda13.0-pytorch2.12.0
# → 12080
```

## Model manifest

The image ships a **minimal fallback** `models-template.json` baked in (currently just the Animagine XL v4.0 checkpoint + the Sadamoto XL LoRA). It's only used when `/workspace/models.json` doesn't already exist on the volume — i.e. for first-time users with no orchestrator pre-staging.

For multi-model runs, the orchestrator writes a full `models.json` onto the network volume **before** the pod starts, and the baked-in template is ignored. The reference multi-model manifest lives in the `cloud-ai-image-generation` skill:

```
~/.hermes/skills/devops/cloud-ai-image-generation/references/models.json
```

That file is the source of truth for which models to download in a real run. Edit it freely; don't edit the in-image `models-template.json` unless you're changing the fallback example.

## Quick Start

### 1. Drop a `models.json` on the volume

```json
{
  "checkpoints": [
    {
      "name": "animagine-xl-4.0",
      "url": "https://huggingface.co/cagliostrolab/animagine-xl-4.0/resolve/main/animagine-xl-4.0.safetensors",
      "dest": "checkpoints/animagine-xl-4.0.safetensors",
      "size": 6938040794,
      "auth": "huggingface"
    }
  ]
}
```

Or just point the orchestrator at the skill's `models.json` and skip this step entirely.

### 2. Launch (example: RunPod)

```bash
docker run -it --gpus all \
  -e HF_TOKEN=***  -e MODELS_MANIFEST=/workspace/models.json \
  -v /path/to/models:/workspace/models \
  ghcr.io/pedro-tramontin/comfyui-anime-bootstrap:latest
```

### 3. Wait for bootstrap

The container will:
1. Generate fresh SSH hostkeys
2. Check `models.json` and download/resume any missing files
3. `git pull` ComfyUI + Manager if older than 3 hours
4. Start ComfyUI on `0.0.0.0:8188`

## Environment Variables

| Variable | Description |
|----------|-------------|
| `HF_TOKEN` | HuggingFace read token for gated models |
| `CIVITAI_API_KEY` | Civitai API key for auth-required downloads |
| `MODELS_MANIFEST` | Path to `models.json` (default: `/workspace/models.json`) |
| `MODELS_ROOT` | Where models live (default: `/workspace/models`) |
| `WORKFLOWS_DIR` | Symlinked to `$COMFYUI_DIR/user/default/workflows` (default: `/workspace/workflows`). Workflows uploaded here appear in the "Workflow → Load" menu. |
| `OUTPUT_DIR` | Symlinked to `$COMFYUI_DIR/output` (default: `/workspace/output`). Generated images and the GalleryManager metadata land here. |

## Building Locally

```bash
docker build -t comfyui-anime-bootstrap .
```

## Running Integration Tests

The test suite spins up a container with docker-py and verifies boot, SSH, ComfyUI API, and workspace layout.

```bash
# 1. Build the image
docker build -t comfyui-anime-bootstrap:test .

# 2. Install test deps
pip install -r tests/requirements.txt

# 3. Run tests
pytest tests/integration_test.py -v --timeout=300
```

In CI, tests execute against the freshly built `load: true` image **before** pushing to GHCR.

## Building other variants

The workflow defaults build the current "latest" variant (cuda13.0-pytorch2.12.0). To publish a different variant without changing the defaults:

1. Go to **Actions** → **Build and Push** → **Run workflow**.
2. Fill in the inputs:
   - `cuda` — CUDA version, e.g. `12.1`
   - `pytorch` — PyTorch version, e.g. `2.5.1`
   - `driver_floor` — minimum NVIDIA driver, e.g. `12010`
   - `is_latest` — tick to ALSO publish as `:latest` (default: ticked). The variant tag is always applied regardless of this flag.
3. Click **Run workflow**.

The workflow derives the `pytorch/pytorch:<X>-cuda<Y>-cudnn9-runtime` base tag from `cuda` + `pytorch` and verifies it exists on Docker Hub before building, so a typo fails fast (seconds) instead of 30s into layer pull.

**To make a new variant the permanent default** (e.g. a new PyTorch release), edit the `default:` values in BOTH the `workflow_dispatch.inputs` block AND the `Set defaults` step in `.github/workflows/build.yml`, and push.

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0), matching the upstream ComfyUI license.
