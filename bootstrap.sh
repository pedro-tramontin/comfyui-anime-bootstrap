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

# ComfyUI source dir is at /opt/ComfyUI (not /workspace/ComfyUI -- see
# the comment in the Dockerfile for the volume-shadowing rationale).
COMFYUI_DIR="${COMFYUI_DIR:-/opt/ComfyUI}"
MODELS_ROOT="${MODELS_ROOT:-/workspace/models}"
MANIFEST="${MODELS_MANIFEST:-/workspace/models.json}"

# Always create model dirs (no-op if they exist)
mkdir -p "$MODELS_ROOT/checkpoints" "$MODELS_ROOT/loras" \
         "$MODELS_ROOT/vae" "$MODELS_ROOT/clip_vision" \
         "$MODELS_ROOT/text_encoders" "$MODELS_ROOT/controlnet" 2>/dev/null

# Disk-space precheck — bail out early with a clear "out of disk" message
# rather than spending 20 minutes on a download that will fail halfway through.
# Counts expected total bytes from the manifest and compares to available free
# space on the destination filesystem. If we don't have room, we log it and
# continue without downloads (ComfyUI can still start; user can free space
# or re-run the bootstrap).
echo "[disk] Checking available space on $(df -P "$MODELS_ROOT" 2>/dev/null | tail -1 | awk '{print $6}' || echo "$MODELS_ROOT")"
df_kb=$(df -Pk "$MODELS_ROOT" 2>/dev/null | tail -1 | awk '{print $4}')
df_bytes=$((df_kb * 1024))
echo "[disk] Free: $(numfmt --to=iec --suffix=B "$df_bytes" 2>/dev/null || echo "${df_bytes} bytes")"

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
    if [ -f /usr/local/share/models-template.json ]; then
        bk_size=$(stat -c%s /usr/local/share/models-template.json 2>/dev/null || stat -f%z /usr/local/share/models-template.json 2>/dev/null || echo 0)
        bk_first=$(head -1 /usr/local/share/models-template.json 2>/dev/null | cut -c1-80)
        bk_last=$(tail -1 /usr/local/share/models-template.json 2>/dev/null | cut -c1-80)
        if cp /usr/local/share/models-template.json "$MANIFEST" 2>/dev/null; then
            echo "[manifest] Copied baked-in template → $MANIFEST (${bk_size} bytes)"
            echo "[manifest]   first: ${bk_first}"
            echo "[manifest]   last:  ${bk_last}"
        else
            echo "[manifest] No baked-in template either — model download step will be skipped"
        fi
    else
        echo "[manifest] No baked-in template either — model download step will be skipped"
    fi
else
    # Manifest exists — log a compact summary (size + first/last lines)
    # so the console doesn't get flooded with the full JSON for large
    # manifests (~12KB) on every container start.
    mf_size=$(stat -c%s "$MANIFEST" 2>/dev/null || stat -f%z "$MANIFEST" 2>/dev/null || echo 0)
    mf_first=$(head -1 "$MANIFEST" 2>/dev/null | cut -c1-80)
    mf_last=$(tail -1 "$MANIFEST" 2>/dev/null | cut -c1-80)
    echo "[manifest] Using existing $MANIFEST (${mf_size} bytes)"
    echo "[manifest]   first: ${mf_first}"
    echo "[manifest]   last:  ${mf_last}"
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
    local timeout_sec=900   # hard cap per download; once hit, abandon to keep ComfyUI starting

    mkdir -p "$(dirname "$dest")"

    # Stale .tmp cleanup — ALWAYS remove any prior .tmp before this attempt.
    # Without this, a fresh aria2c call sees a partial file and tries to resume;
    # if the URL is HF xet-bridge and the local .tmp is corrupt, the resume fails.
    if [ -f "$tmp" ]; then
        local stale_size=$(stat -c%s "$tmp" 2>/dev/null || stat -f%z "$tmp" 2>/dev/null || echo 0)
        echo "  [cleanup] Removing stale $tmp ($stale_size bytes from prior attempt)"
        rm -f "$tmp"
    fi

    # aria2c is the preferred tool — it handles HF's xet-bridge redirects,
    # multi-connection downloads, and resumability correctly. We always run it
    # first; only fall back to wget for auth/CORS edge cases.
    #
    # Auth/URL handling: the manifest convention is to use ${HF_TOKEN} and
    # ${CIVITAI_API_KEY} as placeholders inside the URL itself. We expand
    # them in-place here BEFORE considering whether to add a token to the
    # URL — that way, a manifest with a ${CIVITAI_API_KEY} placeholder gets
    # exactly one token, and we never double-append ?token= (the previous
    # behaviour silently produced a malformed query string when both the
    # manifest had a placeholder AND the env var was set).
    #
    # If the env var is empty, we LEAVE the literal placeholder in the URL
    # — that way the URL round-trips cleanly to the WARN message and to
    # `huggingface-cli download` / manual re-runs, and the bootstrap log
    # makes the missing-auth case obvious from the URL alone.
    if [ -n "$civitai_token" ]; then
        url="${url//\$\{CIVITAI_API_KEY\}/$civitai_token}"
    fi
    if [ -n "$hf_token" ]; then
        url="${url//\$\{HF_TOKEN\}/$hf_token}"
    fi

    local final_url="$url"
    # Backstop: if a civitai URL has no token at all (manifest author
    # forgot the placeholder) and we have a token in env, append one.
    # Use & if the URL already has a query string, ? otherwise.
    # We do a plain substring check for "?token=" or "&token=" (the
    # only two valid positions for a query param) to keep the test
    # portable across bash versions where [[ =~ ]] regex semantics
    # can be surprising with character classes.
    if [ "$auth_type" = "civitai" ] && [ -n "$civitai_token" ] && \
       [ "${url#*?token=}" = "$url" ] && [ "${url#*&token=}" = "$url" ]; then
        if [ "${url#*\?}" != "$url" ]; then
            final_url="${url}&token=${civitai_token}"
        else
            final_url="${url}?token=${civitai_token}"
        fi
    fi

    local extra_args=()
    if [ "$auth_type" = "huggingface" ] && [ -n "$hf_token" ]; then
        extra_args+=("--header=Authorization: Bearer $hf_token")
    fi

    echo "  Downloading $name (${auth_type:-public}, aria2c)..."
    timeout "$timeout_sec" aria2c \
        --continue=false \
        --max-connection-per-server=16 \
        --split=8 \
        --min-split-size=10M \
        --dir="$(dirname "$tmp")" \
        --out="$(basename "$tmp")" \
        --allow-overwrite=true \
        --console-log-level=warn \
        "${extra_args[@]}" \
        "$final_url" 2>&1 | tail -5

    local rc=$?
    if [ $rc -ne 0 ] || [ ! -s "$tmp" ]; then
        # Fallback to wget — only useful for trivial public URLs (wget breaks on HF xet-bridge)
        echo "  [fallback] aria2c failed (rc=$rc), trying wget..."
        rm -f "$tmp"
        local wget_args=(--tries=2 --timeout=60 -O "$tmp")
        if [ "$auth_type" = "huggingface" ] && [ -n "$hf_token" ]; then
            wget_args+=("--header=Authorization: Bearer $hf_token")
        fi
        timeout "$timeout_sec" wget "${wget_args[@]}" "$final_url" 2>&1 | tail -3
        rc=$?
    fi

    if [ $rc -ne 0 ] || [ ! -s "$tmp" ]; then
        echo "  WARN: Failed to download $name (rc=$rc) — continuing without it"
        echo "        You can re-run manually: aria2c --dir='$(dirname "$dest")' --out='$(basename "$dest")' '$url'"
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

# Disk-space precheck: sum the expected bytes of all models that are NOT
# already present and verified. If we don't have room for that, warn loudly
# and continue (the per-model loop will fail individually and log WARN — but
# at least the user will see the early warning explaining the root cause).
# We allow a 1GB safety margin for filesystem overhead and other writes.
safety_margin=$((1024 * 1024 * 1024))   # 1 GiB
needed_bytes=0
needed_files=0
while IFS= read -r entry; do
    size=$(echo "$entry" | jq -r '.size // 0')
    dest_rel=$(echo "$entry" | jq -r '.dest // ""')
    dest="$MODELS_ROOT/$dest_rel"
    if [ -f "$dest" ] && [ -n "$size" ] && [ "$size" -gt 0 ]; then
        actual_size=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest" 2>/dev/null || echo "0")
        if [ "$actual_size" = "$size" ]; then
            continue   # already present, don't count
        fi
    fi
    needed_bytes=$((needed_bytes + size))
    needed_files=$((needed_files + 1))
done < <(jq -c '.[] | arrays | .[] | select(has("url"))' "$MANIFEST" 2>/dev/null)

if [ "$needed_files" -gt 0 ]; then
    needed_iec=$(numfmt --to=iec --suffix=B "$needed_bytes" 2>/dev/null || echo "${needed_bytes} bytes")
    free_iec=$(numfmt --to=iec --suffix=B "$df_bytes" 2>/dev/null || echo "${df_bytes} bytes")
    echo "[disk] Need to download $needed_files model(s), $needed_iec total"
    echo "[disk] Free on volume: $free_iec"
    if [ "$df_bytes" -lt "$((needed_bytes + safety_margin))" ]; then
        echo ""
        echo "  ⚠️  WARNING: Insufficient disk space on the network volume!"
        echo "  ⚠️  Need ~$needed_iec (plus 1 GiB safety margin) but only $free_iec is free."
        echo "  ⚠️  Downloads will likely fail with disk-full errors."
        echo "  ⚠️  Either: (a) free up space on the volume, (b) reduce the model list in $MANIFEST,"
        echo "  ⚠️  or (c) attach a larger volume. Continuing anyway — per-model failures will be logged."
        echo ""
    fi
fi

# Build a list of all model entries (across all top-level arrays)
model_count=$(jq -c '[.[] | arrays | .[] | select(has("url"))] | length' "$MANIFEST" 2>/dev/null || echo "0")
present_count=0
missing_count=0
failed_count=0

# Iterate the manifest
jq -c '.[] | arrays | .[] | select(has("url"))' "$MANIFEST" 2>/dev/null | while IFS= read -r line; do
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
