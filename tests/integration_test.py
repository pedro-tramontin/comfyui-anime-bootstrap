"""
Integration tests for comfyui-anime-bootstrap Docker image.

These tests use the docker Python SDK (testcontainers-style) to verify:
1. The container builds and boots
2. SSH daemon accepts connections
3. ComfyUI API responds on port 8188
4. Model directories are correctly set up

Run locally:
    pip install -r tests/requirements.txt
    docker build -t comfyui-anime-bootstrap:test .
    pytest tests/integration_test.py -v

Run in CI (after image build):
    pytest tests/integration_test.py -v --image-tag=ghcr.io/pedro-tramontin/comfyui-anime-bootstrap:pr-123
"""

import time
import socket
import urllib.request, urllib.error
import pytest
import docker

IMAGE_NAME_DEFAULT = "comfyui-anime-bootstrap:test"
COMFY_PORT = 8188
SSH_PORT = 22
BOOT_TIMEOUT = 120  # seconds for ComfyUI to start
SSH_TIMEOUT = 30    # seconds for SSH to be ready


@pytest.fixture(scope="session")
def docker_client():
    return docker.from_env()


@pytest.fixture(scope="session")
def image_tag(request):
    """Allow overriding the image tag via CLI: pytest --image-tag=foo."""
    return request.config.getoption("--image_tag", IMAGE_NAME_DEFAULT)


@pytest.fixture(scope="session")
def container(docker_client, image_tag):
    """Spin up the container, yield it, then teardown."""
    ctr = docker_client.containers.run(
        image_tag,
        detach=True,
        name="comfyui-test-" + str(int(time.time())),
        ports={
            f"{COMFY_PORT}/tcp": ("127.0.0.1", 0),  # random host port
            f"{SSH_PORT}/tcp": ("127.0.0.1", 0),
        },
        environment={"DEBIAN_FRONTEND": "noninteractive"},
        # No GPU needed for integration tests — we test boot logic, not inference
        runtime=None,
        stdout=True,
        stderr=True,
    )
    try:
        yield ctr
    finally:
        ctr.stop(timeout=10)
        ctr.remove(force=True)


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


def test_container_starts(container):
    """Container reaches running state."""
    container.reload()
    assert container.status in ("running", "created")


def test_ssh_port_reachable(ssh_host_port):
    """The SSH daemon inside the container accepts TCP connections."""
    assert wait_for_port("127.0.0.1", ssh_host_port, timeout=SSH_TIMEOUT), \
        "SSH never came up"


def test_comfyui_port_reachable(comfy_host_port):
    """ComfyUI listens on 8188 and accepts HTTP."""
    assert wait_for_port("127.0.0.1", comfy_host_port, timeout=BOOT_TIMEOUT), \
        "ComfyUI port never opened"


def test_comfyui_api_returns_ok(comfy_host_port):
    """ComfyUI root path returns a valid HTTP response (redirect or 200)."""
    url = f"http://127.0.0.1:{comfy_host_port}/system_stats"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            resp = urllib.request.urlopen(req, timeout=5)
            assert resp.status == 200
            return
        except urllib.error.HTTPError as e:
            # 301/302 redirect is also fine for ComfyUI index
            if e.code in (301, 302, 307, 308):
                return
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    pytest.fail("ComfyUI API did not return a valid HTTP response")


def test_workspace_directories_exist(container):
    """The /workspace mount structure was created in Dockerfile."""
    exit_code, output = container.exec_run("ls -d /workspace/ComfyUI /workspace/models /workspace/output /workspace/input")
    assert exit_code == 0, f"Missing directories: {output.decode()}"


def test_bootstrap_sh_is_executable(container):
    """The bootstrap script landed in the right place."""
    exit_code, _ = container.exec_run("test -x /usr/local/bin/bootstrap.sh")
    assert exit_code == 0


def test_models_template_shipped(container):
    """models-template.json is baked into the image for first-time users."""
    exit_code, _ = container.exec_run("test -f /usr/local/share/models-template.json")
    assert exit_code == 0


def test_ssh_hostkeys_not_baked(container):
    """SSH hostkeys must be generated at runtime, not in the Docker layer."""
    exit_code, output = container.exec_run("stat -c '%Y' /etc/ssh/ssh_host_ed25519_key")
    assert exit_code == 0
    key_mtime = int(output.decode().strip())
    # Container uptime tells us when it started
    # If key is younger than ~300s, it was generated at runtime
    assert key_mtime > (time.time() - 300), "SSH hostkey appears to be baked into image"
