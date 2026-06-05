# Changelog

All notable changes to the comfyui-anime-bootstrap image are documented here.

Versions follow [Semantic Versioning](https://semver.org/). On each release the image is built from the default variant in `variants.json` and published to `ghcr.io/pedro-tramontin/comfyui-anime-bootstrap` with the `v<MAJOR>.<MINOR>.<PATCH>` and `:latest` tags. Other variants in `variants.json` are built manually on request.

## [1.1.0](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.0.0...v1.1.0) (2026-06-05)


### Features

* **ci:** add tag_prefix input to build.yml workflow_dispatch ([#36](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/36)) ([61475e3](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/61475e3475203d696c07e934558b1503d984f7c2))

## 1.0.0 (2026-06-05)


### Features

* **ci:** add release-please + versioned image tags ([#33](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/33)) ([f4b6a06](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/f4b6a060a32d580c961f1aa503a03bcd9ea976df))
* **ci:** build multi-base-image matrix, expose cuda/pytorch/driver-floor labels ([#27](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/27)) ([82bd159](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/82bd159ddab4aec6e1d5c34ae989f499433f4e6f))
* cloud-native ComfyUI anime bootstrap ([e710f56](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/e710f5613e8f1722c2354ee18a56e707a4906bcb))
* **entrypoint:** add MODELS_MANIFEST and WORKFLOWS_DIR env-var hooks ([#32](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/32)) ([23bae69](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/23bae6921b4815000792c59aba225c387d10fc73))


### Bug Fixes

* add missing build step id for attestation digest ([#7](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/7)) ([cef652a](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/cef652af1caba2d03013123fd51b939a6804445a))
* **bootstrap:** never block ComfyUI startup on model download failures ([#26](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/26)) ([4066581](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/4066581ffd713657aa11f378165ab71c42de1e90))
* **bootstrap:** proper auth handling, verified URLs, PyTorch 2.9.1 + CUDA 13.0 ([#22](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/22)) ([6e05088](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/6e050881e436855858fce6c46c51375a109e05dc))
* **image:** install ComfyUI to /opt and harden sshd authorized_keys ([#29](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/29)) ([ab8b9b8](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/ab8b9b897d66589243f722a6185f0406bc7c090c))
* **renovate:** unbalanced mustache braces in prBodyTemplate ([#9](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/9)) ([551d2b2](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/551d2b2a9bbb36944d2047fc3c616dd78f1ef657))
* **start.sh:** copy PUBLIC_KEY env var to authorized_keys for direct SSH access ([#21](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/21)) ([0a524a8](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/0a524a85d0c42709636ef7b82f8e717b2a9a046b))
* use GITHUB_TOKEN for GHCR push, switch to docker/build-push-action ([0ed1a81](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/0ed1a81aa4849d3010c3577fb25cbd1f46c62635))

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
