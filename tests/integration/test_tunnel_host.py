"""Opt-in real macOS lifecycle and administration, without AWS infrastructure."""

import array
import hashlib
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack, suppress
from ipaddress import IPv4Network
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from pytest import mark, raises, skip

from stelvio.tunnel import installation
from stelvio.tunnel.forwarding import Forwarding, packaged_forwarder
from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.helper_protocol import HelperOperation, HelperRequest, ResolverEndpoint
from stelvio.tunnel.processes import identity as process_identity

from .macos_host import HOLDER, cleanup_when_idle, run, wait_empty
from .test_tunnel_dns import _os_lookup
from .tunnel_dns_fixture import SocksDnsFixture

pytestmark = mark.integration_macos_privileged


def _configure(lease):
    for number, octet in enumerate((254, 253), start=1):
        lease.configure(
            unit=f"{number:08x}",
            generation=1,
            vpc_id=f"vpc-{number:08x}",
            cidrs=(f"10.{octet}.0.0/16",),
            resolvers=(
                ResolverEndpoint(f"{lease.session.replace('-', '')}-{number}.invalid", 65130),
            ),
        )


def _ready(path, process):
    deadline = time.monotonic() + 30
    while not path.exists():
        assert process.poll() is None, "Holder exited before readiness; retain holder log"
        assert time.monotonic() < deadline, "Holder readiness expired"
        time.sleep(0.1)
    return json.loads(path.read_text())


@mark.parametrize("mode", ["normal", "owner-kill", "frozen-orphan"])
def test_owner_lifecycle_cleans_without_python_request(tmp_path, mode):
    ready = tmp_path / "ready.json"
    with (tmp_path / "holder.log").open("wb") as log:
        if mode == "frozen-orphan":
            source = (
                "import subprocess,sys\n"
                'p=subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]],stdin=subprocess.PIPE)\n'
                "sys.stdin.read()\n"
            )
            args = [sys.executable, "-c", source, HOLDER, str(ready)]
        else:
            args = [sys.executable, "-c", HOLDER, str(ready)]
        parent = subprocess.Popen(  # noqa: S603 - fixed isolated holder scripts
            args, stdin=subprocess.PIPE, stdout=log, stderr=log
        )
        child_pid = None
        child_birth = None
        try:
            identity = _ready(ready, parent)
            # Inspection is intentionally restricted to the authenticated owner.
            assert identity["pid"] > 0
            if mode == "frozen-orphan":
                child_pid = identity["pid"]
                child_birth = process_identity(child_pid)
                assert child_birth is not None
                assert child_birth.parent == parent.pid
                assert child_birth.uid == os.getuid()
                # Freeze only the freshly identified child, never a reused PID.
                assert child_birth.same_process(process_identity(child_pid))
                os.kill(child_pid, signal.SIGSTOP)
            if mode == "normal":
                parent.stdin.close()
            else:
                parent.kill()
            parent.wait(timeout=30)
            wait_empty()
            if child_pid:
                current = process_identity(child_pid)
                assert current is not None
                assert child_birth.same_process(current)
                # Cleanup must precede termination of this still-live frozen child.
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=10)
            if parent.stdin and not parent.stdin.closed:
                parent.stdin.close()
            if child_birth is not None:
                current = process_identity(child_pid)
                if current is not None:
                    assert child_birth.same_process(current), "Refuse to signal a reused PID"
                    os.kill(child_pid, signal.SIGKILL)
    wait_empty()


def test_helper_restart_revokes_old_capability_and_recovers_journal(macos_host):
    lease = NativeHelper().acquire(str(uuid4()))
    try:
        _configure(lease)
        macos_host.admin("/bin/launchctl kill SIGKILL system/dev.stelvio.tunnel")
        installation._wait_ready()
        wait_empty()
        with raises((HelperError, OSError)):
            lease.remove(unit="00000001", generation=2)
    finally:
        lease.revoke()
    # A new authenticated lease succeeds after restart recovery.
    NativeHelper().acquire(str(uuid4())).close()


def test_live_lease_and_descriptor_privilege_boundaries(macos_host, tmp_path):
    helper = NativeHelper()
    lease = helper.acquire(str(uuid4()))
    try:
        _configure(lease)
        with raises(HelperError):
            helper.acquire(str(uuid4()))
        with raises(HelperError):
            cleanup_when_idle()
        with raises(HelperError):
            installation.uninstall_helper()
        with helper.connect() as connection, Path("/dev/null").open("rb") as sentinel:
            connection.sendmsg(
                [HelperRequest(HelperOperation.INSPECT).encode()],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [sentinel.fileno()]))],
            )
            connection.settimeout(3)
            with suppress(ConnectionResetError):
                assert not connection.recv(256)
            os.fstat(sentinel.fileno())
        result = tmp_path / "elevated-status"
        # Only the installed root-owned image is executed with elevation.
        macos_host.admin(
            f"set +e\n{installation.INSTALLED_HELPER} forwarder 3 >/dev/null 2>&1\n"
            f'code=$?\n/usr/bin/printf "%s" "$code" > {shlex.quote(str(result))}\n'
            f'/bin/chmod 0644 {shlex.quote(str(result))}\n[ "$code" -ne 0 ]'
        )
        assert int(result.read_text()) != 0
        assert len(helper.inspect().units) == 2
    finally:
        lease.close()


def test_hundred_two_unit_dns_route_cycles(macos_host):
    for cycle in range(100):
        with ExitStack() as scope:
            lease = NativeHelper().acquire(str(uuid4()))
            scope.callback(lease.close)
            binary = scope.enter_context(packaged_forwarder())
            forwarding = Forwarding(binary, lease.carrier)
            scope.callback(forwarding.close)
            domains = []
            for index, octet in enumerate((254, 253), start=1):
                proxy = SocksDnsFixture(f"10.{octet}.9.19")
                scope.callback(proxy.close)
                domain = f"cycle-{cycle}-{uuid4().hex}.internal"
                domains.append(domain)
                network = SimpleNamespace(
                    cidrs=(IPv4Network(f"10.{octet}.0.0/16"),), dns_domains=(domain,)
                )
                unit = f"{index:08x}"
                forwarding.add(unit, 1, 1, network, proxy.port)
                port = forwarding.configure_dns(unit, 1, 1, network, ())
                lease.configure(
                    unit=unit,
                    generation=1,
                    vpc_id=f"vpc-{index:08x}",
                    cidrs=(str(network.cidrs[0]),),
                    resolvers=(ResolverEndpoint(domain, port),),
                )
                assert _os_lookup("alias." + domain) == {proxy.server.address}
            lease.remove(unit="00000001", generation=2)
            assert _os_lookup("alias." + domains[1]) == {"10.253.9.19"}
        macos_host.assert_restored(f"cycle-{cycle:03}")


def test_foreign_resolver_is_preserved_then_owned_cleanup_recovers(macos_host):
    lease = NativeHelper().acquire(str(uuid4()))
    backup = None
    path = None
    foreign = None
    marker = f"# foreign test resolver {lease.session}\n"
    try:
        _configure(lease)
        session = lease.session.replace("-", "")
        paths = list(Path("/etc/resolver").glob(f"stelvio.{session}.00000001.*"))
        assert len(paths) == 1
        path = paths[0]
        original = path.lstat()
        backup = path.with_name(".test-backup-" + uuid4().hex)
        qpath, qbackup = shlex.quote(str(path)), shlex.quote(str(backup))
        macos_host.admin(
            f"/bin/mv {qpath} {qbackup}\n"
            f"(set -C; /usr/bin/printf %s {shlex.quote(marker)} > {qpath})\n"
            f"/bin/chmod 0644 {qpath}"
        )
        foreign = path.lstat()
        assert foreign.st_ino != original.st_ino
        lease.revoke()
        deadline = time.monotonic() + 30
        while not NativeHelper().inspect().uncertain:
            assert time.monotonic() < deadline
            time.sleep(0.1)
        with raises(HelperError):
            cleanup_when_idle()
        assert path.lstat().st_ino == foreign.st_ino
        assert path.read_text() == marker
    finally:
        lease.revoke()
        if backup and backup.exists():
            # Restore even if authorization failed after moving the original but
            # before publishing the replacement. Refuse any unrelated new file.
            remove = ""
            if path.exists() or path.is_symlink():
                assert not path.is_symlink()
                assert path.read_text() == marker
                if foreign is not None:
                    assert path.lstat().st_ino == foreign.st_ino
                remove = f"/bin/rm {qpath}\n"
            macos_host.admin(remove + f"/bin/mv {qbackup} {qpath}")
            assert path.lstat().st_ino == original.st_ino
            cleanup_when_idle()
    wait_empty()
    assert installation.INSTALLED_HELPER.exists()


def test_fresh_install_cross_venv_cleanup_and_uninstall(macos_host, tmp_path):
    installation.uninstall_helper()
    assert not installation.INSTALLED_HELPER.exists()
    installation.install_helper()
    digest = installation._installed_digest()
    wheel = os.environ.get("STLV_TEST_STELVIO_WHEEL")
    if not wheel:
        skip("Cross-venv acceptance pending: supply STLV_TEST_STELVIO_WHEEL")
    wheel = Path(wheel)
    assert wheel.is_absolute()
    assert wheel.is_file()
    for index in (1, 2):
        directory = tmp_path / f"venv-{index}"
        run(sys.executable, "-m", "venv", str(directory))
        run(
            "uv",
            "pip",
            "install",
            "--python",
            str(directory / "bin/python"),
            str(wheel),
            timeout=180,
        )
        run(
            str(directory / "bin/python"),
            "-I",
            "-c",
            "import sys,stelvio; from pathlib import Path; "
            "assert Path(stelvio.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()); "
            "from uuid import uuid4; from stelvio.tunnel.helper_client import NativeHelper; "
            "h=NativeHelper(); assert not h.inspect().owned; h.acquire(str(uuid4())).close()",
            cwd=tmp_path,
        )
        wait_empty()
        assert installation._installed_digest() == digest
    cleanup_when_idle()
    assert installation._installed_digest() == digest
    installation.uninstall_helper()
    assert not installation.INSTALLED_HELPER.exists()
    installation.install_helper()


@mark.parametrize("cut", ["staged", "retired", "published"])
def test_changed_image_refusal_idle_upgrade_and_interrupted_replacement(macos_host, tmp_path, cut):
    original = tmp_path / "candidate"
    original.write_bytes(installation.INSTALLED_HELPER.read_bytes())
    original.chmod(0o700)
    # Mach-O permits trailing data: retain version/code but exercise a different
    # digest through the production installer, without compiling Go in pytest.
    changed = tmp_path / "changed"
    changed.write_bytes(original.read_bytes() + b"\nStelvio installation regression fixture\n")
    changed.chmod(0o700)
    digest = hashlib.sha256(changed.read_bytes()).hexdigest()
    before = installation._installed_digest()
    lease = NativeHelper().acquire(str(uuid4()))
    try:
        _configure(lease)
        with raises(HelperError):
            macos_host.install(changed)
        assert installation._installed_digest() == before
        assert len(NativeHelper().inspect().units) == 2
    finally:
        lease.close()
    wait_empty()
    try:
        # Inject only in the test-owned copy of the real bootstrap shell.
        script = installation._bootstrap(changed, digest)
        needle = {
            "staged": "    /bin/sync\n",
            "retired": (
                f"/usr/bin/env -i PATH=/usr/bin:/bin {installation.INSTALLED_HELPER}"
                " helper uninstall\n"
            ),
            "published": '    /bin/ln "$stage" ' + str(installation.INSTALLED_HELPER) + "\n",
        }[cut]
        assert script.count(needle) == 1
        marker = "stelvio-test-cut-" + cut + "-" + uuid4().hex
        fault = f"/usr/bin/printf '%s\\n' {shlex.quote(marker)} >&2\nexit 73\n"
        with raises(HelperError, match=marker):
            macos_host.admin(script.replace(needle, needle + fault))
        if cut == "staged":
            assert installation._installed_digest() == before
        elif cut == "retired":
            assert not installation.INSTALLED_HELPER.exists()
        else:
            assert installation._installed_digest() == digest
        assert list(installation.INSTALLED_HELPER.parent.glob(".stelvio-traforo.bootstrap.*"))
        macos_host.install(changed)
        assert installation._installed_digest() == digest
        assert not list(installation.INSTALLED_HELPER.parent.glob(".stelvio-traforo.*"))
    finally:
        macos_host.install(original)


def test_pinned_release_to_local_candidate_upgrade(macos_host, monkeypatch):
    from stelvio.tunnel.assets import downloaded_helper

    from .test_tunnel_dns import assert_two_vpc_dns_and_packet_forwarding

    selection = macos_host.selection
    if selection.local_path is None:
        skip("Real release upgrade pending: select a local STLV_TRAFORO_DEV_BINARY candidate")
    with downloaded_helper() as release:
        if hashlib.sha256(release.read_bytes()).hexdigest() == selection.digest:
            skip("Real release upgrade pending: candidate and pinned release are identical")
        macos_host.install(release)
        release_digest = installation._installed_digest()

        def refuse_candidate():
            with raises(HelperError):
                installation.install_helper(
                    environment={
                        "STLV_TRAFORO_DEV_BINARY": str(selection.local_path),
                    }
                )
            assert installation._installed_digest() == release_digest
            assert len(NativeHelper().inspect().units) == 2

        with monkeypatch.context() as previous:
            previous.delenv("STLV_TRAFORO_DEV_BINARY", raising=False)
            assert_two_vpc_dns_and_packet_forwarding(during_session=refuse_candidate)
        wait_empty()
        installation.install_helper()
        assert installation._installed_digest() == selection.digest
    # Prove both scopes and host TCP through the new image, not only installation.
    assert_two_vpc_dns_and_packet_forwarding()


def test_legacy_migration_requires_verified_legacy_fixture(macos_host):
    name = os.environ.get("STLV_TEST_LEGACY_HELPER")
    expected = os.environ.get("STLV_TEST_LEGACY_SHA256")
    if not name or not expected:
        skip(
            "Legacy native acceptance pending: supply STLV_TEST_LEGACY_HELPER "
            "and STLV_TEST_LEGACY_SHA256"
        )
    image = Path(name)
    assert image.is_absolute()
    assert not image.is_symlink()
    assert image.is_file()
    assert hashlib.sha256(image.read_bytes()).hexdigest() == expected
    assert run(str(image), "--version").stdout.strip() == "stelvio-tunnel-helper/1"
    # The legacy artifact must install its real ownership receipt itself.
    installation.uninstall_helper()
    target = "/Library/PrivilegedHelperTools/dev.stelvio.tunnel"
    try:
        macos_host.admin(
            f"/usr/bin/install -o root -g wheel -m 0755 {shlex.quote(str(image))} {target}\n"
            f"{target} --install"
        )
        installation.install_helper()
        assert not Path(target).exists()
        wait_empty()
    finally:
        if Path(target).exists():
            macos_host.admin(f"{target} --uninstall")
        installation.install_helper()
