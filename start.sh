#!/bin/bash
set -euo pipefail

# Generate SSH hostkeys fresh (do not bake into image)
ssh-keygen -A

# Start SSH daemon
/usr/sbin/sshd

# Run bootstrap (model checks, custom nodes, updates)
/usr/local/bin/bootstrap.sh

# Start ComfyUI
cd "$COMFYUI_DIR"
exec python main.py --listen 0.0.0.0 --port 8188
