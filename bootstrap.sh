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

    if [ "$auth_type" = "huggingface" ] && [ -n "$hf_token" ]; then
        echo "  Downloading $name (HF auth)..."
        wget --continue --progress=dot:giga \
            --header="Authorization: Bearer $hf_token" \
            -O "$tmp" "$url" || {
            echo "  ERROR: Failed to download $name"
            return 1
        }
    elif [ "$auth_type" = "civitai" ] && [ -n "$civitai_token" ]; then
        echo "  Downloading $name (Civitai auth)..."
        local auth_url="${url}?token=${civitai_token}"
        aria2c --continue=true --max-connection-per-server=16 --split=8 \
            --min-split-size=10M --dir="$(dirname "$tmp")" --out="$(basename "$tmp")" \
            --allow-overwrite=true --quiet "$auth_url" || \
        wget --continue --progress=dot:giga -O "$tmp" "$auth_url" || {
            echo "  ERROR: Failed to download $name"
            return 1
        }
    else
        echo "  Downloading $name (public)..."
        aria2c --continue=true --max-connection-per-server=16 --split=8 \
            --min-split-size=10M --dir="$(dirname "$tmp")" --out="$(basename "$tmp")" \
            --allow-overwrite=true --quiet "$url" || \
        wget --continue --progress=dot:giga -O "$tmp" "$url" || {
            echo "  ERROR: Failed to download $name"
            return 1
        }
    fi

    mv "$tmp" "$dest"
    echo "  $name done."
    return 0
}

# Download models idempotently
echo "Checking model registry..."
jq -c '.[] | arrays | .[]' "$MANIFEST" 2>/dev/null | while IFS= read -r line; do
    name=$(echo "$line" | jq -r '.name')
    url=$(echo "$line" | jq -r '.url')
    dest_rel=$(echo "$line" | jq -r '.dest')
    expect_size=$(echo "$line" | jq -r '.size // empty')
    auth_type=$(echo "$line" | jq -r '.auth // empty')

    dest="$MODELS_ROOT/$dest_rel"

    if [ -f "$dest" ]; then
        actual_size=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest" 2>/dev/null || echo "0")
        if [ "$actual_size" = "$expect_size" ] || [ -n "$expect_size" ] && [ "$actual_size" -eq "$expect_size" ] 2>/dev/null; then
            echo "  $name already exists, skipping."
            continue
        else
            echo "  $name size mismatch ($actual_size vs $expect_size), redownloading..."
        fi
    fi

    download_file "$name" "$url" "$dest" "$auth_type"
done

echo "=== Bootstrap complete ==="
