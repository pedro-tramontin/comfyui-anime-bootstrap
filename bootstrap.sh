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

echo "=== ComfyUI Anime Bootstrap ==="

# ComfyUI source dir is at /opt/ComfyUI (not /workspace/ComfyUI -- see
# the comment in the Dockerfile for the volume-shadowing rationale).
COMFYUI_DIR="${COMFYUI_DIR:-/opt/ComfyUI}"
# Where the bootstrap writes downloads. On a network-volume pod, start.sh's
# EXTERNAL_BASE_FOLDER has already symlinked $COMFYUI_DIR/models →
# $EXTERNAL_BASE_FOLDER/models, and the default for both below is
# /workspace/models, so they line up. If you customize one, customize both
# (or just set EXTERNAL_BASE_FOLDER and let MODELS_ROOT default).
MODELS_ROOT="${MODELS_ROOT:-${EXTERNAL_BASE_FOLDER:-/workspace}/models}"
# IMPORTANT: $MODELS_MANIFEST_B64 is the base64-encoded JSON CONTENT of the
# manifest (set by the orchestrator and decoded by start.sh's entrypoint),
# NOT a file path. The entrypoint already wrote the decoded content to
# /workspace/models.json — bootstrap should always read from the file path
# ($MANIFEST_PATH or its default), never from the env var. See PR #41+#47.
MANIFEST="${MANIFEST_PATH:-/workspace/models.json}"

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
        if cp /usr/local/share/models-template.json "$MANIFEST" 2>/dev/null; then
            echo "[manifest] Copied baked-in template → $MANIFEST (${bk_size} bytes)"
            log_json_preview "[manifest]" "$MANIFEST"
        else
            echo "[manifest] No baked-in template either — model download step will be skipped"
        fi
    else
        echo "[manifest] No baked-in template either — model download step will be skipped"
    fi
else
    # Manifest exists — log a compact summary (size + first 10 / last 10 lines)
    # so the console doesn't get flooded with the full JSON for large
    # manifests (~12KB) on every container start.
    mf_size=$(stat -c%s "$MANIFEST" 2>/dev/null || stat -f%z "$MANIFEST" 2>/dev/null || echo 0)
    echo "[manifest] Using existing $MANIFEST (${mf_size} bytes)"
    log_json_preview "[manifest]" "$MANIFEST"
fi

# Auth helpers
hf_token="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"
civitai_token="${CIVITAI_API_KEY:-}"

download_file() {
    local name="$1"
    local url="$2"
    local dest="$3"
    local auth_type="$4"
    local download_client="${5:-aria2c}"
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

    # Skip aria2c if the manifest entry opted into wget directly.
    # Needed for civitai URLs whose 307 redirects to b2.civitai.com
    # return 403 on the second hop with aria2c (the signed redirect
    # URL gets rejected). wget follows the redirect cleanly because
    # it makes a single GET that includes the original query string.
    if [ "$download_client" = "wget" ]; then
        echo "  Downloading $name (${auth_type:-public}, wget per manifest)..."
        rc=1   # pretend aria2c failed; trigger the wget fallback below
    else
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
    fi
    if [ $rc -ne 0 ] || [ ! -s "$tmp" ]; then
        # Fallback to wget — only useful for trivial public URLs (wget breaks on HF xet-bridge)
        if [ "$download_client" = "wget" ]; then
            echo "  [wget] downloading..."
        else
            echo "  [fallback] aria2c failed (rc=$rc), trying wget..."
        fi
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

# Compute SHA256 of a file using whichever tool is available.
# Prefers sha256sum (GNU coreutils); falls back to openssl; last resort shasum -a 256.
# Outputs lowercase hex on stdout. Returns 0 on success, 1 on missing file.
sha256_file() {
    local path="$1"
    [ -f "$path" ] || return 1
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$path" 2>/dev/null | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$path" 2>/dev/null | awk '{print $NF}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$path" 2>/dev/null | awk '{print $1}'
    else
        return 1
    fi
}

# Cache a verified hash in /workspace/models-hashes.json, scoped to the
# current manifest's SHA256.  Schema:
#
#   { "<dest_rel>": {
#       "manifest_sha256":   "<hex>",   # the sha256 from the manifest
#       "verified_sha256":   "<hex>",   # what the local file actually hashes to
#       "size":              <bytes>,
#       "verified_at":       "<iso8601>"
#   }}
#
# The cache is only valid when the sidecar's manifest_sha256 matches the
# current manifest's sha256 — so a server-side change (you update the
# manifest to a new hash) invalidates the cache automatically without
# us having to track a separate "version" field.
#
# The sidecar is best-effort: any failure (no jq, no write perm) is
# logged but never blocks bootstrap.
write_hash_sidecar() {
    local dest_rel="$1"        # e.g. "checkpoints/animagine-xl-4.0.safetensors"
    local manifest_sha="$2"    # the manifest's sha256 (the one we just compared against)
    local verified_sha="$3"    # what the local file actually hashes to (== manifest_sha on success)
    local size="$4"            # bytes
    local sidecar="/workspace/models-hashes.json"

    if ! command -v jq >/dev/null 2>&1; then
        echo "  [sidecar] jq not present — skipping hash cache write"
        return 0
    fi

    local now
    now=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "unknown")

    if [ -f "$sidecar" ]; then
        if ! tmp=$(jq --arg k "$dest_rel" \
                       --arg ms "$manifest_sha" \
                       --arg vs "$verified_sha" \
                       --argjson s "$size" \
                       --arg t "$now" \
                       '.[$k] = {manifest_sha256: $ms, verified_sha256: $vs, size: $s, verified_at: $t}' \
                       "$sidecar" 2>/dev/null); then
            echo "  [sidecar] existing sidecar is corrupt — rewriting from scratch"
            echo "{\"$dest_rel\": {\"manifest_sha256\": \"$manifest_sha\", \"verified_sha256\": \"$verified_sha\", \"size\": $size, \"verified_at\": \"$now\"}}" > "$sidecar" 2>/dev/null || \
                echo "  [sidecar] WARN: could not write $sidecar (continuing)"
            return 0
        fi
        echo "$tmp" > "$sidecar" 2>/dev/null || \
            echo "  [sidecar] WARN: could not write $sidecar (continuing)"
    else
        jq -n --arg k "$dest_rel" \
              --arg ms "$manifest_sha" \
              --arg vs "$verified_sha" \
              --argjson s "$size" \
              --arg t "$now" \
            '{($k): {manifest_sha256: $ms, verified_sha256: $vs, size: $s, verified_at: $t}}' \
            > "$sidecar" 2>/dev/null || \
            echo "{\"$dest_rel\": {\"manifest_sha256\": \"$manifest_sha\", \"verified_sha256\": \"$verified_sha\", \"size\": $size, \"verified_at\": \"$now\"}}" > "$sidecar" 2>/dev/null || \
            echo "  [sidecar] WARN: could not write $sidecar (continuing)"
    fi
    echo "  [sidecar] cached verified hash for $dest_rel (manifest_sha256=${manifest_sha:0:12}…) → $sidecar"
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
#
# Verification ladder (matches the per-model loop below, including the
# sidecar fast path so we don't re-hash 6+ GB on every boot):
#   1. sha256 + sidecar hit  → present (no file hash needed)
#   2. sha256 (manifest)     → present iff file hash matches manifest
#   3. size only             → present iff file size matches (legacy)
#   4. neither               → assume missing (will be re-downloaded)
safety_margin=$((1024 * 1024 * 1024))   # 1 GiB
needed_bytes=0
needed_files=0
while IFS= read -r entry; do
    size=$(echo "$entry" | jq -r '.size // 0')
    expect_sha=$(echo "$entry" | jq -r '.sha256 // ""')
    dest_rel=$(echo "$entry" | jq -r '.dest // ""')
    dest="$MODELS_ROOT/$dest_rel"
    if [ -f "$dest" ]; then
        if [ -n "$expect_sha" ] && [ "$expect_sha" != "null" ]; then
            # Sidecar fast path: trust the cached verified hash if it's
            # scoped to this exact manifest_sha256.
            if [ -f /workspace/models-hashes.json ] && command -v jq >/dev/null 2>&1; then
                if jq -e --arg k "$dest_rel" --arg ms "$expect_sha" \
                       '.[$k].manifest_sha256 == $ms and .[$k].verified_sha256 == $ms' \
                       /workspace/models-hashes.json >/dev/null 2>&1; then
                    continue
                fi
            fi
            # No sidecar hit — fall back to actually hashing. This is the
            # "cold cache" path; subsequent boots hit the sidecar.
            if actual_sha=$(sha256_file "$dest" 2>/dev/null) && [ "$actual_sha" = "$expect_sha" ]; then
                continue
            fi
        else
            # No manifest hash: fall back to size match.
            actual_size=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest" 2>/dev/null || echo "0")
            if [ "$actual_size" = "$size" ] && [ "$size" -gt 0 ]; then
                continue
            fi
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
    expect_sha=$(echo "$line" | jq -r '.sha256 // ""')
    auth_type=$(echo "$line" | jq -r '.auth // "none"')
    download_client=$(echo "$line" | jq -r '.download_client // "aria2c"')

    dest="$MODELS_ROOT/$dest_rel"

    if [ -z "$url" ] || [ -z "$dest_rel" ]; then
        echo "  [skip] Invalid manifest entry: $line"
        continue
    fi

    # Normalise: jq returns "null" string for JSON null, which we want
    # to treat as "no manifest hash, fall back to size".
    if [ "$expect_sha" = "null" ] || [ -z "$expect_sha" ]; then
        effective_sha=""
    else
        effective_sha="$expect_sha"
    fi

    if [ -f "$dest" ]; then
        actual_size=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest" 2>/dev/null || echo "0")

        if [ -n "$effective_sha" ]; then
            # ---- Strong check (manifest has a server-published sha256) ----
            #
            # Three sub-paths, cheapest first:
            #   1. Sidecar says we already verified this file for THIS
            #      exact manifest_sha256 → trust it, skip the 6 GB read.
            #   2. No sidecar, or sidecar's manifest_sha256 doesn't match
            #      (i.e. the server file changed) → hash the file.
            #   3. Hash mismatch → re-download.

            cached_verified=""
            cached_manifest_match="no"
            if [ -f /workspace/models-hashes.json ] && command -v jq >/dev/null 2>&1; then
                cached_manifest_match=$(jq -r --arg k "$dest_rel" --arg ms "$effective_sha" \
                    'if .[$k].manifest_sha256 == $ms then "yes" else "no" end' \
                    /workspace/models-hashes.json 2>/dev/null)
                cached_verified=$(jq -r --arg k "$dest_rel" \
                    '.[$k].verified_sha256 // ""' \
                    /workspace/models-hashes.json 2>/dev/null)
            fi

            if [ "$cached_manifest_match" = "yes" ] && [ -n "$cached_verified" ]; then
                # ---- Sidecar hit: skip the file hash entirely. ----
                # If the cached verified_sha256 ever disagrees with the
                # manifest, we fall through to a real hash (defence in
                # depth: a sidecar from an older manifest version, or
                # a manually-edited sidecar, should not silently pass).
                if [ "$cached_verified" = "$effective_sha" ]; then
                    echo "  OK: $name present (sha256 match, ${actual_size} bytes; sidecar hit, no hash needed)"
                    continue
                else
                    echo "  [sidecar-stale] $name cached hash ${cached_verified:0:12}… disagrees with manifest ${effective_sha:0:12}… — re-hashing"
                fi
            fi

            # No usable sidecar entry — hash the file.
            actual_sha=$(sha256_file "$dest" 2>/dev/null)
            if [ "$actual_sha" = "$effective_sha" ]; then
                echo "  OK: $name present (sha256 match, ${actual_size} bytes)"
                # Cache it for next boot.  Failures here are non-fatal.
                write_hash_sidecar "$dest_rel" "$effective_sha" "$actual_sha" "$actual_size" >/dev/null 2>&1 || true
                continue
            else
                if [ -n "$actual_sha" ]; then
                    short_actual="${actual_sha:0:12}…"
                else
                    short_actual="<unreadable>"
                fi
                short_expected="${effective_sha:0:12}…"
                echo "  [mismatch] $name sha256 $short_actual != expected $short_expected — re-downloading"
            fi
        elif [ "$actual_size" = "$expect_size" ] && [ "$expect_size" -gt 0 ]; then
            # ---- Size fallback (weak — preserved for manifest entries
            # that have no sha256 at all) ----
            echo "  OK: $name present (size match, $actual_size bytes; no sha256 in manifest — falling back to size)"
            continue
        else
            if [ -n "$expect_size" ] && [ "$expect_size" -gt 0 ]; then
                echo "  [mismatch] $name size $actual_size != expected $expect_size — re-downloading"
            else
                echo "  [present, no size/sha256 in manifest] $name"
                continue
            fi
        fi
    else
        echo "  [missing] $name"
    fi

    # Try to download (failures are logged but don't block)
    download_file "$name" "$url" "$dest" "$auth_type" "$download_client" || true

    # Post-download: if the manifest had a sha256, hash the freshly
    # downloaded file and persist the verified hash so subsequent boots
    # can use the sidecar fast path.
    if [ -f "$dest" ] && [ -n "$effective_sha" ]; then
        downloaded_size=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest" 2>/dev/null || echo "0")
        downloaded_sha=$(sha256_file "$dest" 2>/dev/null)
        if [ "$downloaded_sha" = "$effective_sha" ]; then
            write_hash_sidecar "$dest_rel" "$effective_sha" "$downloaded_sha" "$downloaded_size" || true
        else
            # Server-published hash doesn't match the freshly downloaded
            # bytes — this is a real anomaly worth surfacing loudly.
            echo "  [verify-fail] $name downloaded sha256 ${downloaded_sha:0:12}… != expected ${effective_sha:0:12}… — leaving file in place but NOT caching"
        fi
    fi
done

# Final summary
echo "[models] Bootstrap verification complete."
echo "        (Re-run 'wget' for any model listed in the WARN lines above to install manually.)"
echo "=== Bootstrap complete ==="
exit 0
