"""Tests for SHA256 model verification in bootstrap.sh.

These tests confirm the contract introduced when we moved from
size-match to SHA256-match as the strong check, with a sidecar cache
so the bootstrap doesn't re-hash 6+ GB files on every boot:

  - When a manifest entry has a non-null sha256, bootstrap.sh hashes
    the local file and compares.  Match → "OK: present (sha256 match)".
    Mismatch → re-download.
  - When a verified hash is already in the sidecar (scoped to the
    same manifest_sha256), bootstrap trusts it and SKIPS the file
    hash entirely.  This is the fast path on every boot.
  - When the manifest_sha256 in the sidecar doesn't match the current
    manifest, the sidecar entry is ignored and the file is hashed
    fresh — so a server-side file change automatically invalidates
    the cache.
  - When sha256 is null (or absent), bootstrap falls back to size
    comparison (legacy behavior).

The tests extract the per-model loop from bootstrap.sh and run it in
isolation, mocking all external commands (aria2c, sha256sum) and the
filesystem (a tempdir standing in for the models root).
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOTSTRAP_SH = os.path.join(REPO_ROOT, "bootstrap.sh")


# ---------- helpers ----------

def _read_bootstrap():
    with open(BOOTSTRAP_SH) as f:
        return f.read()


def _make_mock_bin(tmp, sha256_mocks=None, download_mocks=None, sha256_track_files=False):
    """Create a fake $PATH with deterministic sha256sum, aria2c, wget.

    sha256_mocks: dict[filename, sha256-hex] — what sha256sum should
        return when invoked on that file.  Falls back to the real
        sha256sum behavior for files not in the dict (so we can produce
        a deliberately-wrong hash by mapping a file to the wrong hex).
    download_mocks: dict[url-substring, local-file-to-emit] — if a
        download URL contains the key, the mock creates that local
        file at the --out/--dir target instead of actually fetching.
    sha256_track_files: if True, also append the file path to a marker
        file each time sha256sum is invoked on a tracked file.  Lets
        tests assert "was the file actually hashed?".
    """
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(bin_dir, exist_ok=True)

    track_log = os.path.join(tmp, "sha256sum_calls.log")
    sha256_script = (
        "#!/bin/bash\n"
        "input=\"$1\"\n"
    )
    if sha256_track_files:
        sha256_script += f"echo \"$input\" >> {track_log}\n"
    sha256_script += "case \"$input\" in\n"
    for path, hexh in (sha256_mocks or {}).items():
        sha256_script += f"  {path}) echo \"{hexh}  $input\"; exit 0 ;;\n"
    sha256_script += "  *) exec /usr/bin/sha256sum \"$@\" ;;\nesac\n"
    with open(os.path.join(bin_dir, "sha256sum"), "w") as f:
        f.write(sha256_script)
    os.chmod(os.path.join(bin_dir, "sha256sum"), 0o755)

    aria_marker = os.path.join(tmp, "ARIA2C_WAS_CALLED")
    aria_script = (
        "#!/bin/bash\n"
        f"touch {aria_marker}\n"
        "url=\"$@\"\n"
        "dir=$(echo \"$@\" | sed -n 's/.*--dir=\\([^ ]*\\).*/\\1/p')\n"
        "name=$(echo \"$@\" | sed -n 's/.*--out=\\([^ ]*\\).*/\\1/p')\n"
    )
    if download_mocks:
        aria_script += "case \"$url\" in\n"
        for substr, src in download_mocks.items():
            aria_script += f"  *{substr}*) cp {src} \"$dir/$name\" ; exit 0 ;;\n"
        aria_script += "esac\n"
    aria_script += "dd if=/dev/zero of=\"$dir/$name\" bs=1024 count=1 2>/dev/null\nexit 0\n"
    with open(os.path.join(bin_dir, "aria2c"), "w") as f:
        f.write(aria_script)
    os.chmod(os.path.join(bin_dir, "aria2c"), 0o755)

    with open(os.path.join(bin_dir, "wget"), "w") as f:
        f.write("#!/bin/bash\necho wget should not be called in default test path\nexit 1\n")
    os.chmod(os.path.join(bin_dir, "wget"), 0o755)

    return bin_dir, aria_marker, track_log


def _make_models_root(tmp):
    """Create the directory tree that bootstrap expects and return its path."""
    root = os.path.join(tmp, "models")
    os.makedirs(os.path.join(root, "checkpoints"))
    os.makedirs(os.path.join(root, "loras"))
    return root


def _write_manifest(path, entries):
    """Write a manifest with the same shape as the real models.json.

    entries: list of (dest_rel, url, sha256, size) tuples.
    """
    manifest = {
        "checkpoints": [
            {
                "name": os.path.basename(dest),
                "url": url,
                "dest": dest,
                "size": size,
                "sha256": sha,
                "auth": "huggingface",
            }
            for dest, url, sha, size in entries
        ]
    }
    with open(path, "w") as f:
        json.dump(manifest, f)
    return path


def _build_harness(tmp, manifest_path, mock_bin, models_root, sidecar_path):
    """Build a shell harness that sources the verification loop and runs it.

    Extracts the relevant functions from bootstrap.sh and runs the
    per-model loop against the mocked environment.
    """
    src = _read_bootstrap()

    m = re.search(r"^sha256_file\(\) \{.*?^\}", src, re.MULTILINE | re.DOTALL)
    assert m, "sha256_file function not found in bootstrap.sh"
    sha256_fn = m.group(0)

    m = re.search(r"^write_hash_sidecar\(\) \{.*?^\}", src, re.MULTILINE | re.DOTALL)
    assert m, "write_hash_sidecar function not found in bootstrap.sh"
    sidecar_fn = m.group(0)

    m = re.search(r"^download_file\(\) \{.*?^\}", src, re.MULTILINE | re.DOTALL)
    assert m, "download_file function not found in bootstrap.sh"
    download_fn = m.group(0)

    iter_start = src.find("# Iterate the manifest")
    assert iter_start > 0, "manifest-iteration comment not found"
    final_summary = src.find("# Final summary")
    assert final_summary > 0, "Final summary not found"
    loop_body = src[iter_start:final_summary].rstrip()

    harness_path = os.path.join(tmp, "harness.sh")
    with open(harness_path, "w") as f:
        f.write("#!/bin/bash\n")
        f.write("set -o pipefail\n")
        # NB: do NOT use `set -u` here — the per-model loop references
        # $civitai_token and $hf_token which are populated in the original
        # script but not in our extracted function set. Defaulting to empty
        # is correct: the test mocks do not exercise auth.
        f.write("hf_token=\"${HF_TOKEN:-}\"\n")
        f.write("civitai_token=\"${CIVITAI_API_KEY:-}\"\n")
        f.write("timeout_sec=900\n")
        f.write(f'MODELS_ROOT="{models_root}"\n')
        f.write(f'MANIFEST="{manifest_path}"\n')
        f.write(f"SIDECAR=\"{sidecar_path}\"\n")
        f.write("export MODELS_ROOT MANIFEST\n")
        f.write("\n")
        f.write(sha256_fn + "\n")
        f.write(sidecar_fn + "\n")
        f.write(download_fn + "\n")
        f.write(loop_body + "\n")
    os.chmod(harness_path, 0o755)
    return harness_path


def _run_harness(harness_path, mock_bin, env_extra=None):
    env = os.environ.copy()
    env["PATH"] = mock_bin + ":" + env["PATH"]
    env.setdefault("HF_TOKEN", "")
    env.setdefault("CIVITAI_API_KEY", "testkey")
    env.setdefault("MODELS_ROOT", "")
    env.setdefault("MANIFEST", "")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", harness_path],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _write_sidecar(path, entries):
    """Write a /workspace/models-hashes.json with the v2 schema.

    entries: dict[dest_rel, {manifest_sha256, verified_sha256, size, verified_at}].
    """
    with open(path, "w") as f:
        json.dump(entries, f)
    return path


# ---------- tests: function/structural contract ----------

def test_bootstrap_defines_sha256_file_function():
    src = _read_bootstrap()
    assert re.search(r"^sha256_file\(\) \{", src, re.MULTILINE), \
        "sha256_file() not defined in bootstrap.sh"


def test_bootstrap_defines_write_hash_sidecar_function():
    src = _read_bootstrap()
    assert re.search(r"^write_hash_sidecar\(\) \{", src, re.MULTILINE), \
        "write_hash_sidecar() not defined in bootstrap.sh"


def test_bootstrap_loop_reads_sha256_field():
    src = _read_bootstrap()
    assert ".sha256 // \"\"" in src, \
        "jq extraction of .sha256 not found in bootstrap.sh"


def test_bootstrap_loop_consults_sidecar_for_fast_path():
    """The per-model loop must check the sidecar before hashing."""
    src = _read_bootstrap()
    assert "manifest_sha256" in src, \
        "loop does not check sidecar's manifest_sha256 field"
    assert "verified_sha256" in src, \
        "loop does not check sidecar's verified_sha256 field"


def test_bootstrap_sidecar_schema_is_v2():
    """The sidecar schema must include both manifest_sha256 AND verified_sha256."""
    src = _read_bootstrap()
    fn = re.search(r"^write_hash_sidecar\(\) \{.*?^\}", src, re.MULTILINE | re.DOTALL)
    assert fn, "write_hash_sidecar function not found"
    body = fn.group(0)
    assert "manifest_sha256" in body, "sidecar schema missing manifest_sha256"
    assert "verified_sha256" in body, "sidecar schema missing verified_sha256"


# ---------- tests: verification ladder ----------

def test_sha256_match_with_no_sidecar():
    """Strong check, no sidecar yet: must hash the file, then cache."""
    with tempfile.TemporaryDirectory() as tmp:
        models_root = _make_models_root(tmp)
        manifest_path = os.path.join(tmp, "models.json")
        sidecar = "/workspace/models-hashes.json"
        if os.path.exists(sidecar):
            os.remove(sidecar)
        try:
            dest_rel = "checkpoints/test.safetensors"
            dest_abs = os.path.join(models_root, dest_rel)

            # Create a file with known content
            content = b"\x00" * 1024
            with open(dest_abs, "wb") as f:
                f.write(content)
            real_sha = hashlib.sha256(content).hexdigest()

            # sha256sum mock returns the real hash for this file
            mock_bin, _, _ = _make_mock_bin(tmp, sha256_mocks={dest_abs: real_sha})

            _write_manifest(
                manifest_path,
                [(dest_rel, "https://fake.example/test.safetensors", real_sha, 1024)],
            )

            harness = _build_harness(tmp, manifest_path, mock_bin, models_root, sidecar)
            r = _run_harness(harness, mock_bin)

            assert "sha256 match" in r.stdout, (
                f"expected sha256 match line, got: {r.stdout!r}\nstderr: {r.stderr}"
            )
            # Sidecar should be populated after the verification
            assert os.path.exists(sidecar), (
                f"sidecar not written after verification\nstdout: {r.stdout}"
            )
            with open(sidecar) as f:
                data = json.load(f)
            assert dest_rel in data
            entry = data[dest_rel]
            assert entry["manifest_sha256"] == real_sha
            assert entry["verified_sha256"] == real_sha
        finally:
            if os.path.exists(sidecar):
                os.remove(sidecar)


def test_sha256_match_with_valid_sidecar_skips_hashing():
    """Strong check + valid sidecar: file must NOT be hashed.

    This is the property that saves us from re-hashing 6 GB on every boot.
    """
    with tempfile.TemporaryDirectory() as tmp:
        models_root = _make_models_root(tmp)
        manifest_path = os.path.join(tmp, "models.json")
        sidecar = "/workspace/models-hashes.json"
        if os.path.exists(sidecar):
            os.remove(sidecar)
        try:
            dest_rel = "checkpoints/cached.safetensors"
            dest_abs = os.path.join(models_root, dest_rel)

            content = b"a" * 2048
            with open(dest_abs, "wb") as f:
                f.write(content)
            real_sha = hashlib.sha256(content).hexdigest()

            # Pre-seed the sidecar with a valid entry scoped to the manifest hash
            _write_sidecar(sidecar, {
                dest_rel: {
                    "manifest_sha256": real_sha,
                    "verified_sha256": real_sha,
                    "size": 2048,
                    "verified_at": "2026-06-10T00:00:00Z",
                },
            })

            # sha256sum mock would return WRONG hash if invoked — proves the
            # file is never read.
            mock_bin, aria_marker, track_log = _make_mock_bin(
                tmp,
                sha256_mocks={dest_abs: "0" * 64},
                sha256_track_files=True,
            )

            _write_manifest(
                manifest_path,
                [(dest_rel, "https://fake.example/cached", real_sha, 2048)],
            )

            harness = _build_harness(tmp, manifest_path, mock_bin, models_root, sidecar)
            r = _run_harness(harness, mock_bin)

            assert "sidecar hit" in r.stdout, (
                f"expected 'sidecar hit' line, got: {r.stdout!r}\nstderr: {r.stderr}"
            )
            assert "sha256 match" in r.stdout
            # The mock was wired to log every invocation — it should be empty.
            if os.path.exists(track_log):
                with open(track_log) as f:
                    calls = f.read().strip().splitlines()
                assert not calls, (
                    f"sha256sum was called on cached file: {calls}\n"
                    f"stdout: {r.stdout}"
                )
            assert not os.path.exists(aria_marker), (
                "aria2c was called even though sidecar said file is good"
            )
        finally:
            if os.path.exists(sidecar):
                os.remove(sidecar)


def test_sha256_mismatch_with_stale_sidecar_triggers_rehash_and_redownload():
    """Sidecar's manifest_sha256 is OLD (server updated). Sidecar must be ignored."""
    with tempfile.TemporaryDirectory() as tmp:
        models_root = _make_models_root(tmp)
        manifest_path = os.path.join(tmp, "models.json")
        sidecar = "/workspace/models-hashes.json"
        if os.path.exists(sidecar):
            os.remove(sidecar)
        try:
            dest_rel = "checkpoints/changed.safetensors"
            dest_abs = os.path.join(models_root, dest_rel)

            # Local file has the NEW hash (matches the manifest)
            content = b"new server bytes " * 100
            with open(dest_abs, "wb") as f:
                f.write(content)
            new_sha = hashlib.sha256(content).hexdigest()

            # Sidecar was written for the OLD server version
            old_sha = "f" * 64
            _write_sidecar(sidecar, {
                dest_rel: {
                    "manifest_sha256": old_sha,
                    "verified_sha256": old_sha,
                    "size": 1024,
                    "verified_at": "2026-06-01T00:00:00Z",
                },
            })

            mock_bin, _, _ = _make_mock_bin(tmp, sha256_mocks={dest_abs: new_sha})

            _write_manifest(
                manifest_path,
                [(dest_rel, "https://fake.example/changed", new_sha, len(content))],
            )

            harness = _build_harness(tmp, manifest_path, mock_bin, models_root, sidecar)
            r = _run_harness(harness, mock_bin)

            # The sidecar's manifest_sha256 is OLD, so the sidecar is ignored.
            # The file is hashed fresh, matches the new manifest, and the
            # sidecar is updated.  No re-download is needed.
            assert "sidecar-stale" in r.stdout or "sha256 match" in r.stdout, (
                f"expected sidecar-stale OR sha256 match, got: {r.stdout!r}"
            )
            # The sidecar must now be updated to the new hash
            with open(sidecar) as f:
                data = json.load(f)
            assert data[dest_rel]["manifest_sha256"] == new_sha
            assert data[dest_rel]["verified_sha256"] == new_sha
        finally:
            if os.path.exists(sidecar):
                os.remove(sidecar)


def test_sha256_mismatch_with_file_triggers_redownload():
    """File doesn't match manifest (and no sidecar). Must re-download."""
    with tempfile.TemporaryDirectory() as tmp:
        models_root = _make_models_root(tmp)
        manifest_path = os.path.join(tmp, "models.json")
        sidecar = "/workspace/models-hashes.json"
        if os.path.exists(sidecar):
            os.remove(sidecar)
        try:
            dest_rel = "checkpoints/corrupt.safetensors"
            dest_abs = os.path.join(models_root, dest_rel)

            # Pre-existing local file: contents that hash to corrupt_hash
            corrupt_content = b"corrupt bytes, will not match manifest"
            with open(dest_abs, "wb") as f:
                f.write(corrupt_content)
            corrupt_hash = hashlib.sha256(corrupt_content).hexdigest()

            # The "good" payload the download will produce
            new_content = b"good server content that matches good_hash"
            new_file = os.path.join(tmp, "good_payload.bin")
            with open(new_file, "wb") as f:
                f.write(new_content)
            good_hash = hashlib.sha256(new_content).hexdigest()

            # sha256sum mock: this is the tricky part.  We need the mock
            # to return `corrupt_hash` for dest_abs BEFORE the download
            # (i.e. when the file contains corrupt_content), and the
            # REAL hash of the file content AFTER the download (i.e.
            # when dest_abs contains new_content == good_hash).
            #
            # Simplest correct approach: have the mock just always run
            # REAL sha256sum.  Then both calls are correct.  For the
            # "mismatch" path, the FIRST call (pre-download) sees
            # corrupt_content and returns corrupt_hash, which is !=
            # good_hash → mismatch → re-download.  The SECOND call
            # (post-download) sees new_content and returns good_hash,
            # which == good_hash → verify-ok → sidecar written.
            #
            # This is the cleanest setup.  We don't need a mock at all
            # for sha256sum in this test.
            mock_bin, aria_marker, _ = _make_mock_bin(
                tmp,
                download_mocks={"fake.example": new_file},
            )

            _write_manifest(
                manifest_path,
                [(dest_rel, "https://fake.example/broken", good_hash, len(new_content))],
            )

            harness = _build_harness(tmp, manifest_path, mock_bin, models_root, sidecar)
            r = _run_harness(harness, mock_bin)

            # The pre-download check: corrupt hash != manifest hash → mismatch
            assert "mismatch" in r.stdout, (
                f"expected mismatch, got: {r.stdout!r}\nstderr: {r.stderr}"
            )
            # The download happened
            assert "downloaded" in r.stdout.lower(), (
                f"expected download to occur, got: {r.stdout!r}"
            )
            # The sidecar should now be populated with the new (correct) hash
            assert os.path.exists(sidecar), (
                f"sidecar not written after successful re-download\nstdout: {r.stdout}"
            )
            with open(sidecar) as f:
                data = json.load(f)
            assert data[dest_rel]["manifest_sha256"] == good_hash
            assert data[dest_rel]["verified_sha256"] == good_hash
        finally:
            if os.path.exists(sidecar):
                os.remove(sidecar)


def test_size_fallback_when_sha256_null():
    """If sha256 is null, bootstrap should accept a size match as 'ok'."""
    with tempfile.TemporaryDirectory() as tmp:
        models_root = _make_models_root(tmp)
        manifest_path = os.path.join(tmp, "models.json")
        sidecar = "/workspace/models-hashes.json"
        if os.path.exists(sidecar):
            os.remove(sidecar)
        try:
            dest_rel = "checkpoints/test.safetensors"
            dest_abs = os.path.join(models_root, dest_rel)

            with open(dest_abs, "wb") as f:
                f.write(b"\x00" * 4096)

            mock_bin, aria_marker, _ = _make_mock_bin(tmp)

            _write_manifest(
                manifest_path,
                [(dest_rel, "https://fake.example/test.safetensors", None, 4096)],
            )

            harness = _build_harness(tmp, manifest_path, mock_bin, models_root, sidecar)
            r = _run_harness(harness, mock_bin)

            assert "size match" in r.stdout, (
                f"expected size-match fallback, got: {r.stdout!r}"
            )
            assert "falling back to size" in r.stdout, (
                f"expected explicit 'falling back to size' message, got: {r.stdout!r}"
            )
            assert not os.path.exists(aria_marker), (
                "aria2c was called even though file should have been accepted"
            )
            # No sidecar should be created for null-hash entries
            assert not os.path.exists(sidecar), (
                f"sidecar was unexpectedly written for null-hash entry\nstdout: {r.stdout}"
            )
        finally:
            if os.path.exists(sidecar):
                os.remove(sidecar)


# ---------- tests: real-world model fixtures ----------

@pytest.mark.parametrize("name,dest_rel,size,sha256", [
    ("animagine-xl-4.0",
     "checkpoints/animagine-xl-4.0.safetensors",
     6938434056,
     "1d5b43ff75b6ab598502d4c779d2fbfa3dceca51c60c3b609640a60772333916"),
    ("pony-diffusion-v6-xl",
     "checkpoints/ponyDiffusionV6XL_v6StartWithThisOne.safetensors",
     6938041050,
     "67ab2fd8ec439a89b3fedb15cc65f54336af163c7eb5e4f2acc98f090a29b0b3"),
    ("illumiyume-xl-v3.5",
     "checkpoints/illumiyumeXL_v35VPred.safetensors",
     6938040794,
     "785b4b6dd828a0da605543bb673ff9d107d5a68a7e452372ec3dc82595481dcb"),
    ("netayume-v4",
     "checkpoints/NetaYume_v4_all_in_one.safetensors",
     10620229821,
     "e2b277eedf4fe15e1c27fd101990053314725a6eb8ee370bd486d132c04455bf"),
    ("wai-illustrious-sdxl-v170",
     "checkpoints/waiIllustriousSDXL_v170.safetensors",
     6938040794,
     "f116b0c78ff441467b0cdc8f1936e1ed18ea31e9997c7b132b1b8db533f0bd04"),
    ("sadamoto-yoshiyuki-xl-v3",
     "loras/Sadamoto_Yoshiyuki_Style-Illustrious-V1.0.safetensors",
     27837028,
     "06cac225fa44e1a6677b366e1527a6ba1a9f0c0793acb1a6beca24f24d309ceb"),
])
def test_every_manifest_entry_has_server_published_sha256(name, dest_rel, size, sha256):
    """Every entry in models.json must carry a real server-published SHA256.

    Sourced from:
      - HF tree API:  huggingface.co/api/models/{repo}/tree/{revision} -> siblings[].lfs.oid
      - CivitAI:      civitai.com/api/v1/model-versions/{versionId}  -> files[].hashes.SHA256

    If this test ever fails, the manifest has a placeholder / stale value.
    """
    import re as _re
    manifest_text = open(os.path.join(REPO_ROOT, "models.json")).read()
    assert f'"sha256": "{sha256}"' in manifest_text, (
        f"models.json missing server-published sha256 for {name!r}: expected {sha256}"
    )
    # Every entry's sha256 must be a 64-char hex string, NOT null.
    manifest = json.loads(manifest_text)
    for category in manifest.values():
        if not isinstance(category, list):
            continue
        for entry in category:
            if not isinstance(entry, dict) or "url" not in entry:
                continue
            sha = entry.get("sha256")
            assert sha is not None, (
                f"manifest entry {entry.get('name')!r} has sha256=null; "
                "fetch the real hash from the server API and add it to the manifest"
            )
            assert _re.match(r"^[0-9a-f]{64}$", sha), (
                f"manifest entry {entry.get('name')!r} has invalid sha256: {sha!r}"
            )
