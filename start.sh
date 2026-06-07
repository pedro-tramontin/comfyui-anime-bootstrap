#!/bin/bash
# Container entrypoint.
#
# Failure-tolerance contract:
#   - SSH setup failure  → log, continue (ComfyUI can still come up)
#   - Bootstrap failure  → log, continue (ComfyUI can start without models; download manually later)
#   - ComfyUI exit       → re-thrown so container restart can take over
set -uo pipefail
shopt -s nullglob

# Compact JSON-file summary for console logs.
# Shows the first N + last N lines (each cut to 100 chars) plus total line count
# so you can see structure without flooding the console with a full ~12KB manifest.
log_json_preview() {
    local label="$1"  # e.g. "[manifest]" or "[entrypoint]"
    local path="$2"
    [ -f "$path" ] || { echo "$label (no file at $path)"; return; }
    local total
    total=$(wc -l < "$path" 2>/dev/null | tr -d ' ')
    echo "$label first 10 / last 10 of $total lines ($path):"
    head -10 "$path" 2>/dev/null | sed "s/.\{100\}.*/&…[trim]/" | awk -v p="$label" '{printf "  %s | %s\n", p, $0}'
    local skip
    skip=$(( total > 20 ? total - 10 : 0 ))
    if [ "$skip" -gt 0 ]; then
        tail -n +"$((skip + 1))" "$path" 2>/dev/null | head -10 | \
            sed "s/.\{100\}.*/&…[trim]/" | awk -v p="$label" '{printf "  %s | %s\n", p, $0}'
    fi
}

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
# If the orchestrator (e.g. runpod_cheapest_loop.py) passes a manifest
# containing a JSON string, write it to /workspace/models.json so
# bootstrap uses our list instead of the baked-in fallback. This avoids
# the need for any pre-staging step or scp — the env var is the single
# source of truth for the manifest on a fresh volume.
#
# Two env-var keys are supported, in priority order:
#   1. MODELS_MANIFEST_B64 — base64-encoded JSON. This is the preferred
#      path because Linux env vars cannot contain raw newlines (the
#      RunPod container agent truncates at the first \n, silently
#      corrupting the JSON). Base64 is single-line and survives the
#      transport intact. The launcher writes this.
#   2. MODELS_MANIFEST — raw JSON. Kept for back-compat with any older
#      orchestrator that hasn't been updated. Often truncated on real
#      RunPod deploys; we still try it as a best-effort fallback.
#
# If neither env var is set (or both fail jq validation), bootstrap will
# fall back to /usr/local/share/models-template.json (baked into the
# image at build time).
write_manifest_from_env() {
    local src_desc="$1"  # human-readable label for log lines
    local raw="$2"       # the manifest content (decoded bytes if B64, raw text if not)
    if printf '%s' "$raw" > /workspace/models.json.tmp 2>/dev/null; then
        if command -v jq >/dev/null 2>&1 && jq -e . /workspace/models.json.tmp >/dev/null 2>&1; then
            local size=$(stat -c%s /workspace/models.json.tmp 2>/dev/null || stat -f%z /workspace/models.json.tmp 2>/dev/null || echo 0)
            mv /workspace/models.json.tmp /workspace/models.json
            # Log a compact summary (size + first 10 / last 10 lines) so the
            # console doesn't get flooded with the full ~12KB JSON.
            echo "[entrypoint] Wrote /workspace/models.json from $src_desc (valid JSON, ${size} bytes)"
            log_json_preview "[entrypoint]" "/workspace/models.json"
            return 0
        else
            echo "[entrypoint] WARN: $src_desc failed jq validation — falling back"
            rm -f /workspace/models.json.tmp
            return 1
        fi
    else
        echo "[entrypoint] WARN: could not write /workspace/models.json.tmp from $src_desc — falling back"
        return 1
    fi
}

# Path 1: base64-encoded manifest (preferred — survives newline mangling)
if [ -n "${MODELS_MANIFEST_B64:-}" ]; then
    echo "[entrypoint] MODELS_MANIFEST_B64 env var set (${#MODELS_MANIFEST_B64} b64-chars) — decoding"
    if command -v base64 >/dev/null 2>&1; then
        decoded=$(printf '%s' "$MODELS_MANIFEST_B64" | base64 -d 2>/dev/null) || decoded=""
        if [ -n "$decoded" ]; then
            write_manifest_from_env "MODELS_MANIFEST_B64" "$decoded" && goto_manifest_done=true
        else
            echo "[entrypoint] WARN: base64 decode failed — falling back to MODELS_MANIFEST"
        fi
    else
        echo "[entrypoint] WARN: base64 command not available — falling back to MODELS_MANIFEST"
    fi
fi

# Path 2: raw manifest (back-compat — often truncated on RunPod)
if [ "${goto_manifest_done:-}" != "true" ] && [ -n "${MODELS_MANIFEST:-}" ]; then
    echo "[entrypoint] MODELS_MANIFEST env var set (${#MODELS_MANIFEST} bytes) — writing /workspace/models.json"
    write_manifest_from_env "MODELS_MANIFEST" "$MODELS_MANIFEST" || true
fi

unset decoded goto_manifest_done

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
    # Ensure the symlink's parent dir exists. Without this, `ln -s` on a
    # fresh image fails silently (with 2>/dev/null || true above) and
    # ComfyUI's "Workflow → Open" dialog can't find any asuka-*.json files.
    # On first-launch images COMFYUI_DIR/user/default/ doesn't exist.
    mkdir -p "$(dirname "$WF_LINK")" 2>/dev/null || true
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
    # Sanity check — verify the symlink resolves to a non-empty dir
    if [ -L "$WF_LINK" ]; then
        wf_count=$(ls -A "$WF_LINK" 2>/dev/null | wc -l)
        echo "[entrypoint] Workflows dir contains $wf_count file(s)"
    fi
fi

# Custom output dir from env. If OUTPUT_DIR is set (typically a path on the
# network volume, e.g. /workspace/output), symlink ComfyUI's default
# $COMFYUI_DIR/output to it. Generated images, the GalleryManager's
# .metadata.jsonl sidecar, and .thumbs/ all then land on the volume and
# survive pod termination.
#
# We do NOT use extra_model_paths.yaml for this — that file only handles
# model types (checkpoints, loras, vae, ...), not the output/input/temp/
# user dirs. Those are hardcoded in folder_paths.py to $base_path/output,
# where $base_path defaults to $COMFYUI_DIR. So a symlink on the FS is the
# only way to redirect output to a network volume.
#
# We also do NOT bake any outputs into the image — the orchestrator owns
# the canonical gallery on the host and pushes new images to the volume.
# This keeps the image small and lets outputs accumulate independently of
# image rebuilds.
#
# Behavior mirrors WORKFLOWS_DIR above:
#   - OUTPUT_DIR unset → no symlink; ComfyUI writes to its default
#     $COMFYUI_DIR/output on the container disk (lost on pod termination)
#   - OUTPUT_DIR set, symlink doesn't exist → create it
#   - OUTPUT_DIR set, symlink already exists → leave it alone (idempotent
#     on container restarts, so re-running the entrypoint doesn't break)
#   - OUTPUT_DIR set, $COMFYUI_DIR/output exists as a non-empty real dir
#     (existing generated images) → migrate the contents to $OUTPUT_DIR,
#     then replace the dir with a symlink
if [ -n "${OUTPUT_DIR:-}" ]; then
    OUT_LINK="$COMFYUI_DIR/output"
    mkdir -p "$OUTPUT_DIR" 2>/dev/null || true
    if [ -L "$OUT_LINK" ] || [ -e "$OUT_LINK" ]; then
        if [ -d "$OUT_LINK" ] && [ ! -L "$OUT_LINK" ]; then
            # Real (non-symlink) dir: migrate contents to OUTPUT_DIR, then
            # atomically replace the dir with a symlink. We move files
            # individually (not `mv $OUT_LINK/* $OUTPUT_DIR/` after rmdir)
            # because the dir often contains a "_output_images_will_be_put_here"
            # placeholder or partial subdirs we want to keep.
            existing=$(ls -A "$OUT_LINK" 2>/dev/null | wc -l)
            if [ "$existing" -gt 0 ]; then
                echo "[entrypoint] Migrating $existing existing output file(s) from $OUT_LINK → $OUTPUT_DIR"
                # Use find -mindepth 1 to move both top-level files and
                # subdirs (e.g. .thumbs/, subfolders). Skip if the target
                # already has a same-named file (don't clobber).
                (cd "$OUT_LINK" && find . -mindepth 1 -maxdepth 1 \
                    -exec sh -c '[ ! -e "$0/$1" ] && mv "$1" "$0/"' "$OUTPUT_DIR" {} \;)
            fi
            rmdir "$OUT_LINK" 2>/dev/null || rm -rf "$OUT_LINK"
            ln -s "$OUTPUT_DIR" "$OUT_LINK"
            echo "[entrypoint] Replaced real output dir with symlink → $OUTPUT_DIR"
        else
            echo "[entrypoint] Output dir $OUT_LINK is already a symlink, leaving as-is"
        fi
    else
        ln -s "$OUTPUT_DIR" "$OUT_LINK"
        echo "[entrypoint] Symlinked $OUT_LINK → $OUTPUT_DIR"
    fi
    # Sanity check — verify the symlink resolves
    if [ -L "$OUT_LINK" ]; then
        out_count=$(ls -A "$OUT_LINK" 2>/dev/null | wc -l)
        echo "[entrypoint] Output dir contains $out_count file(s)"
    fi
fi

# SSH env propagation: write /root/.ssh/environment from a curated list of
# env vars so they survive into SSH login shells. By default sshd's
# AcceptEnv is empty (or LANG LC_*), so PID 1's env is NOT inherited by
# SSH sessions. The orchestrator (runpod_cheapest_loop.py) needs HF_TOKEN
# and CIVITAI_API_KEY available to whatever shell opens an SSH session
# into the pod (e.g. for `huggingface-cli`, `aria2c --header`, or just
# manual debugging). This requires PermitUserEnvironment yes in
# sshd_config (see Dockerfile) and a 600-perm /root/.ssh/environment file.
#
# We only forward a curated allowlist — never blanket-forward PID 1's env,
# which can include RUNPOD_API_KEY and other secrets that should NOT leak
# into interactive shells.
if [ -d /root/.ssh ]; then
    env_allowlist="HF_TOKEN HUGGING_FACE_HUB_TOKEN CIVITAI_API_KEY WORKFLOWS_DIR OUTPUT_DIR MODELS_MANIFEST MODELS_MANIFEST_B64"
    : > /root/.ssh/environment
    found_any=0
    for k in $env_allowlist; do
        v="${!k:-}"
        if [ -n "$v" ]; then
            # `env` format: KEY=VALUE per line, no quoting (sshd parses it)
            # Strip newlines from the value to keep it on one line.
            v_oneline=$(printf '%s' "$v" | tr '\n' ' ')
            echo "${k}=${v_oneline}" >> /root/.ssh/environment
            found_any=1
        fi
    done
    if [ "$found_any" = "1" ]; then
        chmod 600 /root/.ssh/environment
        n=$(wc -l < /root/.ssh/environment)
        echo "[entrypoint] Wrote /root/.ssh/environment ($n allowlisted env var(s) for SSH login shells)"
    fi
fi

# Start ComfyUI
echo "[entrypoint] Starting ComfyUI on :8188..."
cd "$COMFYUI_DIR"
exec python main.py --listen 0.0.0.0 --port 8188
