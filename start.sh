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

# Link a ComfyUI top-level dir to an external volume path, idempotently.
# Idempotency: if the link already exists, leave it alone — re-running
# the entrypoint on container restart must not clobber or migrate twice.
# Migration: if the link target is a real (non-symlink) dir with content,
# move the contents to the external path, then atomically replace the
# real dir with a symlink. This is the upgrade path for existing deploys
# that pre-date the EXTERNAL_BASE_FOLDER convention.
#
# Args:
#   $1 — human label, e.g. "workflows" (for log lines)
#   $2 — the ComfyUI dir we want to symlink (e.g. $COMFYUI_DIR/output)
#   $3 — the external target path (e.g. /workspace/output)
#   $4 — "yes" if we should migrate existing contents (output yes,
#        workflows no — workflows are always owned by the orchestrator
#        and any local "put_*_here" placeholder is junk we don't want
#        to copy to the volume)
link_to_external_volume() {
    local label="$1"
    local link_path="$2"
    local target_path="$3"
    local migrate_contents="${4:-no}"

    mkdir -p "$target_path" 2>/dev/null || true
    # Ensure the symlink's parent dir exists. Without this, `ln -s` on a
    # fresh image fails silently (e.g. /opt/ComfyUI/user/default/ may
    # not exist on first launch).
    mkdir -p "$(dirname "$link_path")" 2>/dev/null || true

    if [ -L "$link_path" ] || [ -e "$link_path" ]; then
        if [ -d "$link_path" ] && [ ! -L "$link_path" ]; then
            # Real (non-symlink) dir: optionally migrate contents to the
            # external path, then atomically replace the dir with a
            # symlink. We move files individually (not `mv $link_path/*
            # $target_path/` after rmdir) because the dir often contains
            # placeholder files like "_output_images_will_be_put_here" or
            # partial subdirs we want to keep.
            local existing
            existing=$(ls -A "$link_path" 2>/dev/null | wc -l)
            if [ "$existing" -gt 0 ] && [ "$migrate_contents" = "yes" ]; then
                echo "[entrypoint] Migrating $existing existing $label file(s) from $link_path → $target_path"
                (cd "$link_path" && find . -mindepth 1 -maxdepth 1 \
                    -exec sh -c '[ ! -e "$0/$1" ] && mv "$1" "$0/"' "$target_path" {} \;)
            fi
            rmdir "$link_path" 2>/dev/null || rm -rf "$link_path"
            ln -s "$target_path" "$link_path"
            echo "[entrypoint] Replaced real $label dir with symlink → $target_path"
        else
            echo "[entrypoint] $label dir $link_path is already a symlink, leaving as-is"
        fi
    else
        ln -s "$target_path" "$link_path"
        echo "[entrypoint] Symlinked $link_path → $target_path"
    fi

    # Sanity check — verify the symlink resolves to a non-empty dir
    if [ -L "$link_path" ]; then
        local count
        count=$(ls -A "$link_path" 2>/dev/null | wc -l)
        echo "[entrypoint] $label dir contains $count file(s)"
    fi
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

# Link the three ComfyUI top-level dirs (models, output, workflows) to an
# external volume when EXTERNAL_BASE_FOLDER is set. EXTERNAL_BASE_FOLDER is
# the canonical knob — one env var, three symlinks, all of them on the
# same persistent volume.
#
# Per-dir overrides (MODELS_DIR, OUTPUT_DIR, WORKFLOWS_DIR) take precedence
# over the derived values for advanced cases (e.g. a user wants models on
# one volume and output on another). They are independent: any subset
# may be set, and unset vars fall through to the EXTERNAL_BASE_FOLDER
# derivation.
#
# Why a symlink and not extra_model_paths.yaml? extra_model_paths.yaml
# only handles model TYPES (checkpoints, loras, vae, ...), not the
# top-level output/input/temp/user dirs. Those are hardcoded in
# folder_paths.py to `os.path.join(base_path, "output")` where base_path
# defaults to $COMFYUI_DIR. So a symlink at the FS level is the only
# mechanism that works uniformly for ALL three dirs (models, output,
# workflows), avoids the "no models visible despite successful downloads"
# pitfall the YAML was a workaround for, and is OS-level so a broken
# mount fails loudly instead of silently.
#
# We do NOT bake models, output, or workflows into the image. The
# orchestrator owns the canonical templates at ~/.hermes/comfyui/
# on the host and pushes them to the volume. This keeps the image
# small and lets outputs accumulate independently of image rebuilds.
if [ -n "${EXTERNAL_BASE_FOLDER:-}" ]; then
    echo "[entrypoint] EXTERNAL_BASE_FOLDER=$EXTERNAL_BASE_FOLDER — linking models/output/workflows to volume"
    # MODELS_DIR: migrate contents because the image bakes 28 placeholder
    # subdirs (checkpoints/, loras/, ...) into $COMFYUI_DIR/models. We
    # DO NOT want those to land on the volume, so don't migrate by
    # default. Override with MODELS_DIR_MIGRATE=yes if you really need
    # to keep local models.
    link_to_external_volume "models" \
        "$COMFYUI_DIR/models" \
        "${MODELS_DIR:-$EXTERNAL_BASE_FOLDER/models}" \
        "${MODELS_DIR_MIGRATE:-no}"
    # OUTPUT_DIR: migrate — generated images on the container disk
    # should move to the volume.
    link_to_external_volume "output" \
        "$COMFYUI_DIR/output" \
        "${OUTPUT_DIR:-$EXTERNAL_BASE_FOLDER/output}" \
        "yes"
    # WORKFLOWS_DIR: don't migrate. Workflows are always owned by the
    # orchestrator; any local "put_*_here" placeholder is junk.
    link_to_external_volume "workflows" \
        "$COMFYUI_DIR/user/default/workflows" \
        "${WORKFLOWS_DIR:-$EXTERNAL_BASE_FOLDER/workflows}" \
        "no"
elif [ -n "${WORKFLOWS_DIR:-}" ] || [ -n "${OUTPUT_DIR:-}" ] || [ -n "${MODELS_DIR:-}" ]; then
    # Backward-compat: allow the per-dir vars to work on their own
    # (e.g. an older orchestrator that hasn't been updated to set
    # EXTERNAL_BASE_FOLDER). One or more of the three may be set.
    echo "[entrypoint] Per-dir override detected (no EXTERNAL_BASE_FOLDER) — linking only the specified dir(s)"
    [ -n "${MODELS_DIR:-}" ] && link_to_external_volume "models" "$COMFYUI_DIR/models" "$MODELS_DIR" "${MODELS_DIR_MIGRATE:-no}"
    [ -n "${OUTPUT_DIR:-}" ] && link_to_external_volume "output" "$COMFYUI_DIR/output" "$OUTPUT_DIR" "yes"
    [ -n "${WORKFLOWS_DIR:-}" ] && link_to_external_volume "workflows" "$COMFYUI_DIR/user/default/workflows" "$WORKFLOWS_DIR" "no"
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
    env_allowlist="HF_TOKEN HUGGING_FACE_HUB_TOKEN CIVITAI_API_KEY EXTERNAL_BASE_FOLDER MODELS_DIR WORKFLOWS_DIR OUTPUT_DIR MODELS_DIR_MIGRATE MODELS_MANIFEST MODELS_MANIFEST_B64"
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
