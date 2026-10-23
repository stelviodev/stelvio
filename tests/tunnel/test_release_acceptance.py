"""Opt-in real release-asset validation, with no helper installation or host changes."""

import hashlib
import json
import os
import platform
import subprocess

from pytest import mark, skip

from stelvio.tunnel import assets

pytestmark = mark.skipif(
    os.environ.get("STLV_TEST_TRAFORO_RELEASE_DOWNLOADS") != "1",
    reason="Live release acceptance needs STLV_TEST_TRAFORO_RELEASE_DOWNLOADS=1",
)

LIBRARIES = {
    "/usr/lib/libSystem.B.dylib",
    "/usr/lib/libresolv.9.dylib",
    "/usr/lib/libbsm.0.dylib",
    "/System/Library/Frameworks/SystemConfiguration.framework/Versions/A/SystemConfiguration",
    "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
    "/System/Library/Frameworks/Security.framework/Versions/A/Security",
}


@mark.parametrize("architecture", ["arm64", "x86_64"])
def test_actual_pinned_release_and_verified_cache(tmp_path, monkeypatch, architecture):
    if platform.system() != "Darwin":
        skip("Mach-O execution acceptance needs macOS")
    host = platform.machine()
    pending = None
    if host == "x86_64" and architecture == "arm64":
        pending = "Native arm64 release execution pending: use an Apple Silicon Mac"
    if host == "arm64" and architecture == "x86_64":
        result = subprocess.run(
            ["/usr/bin/arch", "-x86_64", "/usr/bin/true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            pending = "amd64 release execution pending: install Rosetta"
    if pending:
        # Keep production HTTP/cache/hash checks; do not execute an unsupported ISA.
        monkeypatch.setattr(assets, "validate_version", lambda path: "execution pending")
    monkeypatch.setattr(assets, "_architecture", lambda: architecture)
    monkeypatch.setattr(assets, "get_bin_path", lambda: tmp_path)
    path = assets.ensure_traforo()
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert digest == assets.TARGETS[architecture]["sha256"]
    assets.validate_macho(data, architecture)
    if not pending:
        assert assets.validate_version(path) == "v" + assets.TRAFORO_VERSION
    output = subprocess.run(  # noqa: S603 - fixed native binary inspector
        ["/usr/bin/otool", "-L", str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    libraries = {line.strip().split(" (", 1)[0] for line in output.splitlines()[1:]}
    assert "/usr/lib/libSystem.B.dylib" in libraries
    assert libraries <= LIBRARIES
    original = path.stat()

    def forbidden(*args, **kwargs):
        raise AssertionError("Verified release cache attempted another download")

    monkeypatch.setattr(assets.requests, "get", forbidden)
    assert assets.ensure_traforo() == path
    assert path.stat().st_ino == original.st_ino
    (tmp_path / "release-evidence.json").write_text(
        json.dumps(
            {
                "architecture": architecture,
                "sha256": digest,
                "bytes": len(data),
                "libraries": sorted(libraries),
                "os": platform.mac_ver()[0],
                "host": host,
                "execution": pending or "passed",
            },
            indent=2,
        )
    )
    if pending:
        skip(pending + "; structural validation passed (see release-evidence.json)")
