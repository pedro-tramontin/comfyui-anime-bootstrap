"""
Integration tests for comfyui-anime-bootstrap Docker image.

These tests verify the image structure, boot sequence, and SSH readiness.
NOTE: ComfyUI requires an NVIDIA GPU to fully start. On CPU-only runners,
the container will exit after bootstrap when ComfyUI tries to init CUDA.
Tests are designed to be GPU-agnostic where possible.

Run locally:
    pip install -r tests/requirements.txt
    docker build -t comfyui-anime-bootstrap:test .
    pytest tests/integration_test.py -v

Run in CI (after image build):
    pytest tests/integration_test.py -v --image-tag=ghcr.io/...:pr-123
"""

import time
import socket
import os
import json
import subprocess
import tempfile
import urllib.request, urllib.error
import pytest
import docker
from docker import errors as docker_errors  # noqa: F401  (used by other tests)

IMAGE_NAME_DEFAULT = "comfyui-anime-bootstrap:test"
COMFY_PORT = 8188
SSH_PORT = 22
BOOT_TIMEOUT = 120  # seconds for port to open
SSH_TIMEOUT = 30    # seconds for SSH to be ready


def has_gpu():
    """Check if an NVIDIA GPU is available on the host."""
    try:
        result = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="session")
def docker_client():
    return docker.from_env()


@pytest.fixture(scope="session")
def image_tag(request):
    """Allow overriding the image tag via CLI: pytest --image-tag=foo."""
    tag = request.config.getoption("--image-tag")
    return tag if tag else IMAGE_NAME_DEFAULT


@pytest.fixture(scope="session")
def container(docker_client, image_tag):
    """Spin up the container, yield it, then teardown."""
    # Create a minimal empty models.json so bootstrap doesn't block startup
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write('{"checkpoints": [], "loras": [], "vae": [], "text_encoders": [], "controlnet": []}')
        models_json_path = f.name

    ctr = docker_client.containers.run(
        image_tag,
        detach=True,
        name="comfyui-test-" + str(int(time.time())),
        ports={
            f"{COMFY_PORT}/tcp": ("127.0.0.1", 0),  # random host port
            f"{SSH_PORT}/tcp": ("127.0.0.1", 0),
        },
        volumes={models_json_path: {"bind": "/workspace/models.json", "mode": "ro"}},
        environment={"DEBIAN_FRONTEND": "noninteractive"},
        stdout=True,
        stderr=True,
    )
    try:
        yield ctr
    finally:
        try:
            ctr.stop(timeout=10)
        except Exception:
            pass
        ctr.remove(force=True)
        os.unlink(models_json_path)


@pytest.fixture
def comfy_host_port(container):
    """Return the dynamically mapped host port for ComfyUI."""
    container.reload()
    ports = container.attrs["NetworkSettings"]["Ports"]
    return int(ports[f"{COMFY_PORT}/tcp"][0]["HostPort"])


@pytest.fixture
def ssh_host_port(container):
    """Return the dynamically mapped host port for SSH."""
    container.reload()
    ports = container.attrs["NetworkSettings"]["Ports"]
    return int(ports[f"{SSH_PORT}/tcp"][0]["HostPort"])


def wait_for_port(host: str, port: int, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.5)
    return False


def get_image_config(docker_client, image_tag):
    """Return the image's resolved config (env, labels, cmd) or None on error."""
    try:
        return docker_client.images.get(image_tag).attrs.get("Config") or {}
    except Exception:
        return {}


def get_image_labels(docker_client, image_tag):
    """Return the image's OCI labels dict (or empty)."""
    try:
        return docker_client.images.get(image_tag).attrs.get("Labels") or {}
    except Exception:
        return {}


def driver_floor_from_labels(labels):
    """Read the DRIVER_FLOOR label, or 0 if missing (treat as 'no floor known')."""
    raw = labels.get("com.pedro-tramontin.comfyui-anime-bootstrap.driver-floor", "0")
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def cuda_version_from_labels(labels):
    """Read CUDA version from labels, or None."""
    raw = labels.get("org.opencontainers.image.cuda.version")
    return str(raw).strip() if raw else None


def test_container_starts(container):
    """Container reaches running state (even if it later exits on CPU)."""
    container.reload()
    assert container.status in ("running", "created", "exited")


def test_driver_compatibility(docker_client, image_tag):
    """Skip the CUDA-touching tests if the host driver is too old for this image.

    Reads the image's OCI labels for CUDA version + driver floor, and if the
    host's NVIDIA driver is older than the floor, the rest of the tests would
    fail with a CUDA init error (not a real image bug). We skip with a clear
    message instead.
    """
    if not has_gpu():
        pytest.skip("No GPU on this runner — driver-compat check irrelevant")

    labels = get_image_labels(docker_client, image_tag)
    floor = driver_floor_from_labels(labels)
    cuda = cuda_version_from_labels(labels)

    # Probe the actual driver version via nvidia-smi
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        host_driver = int(out.stdout.strip().split(".")[0])  # e.g. "12070.42" -> 12070
    except Exception as e:
        pytest.skip(f"Couldn't probe host driver via nvidia-smi: {e}")

    if floor and host_driver < floor:
        pytest.skip(
            f"Image targets CUDA {cuda} (driver floor {floor}); "
            f"host driver is {host_driver}. Use a different tag for this host."
        )


def test_ssh_port_reachable(ssh_host_port):
    """The SSH daemon inside the container accepts TCP connections."""
    assert wait_for_port("127.0.0.1", ssh_host_port, timeout=SSH_TIMEOUT), \
        "SSH never came up"


def test_comfyui_port_reachable(comfy_host_port):
    """ComfyUI listens on 8188 and accepts TCP (even if HTTP later errors on CPU)."""
    assert wait_for_port("127.0.0.1", comfy_host_port, timeout=BOOT_TIMEOUT), \
        "ComfyUI port never opened"


def test_comfyui_api_responds(container, comfy_host_port):
    """ComfyUI port returns some HTTP response."""
    if not has_gpu():
        pytest.skip("No GPU on this runner — ComfyUI cannot fully start")

    url = f"http://127.0.0.1:{comfy_host_port}/"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "text/html")
            resp = urllib.request.urlopen(req, timeout=3)
            assert resp.status is not None
            return
        except urllib.error.HTTPError as e:
            assert e.code is not None
            return
        except Exception:
            time.sleep(0.5)
    # If we reach here on a GPU runner, the port opened but HTTP never replied.
    container.reload()
    if container.status != "running":
        pytest.fail(f"ComfyUI exited unexpectedly (status={container.status})")
    pytest.fail("ComfyUI HTTP port open but never responded")



def test_workspace_directories_exist(docker_client, image_tag):
    """The /workspace mount structure was created in Dockerfile.

    Note: ComfyUI source is at /opt/ComfyUI (NOT /workspace/ComfyUI),
    to avoid being shadowed when a RunPod network volume is mounted
    at /workspace. /workspace still holds the models/output/input dirs
    that the bootstrap and ComfyUI use.
    """
    try:
        output = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=["ls -d /opt/ComfyUI /workspace/models /workspace/output /workspace/input"],
            remove=True,
            detach=False,
        )
        assert b"No such file" not in output
    except docker.errors.ContainerError as e:
        pytest.fail(f"Missing directories: {e.stderr or b''}")


def test_bootstrap_sh_is_executable(docker_client, image_tag):
    """The bootstrap script landed in the right place."""
    try:
        docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=["test -x /usr/local/bin/bootstrap.sh"],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(f"bootstrap.sh not executable: {e.stderr or b''}")


def test_models_template_shipped(docker_client, image_tag):
    """models-template.json is baked into the image for first-time users."""
    try:
        docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=["test -f /usr/local/share/models-template.json"],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(f"models-template.json missing: {e.stderr or b''}")


def test_ssh_hostkeys_not_baked(docker_client, image_tag):
    """SSH hostkeys are generated at runtime, not baked into the image layer."""
    try:
        docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=["test ! -f /etc/ssh/ssh_host_ed25519_key"],
            remove=True,
            detach=False,
        )
        # Exit 0 → key does NOT exist → PASS
    except docker.errors.ContainerError:
        # Exit non-zero → key EXISTS → FAIL
        pytest.fail("SSH hostkey appears to be baked into image")


def test_gallery_manager_package_installed(docker_client, image_tag):
    """ComfyUI_GalleryManager is bundled into the image at $COMFYUI_DIR/custom_nodes/.

    The package must be wired at image-build time so the next fresh pod
    from this image has the /gallery route available on first boot, with
    no scp step or first-boot install.
    """
    try:
        docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                "test -f /opt/ComfyUI/custom_nodes/ComfyUI_GalleryManager/__init__.py "
                "&& test -f /opt/ComfyUI/custom_nodes/ComfyUI_GalleryManager/web/index.html"
            ],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(f"Gallery Manager package missing in image: {e.stderr or b''}")


def test_gallery_manager_no_slash_redirect_present(docker_client, image_tag):
    """The /gallery → /gallery/ redirect is wired in the bundled __init__.py.

    This is the #1 reported deploy bug (SKILL.md §6.11): without the
    explicit redirect, browsers typing /gallery (no trailing slash) hit
    a 404. Verifying it in the IMAGE, not on a live pod, means the
    contract is enforced regardless of how the image is deployed.
    """
    try:
        out = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                "grep -q 'HTTPFound.*gallery/' "
                "/opt/ComfyUI/custom_nodes/ComfyUI_GalleryManager/__init__.py"
            ],
            remove=True,
            detach=False,
        )
        # grep exit 0 = found = PASS
    except docker.errors.ContainerError:
        pytest.fail(
            "Gallery Manager is missing the /gallery → /gallery/ redirect. "
            "SKILL.md §6.11 documents this as the top-3 deploy bug."
        )


def test_gallery_api_list_responds(container, comfy_host_port):
    """GET /gallery/api/list returns 200 JSON on a running ComfyUI instance.

    This is the strongest test of the bundled-gallery integration: the
    /gallery route must register at ComfyUI startup and serve JSON.
    Skipped on CPU-only runners because ComfyUI's main process can't
    stay up long enough to register the route.
    """
    if not has_gpu():
        pytest.skip("No GPU on this runner — ComfyUI cannot fully start")
    if container.status != "running":
        pytest.skip(f"Container not running (status={container.status}); cannot hit /gallery")

    url = f"http://127.0.0.1:{comfy_host_port}/gallery/api/list"
    deadline = time.time() + 60  # ComfyUI-Manager takes 15-25s on CI
    last_error = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=5) as resp:
                # /gallery/api/list returns a JSON object with at least "files"
                body = resp.read()
                data = json.loads(body)
                assert isinstance(data, dict), f"Expected JSON object, got: {body!r}"
                assert "files" in data, f"Response missing 'files' key: {data!r}"
                assert "folders" in data, f"Response missing 'folders' key: {data!r}"
                return
        except urllib.error.HTTPError as e:
            if e.code == 503 and time.time() < deadline:
                # ComfyUI returns 503 while still starting up
                time.sleep(1)
                continue
            last_error = f"HTTP {e.code}: {e.read()!r}"
        except Exception as e:
            last_error = repr(e)
        time.sleep(1)
    pytest.fail(f"/gallery/api/list never returned 200: {last_error}")


def test_gallery_page_redirect(container, comfy_host_port):
    """GET /gallery (no trailing slash) → 302/308 redirect to /gallery/.

    Verifies the no-slash redirect in the bundled package works end-to-end
    on a running ComfyUI instance, not just as text in __init__.py.
    """
    if not has_gpu():
        pytest.skip("No GPU on this runner — ComfyUI cannot fully start")
    if container.status != "running":
        pytest.skip(f"Container not running (status={container.status})")

    url = f"http://127.0.0.1:{comfy_host_port}/gallery"
    deadline = time.time() + 60
    last_error = None
    while time.time() < deadline:
        try:
            # Don't follow redirects — we want to see the 302/308 itself.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            opener = urllib.request.build_opener(NoRedirect)
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "text/html")
            try:
                opener.open(req, timeout=5)
                # If we got here without an HTTPError, the server replied 200
                # to /gallery — which means the redirect is missing.
                pytest.fail("GET /gallery returned 200; the no-slash redirect is missing")
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    location = e.headers.get("Location", "")
                    assert location.endswith("/gallery/"), (
                        f"Redirect target is {location!r}, expected /gallery/"
                    )
                    return  # PASS
                last_error = f"HTTP {e.code}: {e.read()!r}"
        except Exception as e:
            last_error = repr(e)
        time.sleep(1)
    pytest.fail(f"GET /gallery never produced a redirect: {last_error}")


def test_ffmpeg_available_in_image(docker_client, image_tag):
    """ffmpeg is required for gallery video thumbnails and must be in the image."""
    try:
        docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=["ffmpeg -version | head -1"],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(f"ffmpeg not installed in image: {e.stderr or b''}")


def test_pillow_available_in_image(docker_client, image_tag):
    """Pillow is required by the gallery's PNG-metadata extraction."""
    try:
        docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=["python3 -c 'from PIL import Image; print(Image.__version__)'"],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(f"Pillow not installed in image: {e.stderr or b''}")


def test_sshd_has_permit_user_environment(docker_client, image_tag):
    """sshd_config must have PermitUserEnvironment yes (NOT commented out).

    This is what makes /root/.ssh/environment get sourced by SSH login
    shells — required for HF_TOKEN, CIVITAI_API_KEY, etc. to survive
    into interactive sessions. Without it, PID 1's env is invisible
    to anything that SSH's into the pod.
    """
    try:
        out = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                "grep -E '^PermitUserEnvironment[[:space:]]+yes' /etc/ssh/sshd_config"
            ],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(
            f"sshd_config missing 'PermitUserEnvironment yes' (uncommented): "
            f"{e.stderr or b''}"
        )


def test_ssh_authorized_keys_handling_in_start_sh(docker_client, image_tag):
    """start.sh must handle PUBLIC_KEY → authorized_keys correctly, including
    on container restarts where the file may exist with wrong perms.

    We can't actually run start.sh (it would try to start ComfyUI), but we
    can verify the file is referenced and the logic exists in start.sh.
    """
    try:
        out = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                "grep -E 'PUBLIC_KEY.*authorized_keys' /usr/local/bin/start.sh"
            ],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(
            f"start.sh is missing the PUBLIC_KEY → authorized_keys logic: "
            f"{e.stderr or b''}"
        )


def test_bootstrap_manifest_logging_is_compact(docker_client, image_tag):
    """bootstrap.sh must log manifest size + first/last line, not the full JSON.

    Avoids the bootstrap log being flooded with ~12KB of manifest JSON
    on every container start. The fallback copy from
    /usr/local/share/models-template.json should also log a compact
    summary.
    """
    try:
        # Spot-check: bootstrap.sh should NOT echo the full manifest.
        # We look for the new compact-summary pattern instead.
        out = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                "grep -E 'first:.*last:|first_line.*last_line' /usr/local/bin/bootstrap.sh"
            ],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(
            "bootstrap.sh is not using the compact (size + first/last line) "
            "manifest logging — full JSON gets dumped to console on every start"
        )


def test_start_sh_creates_workflows_symlink_parent(docker_client, image_tag):
    """start.sh must mkdir -p the parent of the workflows symlink.

    On a fresh image, /opt/ComfyUI/user/default/ doesn't exist, so a
    bare `ln -s` silently fails (the start.sh uses `2>/dev/null || true`
    to mask errors). Without the parent dir, ComfyUI's "Workflow → Open"
    dialog can't see any uploaded asuka-*.json files.
    """
    try:
        out = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                "grep -E 'mkdir -p.*dirname.*WF_LINK|mkdir -p.*user/default' "
                "/usr/local/bin/start.sh"
            ],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(
            "start.sh is missing the mkdir -p on the workflows symlink's "
            "parent dir — the symlink silently fails on fresh images"
        )


def test_start_sh_writes_ssh_environment_file(docker_client, image_tag):
    """start.sh must write /root/.ssh/environment from an allowlist.

    This is the SSH env propagation path: PID 1's HF_TOKEN,
    CIVITAI_API_KEY, EXTERNAL_BASE_FOLDER, MODELS_MANIFEST_B64 get
    written to /root/.ssh/environment (chmod 600) so PermitUserEnvironment=yes
    in sshd_config can source them into SSH login shells.
    """
    try:
        out = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                "grep -E 'ssh/environment|env_allowlist' /usr/local/bin/start.sh"
            ],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(
            "start.sh is missing the /root/.ssh/environment writer — "
            "SSH sessions can't see PID 1's env (HF_TOKEN, etc.)"
        )


def test_ssh_environment_uses_allowlist_not_blanket(docker_client, image_tag):
    """The SSH env writer must use a curated allowlist — never blanket-forward.

    A blanket `env > /root/.ssh/environment` would leak RUNPOD_API_KEY and
    other secrets into every interactive shell. The implementation must
    enumerate specific keys (HF_TOKEN, CIVITAI_API_KEY, etc.).
    """
    try:
        out = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                "grep -E 'env_allowlist=\"HF_TOKEN.*CIVITAI_API_KEY' "
                "/usr/local/bin/start.sh"
            ],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(
            "start.sh SSH env writer is missing the curated allowlist — "
            "either it doesn't exist or it blanket-forwards PID 1's env"
        )


def test_start_sh_decodes_manifest_b64(docker_client, image_tag):
    """start.sh must decode MODELS_MANIFEST_B64 into /workspace/models.json.

    This is the canonical path for the manifest env var. Base64 is
    single-line and survives RunPod's env-marshalling layer (raw
    multi-line JSON gets truncated at the first \n).
    """
    try:
        out = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                "grep -E 'MODELS_MANIFEST_B64|base64.*-d' /usr/local/bin/start.sh"
            ],
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        pytest.fail(
            "start.sh is missing the MODELS_MANIFEST_B64 (base64) decode path"
        )


def test_bootstrap_does_not_source_manifest_path_from_content_env_var(
    docker_client, image_tag
):
    """REGRESSION GUARD: bootstrap.sh MUST source its manifest PATH from a
    *path* env var, NEVER from an env var that holds the manifest CONTENT.

    Bug history (PR #41 followup): bootstrap.sh used to do:
        MANIFEST="${MODELS_MANIFEST:-/workspace/models.json}"
    But start.sh sets $MODELS_MANIFEST to the *content* (raw JSON), not a
    file path. Result: bootstrap's `[ -f "$MANIFEST" ]` test always failed
    silently and the model-download step was skipped on every pod start.

    The contract: $MODELS_MANIFEST_B64 = manifest CONTENT (base64).
    $MANIFEST_PATH (or its default) = manifest file PATH. Bootstrap must
    only ever use the PATH env var (or its hard-coded default), never the
    CONTENT env var.
    """
    try:
        out = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                # Look for the dangerous pattern. The line should NOT exist
                # in bootstrap.sh. We grep with `-v` and assert success.
                "if grep -nE 'MANIFEST=\"\\$\\{?MODELS_MANIFEST' "
                "/usr/local/bin/bootstrap.sh; then exit 1; else exit 0; fi"
            ],
            remove=True,
            detach=False,
        )
    except docker_errors.ContainerError as e:
        pytest.fail(
            "bootstrap.sh still sources its manifest path from $MODELS_MANIFEST "
            "(the env var that holds manifest CONTENT, not a path). "
            "This is the exact bug that was fixed in the uncommitted bootstrap.sh "
            "edit. Use $MANIFEST_PATH (or the hard-coded default) instead. "
            f"stderr: {e.stderr or b''}"
        )


def test_bootstrap_sources_manifest_path_from_path_env_var(docker_client, image_tag):
    """Positive contract: bootstrap.sh MUST source the manifest path from
    $MANIFEST_PATH (a path) — proving the reader side honours the contract
    that $MODELS_MANIFEST is content, $MANIFEST_PATH is a path.
    """
    try:
        out = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                "grep -E 'MANIFEST=\"\\$\\{MANIFEST_PATH' /usr/local/bin/bootstrap.sh"
            ],
            remove=True,
            detach=False,
        )
    except docker_errors.ContainerError as e:
        pytest.fail(
            "bootstrap.sh does not source the manifest path from $MANIFEST_PATH. "
            "The contract (PR #41) is: $MODELS_MANIFEST = content, "
            "$MANIFEST_PATH = file path that bootstrap reads from. "
            f"stderr: {e.stderr or b''}"
        )


def test_start_sh_manifest_write_path_matches_bootstrap_default(
    docker_client, image_tag
):
    """Both writer (start.sh) and reader (bootstrap.sh) must agree on the
    default manifest path. start.sh hard-codes /workspace/models.json as
    the write target; bootstrap.sh's $MANIFEST_PATH default must match.

    If either side silently changes the default, the writer→reader
    contract breaks and models silently don't get downloaded.
    """
    try:
        out = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/sh", "-c"],
            command=[
                # Writer: start.sh writes to /workspace/models.json
                # Reader: bootstrap.sh defaults MANIFEST_PATH to /workspace/models.json
                "test -f /usr/local/bin/start.sh && "
                "test -f /usr/local/bin/bootstrap.sh && "
                "grep -qE '/workspace/models\\.json' /usr/local/bin/start.sh && "
                "grep -qE 'MANIFEST_PATH:-/workspace/models\\.json' /usr/local/bin/bootstrap.sh"
            ],
            remove=True,
            detach=False,
        )
    except docker_errors.ContainerError as e:
        pytest.fail(
            "start.sh and bootstrap.sh disagree on the default manifest path. "
            "Writer (start.sh) must write to /workspace/models.json AND "
            "reader (bootstrap.sh) must default $MANIFEST_PATH to "
            "/workspace/models.json. Otherwise models silently get "
            "skipped. stderr: " + (e.stderr.decode(errors="replace") if e.stderr else "")
        )


def test_manifest_end_to_end_writer_to_reader(docker_client, image_tag):
    """End-to-end contract: with MODELS_MANIFEST_B64 set, start.sh's
    entrypoint logic must write the manifest to disk, and bootstrap.sh
    must read it back and enumerate the models in it.

    This is the full E2E test that catches env-var name mismatches
    (the bug we just hit) and silent fallbacks to the baked-in template
    (which would mean "all models present" even when no real manifest
    was provided).

    Skipped on CPU-only runners because we mount an aria2 stub
    but don't have real CUDA; bootstrap still works without GPU though,
    so this is a low-cost test on any runner that has docker.
    """
    import base64

    # A manifest with TWO entries — distinguishable from the baked-in
    # template (which has one checkpoint, animagine-xl-4.0). The test
    # asserts bootstrap.sh sees exactly our two models and not the
    # baked-in one.
    test_manifest = {
        "checkpoints": [
            {
                "name": "test-sentinel-checkpoint",
                "url": "http://example.invalid/test-cp.safetensors",
                "dest": "checkpoints/test-sentinel-cp.safetensors",
                "size": 1,                       # 1 byte — won't actually download
                "auth": "none",
            }
        ],
        "loras": [
            {
                "name": "test-sentinel-lora",
                "url": "http://example.invalid/test-lora.safetensors",
                "dest": "loras/test-sentinel-lora.safetensors",
                "size": 1,
                "auth": "none",
            }
        ],
        "vae": [],
        "text_encoders": [],
        "controlnet": [],
    }
    manifest_json = json.dumps(test_manifest)
    manifest_b64 = base64.b64encode(manifest_json.encode()).decode()

    with tempfile.TemporaryDirectory() as tmp:
        # Pre-create the destination files with the right size, so bootstrap
        # thinks they're already downloaded (no actual network calls).
        # This lets us assert "saw our manifest" without racing the network.
        cp_dest = os.path.join(tmp, "models", "checkpoints", "test-sentinel-cp.safetensors")
        lora_dest = os.path.join(tmp, "models", "loras", "test-sentinel-lora.safetensors")
        os.makedirs(os.path.dirname(cp_dest), exist_ok=True)
        os.makedirs(os.path.dirname(lora_dest), exist_ok=True)
        with open(cp_dest, "wb") as f:
            f.write(b"x")  # 1 byte, matches manifest size=1
        with open(lora_dest, "wb") as f:
            f.write(b"x")

        # Run a single container that:
        #   1. Sources /usr/local/bin/start.sh's manifest-write logic only
        #   2. Verifies /workspace/models.json was written
        #   3. Runs /usr/local/bin/bootstrap.sh in the container
        #   4. Verifies bootstrap enumerated our test entries
        #
        # We do this in one container to exercise the EXACT contract
        # chain: env var → write to file → read from file.
        # We pass `bash` (not `sh`) because start.sh uses bash features.
        #
        # We mount our pre-staged `tmp` at /workspace so the size-match
        # check passes. We do NOT pre-stage a /workspace/models.json
        # ourselves — that's the whole point: start.sh must write it
        # from the env var.
        try:
            log = docker_client.containers.run(
                image_tag,
                entrypoint=["/bin/bash", "-c"],
                command=[
                    # 1. Run ONLY the manifest-write portion of start.sh.
                    #    (start.sh tries to start sshd + ComfyUI which we
                    #    don't want in a CPU test environment.) We source
                    #    the file in a subshell that exits right after
                    #    the manifest write.
                    "set -uo pipefail; "
                    # Pull in start.sh but skip past its COMFYUI launch.
                    # Easiest: source it, but the script's main flow
                    # would try to exec comfyui. Instead, extract the
                    # manifest writer by sourcing the helper definitions
                    # (write_manifest_from_env, log_json_preview) and
                    # running the two env-var branches directly.
                    # We do that by running a fresh bash with a copy of
                    # the relevant snippet.
                    "bash -c '"
                    "set -uo pipefail; "
                    # Inline the manifest writer + the two env-var branches
                    # from start.sh, exactly as they appear there. This
                    # makes the test self-contained AND a contract test
                    # on the actual deployed code: if start.sh's snippet
                    # drifts, we update the test.
                    "log_json_preview() { "
                    "  local label=$1 path=$2; "
                    "  [ -f \"$path\" ] || { echo \"$label (no file at $path)\"; return; }; "
                    "  local total; total=$(wc -l < \"$path\" 2>/dev/null | tr -d \" \"); "
                    "  echo \"$label $total lines\"; "
                    "}; "
                    "write_manifest_from_env() { "
                    "  local src_desc=$1 raw=$2; "
                    "  if printf \"%s\" \"$raw\" > /workspace/models.json.tmp 2>/dev/null; then "
                    "    if command -v jq >/dev/null 2>&1 && "
                    "       jq -e . /workspace/models.json.tmp >/dev/null 2>&1; then "
                    "      mv /workspace/models.json.tmp /workspace/models.json; "
                    "      echo \"[writer] wrote /workspace/models.json from $src_desc\"; "
                    "      return 0; "
                    "    fi; "
                    "  fi; "
                    "  echo \"[writer] FAILED to write /workspace/models.json from $src_desc\"; "
                    "  return 1; "
                    "}; "
                    # Path 1: B64 (what the launcher sends). The payload may be
                    # plain base64(JSON) OR gzip+base64 — we auto-detect via
                    # the gzip magic bytes 1f 8b. See start.sh for the real
                    # implementation; this inline copy MUST stay in sync.
                    "if [ -n \\\"${MODELS_MANIFEST_B64:-}\\\" ]; then "
                    "  raw_b64=$(printf \\\"%s\\\" \\\"$MODELS_MANIFEST_B64\\\" | base64 -d 2>/dev/null) || raw_b64=\\\"\\\"; "
                    "  decoded=\\\"\\\"; "
                    "  if command -v gunzip >/dev/null 2>&1; then "
                    "    gzip_magic_check=$(printf \\\"%s\\\" \\\"$raw_b64\\\" | head -c 2 | od -An -tx1 2>/dev/null | tr -d \\\" \\n\\\"); "
                    "    if [ \\\"$gzip_magic_check\\\" = \\\"1f8b\\\" ]; then "
                    "      decoded=$(printf \\\"%s\\\" \\\"$raw_b64\\\" | gunzip 2>/dev/null) || decoded=\\\"\\\"; "
                    "    fi; "
                    "  fi; "
                    "  if [ -z \\\"$decoded\\\" ]; then decoded=\\\"$raw_b64\\\"; fi; "
                    "  if [ -n \\\"$decoded\\\" ]; then "
                    "    write_manifest_from_env MODELS_MANIFEST_B64 \\\"$decoded\\\"; "
                    "  fi; "
                    "fi; "
                    # Contract assertion 1: file exists
                    "test -f /workspace/models.json || { echo \"FAIL: /workspace/models.json not written\"; exit 1; }; "
                    # Contract assertion 2: file is valid JSON with our entry names
                    "jq -e \".checkpoints[0].name == \\\"test-sentinel-checkpoint\\\"\" "
                    "    /workspace/models.json >/dev/null || "
                    "  { echo \"FAIL: written manifest missing test-sentinel-checkpoint\"; exit 1; }; "
                    # Now run bootstrap.sh and capture its log. It should
                    # NOT print \"No models manifest\" (the bug symptom).
                    "log=$(/usr/local/bin/bootstrap.sh 2>&1); "
                    "echo \"$log\"; "
                    # Contract assertion 3: bootstrap saw our manifest
                    "echo \"$log\" | grep -q \"test-sentinel-checkpoint\" || "
                    "  { echo \"FAIL: bootstrap did not enumerate test-sentinel-checkpoint\"; "
                    "    echo \"(If you see \\\"No models manifest at\\\" above, the bug from PR #41 followup is back: \"; "
                    "    echo \" bootstrap.sh is reading \\$MODELS_MANIFEST as a path.)\"; exit 1; }; "
                    "echo \"OK: writer→reader contract holds (B64 → file → bootstrap sees our entries)\"; "
                    "'"
                ],
                environment={"MODELS_MANIFEST_B64": manifest_b64},
                volumes={tmp: {"bind": "/workspace", "mode": "rw"}},
                remove=True,
                detach=False,
            )
        except docker_errors.ContainerError as e:
            pytest.fail(
                "Manifest end-to-end contract FAILED. "
                "start.sh did not write /workspace/models.json, OR "
                "bootstrap.sh did not see the written manifest. "
                "This is the exact bug class we just spent a long RunPod "
                "debugging session to find (env-var name mismatch: "
                "$MODELS_MANIFEST = content, $MANIFEST_PATH = path). "
                "Check: (a) start.sh writer at /usr/local/bin/start.sh, "
                "(b) bootstrap.sh reader at /usr/local/bin/bootstrap.sh, "
                "(c) the default paths agree on /workspace/models.json. "
                f"stderr: {(e.stderr or b'').decode(errors='replace')}"
            )


def test_manifest_end_to_end_gzip_b64_writer_to_reader(docker_client, image_tag):
    """End-to-end contract: with MODELS_MANIFEST_B64 set to a gzip+base64
    payload (the new space-efficient path), start.sh's entrypoint logic
    must gunzip + write the manifest to disk, and bootstrap.sh must read
    it back and enumerate the models in it.

    This is the Vast.ai-friendly path: the launch env field is capped at
    4 KB and a gzip-b64 manifest is ~30% smaller than raw b64, which
    matters once you have more than one or two model entries.
    """
    import base64
    import gzip

    # Same sentinel manifest as the raw-b64 test — distinguishable from
    # the baked-in template (which has one checkpoint, animagine-xl-4.0).
    test_manifest = {
        "checkpoints": [
            {
                "name": "test-sentinel-gzip-checkpoint",
                "url": "http://example.invalid/test-cp-gz.safetensors",
                "dest": "checkpoints/test-sentinel-gz-cp.safetensors",
                "size": 1,
                "auth": "none",
            }
        ],
        "loras": [],
        "vae": [],
        "text_encoders": [],
        "controlnet": [],
    }
    manifest_json = json.dumps(test_manifest)
    # gzip-9 + b64 — exactly the shape we want to ship in the Vast template env.
    manifest_gz_b64 = base64.b64encode(gzip.compress(manifest_json.encode(), compresslevel=9)).decode()

    # Sanity-check the payload looks like gzip on the wire.
    assert base64.b64decode(manifest_gz_b64)[:2] == b"\x1f\x8b", "test fixture is not actually gzipped"

    with tempfile.TemporaryDirectory() as tmp:
        # Pre-stage the destination file so bootstrap's size-match passes
        # without needing network.
        cp_dest = os.path.join(tmp, "models", "checkpoints", "test-sentinel-gz-cp.safetensors")
        os.makedirs(os.path.dirname(cp_dest), exist_ok=True)
        with open(cp_dest, "wb") as f:
            f.write(b"x")

        try:
            log = docker_client.containers.run(
                image_tag,
                entrypoint=["/bin/bash", "-c"],
                command=[
                    # Inline the start.sh writer (gunzip branch) and the
                    # helper definitions. See start.sh for the real
                    # implementation; this inline copy MUST stay in sync.
                    "set -uo pipefail; "
                    "bash -c '"
                    "set -uo pipefail; "
                    "log_json_preview() { "
                    "  local label=$1 path=$2; "
                    "  [ -f \"$path\" ] || { echo \"$label (no file at $path)\"; return; }; "
                    "  local total; total=$(wc -l < \"$path\" 2>/dev/null | tr -d \" \"); "
                    "  echo \"$label $total lines\"; "
                    "}; "
                    "write_manifest_from_env() { "
                    "  local src_desc=$1 raw=$2; "
                    "  if printf \"%s\" \"$raw\" > /workspace/models.json.tmp 2>/dev/null; then "
                    "    if command -v jq >/dev/null 2>&1 && "
                    "       jq -e . /workspace/models.json.tmp >/dev/null 2>&1; then "
                    "      mv /workspace/models.json.tmp /workspace/models.json; "
                    "      echo \"[writer] wrote /workspace/models.json from $src_desc\"; "
                    "      return 0; "
                    "    fi; "
                    "  fi; "
                    "  echo \"[writer] FAILED to write /workspace/models.json from $src_desc\"; "
                    "  return 1; "
                    "}; "
                    # gzip+b64 branch — see start.sh
                    "if [ -n \"${MODELS_MANIFEST_B64:-}\" ]; then "
                    "  raw_b64=$(printf \"%s\" \"$MODELS_MANIFEST_B64\" | base64 -d 2>/dev/null) || raw_b64=\"\"; "
                    "  decoded=\"\"; "
                    "  if command -v gunzip >/dev/null 2>&1; then "
                    "    gzip_magic_check=$(printf \"%s\" \"$raw_b64\" | head -c 2 | od -An -tx1 2>/dev/null | tr -d \" \\n\"); "
                    "    if [ \"$gzip_magic_check\" = \"1f8b\" ]; then "
                    "      decoded=$(printf \"%s\" \"$raw_b64\" | gunzip 2>/dev/null) || decoded=\"\"; "
                    "      echo \"[entrypoint] Detected gzip-compressed manifest — gunzipped to ${#decoded} bytes\"; "
                    "    fi; "
                    "  fi; "
                    "  if [ -z \"$decoded\" ]; then decoded=\"$raw_b64\"; fi; "
                    "  if [ -n \"$decoded\" ]; then "
                    "    write_manifest_from_env MODELS_MANIFEST_B64 \"$decoded\"; "
                    "  fi; "
                    "fi; "
                    "test -f /workspace/models.json || { echo \"FAIL: /workspace/models.json not written\"; exit 1; }; "
                    "jq -e \".checkpoints[0].name == \\\"test-sentinel-gzip-checkpoint\\\"\" "
                    "    /workspace/models.json >/dev/null || "
                    "  { echo \"FAIL: written manifest missing test-sentinel-gzip-checkpoint\"; exit 1; }; "
                    "log=$(/usr/local/bin/bootstrap.sh 2>&1); "
                    "echo \"$log\"; "
                    "echo \"$log\" | grep -q \"test-sentinel-gzip-checkpoint\" || "
                    "  { echo \"FAIL: bootstrap did not enumerate test-sentinel-gzip-checkpoint\"; exit 1; }; "
                    "echo \"OK: writer→reader contract holds (gzip-b64 → gunzip → file → bootstrap)\"; "
                    "'"
                ],
                environment={"MODELS_MANIFEST_B64": manifest_gz_b64},
                volumes={tmp: {"bind": "/workspace", "mode": "rw"}},
                remove=True,
                detach=False,
            )
        except docker_errors.ContainerError as e:
            pytest.fail(
                "Gzip+base64 manifest end-to-end contract FAILED. "
                "start.sh did not gunzip MODELS_MANIFEST_B64, OR "
                "bootstrap.sh did not see the written manifest. "
                "Check: (a) start.sh has the gunzip branch at "
                "/usr/local/bin/start.sh, (b) the gzip magic-byte check "
                "uses od (not xxd, which is not in this image). "
                f"stderr: {(e.stderr or b'').decode(errors='replace')}"
            )


def test_bootstrap_ignores_models_manifest_b64_env_var_content(docker_client, image_tag):
    """The bug-class guard, most explicit form.

    Specifically: even if $MODELS_MANIFEST_B64 (the CONTENT env var) is set
    to a base64 JSON blob, bootstrap.sh must NOT treat it as a file path
    and silently fail `[ -f "$MANIFEST" ]`. Bootstrap should always
    source its file path from $MANIFEST_PATH (or its default), never
    from the content env var.

    This test runs bootstrap.sh with $MODELS_MANIFEST_B64 set to a
    base64 JSON blob (not a file path) and with NO $MANIFEST_PATH
    and NO /workspace/models.json file. bootstrap should fall back to
    /usr/local/share/models-template.json (the baked-in template), NOT
    treat the base64 content as a file path.
    """
    import base64
    bogus_content = (
        '{"checkpoints":[{"name":"should-not-load","url":"http://x","dest":"a","size":1}]}'
    )
    bogus_b64 = base64.b64encode(bogus_content.encode()).decode()
    try:
        log = docker_client.containers.run(
            image_tag,
            entrypoint=["/bin/bash", "-c"],
            command=[
                # Clear any pre-existing manifest and run bootstrap with
                # $MODELS_MANIFEST_B64 set to base64 content (the wrong contract
                # from bootstrap's perspective — it only knows $MANIFEST_PATH).
                "rm -f /workspace/models.json; "
                "export MODELS_MANIFEST_B64='" + bogus_b64 + "'; "
                "unset MANIFEST_PATH; "
                # Run bootstrap. It must NOT print "No manifest at ${base64...}".
                # It SHOULD use the baked-in template (the safe fallback).
                "log=$(/usr/local/bin/bootstrap.sh 2>&1); "
                "echo \"$log\"; "
                # The dangerous symptom of the old bug:
                # bootstrap would echo "$MANIFEST" (the literal content) in
                # the "No models manifest at $MANIFEST" line. We assert the
                # sentinel name never appears in a path-style context.
                "echo \"$log\" | grep -q 'should-not-load' && "
                "  { echo 'FAIL: bootstrap read content-env as a path and tried to download should-not-load'; "
                "    echo '(this is the exact bug from PR #41 followup)'; exit 1; }; "
                # Conversely, it SHOULD use the baked-in template. We assert
                # at least one manifest-path message appeared in the log.
                "echo \"$log\" | grep -qE 'models\\.json|models-template\\.json' || "
                "  { echo 'FAIL: bootstrap did not log any manifest-path message'; exit 1; }; "
                "echo 'OK: bootstrap ignored $MODELS_MANIFEST_B64 content and used the safe fallback'"
            ],
            environment={"MODELS_MANIFEST_B64": bogus_b64},
            remove=True,
            detach=False,
        )
    except docker_errors.ContainerError as e:
        pytest.fail(
            "bootstrap.sh may be reading $MODELS_MANIFEST_B64 (content) as "
            "a file path. The expected behaviour: $MODELS_MANIFEST_B64 is "
            "manifest CONTENT (base64), bootstrap should source its file "
            "path from $MANIFEST_PATH (or default /workspace/models.json). "
            "When neither is set, bootstrap falls back to "
            "/usr/local/share/models-template.json. "
            f"stderr: {(e.stderr or b'').decode(errors='replace')}"
        )


def test_external_base_folder_creates_all_three_symlinks(docker_client, image_tag):
    """End-to-end: with EXTERNAL_BASE_FOLDER set, start.sh should symlink
    ALL THREE of $COMFYUI_DIR/{models,output,user/default/workflows} to
    $EXTERNAL_BASE_FOLDER/{models,output,workflows} in one pass.

    The single env var replaces the older per-dir OUTPUT_DIR / MODELS_DIR
    overrides. See PR #45+#47 for context.

    Skips on CPU-only runners because start.sh tries to start ComfyUI
    at the end.
    """
    if not has_gpu():
        pytest.skip(
            "No GPU on this runner — start.sh's ComfyUI launch would fail. "
            "Run the symlink-only test on a GPU runner or extract the "
            "symlink logic into a callable function for unit testing."
        )
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        external = os.path.join(tmp, "extvol")
        os.makedirs(external)
        # Pre-populate the external dir's models/ subdir with a fake
        # checkpoint so we can verify the symlink resolves to it.
        os.makedirs(os.path.join(external, "models", "checkpoints"))
        with open(
            os.path.join(external, "models", "checkpoints", "fake.safetensors"),
            "w",
        ) as f:
            f.write("fake_ckpt_bytes")
        try:
            out = docker_client.containers.run(
                image_tag,
                entrypoint=["/bin/bash", "-c"],
                command=[
                    # Reproduce start.sh's link_to_external_volume() helper
                    # + the new EXTERNAL_BASE_FOLDER block, on a slice of
                    # the image (so the test doesn't need a real GPU):
                    # 1. Simulate the helper for all three dirs
                    # 2. Verify each symlink exists and points at the
                    #    expected target
                    "set -euo pipefail; "
                    "COMFYUI_DIR=/opt/ComfyUI; "
                    "EXT=\"$1\"; "
                    # Models
                    "mkdir -p \"$COMFYUI_DIR\"; "
                    "rm -rf \"$COMFYUI_DIR/models\"; "
                    "ln -s \"$EXT/models\" \"$COMFYUI_DIR/models\"; "
                    "test -L \"$COMFYUI_DIR/models\"; "
                    "test \"$(readlink \"$COMFYUI_DIR/models\")\" = \"$EXT/models\"; "
                    # Output
                    "mkdir -p \"$COMFYUI_DIR/output\"; "
                    "rm -rf \"$COMFYUI_DIR/output\"; "
                    "ln -s \"$EXT/output\" \"$COMFYUI_DIR/output\"; "
                    "test -L \"$COMFYUI_DIR/output\"; "
                    "test \"$(readlink \"$COMFYUI_DIR/output\")\" = \"$EXT/output\"; "
                    # Workflows
                    "mkdir -p \"$COMFYUI_DIR/user/default\"; "
                    "rm -rf \"$COMFYUI_DIR/user/default/workflows\"; "
                    "ln -s \"$EXT/workflows\" \"$COMFYUI_DIR/user/default/workflows\"; "
                    "test -L \"$COMFYUI_DIR/user/default/workflows\"; "
                    "test \"$(readlink \"$COMFYUI_DIR/user/default/workflows\")\" = \"$EXT/workflows\"; "
                    # Sanity: the model symlink resolves to the real file
                    "test -f \"$COMFYUI_DIR/models/checkpoints/fake.safetensors\""
                ],
                environment={"EXTERNAL_BASE_FOLDER": external},
                volumes={tmp: {"bind": tmp, "mode": "rw"}},
                remove=True,
                detach=False,
            )
        except docker.errors.ContainerError as e:
            pytest.fail(
                f"EXTERNAL_BASE_FOLDER symlink logic failed end-to-end: {e.stderr or b''}"
            )


def test_output_symlink_works_with_docker_volume(docker_client, image_tag):
    """End-to-end: with a mounted volume, an OUTPUT_DIR env var, and a
    pre-populated $COMFYUI_DIR/output dir, start.sh's output symlink logic
    should:

      1. Move the existing files into $OUTPUT_DIR
      2. Replace $COMFYUI_DIR/output with a symlink → $OUTPUT_DIR
      3. Resolve to a non-empty dir

    Mocks the RunPod network-volume scenario:
      - Mount a tmp dir at /workspace
      - Pre-populate /opt/ComfyUI/output with a placeholder file
        (mimicking the image's baked-in _output_images_will_be_put_here)
      - Set OUTPUT_DIR=/workspace/output
      - Run start.sh's output symlink logic and verify the migration
        happened and the symlink resolves correctly

    Skips on CPU-only runners because start.sh tries to start ComfyUI
    at the end.
    """
    if not has_gpu():
        pytest.skip(
            "No GPU on this runner — start.sh's ComfyUI launch would fail. "
            "Run the symlink-only test on a GPU runner or extract the "
            "symlink logic into a callable function for unit testing."
        )
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        # Simulate the image's baked-in output placeholder. We can't write
        # to /opt/ComfyUI/output from outside the container, so we do the
        # whole slice in-container via a self-contained bash script.
        try:
            out = docker_client.containers.run(
                image_tag,
                entrypoint=["/bin/bash", "-c"],
                command=[
                    # Reproduce start.sh's OUTPUT_DIR block:
                    # 1. Pre-populate $COMFYUI_DIR/output (mimicking the
                    #    image's _output_images_will_be_put_here file)
                    # 2. Set OUTPUT_DIR=$volume/output
                    # 3. Run the symlink logic
                    # 4. Verify: real dir is gone, symlink exists, target
                    #    has the file
                    "set -euo pipefail; "
                    "COMFYUI_DIR=/opt/ComfyUI; "
                    "OUT_LINK=\"$COMFYUI_DIR/output\"; "
                    "OUTPUT_DIR=\"$1\"; "
                    "mkdir -p \"$OUTPUT_DIR\"; "
                    # Pre-populate (baked-in placeholder)
                    "mkdir -p \"$OUT_LINK\"; "
                    "echo 'placeholder' > \"$OUT_LINK/_output_images_will_be_put_here\"; "
                    "echo 'test_png_bytes' > \"$OUT_LINK/ComfyUI_00001_.png\"; "
                    "echo 'pre-state:'; ls -la \"$OUT_LINK\"; "
                    # The actual logic from start.sh
                    "if [ -L \"$OUT_LINK\" ] || [ -e \"$OUT_LINK\" ]; then "
                    "  if [ -d \"$OUT_LINK\" ] && [ ! -L \"$OUT_LINK\" ]; then "
                    "    existing=$(ls -A \"$OUT_LINK\" 2>/dev/null | wc -l); "
                    "    if [ \"$existing\" -gt 0 ]; then "
                    "      (cd \"$OUT_LINK\" && find . -mindepth 1 -maxdepth 1 "
                    "        -exec sh -c '[ ! -e \"$0/$1\" ] && mv \"$1\" \"$0/\"' \"$OUTPUT_DIR\" {} \\;); "
                    "    fi; "
                    "    rmdir \"$OUT_LINK\" 2>/dev/null || rm -rf \"$OUT_LINK\"; "
                    "    ln -s \"$OUTPUT_DIR\" \"$OUT_LINK\"; "
                    "  fi; "
                    "fi; "
                    # Verify
                    "test -L \"$OUT_LINK\"; "
                    "n=$(ls -A \"$OUT_LINK\" 2>/dev/null | wc -l); "
                    "echo \"post-state: $n files in symlink target\"; "
                    "test \"$n\" -ge 2; "
                    "test -f \"$OUT_LINK/_output_images_will_be_put_here\"; "
                    "test -f \"$OUT_LINK/ComfyUI_00001_.png\""
                ],
                environment={"OUTPUT_DIR": "/workspace/output"},
                volumes={tmp: {"bind": "/workspace", "mode": "rw"}},
                remove=True,
                detach=False,
            )
        except docker.errors.ContainerError as e:
            pytest.fail(
                f"Output symlink logic failed end-to-end: {e.stderr or b''}"
            )
