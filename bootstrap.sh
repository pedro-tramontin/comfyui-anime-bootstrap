#!/bin/bash
set -uo pipefail

echo "=== ComfyUI Anime Bootstrap ==="

COMFYUI_DIR="${COMFYUI_DIR:-/workspace/ComfyUI}"
MODELS_ROOT="${MODELS_ROOT:-/workspace/models}"
MANIFEST="${MODELS_MANIFEST:-/workspace/models.json}"

mkdir -p "$MODELS_ROOT/checkpoints" "$MODELS_ROOT/loras" \
    "$MODELS_ROOT/vae" "$MODELS_ROOT/clip_vision" \
    "$MODELS_ROOT/text_encoders" "$MODELS_ROOT/controlnet"

# Update ComfyUI + Manager daily-ish if older than 3h
UPDATE_MARKER="/workspace/.last_update"
if [ ! -f "$UPDATE_MARKER" ] || [ "$(find "$UPDATE_MARKER" -mmin +180)" ]; then
    echo "Updating ComfyUI..."
    cd "$COMFYUI_DIR"
    git pull --depth 1 || true
    echo "Updating ComfyUI-Manager..."
    cd custom_nodes/ComfyUI-Manager
    git pull --depth 1 || true
    touch "$UPDATE_MARKER"
fi

if [ ! -f "$MANIFEST" ]; then
    echo "No models manifest found at $MANIFEST. Creating from template..."
    cp /usr/local/share/models-template.json "$MANIFEST"
fi

# Auth
WGET_EXTRA=""
if [ -n "${HF_TOKEN:-}" ]; then
    WGET_EXTRA="--header=Authorization: Bearer ${HF_TOKEN}"
    export HF_HOME="${HF_HOME:-/workspace/.cache/huggingface}"
fi

# Download models idempotently
echo "Checking model registry..."
jq -c '.[] | arrays | .[]' "$MANIFEST" 2>/dev/null | while IFS= read -r line; do
    name=$(echo "$line" | jq -r '.name')
    url=$(echo "$line" | jq -r '.url')
    dest_rel=$(echo "$line" | jq -r '.dest')
    expect_size=$(echo "$line" | jq -r '.size // empty')
    auth_type=$(echo "$line" | jq -r '.auth // empty')

    dest="$MODELS_ROOT/$dest_rel"
    mkdir -p "$(dirname "$dest")"

    if [ -f "$dest" ]; then
        actual_size=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest" 2>/dev/null || echo "0")
        if [ "$actual_size" = "$expect_size" ] || [ -n "$expect_size" ] && [ "$actual_size" -eq "$expect_size" ] 2>/dev/null; then
            echo "  $name already exists, skipping."
            continue
        else
            echo "  $name size mismatch ($actual_size vs $expect_size), redownloading..."
        fi
    fi

    tmp="$dest.tmp"
    echo "  Downloading $name ..."
    if [ "$auth_type" = "civitai" ] && [ -n "${CIVITAI_API_KEY:-}" ]; then
        url="${url}?token=${CIVITAI_API_KEY}"
    fi

    # Aria2c for resume-capable fast downloads
    aria2c --continue=true --max-connection-per-server=16 --split=8 \
        --min-split-size=10M --dir="$(dirname "$tmp")" --out="$(basename "$tmp")" \
        --allow-overwrite=true --quiet "$url" || \
    wget --continue --progress=dot:giga -O "$tmp" $WGET_EXTRA "$url" || {
        echo "  ERROR: Failed to download $name"
        continue
    }

    mv "$tmp" "$dest"
    echo "  $name done."
done

echo "=== Bootstrap complete ==="
