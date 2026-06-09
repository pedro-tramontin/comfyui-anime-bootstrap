"""Tests for the download_client bootstrap.sh patch (civitai b2 redirect fix).
Date: 2026-06-09
"""
import os
import re
import subprocess
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOTSTRAP_SH = os.path.join(REPO_ROOT, "bootstrap.sh")


def _read_bootstrap():
    with open(BOOTSTRAP_SH) as f:
        return f.read()


def test_bootstrap_sh_syntax():
    """bootstrap.sh must pass bash -n."""
    r = subprocess.run(["bash", "-n", BOOTSTRAP_SH], capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n failed: {r.stderr}"


def test_download_file_accepts_download_client_arg():
    """download_file() must accept a 5th $5 = download_client argument."""
    src = _read_bootstrap()
    assert 'local download_client="${5:-aria2c}"' in src, (
        "download_file() does not accept $5 download_client arg"
    )


def test_bootstrap_jq_reads_download_client_field():
    """The manifest loop must extract .download_client via jq."""
    src = _read_bootstrap()
    assert ".download_client // \"aria2c\"" in src, (
        "jq extraction of .download_client not found in bootstrap.sh"
    )


def test_bootstrap_call_passes_download_client():
    """download_file call must pass the new 5th argument."""
    src = _read_bootstrap()
    assert (
        'download_file "$name" "$url" "$dest" "$auth_type" "$download_client"'
        in src
    ), "download_file call missing $download_client arg"


def test_aria2c_invocation_is_inside_else_branch():
    """When download_client=wget, the aria2c call must be in the else branch."""
    src = _read_bootstrap()
    if_start = src.find('if [ "$download_client" = "wget" ]')
    assert if_start > 0, "wget branch not found"
    else_off = src.find("    else\n", if_start)
    assert else_off > 0, "else branch not found inside wget if"
    aria_off = src.find('"  Downloading $name (${auth_type:-public}, aria2c)..."', if_start)
    assert aria_off > else_off, "aria2c invocation is not inside the else branch"
    # Find the matching fi
    fi_off = src.find("\n    fi\n", aria_off)
    assert fi_off > 0, "matching fi for wget branch not found"


def test_fallback_message_honest_for_wget_path():
    """The '[fallback] aria2c failed' message must only appear when aria2c actually ran."""
    src = _read_bootstrap()
    fb = src.find('echo "  [fallback] aria2c failed')
    assert fb > 0, "fallback echo line not found"
    # The line should be inside an if/else gated on download_client
    context = src[max(0, fb - 200):fb]
    assert 'if [ "$download_client" = "wget" ]' in context, (
        "fallback message is not gated on download_client"
    )


def test_download_client_wget_does_not_invoke_aria2c():
    """Live test: with download_client=wget, aria2c must NOT be invoked.

    Mocks both wget and aria2c in PATH; aria2c touches a marker file
    if called. Verifies the marker is absent after the run.
    """
    src = _read_bootstrap()
    # Extract the download_file function: from the line starting with
    # 'download_file() {' to the next '}' at column 0.
    fn_text = re.search(
        r"^download_file\(\) \{.*?^\}",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert fn_text, "download_file function not found"

    with tempfile.TemporaryDirectory() as tmp:
        mock_bin = os.path.join(tmp, "bin")
        os.makedirs(mock_bin)
        aria_marker = os.path.join(tmp, "ARIA2C_WAS_CALLED")
        out_file = os.path.join(tmp, "out.safetensors")
        # aria2c mock: create marker, exit 0
        with open(os.path.join(mock_bin, "aria2c"), "w") as f:
            f.write("#!/bin/bash\ntouch %s\nexit 0\n" % aria_marker)
        os.chmod(os.path.join(mock_bin, "aria2c"), 0o755)
        # wget mock: extract -O arg, create 10KB file
        wget_script = (
            "#!/bin/bash\n"
            "out=$(echo \"$@\" | sed -n 's/.*-O \\([^ ]*\\).*/\\1/p')\n"
            "dd if=/dev/zero of=\"$out\" bs=1024 count=10 2>/dev/null\n"
            "exit 0\n"
        )
        with open(os.path.join(mock_bin, "wget"), "w") as f:
            f.write(wget_script)
        os.chmod(os.path.join(mock_bin, "wget"), 0o755)

        env = os.environ.copy()
        env["PATH"] = mock_bin + ":" + env["PATH"]
        env["CIVITAI_API_KEY"] = "testkey"
        env["HF_TOKEN"] = ""

        harness_path = os.path.join(tmp, "harness.sh")
        with open(harness_path, "w") as f:
            f.write(fn_text.group(0) + "\n")
            f.write(
                'download_file "test-model" "https://fake.example/m.safetensors" '
                '"%s" "civitai" "wget"\n' % out_file
            )
            f.write('echo "result: $?"\n')

        r = subprocess.run(
            ["bash", harness_path],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert not os.path.exists(aria_marker), (
            "aria2c was invoked for download_client=wget "
            "(should have been skipped)\n"
            "stdout: %s\nstderr: %s" % (r.stdout, r.stderr)
        )
        assert os.path.exists(out_file), (
            "wget mock did not write output file.\n"
            "stdout: %s\nstderr: %s" % (r.stdout, r.stderr)
        )
        assert "[wget] downloading" in r.stdout, (
            "wget branch message not found. stdout: %s" % r.stdout
        )
        assert "(civitai, aria2c)" not in r.stdout, (
            "wget path incorrectly mentions aria2c. stdout: %s" % r.stdout
        )


def test_download_client_aria2c_invokes_aria2c():
    """Live test: with download_client=aria2c (default), aria2c IS invoked."""
    src = _read_bootstrap()
    fn_text = re.search(
        r"^download_file\(\) \{.*?^\}",
        src,
        re.MULTILINE | re.DOTALL,
    )

    with tempfile.TemporaryDirectory() as tmp:
        mock_bin = os.path.join(tmp, "bin")
        os.makedirs(mock_bin)
        aria_marker = os.path.join(tmp, "ARIA2C_WAS_CALLED")
        out_file = os.path.join(tmp, "out.safetensors")
        with open(os.path.join(mock_bin, "aria2c"), "w") as f:
            f.write(
                "#!/bin/bash\n"
                "touch %s\n"
                # Find --dir= and --out= and create a dummy file at dir/out
                "dir=$(echo \"$@\" | sed -n 's/.*--dir=\\([^ ]*\\).*/\\1/p')\n"
                "name=$(echo \"$@\" | sed -n 's/.*--out=\\([^ ]*\\).*/\\1/p')\n"
                "dd if=/dev/zero of=\"$dir/$name\" bs=1024 count=10 2>/dev/null\n"
                "exit 0\n" % aria_marker
            )
        os.chmod(os.path.join(mock_bin, "aria2c"), 0o755)
        with open(os.path.join(mock_bin, "wget"), "w") as f:
            f.write("#!/bin/bash\necho wget should not be called for aria2c path\nexit 1\n")
        os.chmod(os.path.join(mock_bin, "wget"), 0o755)

        env = os.environ.copy()
        env["PATH"] = mock_bin + ":" + env["PATH"]
        env["CIVITAI_API_KEY"] = "testkey"
        env["HF_TOKEN"] = ""

        harness_path = os.path.join(tmp, "harness.sh")
        with open(harness_path, "w") as f:
            f.write(fn_text.group(0) + "\n")
            f.write(
                'download_file "test-model" "https://fake.example/m.safetensors" '
                '"%s" "civitai" "aria2c"\n' % out_file
            )

        r = subprocess.run(
            ["bash", harness_path],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        assert os.path.exists(aria_marker), (
            "aria2c was NOT invoked for download_client=aria2c\n"
            "stdout: %s\nstderr: %s" % (r.stdout, r.stderr)
        )
        assert os.path.exists(out_file), (
            "output file not created. stdout: %s\nstderr: %s" % (r.stdout, r.stderr)
        )
