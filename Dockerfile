# syntax=docker/dockerfile:1
FROM pytorch/pytorch:2.12.0-cuda13.0-cudnn9-runtime

LABEL org.opencontainers.image.title="comfyui-anime-bootstrap"
LABEL org.opencontainers.image.description="Cloud-native ComfyUI base for anime model pipelines (RunPod, Vast.ai, local)"
LABEL org.opencontainers.image.source="https://github.com/pedro-tramontin/comfyui-anime-bootstrap"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# Runtime deps: git, wget, curl, openssh, aria2 (faster resume downloads)
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
    && rm -rf /var/lib/apt/lists/*

# ComfyUI installation to /workspace/ComfyUI
ENV COMFYUI_DIR=/workspace/ComfyUI
RUN git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$COMFYUI_DIR" \
    && pip install --no-cache-dir -r "$COMFYUI_DIR/requirements.txt"

# ComfyUI-Manager
RUN git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Manager \
    "$COMFYUI_DIR/custom_nodes/ComfyUI-Manager"

# Workspace symlinks for mounts
RUN mkdir -p /workspace/models /workspace/output /workspace/input

# Bootstrap script
COPY bootstrap.sh /usr/local/bin/bootstrap.sh
RUN chmod +x /usr/local/bin/bootstrap.sh

COPY models-template.json /usr/local/share/models-template.json

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
