# ComfyUI Anime Bootstrap

A cloud-native, GPU-ready Docker image for running ComfyUI with anime diffusion models. Works on RunPod, Vast.ai, or any CUDA-capable host.

## Features

- **Base image**: matrix over `pytorch/pytorch` CUDA 12.1 / 12.4 / 12.8 / 13.0 tags. See [Base images & tags](#base-images--tags) below.
- **Pre-installed**: ComfyUI + ComfyUI-Manager + aria2 (fast resume downloads)
- **Model bootstrap**: `models.json` manifest for idempotent, resumable downloads
- **SSH ready**: Host keys generated at container start (secure, no baked keys)
- **Multi-provider**: Tested on RunPod and Vast.ai

## Base images & tags

The `latest` tag is the broadest-compatible variant. Pin a specific tag if your
host's NVIDIA driver can't run the latest. Each image carries OCI labels so
consumers can `docker inspect` to find the right tag for their host.

| Tag | PyTorch | CUDA | NVIDIA driver floor | Use when |
|---|---|---|---|---|
| `cuda12.1-pytorch2.5.1` | 2.5.1 | 12.1 | 12010 | Broadest compat — use on legacy RunPod/Vast.ai hosts |
| `cuda12.4-pytorch2.6.0` | 2.6.0 | 12.4 | 12040 | Mid-range |
| `cuda12.8-pytorch2.9.1` | 2.9.1 | 12.8 | 12080 | Newer stable |
| `latest` (alias of `cuda13.0-pytorch2.12.0`) | 2.12.0 | 13.0 | 12080 | Most recent — default for new hosts with current NVIDIA drivers |

**Programmatic selection:**

```bash
# List all available tags
docker manifest inspect ghcr.io/pedro-tramontin/comfyui-anime-bootstrap | jq -r '.manifests[].digest'

# Inspect a tag's driver floor
docker pull ghcr.io/pedro-tramontin/comfyui-anime-bootstrap:cuda12.1-pytorch2.5.1
docker inspect --format '{{ index .Config.Labels "com.pedro-tramontin.comfyui-anime-bootstrap.driver-floor" }}' \
  ghcr.io/pedro-tramontin/comfyui-anime-bootstrap:cuda12.1-pytorch2.5.1
# → 12010
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

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0), matching the upstream ComfyUI license.
