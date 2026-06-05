# Changelog

All notable changes to the comfyui-anime-bootstrap image are documented here.

Versions follow [Semantic Versioning](https://semver.org/). On each release the image is built from the default variant in `variants.json` and published to `ghcr.io/pedro-tramontin/comfyui-anime-bootstrap` with the `v<MAJOR>.<MINOR>.<PATCH>` and `:latest` tags. Other variants in `variants.json` are built manually on request.

## [Unreleased]

## [0.1.0] - 2026-06-05

### Added
- Initial release of comfyui-anime-bootstrap image
- ComfyUI installation at `/opt/ComfyUI`
- Network volume mounted at `/workspace` for models
- Bootstrap script (`/usr/local/bin/bootstrap.sh`) for idempotent model downloads via `/workspace/models.json`
- ComfyUI-Manager pre-installed
- File Browser pre-installed on port 8080
- SSH access via `PUBLIC_KEY` env var (public IP, port 22)
- Multi-base-image build matrix (CUDA 12.4 + PyTorch 2.6.0, CUDA 13.0 + PyTorch 2.12.0)
