# ComfyUI Anime Bootstrap

A cloud-native, GPU-ready Docker image for running ComfyUI with anime diffusion models. Works on RunPod, Vast.ai, or any CUDA-capable host.

## Features

- **Base image**: built on a `pytorch/pytorch` tag — see [Base images & tags](#base-images--tags) below.
- **Pre-installed**: ComfyUI + ComfyUI-Manager + aria2 (fast resume downloads)
- **Model bootstrap**: `models.json` manifest for idempotent, resumable downloads
- **SSH ready**: Host keys generated at container start (secure, no baked keys)
- **Multi-provider**: Tested on RunPod and Vast.ai

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

**Quick Start**

### 1. Create a `models.json` manifest

```json
{
  "checkpoints": [
    {
      "name": "Illustrious-XL-v0.1",
      "url": "https://huggingface.co/.../resolve/main/Illustrious-XL-v0.1.safetensors",
      "dest": "checkpoints/Illustrious-XL-v0.1.safetensors",
      "size": 6938292816,
      "auth": "huggingface"
    }
  ]
}
```

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
   - `pytorch_tag` — full `pytorch/pytorch` tag, e.g. `2.5.1-cuda12.1-cudnn9-runtime`
   - `cuda` — CUDA version, e.g. `12.1`
   - `pytorch` — PyTorch version, e.g. `2.5.1`
   - `driver_floor` — minimum NVIDIA driver, e.g. `12010`
   - `is_latest` — tick to also publish as `:latest` (default: ticked)
3. Click **Run workflow**.

The CI verifies the `pytorch/pytorch:<tag>` exists on Docker Hub before building, so a typo fails fast (seconds) instead of 30s into layer pull.

**To make a new variant the permanent default** (e.g. a new PyTorch release), edit the `default:` values in both the `workflow_dispatch.inputs` block and the `Set defaults` step in `.github/workflows/build.yml`, and push.

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0), matching the upstream ComfyUI license.
