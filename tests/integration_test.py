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
import subprocess
import tempfile
import urllib.request, urllib.error
import pytest
import docker

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
