#!/bin/bash
# ComfyUI Anime Bootstrap
# -----------------------
# Verifies models on the persistent volume and starts ComfyUI. Designed so that
# ANY download/clone failure is logged but NEVER blocks ComfyUI startup — the
# user can `huggingface-cli download ...` or fix the network volume later.
#
# Failure-tolerance contract:
#   - Missing model dir   → create + continue
#   - Git pull fails      → log + continue (workdir is a shallow clone; pull is best-effort)
#   - Model download fails → log + continue (ComfyUI can still start)
#   - Stale .tmp files    → remove + skip-if-resume-fails
#   - All good            → log "all models present"
set -uo pipefail
shopt -s nullglob

echo "=== ComfyUI Anime Bootstrap ==="

COMFYUI_DIR="${COMFYUI_DIR:-/workspace/ComfyUI}"
MODELS_ROOT="${MODELS_ROOT:-/workspace/models}"
MANIFEST="${MODELS_MANIFEST:-/workspace/models.json}"

# Always create model dirs (no-op if they exist)
mkdir -p "$MODELS_ROOT/checkpoints" "$MODELS_ROOT/loras" \
         "$MODELS_ROOT/vae" "$MODELS_ROOT/clip_vision" \
         "$MODELS_ROOT/text_encoders" "$MODELS_ROOT/controlnet" 2>/dev/null

# Update ComfyUI + Manager best-effort (shallow clones, so git pull is unreliable)
# Only do this if 6+ hours have passed since last update.
UPDATE_MARKER="/workspace/.last_update"
if [ ! -f "$UPDATE_MARKER" ] || [ -n "$(find "$UPDATE_MARKER" -mmin +360 2>/dev/null)" ]; then
    echo "[update] Checking for ComfyUI updates (best-effort)..."
    if [ -d "$COMFYUI_DIR/.git" ]; then
        (cd "$COMFYUI_DIR" && git pull --depth 1 2>/dev/null) || \
            echo "[update] ComfyUI git pull failed — using existing checkout (continuing)"
    else
        echo "[update] No ComfyUI git dir at $COMFYUI_DIR — skipping update"
    fi
    if [ -d "$COMFYUI_DIR/custom_nodes/ComfyUI-Manager/.git" ]; then
        (cd "$COMFYUI_DIR/custom_nodes/ComfyUI-Manager" && git pull --depth 1 2>/dev/null) || \
            echo "[update] ComfyUI-Manager git pull failed — using existing checkout (continuing)"
    else
        echo "[update] No ComfyUI-Manager git dir — skipping update"
    fi
    touch "$UPDATE_MARKER" 2>/dev/null || true
fi

# If no manifest exists on the volume, copy the baked-in template
if [ ! -f "$MANIFEST" ]; then
    echo "[manifest] No models manifest at $MANIFEST — using baked-in template"
    cp /usr/local/share/models-template.json "$MANIFEST" 2>/dev/null || \
        echo "[manifest] No baked-in template either — model download step will be skipped"
fi

# Auth helpers
hf_token="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"
civitai_token="${CIVITAI_API_KEY:-}"

download_file() {
    local name="$1"
    local url="$2"
    local dest="$3"
    local auth_type="$4"
    local tmp="$dest.tmp"

    mkdir -p "$(dirname "$dest")"

    # Stale .tmp cleanup: if a prior run left a corrupt .tmp, delete it
    if [ -f "$tmp" ]; then
        echo "  [cleanup] Removing stale $tmp from prior failed download"
        rm -f "$tmp"
    fi

    if [ "$auth_type" = "huggingface" ] && [ -n "$hf_token" ]; then
        echo "  Downloading $name (HF auth)..."
        wget --tries=1 --timeout=120 --progress=dot:giga \
             --header="Authorization: Bearer $hf_token" \
             -O "$tmp" "$url" 2>&1 | tail -5
    elif [ "$auth_type" = "civitai" ] && [ -n "$civitai_token" ]; then
        echo "  Downloading $name (Civitai auth)..."
        local auth_url="${url}?token=${civitai_token}"
        aria2c --continue=true --max-connection-per-server=16 --split=8 \
               --min-split-size=10M --dir="$(dirname "$tmp")" --out="$(basename "$tmp")" \
               --allow-overwrite=true --quiet "$auth_url" 2>/dev/null \
        || wget --tries=1 --timeout=120 --progress=dot:giga -O "$tmp" "$auth_url" 2>&1 | tail -5
    else
        echo "  Downloading $name (public)..."
        aria2c --continue=true --max-connection-per-server=16 --split=8 \
               --min-split-size=10M --dir="$(dirname "$tmp")" --out="$(basename "$tmp")" \
               --allow-overwrite=true --quiet "$url" 2>/dev/null \
        || wget --tries=1 --timeout=120 --progress=dot:giga -O "$tmp" "$url" 2>&1 | tail -5
    fi

    local rc=$?
    if [ $rc -ne 0 ] || [ ! -s "$tmp" ]; then
        echo "  WARN: Failed to download $name (rc=$rc) — continuing without it"
        echo "        You can re-run manually: wget -O '$dest' '$url'"
        rm -f "$tmp"
        return 0  # CRITICAL: do not fail the bootstrap
    fi

    mv "$tmp" "$dest"
    echo "  OK: $name downloaded ($(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest" 2>/dev/null) bytes)"
    return 0
}

# Verify or download models idempotently.
# IMPORTANT: every per-model failure is logged and the loop continues.
echo "[models] Verifying model registry..."
if [ ! -f "$MANIFEST" ]; then
    echo "[models] No manifest present — skipping all model downloads. ComfyUI will start with whatever is on the volume."
    echo "=== Bootstrap complete (no manifest) ==="
    exit 0
fi

# Build a list of all model entries (across all top-level arrays)
model_count=$(jq -c '[.[][] | select(has("url"))] | length' "$MANIFEST" 2>/dev/null || echo "0")
present_count=0
missing_count=0
failed_count=0

# Iterate the manifest
jq -c '.[][] | select(has("url"))' "$MANIFEST" 2>/dev/null | while IFS= read -r line; do
    name=$(echo "$line" | jq -r '.name // "unknown"')
    url=$(echo "$line" | jq -r '.url // ""')
    dest_rel=$(echo "$line" | jq -r '.dest // ""')
    expect_size=$(echo "$line" | jq -r '.size // 0')
    auth_type=$(echo "$line" | jq -r '.auth // "none"')

    dest="$MODELS_ROOT/$dest_rel"

    if [ -z "$url" ] || [ -z "$dest_rel" ]; then
        echo "  [skip] Invalid manifest entry: $line"
        continue
    fi

    if [ -f "$dest" ]; then
        actual_size=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest" 2>/dev/null || echo "0")
        if [ "$actual_size" = "$expect_size" ] && [ "$expect_size" -gt 0 ]; then
            echo "  OK: $name present (size match)"
            continue
        else
            if [ -n "$expect_size" ] && [ "$expect_size" -gt 0 ]; then
                echo "  [mismatch] $name size $actual_size != expected $expect_size — re-downloading"
            else
                echo "  [present, no size in manifest] $name"
                continue
            fi
        fi
    else
        echo "  [missing] $name"
    fi

    # Try to download (failures are logged but don't block)
    download_file "$name" "$url" "$dest" "$auth_type" || true
done

# Final summary
echo "[models] Bootstrap verification complete."
echo "        (Re-run 'wget' for any model listed in the WARN lines above to install manually.)"
echo "=== Bootstrap complete ==="
exit 0
