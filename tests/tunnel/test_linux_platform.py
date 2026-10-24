"""Linux trust boundaries use ELF, native credentials, and POSIX ACL metadata."""

import errno
import struct
from unittest.mock import Mock

import pytest

from stelvio.tunnel import assets, helper_client, linux_platform, linux_plugin


def elf(machine=183):
    header = b"\x7fELF\x02\x01\x01" + bytes(9)
    header += struct.pack("<HHIQQQIHHHHHH", 2, machine, 1, 0x400078, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    segment = struct.pack("<IIQQQQQQ", 1, 5, 0, 0x400000, 0, 121, 121, 4096)
    return header + segment + b"\xc3"


def test_elf_requires_matching_architecture_and_executable_entry():
    linux_platform.validate_elf(elf(), "arm64", limit=1024)
    for data in (elf(62), elf()[:80], elf()[:24] + bytes(8) + elf()[32:]):
        with pytest.raises(ValueError, match="ELF"):
            linux_platform.validate_elf(data, "arm64", limit=1024)


def test_linux_unpublished_release_requires_explicit_override(monkeypatch):
    monkeypatch.setattr(assets.platform, "system", lambda: "Linux")
    monkeypatch.setattr(assets.platform, "machine", lambda: "aarch64")
    assert assets._architecture() == "arm64"
    with pytest.raises(assets.NativeAssetError, match="STLV_TRAFORO_DEV_BINARY"):
        assets.select_traforo({})


def test_linux_acl_absence_is_distinct_from_inspection_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        linux_platform.os,
        "getxattr",
        Mock(side_effect=OSError(errno.ENODATA, "none")),
        raising=False,
    )
    linux_platform.no_acl(tmp_path)
    monkeypatch.setattr(linux_platform.os, "getxattr", lambda *args, **kwargs: b"acl")
    with pytest.raises(ValueError, match="ACL"):
        linux_platform.no_acl(tmp_path)
    monkeypatch.setattr(linux_platform.os, "getxattr", Mock(side_effect=PermissionError("denied")))
    with pytest.raises(PermissionError):
        linux_platform.no_acl(tmp_path)


def test_linux_helper_authenticates_server_uid(monkeypatch):
    monkeypatch.setattr(helper_client.sys, "platform", "linux")
    monkeypatch.setattr(helper_client.socket, "SO_PEERCRED", 17, raising=False)
    connection = Mock()
    connection.getsockopt.return_value = struct.pack("3i", 123, 0, 0)
    helper_client._root_peer(connection)
    connection.getsockopt.return_value = struct.pack("3i", 123, 502, 1000)
    with pytest.raises(helper_client.HelperError, match="root server"):
        helper_client._root_peer(connection)


def test_debian_package_rejects_ambiguous_or_unbounded_input():
    from stelvio.tunnel.plugin import PluginAssetError

    for data in (b"", b"!<arch>\n", b"!<arch>\n" + b"x" * 60):
        with pytest.raises((PluginAssetError, ValueError)):
            linux_plugin.executable(data)
