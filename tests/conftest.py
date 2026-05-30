import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--image-tag",
        action="store",
        default=None,
        help="Docker image tag to test (default: comfyui-anime-bootstrap:test)",
    )


def pytest_generate_tests(metafunc):
    if "image_tag" in metafunc.fixturenames:
        tag = metafunc.config.getoption("--image-tag")
        metafunc.parametrize("image_tag", [tag], scope="session")
