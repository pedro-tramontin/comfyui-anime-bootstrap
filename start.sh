#!/bin/bash
set -euo pipefail

# Copy PUBLIC_KEY from env to authorized_keys (for direct public IP SSH)
if [ -n "${PUBLIC_KEY:-}" ] && [ ! -f /root/.ssh/authorized_keys ]; then
    mkdir -p /root/.ssh
    echo "$PUBLIC_KEY" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
fi

# Generate SSH hostkeys fresh (do not bake into image)
ssh-keygen -A

# Start SSH daemon
/usr/sbin/sshd

# Run bootstrap (model checks, custom nodes, updates)
/usr/local/bin/bootstrap.sh

# Start ComfyUI
cd "$COMFYUI_DIR"
exec python main.py --listen 0.0.0.0 --port 8188
