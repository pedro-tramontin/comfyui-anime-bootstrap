# syntax=docker/dockerfile:1
#
# ComfyUI Anime Bootstrap — base image
# ------------------------------------
# Single-variant image. The base pytorch/pytorch tag, PyTorch version, CUDA
# version, and minimum NVIDIA driver are passed as build args. The
# pytorch/pytorch tag itself is DERIVED from PYROCH_VERSION + CUDA_VERSION
# as `<pytorch>-cuda<cuda>-cudnn9-runtime` — we don't take it as a separate
# arg. See .github/workflows/build.yml for the current defaults and how to
# override them on a manual workflow trigger.
#
# Image tag scheme: cuda<MAJOR>.<MINOR>-pytorch<MAJOR>.<MINOR>.<PATCH>
# Example:         ghcr.io/pedro-tramontin/comfyui-anime-bootstrap:cuda13.0-pytorch2.12.0
#
# Build with custom values:
#   docker build --build-arg CUDA_VERSION=12.1 \
#                --build-arg PYTORCH_VERSION=2.5.1 \
#                --build-arg DRIVER_FLOOR=12010 .

# Derive the base image tag from CUDA + PyTorch versions. We declare the
# pre-FROM ARGs (PYTORCH_VERSION, CUDA_VERSION) here so we can reference
# their defaults in the FROM line — and then re-declare them with the
# same defaults post-FROM for the rest of the Dockerfile.
ARG PYTORCH_VERSION=2.12.0
ARG CUDA_VERSION=13.0
FROM pytorch/pytorch:${PYTORCH_VERSION}-cuda${CUDA_VERSION}-cudnn9-runtime

# Build args (must come after FROM to be visible in the rest of the Dockerfile).
# Defaults match the current "latest" variant in .github/workflows/build.yml.
ARG CUDA_VERSION=13.0
ARG PYTORCH_VERSION=2.12.0
# DRIVER_FLOOR is consumed only by the OCI label below — integration tests
# read it via `docker inspect` to decide whether to skip CUDA-touching tests
# on hosts whose NVIDIA driver is too old for the base pytorch/cuda combo.
ARG DRIVER_FLOOR=12080

# OCI / container labels — consumers can read these via `docker inspect` or
# `crane manifest` to pick the right image for their host's driver.
LABEL org.opencontainers.image.title="comfyui-anime-bootstrap"
LABEL org.opencontainers.image.description="Cloud-native ComfyUI base for anime model pipelines (RunPod, Vast.ai, local)"
LABEL org.opencontainers.image.source="https://github.com/pedro-tramontin/comfyui-anime-bootstrap"
LABEL org.opencontainers.image.cuda.version="${CUDA_VERSION}"
LABEL org.opencontainers.image.pytorch.version="${PYTORCH_VERSION}"
LABEL com.pedro-tramontin.comfyui-anime-bootstrap.driver-floor="${DRIVER_FLOOR}"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# Runtime deps: git, wget, curl, openssh, aria2 (faster resume downloads),
# ffmpeg (gallery video thumbnails).
# NOTE: pytorch base image already has pip (conda). Do NOT install python3-pip from apt.
RUN apt-get update && apt-get install -y \
    git \
    wget \
    curl \
    ca-certificates \
    openssh-server \
    aria2 \
    jq \
    rsync \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ComfyUI installation to /opt/ComfyUI.
#
# Why /opt and not /workspace/ComfyUI? On RunPod a network volume can be
# mounted at /workspace -- when it is, the mount SHADOWS everything baked
# into the image at /workspace, including the cloned ComfyUI source. The
# start.sh script then bails with "FATAL: /workspace/ComfyUI/main.py
# not found" and the container crash-loops. Moving the ComfyUI source
# to /opt/ComfyUI (outside the volume mount point) makes the image
# volume-mount-friendly; models are still downloaded to /workspace/models
# (which is the volume, when one is attached, and a tmpfs otherwise).
ENV COMFYUI_DIR=/opt/ComfyUI
RUN git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$COMFYUI_DIR" \
    && pip install --break-system-packages --no-cache-dir -r "$COMFYUI_DIR/requirements.txt"

# ComfyUI-Manager
RUN git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Manager \
    "$COMFYUI_DIR/custom_nodes/ComfyUI-Manager"

# ComfyUI_GalleryManager (bundled in this image — no first-boot install needed).
# Provides /gallery (with /gallery → /gallery/ redirect) and the JSON metadata API.
# Pillow is required by the PNG-metadata extraction path.
COPY custom_nodes/ComfyUI_GalleryManager "$COMFYUI_DIR/custom_nodes/ComfyUI_GalleryManager"
RUN pip install --break-system-packages --no-cache-dir Pillow

# Workspace model/output/input dirs. /workspace is the RunPod volume
# mount path; when no volume is attached, this just lives on the
# container disk.
RUN mkdir -p /workspace/models /workspace/output /workspace/input

# Bootstrap script
COPY bootstrap.sh /usr/local/bin/bootstrap.sh
RUN chmod +x /usr/local/bin/bootstrap.sh

COPY models-template.json /usr/local/share/models-template.json

# ComfyUI extra_model_paths.yaml — tells ComfyUI to scan /workspace/models/ in
# addition to its default /opt/ComfyUI/models/. Without this, the bootstrap's
# downloads silently land in a path ComfyUI never reads. See SKILL.md §6.x
# and references/comfyui-startup-pitfalls.md for the "no models visible
# despite successful downloads" symptom this fixes.
COPY extra_model_paths.yaml $COMFYUI_DIR/extra_model_paths.yaml

# Entry script
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

# SSH: generate hostkeys at runtime, not build time
RUN mkdir -p /var/run/sshd /root/.ssh \
    && sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && rm -f /etc/ssh/ssh_host_*

EXPOSE 22 8188

WORKDIR $COMFYUI_DIR
ENTRYPOINT ["/usr/local/bin/start.sh"]
