"""Classify actual write prefixes without treating complete corrupt files as partial."""

import shutil
import struct
import subprocess
from pathlib import Path

from pytest import fixture, mark, skip


@fixture(scope="module")
def classifier(tmp_path_factory):
    compiler = shutil.which("clang")
    if not compiler:
        skip("Native framing checks require clang")
    root = Path(__file__).parents[2] / "stelvio/tunnel/native"
    binary = tmp_path_factory.mktemp("journal-format") / "check-format"
    subprocess.run(  # noqa: S603 - fixed pure C harness
        [
            compiler,
            "-std=c17",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(root / "journal_format.c"),
            str(root / "check_journal_format.c"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return binary


HEADER = b"STLVJNL1" + struct.pack("!II", 100, 0x12345678)


@mark.parametrize("size", [*range(16), 16, 17, 64, 115])
def test_every_header_write_prefix_and_partial_body_is_recoverable(classifier, size):
    result = subprocess.run(  # noqa: S603 - fixed pure harness
        [str(classifier)],
        input=struct.pack("!Q", size) + HEADER[: min(size, 16)],
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""


@mark.parametrize(
    ("size", "header"),
    [
        (116, HEADER),
        (117, HEADER),
        (0xFFFFFFFFFFFFFFFF, HEADER),
        (4, b"STLVJNL1"),
        (16, HEADER[:15]),
        (1, b"X"),
        (8, b"STLVJNL2"),
        (16, b"STLVJNL1" + struct.pack("!II", 0, 0)),
        (16, b"STLVJNL1" + struct.pack("!II", 512 * 1024 + 1, 0)),
        (9, b"STLVJNL1\x01"),
        (10, b"STLVJNL1\x00\x09"),
        (17, b"STLVJNL1" + struct.pack("!II", 1, 0)),
    ],
)
def test_complete_files_and_foreign_or_impossible_prefixes_are_retained(classifier, size, header):
    # A complete file with an invalid checksum remains complete: this classifier
    # cannot authorize its disposal. Header/body checks belong to journal read.
    result = subprocess.run(  # noqa: S603 - fixed pure harness
        [str(classifier)],
        input=struct.pack("!Q", size) + header,
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""
