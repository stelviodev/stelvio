"""Test inspection of persistent and interrupted native administration files."""

import os

import pytest

from tests.integration.macos_host import inspect_administration_artifacts


def inspect(directory, *, ownership=True, acl=True):
    return inspect_administration_artifacts(
        directory,
        root_owner=lambda _info: ownership,
        acl_free=lambda _path: acl,
    )


def test_no_administration_artifacts_is_clean(tmp_path):
    assert inspect(tmp_path) == ()


def test_valid_persistent_lock_is_not_a_transaction_remnant(tmp_path):
    lock = tmp_path / ".stelvio-traforo.admin.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)

    assert inspect(tmp_path) == ()


@pytest.mark.parametrize("unsafe", ["symlink", "mode", "owner", "acl", "hardlink"])
def test_unsafe_persistent_lock_fails_validation(tmp_path, unsafe):
    lock = tmp_path / ".stelvio-traforo.admin.lock"
    target = tmp_path / "target"
    target.write_text("lock")
    if unsafe == "symlink":
        lock.symlink_to(target)
    else:
        lock.touch(mode=0o600)
        lock.chmod(0o600)
        if unsafe == "mode":
            lock.chmod(0o640)
        elif unsafe == "hardlink":
            os.link(lock, tmp_path / "second-link")

    with pytest.raises(AssertionError, match="unsafe"):
        inspect(
            tmp_path,
            ownership=unsafe != "owner",
            acl=unsafe != "acl",
        )


@pytest.mark.parametrize(
    "name",
    [".stelvio-traforo.retired", ".stelvio-traforo.bootstrap." + "a" * 64],
)
def test_known_transaction_files_require_recovery(tmp_path, name):
    artifact = tmp_path / name
    artifact.touch(mode=0o600)
    artifact.chmod(0o600)

    assert inspect(tmp_path) == (artifact,)


def test_valid_lock_and_transaction_file_require_recovery(tmp_path):
    lock = tmp_path / ".stelvio-traforo.admin.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    transaction = tmp_path / (".stelvio-traforo.bootstrap." + "b" * 64)
    transaction.touch(mode=0o600)
    transaction.chmod(0o600)

    assert inspect(tmp_path) == (transaction,)


def test_unknown_matching_file_fails_closed(tmp_path):
    (tmp_path / ".stelvio-traforo.unknown").touch()

    with pytest.raises(AssertionError, match="Unknown Traforo administration artifact"):
        inspect(tmp_path)


def test_symlinked_administration_directory_fails_closed(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    directory = tmp_path / "PrivilegedHelperTools"
    directory.symlink_to(target, target_is_directory=True)

    with pytest.raises(AssertionError, match="directory is unsafe"):
        inspect(directory)
