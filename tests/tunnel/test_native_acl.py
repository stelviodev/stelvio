"""macOS ACL permissions must not hide behind restrictive POSIX modes."""

import os
import subprocess
import sys

from pytest import fixture, mark

pytestmark = mark.skipif(sys.platform != "darwin", reason="macOS extended ACL semantics")


@fixture(scope="module")
def acl_checker(traforo_checker):
    return traforo_checker("acl")


def _check(binary, path):
    result = subprocess.run(  # noqa: S603 - fixed read-only native harness
        [str(binary), str(path)],
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    return result.stdout


@mark.parametrize("directory", [False, True])
def test_plain_restrictive_file_and_directory_have_no_extended_entries(
    acl_checker, tmp_path, directory
):
    path = tmp_path / "plain"
    if directory:
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(b"")
        path.chmod(0o600)
    assert _check(acl_checker, path) == b"1 1\n"


def test_allow_acl_is_rejected_even_when_mode_stays_private(acl_checker, tmp_path):
    path = tmp_path / "acl-fixture"
    path.write_bytes(b"")
    path.chmod(0o600)
    subprocess.run(  # noqa: S603 - system chmod affects only empty user-owned fixture
        ["/bin/chmod", "+a", "everyone allow read,write", str(path)],
        check=True,
        capture_output=True,
        timeout=3,
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert _check(acl_checker, path) == b"0 0\n"


def test_parent_acl_can_reach_new_private_file_without_mode_changes(acl_checker, tmp_path):
    parent = tmp_path / "inherited"
    parent.mkdir(mode=0o700)
    subprocess.run(  # noqa: S603 - only empty user-owned isolated fixture directory
        [
            "/bin/chmod",
            "+a",
            "everyone allow read,write,file_inherit,directory_inherit",
            str(parent),
        ],
        check=True,
        capture_output=True,
        timeout=3,
    )
    assert _check(acl_checker, parent) == b"0 0\n"
    descriptor = os.open(parent / "secret", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    assert (parent / "secret").stat().st_mode & 0o777 == 0o600
    assert _check(acl_checker, parent / "secret") == b"0 0\n"
