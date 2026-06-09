# Changelog

All notable changes to the comfyui-anime-bootstrap image are documented here.

Versions follow [Semantic Versioning](https://semver.org/). On each release the image is built from the default variant in `variants.json` and published to Docker Hub (`pedrotramn/comfyui-anime-bootstrap`) with the `v<MAJOR>.<MINOR>.<PATCH>` and `:latest` tags. Other variants in `variants.json` are built manually on request.

## [1.7.0](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.6.5...v1.7.0) (2026-06-09)


### Features

* **gallery:** add metadata sidecar panel to lightbox ([#69](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/69)) ([8987089](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/8987089ac584428f7a6bb588dab646b710ff62c2))

## [1.6.5](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.6.4...v1.6.5) (2026-06-09)


### Bug Fixes

* **ci:** use git tag as version source, not version.txt ([#67](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/67)) ([e42d341](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/e42d341c7b21bda1998d1672b1e12926097b21e0))

## [1.6.4](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.6.3...v1.6.4) (2026-06-09)


### Bug Fixes

* **ci:** rely on build-push-action provenance, drop duplicate attest step ([#65](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/65)) ([db4954b](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/db4954bb89ebd5355b9fbbebd2fcfe6b584e3237))

## [1.6.3](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.6.2...v1.6.3) (2026-06-09)


### Bug Fixes

* **bootstrap:** honor per-entry download_client field (civitai b2 redirect) ([#62](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/62)) ([f6f5fb4](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/f6f5fb4bbeec2809b895911f3b7d8d40cd9e3c0d))

## [1.6.2](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.6.1...v1.6.2) (2026-06-09)


### Bug Fixes

* **docker:** opt out of PEP 668 so ComfyUI-Manager's auto-install works ([#60](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/60)) ([c85bfdd](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/c85bfddd962fda15ea522b98a8f52dbea19f58ee))

## [1.6.1](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.6.0...v1.6.1) (2026-06-08)


### Bug Fixes

* **start.sh:** NUL-byte-safe decode of MODELS_MANIFEST_B64 ([#58](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/58)) ([c77d852](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/c77d852b55112ed0c5a613c78ab1b7fe5198abd0))

## [1.6.0](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.5.0...v1.6.0) (2026-06-08)


### Features

* **bootstrap:** support gzip+base64 MODELS_MANIFEST_B64 ([#56](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/56)) ([3651ecc](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/3651ecc317ca3c993b801aa1306019593e8d7b03))

## [1.5.0](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.4.1...v1.5.0) (2026-06-08)


### Features

* **ci:** add multi-variant matrix build ([#54](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/54)) ([1f91c35](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/1f91c358553cde890f81a177843bfc169a26db17))

## [1.4.1](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.4.0...v1.4.1) (2026-06-07)


### Bug Fixes

* **ci:** strip whitespace from Docker Hub secrets before tagging image ([#52](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/52)) ([8714e97](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/8714e97f8ee029a53f2fa21a15c00194ee44842a))

## [1.4.0](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.3.0...v1.4.0) (2026-06-07)


### Features

* **ci:** publish only to Docker Hub, drop GHCR ([#50](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/50)) ([ce6c9e1](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/ce6c9e1c15cc153f677eb0c80f0485fedf45a4b4))

## [1.3.0](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.2.0...v1.3.0) (2026-06-07)


### Features

* **ci:** mirror release image to Docker Hub (opt-in) ([#48](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/48)) ([4b45d83](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/4b45d83c12703e2d545bdeeac7640340e5e0d947))

## [1.2.0](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.1.2...v1.2.0) (2026-06-07)


### Features

* **start.sh:** EXTERNAL_BASE_FOLDER env var symlinks models/output/workflows to a volume (replaces extra_model_paths.yaml) ([#45](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/45)) ([e582969](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/e582969bfb51ca5c68af96656f5bd6850886119a))


### Bug Fixes

* **bootstrap:** source manifest path from MANIFEST_PATH + add 5 integration tests ([#43](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/43)) ([8237774](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/82377742e1dec280dee1de2a98b64415102ca02f))

## [1.1.2](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.1.1...v1.1.2) (2026-06-06)


### Bug Fixes

* **start.sh:** mkdir -p WF_LINK parent + add SSH env propagation ([#41](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/41)) ([7553c47](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/7553c47605f5e4d1aa45b9138a0db0ec6f2fa788))

## [1.1.1](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/compare/v1.1.0...v1.1.1) (2026-06-06)


### Bug Fixes

* **bootstrap:** safe jq query over manifest + auth-token URL interpolation ([#39](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/issues/39)) ([029c9d1](https://github.com/pedro-tramontin/comfyui-anime-bootstrap/commit/029c9d1b4f22f0ecac77530e73cd745838d9ef23))

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
