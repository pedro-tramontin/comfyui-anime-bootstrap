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

# Custom models.json from env (BEFORE bootstrap reads /workspace/models.json).
# If the orchestrator (e.g. runpod_cheapest_loop.py) passes a MODELS_MANIFEST
# env var containing a JSON manifest, write it to /workspace/models.json so
# bootstrap uses our list instead of the baked-in fallback. This avoids the
# need for any pre-staging step or scp — the env var is the single source
# of truth for the manifest on a fresh volume.
#
# Failures here are non-fatal: if jq is missing or the JSON is malformed,
# bootstrap will fall back to /usr/local/share/models-template.json.
if [ -n "${MODELS_MANIFEST:-}" ]; then
    echo "[entrypoint] MODELS_MANIFEST env var set (${#MODELS_MANIFEST} bytes) — writing /workspace/models.json"
    if printf '%s' "$MODELS_MANIFEST" > /workspace/models.json.tmp 2>/dev/null; then
        if command -v jq >/dev/null 2>&1 && jq -e . /workspace/models.json.tmp >/dev/null 2>&1; then
            mv /workspace/models.json.tmp /workspace/models.json
            echo "[entrypoint] Wrote /workspace/models.json (valid JSON)"
        else
            echo "[entrypoint] WARN: MODELS_MANIFEST failed jq validation — falling back to baked-in template"
            rm -f /workspace/models.json.tmp
        fi
    else
        echo "[entrypoint] WARN: could not write /workspace/models.json.tmp — falling back to baked-in template"
    fi
fi

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

# Custom workflows dir from env. If WORKFLOWS_DIR is set (typically a path on
# the network volume, e.g. /workspace/workflows), symlink ComfyUI's default
# workflows dir to it. Workflows uploaded to that volume path appear
# instantly in ComfyUI's "Workflow → Load" menu.
#
# We do NOT bake any workflows into the image — the orchestrator owns the
# canonical templates at ~/.hermes/comfyui/templates/ on the host and pushes
# them to the volume. This keeps the image small and lets workflows evolve
# independently of image rebuilds.
#
# Behavior:
#   - WORKFLOWS_DIR unset → no symlink; ComfyUI uses its empty default dir
#     (workflows must be uploaded per-launch via scp, or none)
#   - WORKFLOWS_DIR set, symlink doesn't exist → create it
#   - WORKFLOWS_DIR set, symlink already exists → leave it alone (idempotent
#     on container restarts, so re-running the entrypoint doesn't break)
if [ -n "${WORKFLOWS_DIR:-}" ]; then
    WF_LINK="$COMFYUI_DIR/user/default/workflows"
    if [ -L "$WF_LINK" ] || [ -e "$WF_LINK" ]; then
        # If it's a real (non-empty) dir, leave it. If it's an empty default
        # dir (the put_checkpoints_here style), replace with symlink.
        if [ -d "$WF_LINK" ] && [ -z "$(ls -A "$WF_LINK" 2>/dev/null)" ]; then
            rmdir "$WF_LINK" 2>/dev/null || rm -rf "$WF_LINK"
            ln -s "$WORKFLOWS_DIR" "$WF_LINK"
            echo "[entrypoint] Replaced empty default workflows dir with symlink → $WORKFLOWS_DIR"
        else
            echo "[entrypoint] Workflows dir $WF_LINK already exists, leaving as-is"
        fi
    else
        mkdir -p "$WORKFLOWS_DIR" 2>/dev/null || true
        ln -s "$WORKFLOWS_DIR" "$WF_LINK"
        echo "[entrypoint] Symlinked $WF_LINK → $WORKFLOWS_DIR"
    fi
fi

# Start ComfyUI
echo "[entrypoint] Starting ComfyUI on :8188..."
cd "$COMFYUI_DIR"
exec python main.py --listen 0.0.0.0 --port 8188
