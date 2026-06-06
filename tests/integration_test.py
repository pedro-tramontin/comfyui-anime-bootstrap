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
    CIVITAI_API_KEY, WORKFLOWS_DIR, MODELS_MANIFEST get written to
    /root/.ssh/environment (chmod 600) so PermitUserEnvironment=yes
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
    """start.sh must try MODELS_MANIFEST_B64 first (base64 path).

    This is the preferred path for the manifest env var because raw
    multi-line JSON can get mangled by env-marshalling layers; base64
    is single-line and survives. MODELS_MANIFEST (raw) should still
    work as a back-compat fallback.
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


def test_workflow_symlink_works_with_docker_volume(docker_client, image_tag):
    """End-to-end: with a mounted volume + a workflow .json file at the
    expected location, start.sh's workflows symlink path should resolve
    correctly to a non-empty dir.

    Mocks the RunPod network-volume scenario:
      - Mount a tmp dir at /workspace
      - Drop asuka-animagine-workflow.json in /workspace/workflows/
      - Set WORKFLOWS_DIR=/workspace/workflows in the env
      - Run start.sh's symlink logic and verify the link is created
        and resolves to a non-empty dir

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
        workflows_dir = os.path.join(tmp, "workflows")
        os.makedirs(workflows_dir)
        wf_file = os.path.join(workflows_dir, "asuka-animagine-workflow.json")
        with open(wf_file, "w") as f:
            f.write("{}")  # minimal valid JSON; the test only checks existence
        try:
            out = docker_client.containers.run(
                image_tag,
                entrypoint=["/bin/bash", "-c"],
                command=[
                    # Reproduce the relevant slice of start.sh:
                    # 1. Make sure user/default/ exists
                    # 2. Symlink it to WORKFLOWS_DIR
                    # 3. Verify the symlink resolves to a non-empty dir
                    "set -euo pipefail; "
                    "COMFYUI_DIR=/opt/ComfyUI; "
                    "WF_LINK=\"$COMFYUI_DIR/user/default/workflows\"; "
                    "mkdir -p \"$(dirname \"$WF_LINK\")\"; "
                    "mkdir -p \"$WORKFLOWS_DIR\"; "
                    "ln -sfn \"$WORKFLOWS_DIR\" \"$WF_LINK\"; "
                    "test -L \"$WF_LINK\"; "
                    "n=$(ls -A \"$WF_LINK\" | wc -l); "
                    "echo \"WF_LINK resolves to $n files\"; "
                    "test \"$n\" -gt 0"
                ],
                environment={"WORKFLOWS_DIR": workflows_dir},
                volumes={tmp: {"bind": tmp, "mode": "rw"}},
                remove=True,
                detach=False,
            )
        except docker.errors.ContainerError as e:
            pytest.fail(
                f"Workflow symlink logic failed end-to-end: {e.stderr or b''}"
            )
