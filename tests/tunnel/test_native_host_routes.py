"""Route-table growth retries never certify partial or foreign ownership."""

import shutil
import subprocess
import sys
from pathlib import Path

from pytest import fixture, mark, skip

pytestmark = mark.skipif(sys.platform != "darwin", reason="Darwin routing snapshot ABI")


@fixture(scope="module")
def inspector(tmp_path_factory):
    compiler = shutil.which("clang")
    if not compiler:
        skip("Native routing snapshot checks require clang")
    root = Path(__file__).parents[2] / "stelvio/tunnel/native"
    binary = tmp_path_factory.mktemp("host-routes") / "check-host-routes"
    subprocess.run(  # noqa: S603 - fixed native sources with nonprivileged syscall fixture
        [
            compiler,
            "-std=c17",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(root / "host.c"),
            str(root / "check_host_routes.c"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return binary


@mark.parametrize(
    ("scenario", "status"),
    [
        ("owned", 0),
        ("growth", 0),
        ("growth-foreign", 1),
        ("growth-malformed", 2),
        ("always-growth", 2),
        ("oversized", 2),
        ("permission", 2),
    ],
)
def test_route_snapshot_only_certifies_complete_owned_copy(inspector, scenario, status):
    result = subprocess.run(  # noqa: S603 - own compiled nonprivileged syscall fixture
        [str(inspector), scenario], check=False, capture_output=True, timeout=3
    )
    assert result.returncode == status
    assert result.stdout == result.stderr == b""
