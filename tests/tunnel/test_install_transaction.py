"""Execute the privileged bootstrap with controlled identity/service boundaries.

System file operations affect only the temporary tree; fake helper images model
safe native uninstall refusal/publication without executing Go from pytest.
"""

import shlex
import subprocess
import sys
from hashlib import sha256

from pytest import fixture, mark

from stelvio.tunnel import installation

pytestmark = mark.skipif(sys.platform != "darwin", reason="Darwin privileged bootstrap")


@fixture
def transaction(tmp_path, monkeypatch):
    library = tmp_path / "Library"
    tools = library / "PrivilegedHelperTools"
    tools.mkdir(parents=True)
    tools.chmod(0o755)
    installed = tools / "stelvio-traforo"
    receipt = tmp_path / "receipt-present"
    receipt.touch()
    active = tmp_path / "active"
    result = tmp_path / "published"
    # Root identity and receipt validation are native boundaries, tested on macOS
    # acceptance. All other bootstrap checks and filesystem operations run here.
    stat = tmp_path / "stat"
    stat.write_text(
        '#!/bin/sh\nif [ "$2" = "%u" ]; then echo 0; else exec /usr/bin/stat "$@"; fi\n'
    )
    xattr = tmp_path / "xattr"
    xattr.write_text(f"#!/bin/sh\n[ -e {shlex.quote(str(receipt))} ]\n")
    stat.chmod(0o755)
    xattr.chmod(0o755)

    def image(path, *, version="v0.0.7"):
        path.write_text(
            '#!/bin/sh\ncase "$*" in\n'
            f'"version") echo {shlex.quote(version)};;\n'
            f'"helper uninstall") [ ! -e {shlex.quote(str(active))} ] || exit 19; '
            '/bin/rm "$0";;\n'
            f'"helper install") /usr/bin/touch {shlex.quote(str(result))};;\n'
            "*) exit 2;;\nesac\n"
        )
        path.chmod(0o755)

    asset = tmp_path / "new asset"
    image(asset)
    # Make the old and new image byte identities distinct while behavior matches.
    image(installed)
    installed.write_text(installed.read_text() + "# previous release\n")
    digest = sha256(asset.read_bytes()).hexdigest()

    def script():
        return (
            installation._bootstrap(asset, digest)
            .replace("/Library", str(library))
            .replace("/usr/bin/stat", shlex.quote(str(stat)))
            .replace("/usr/bin/xattr", shlex.quote(str(xattr)))
            .replace("/bin/sync", "/usr/bin/true")
        )

    def execute(text):
        return subprocess.run(  # noqa: S603 - controlled fixture shell and temporary tree
            ["/bin/sh", "-c", text], capture_output=True, text=True, timeout=10, check=False
        )

    return dict(  # noqa: C408 - named fixture boundaries
        image=image,
        installed=installed,
        asset=asset,
        digest=digest,
        script=script,
        execute=execute,
        active=active,
        receipt=receipt,
        result=result,
        tools=tools,
    )


@mark.parametrize("interrupted", [False, True])
def test_idle_upgrade_and_interrupted_retirement_resume(transaction, interrupted):
    world = transaction
    script = world["script"]()
    if interrupted:
        interrupted_script = script.replace(" helper uninstall\n", " helper uninstall\nexit 73\n")
        first = world["execute"](interrupted_script)
        assert first.returncode == 73, first.stderr
        assert not world["installed"].exists()
        assert not world["result"].exists()
    result = world["execute"](script)
    assert result.returncode == 0, result.stderr
    assert world["installed"].read_bytes() == world["asset"].read_bytes()
    assert world["result"].exists()
    assert not list(world["tools"].glob(".stelvio-traforo.bootstrap.*"))


@mark.parametrize("refusal", ["active", "foreign"])
def test_refused_retirement_preserves_installed_image(transaction, refusal):
    world = transaction
    before = world["installed"].read_bytes()
    if refusal == "active":
        world["active"].touch()
    else:
        world["receipt"].unlink()
    result = world["execute"](world["script"]())
    assert result.returncode != 0
    assert world["installed"].read_bytes() == before
    assert not world["result"].exists()


@mark.parametrize(
    "version",
    [
        "v0.0.7",
        "v0.0.7-dev+gabcdef123456",
        "stelvio-traforo/1 helper=1 carrier=1",
        "foreign-image",
    ],
)
def test_retirement_accepts_owned_semver_but_refuses_unknown(transaction, version):
    world = transaction
    world["image"](world["installed"], version=version)
    before = world["installed"].read_bytes()
    result = world["execute"](world["script"]())
    if not version.startswith("v"):
        assert result.returncode != 0
        assert world["installed"].read_bytes() == before
        assert not world["result"].exists()
    else:
        assert result.returncode == 0, result.stderr
        assert world["installed"].read_bytes() == world["asset"].read_bytes()
