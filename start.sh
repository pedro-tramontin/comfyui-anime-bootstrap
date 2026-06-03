#!/bin/bash
# Container entrypoint.
#
# Failure-tolerance contract:
#   - SSH setup failure  → log, continue (ComfyUI can still come up)
#   - Bootstrap failure  → log, continue (ComfyUI can start without models; download manually later)
#   - ComfyUI exit       → re-thrown so container restart can take over
set -uo pipefail
shopt -s nullglob

# Copy PUBLIC_KEY from env to authorized_keys (for direct public IP SSH).
# We intentionally do NOT gate on [ ! -f authorized_keys ] here -- on
# container restarts the file may exist with wrong perms or content
# (e.g. a stale/empty key written before PUBLIC_KEY was injected), and
# sshd with StrictModes=yes will silently reject the connection. Always
# rewrite from the current env value and reset perms to OpenSSH's
# required 700/600 (chmod the home dir AND the file; sshd refuses if
# either is world- or group-writable).
if [ -n "${PUBLIC_KEY:-}" ]; then
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    # Strip any trailing whitespace/newline from the env value, then
    # re-add a single trailing newline so the file is well-formed.
    printf '%s\n' "$PUBLIC_KEY" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    chown -R root:root /root/.ssh
fi

# Generate SSH hostkeys fresh (do not bake into image)
ssh-keygen -A 2>/dev/null || echo "[ssh] hostkey generation failed (non-fatal)"

# Start SSH daemon (don't let sshd failure stop ComfyUI)
/usr/sbin/sshd 2>/dev/null || echo "[ssh] sshd failed to start (non-fatal)"

# Run bootstrap — failures here MUST NOT block ComfyUI startup.
# A download or git pull issue should be logged and the user can fix it manually.
echo "[entrypoint] Running bootstrap (failures will be logged but not block)..."
/usr/local/bin/bootstrap.sh || {
    rc=$?
    echo "[entrypoint] Bootstrap returned $rc — continuing to ComfyUI anyway"
    echo "[entrypoint] Fix model issues manually with: huggingface-cli download <repo> or wget"
}

# Sanity-check that ComfyUI is actually installed
if [ ! -f "$COMFYUI_DIR/main.py" ]; then
    echo "[entrypoint] FATAL: $COMFYUI_DIR/main.py not found — ComfyUI not installed"
    echo "[entrypoint] Container will exit so the orchestrator can detect the failure"
    exit 127
fi

# Workaround: comfyui_workflow_templates package is missing the templates/ subdir
# in the pip wheel. Create an empty placeholder so aiohttp's add_static() doesn't crash.
WF_TPL_DIR="/usr/local/lib/python3.12/dist-packages/comfyui_workflow_templates/templates"
if [ ! -d "$WF_TPL_DIR" ]; then
    echo "[entrypoint] Creating missing $WF_TPL_DIR (workaround for pip wheel bug)"
    mkdir -p "$WF_TPL_DIR"
fi

# Start ComfyUI
echo "[entrypoint] Starting ComfyUI on :8188..."
cd "$COMFYUI_DIR"
exec python main.py --listen 0.0.0.0 --port 8188
