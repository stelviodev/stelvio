"""Routing socket noise must not reject a committed route's own ACK.

The nonprivileged native socket boundary makes short Darwin notifications and
missing acknowledgements deterministic without changing the host routing table.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from pytest import fixture, mark, skip

pytestmark = mark.skipif(sys.platform != "darwin", reason="Darwin routing message ABI")


@fixture(scope="module")
def receiver(tmp_path_factory):
    compiler = shutil.which("clang")
    if not compiler:
        skip("Native routing ACK validation requires clang")
    root = Path(__file__).parents[2] / "stelvio/tunnel/native"
    binary = tmp_path_factory.mktemp("route-reply") / "check-route-reply"
    subprocess.run(  # noqa: S603 - fixed native sources, nonprivileged socket fixture
        [
            compiler,
            "-std=c17",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(root / "route_reply.c"),
            str(root / "check_route_reply.c"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return binary


@mark.parametrize(
    ("scenario", "accepted"),
    [
        ("success", True),
        ("notifications", True),
        ("foreign", True),
        ("length", False),
        ("version", False),
        ("error", False),
        ("short-add", False),
        ("truncated", False),
        ("foreign-only", False),
        ("sequence-only", False),
        ("missing", False),
    ],
)
def test_route_add_accepts_only_its_successful_ack(receiver, scenario, accepted):
    result = subprocess.run(  # noqa: S603 - own compiled nonprivileged harness
        [str(receiver), scenario], check=False, capture_output=True, timeout=5
    )
    assert result.returncode == (0 if accepted else 1)
    assert result.stdout == result.stderr == b""
