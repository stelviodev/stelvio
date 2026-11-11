"""Exercise remote-probe ownership and forwarding without AWS or privileged changes."""

import base64
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

from pytest import fixture, mark, raises

from . import bastion_probe as probe

_PUBLIC = (
    "ssh-ed25519 "
    + base64.b64encode(b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + bytes(range(32))).decode()
)
_POSIX = mark.skipif(os.name != "posix", reason="Linux authorization and POSIX ownership")


@fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "relabel", lambda path: None)
    return tmp_path


def _authorization(home):
    return probe.AuthorizedKey(home, os.getuid(), os.getgid(), _PUBLIC, "unique-test")


@_POSIX
@mark.parametrize("original", [b"", b"existing key\n", b"existing key without newline"])
def test_authorization_restores_existing_bytes_inode_and_metadata(home, original):
    directory = home / ".ssh"
    directory.mkdir(mode=0o700)
    path = directory / "authorized_keys"
    path.write_bytes(original)
    path.chmod(0o640)
    os.utime(path, ns=(1234567890000000000, 1234567891000000000))
    before = path.stat()
    authorization = _authorization(home)

    try:
        authorization.install()
        assert authorization.line in path.read_bytes()
    finally:
        authorization.close()

    after = path.stat()
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_gid) == (
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
    )
    assert (after.st_atime_ns, after.st_mtime_ns) == (before.st_atime_ns, before.st_mtime_ns)
    assert path.read_bytes() == original


@_POSIX
def test_new_authorization_and_directory_are_removed(home):
    authorization = _authorization(home)
    try:
        authorization.install()
        assert authorization.path.stat().st_mode & 0o777 == 0o600
        assert authorization.directory.stat().st_mode & 0o777 == 0o700
    finally:
        authorization.close()
    assert not authorization.directory.exists()


@_POSIX
def test_cleanup_preserves_keys_added_after_probe_installation(home):
    directory = home / ".ssh"
    directory.mkdir(mode=0o700)
    path = directory / "authorized_keys"
    path.write_bytes(b"original without newline")
    authorization = _authorization(home)
    try:
        authorization.install()
        with path.open("ab") as output:
            output.write(b"another authorized key\n")
    finally:
        authorization.close()
    assert path.read_bytes() == b"original without newline\nanother authorized key\n"


@_POSIX
def test_installation_does_not_overwrite_a_concurrent_key_append(home, monkeypatch):
    directory = home / ".ssh"
    directory.mkdir(mode=0o700)
    path = directory / "authorized_keys"
    path.write_bytes(b"original key\n")
    real_fdopen = probe.os.fdopen

    class File:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, *args):
            data = self.stream.read(*args)
            if args == ():
                with path.open("ab") as output:
                    output.write(b"concurrent key\n")
            return data

    monkeypatch.setattr(
        probe.os, "fdopen", lambda *args, **kwargs: File(real_fdopen(*args, **kwargs))
    )
    authorization = _authorization(home)
    try:
        authorization.install()
        assert b"concurrent key\n" in path.read_bytes()
    finally:
        authorization.close()
    assert path.read_bytes() == b"original key\nconcurrent key\n"


@_POSIX
def test_new_file_is_removed_when_relabeling_fails(home, monkeypatch):
    def relabel(path):
        if path.name == "authorized_keys":
            raise OSError("fixture relabel failure")

    monkeypatch.setattr(probe, "relabel", relabel)
    authorization = _authorization(home)
    try:
        with raises(OSError, match="relabel failure"):
            authorization.install()
    finally:
        authorization.close()
    assert not authorization.directory.exists()


@_POSIX
@mark.parametrize("failure", ["write", "flush"])
@mark.parametrize("preexisting", [False, True])
def test_failed_authorization_write_is_cleaned_up(home, monkeypatch, failure, preexisting):
    path = home / ".ssh" / "authorized_keys"
    original = b"existing key\n"
    if preexisting:
        path.parent.mkdir(mode=0o700)
        path.write_bytes(original)
    real_fdopen = probe.os.fdopen

    class File:
        def __init__(self, stream):
            self.stream = stream
            self.failed = False

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, data):
            if failure == "write" and not self.failed:
                self.failed = True
                self.stream.write(data[:8])
                raise OSError("fixture partial write")
            return self.stream.write(data)

        def flush(self):
            if failure == "flush" and not self.failed:
                self.failed = True
                raise OSError("fixture flush failure")
            return self.stream.flush()

    monkeypatch.setattr(
        probe.os, "fdopen", lambda *args, **kwargs: File(real_fdopen(*args, **kwargs))
    )
    authorization = _authorization(home)
    try:
        with raises(OSError, match="fixture"):
            authorization.install()
    finally:
        authorization.close()
    if preexisting:
        assert path.read_bytes() == original
    else:
        assert not path.parent.exists()


@_POSIX
def test_cleanup_completes_short_writes_before_truncating(home):
    path = home / ".ssh" / "authorized_keys"
    path.parent.mkdir(mode=0o700)
    original = b"first existing key\nsecond existing key\n"
    path.write_bytes(original)
    authorization = _authorization(home)
    authorization.install()
    stream = authorization.file

    class ShortWrites:
        def __getattr__(self, name):
            return getattr(stream, name)

        def write(self, data):
            return stream.write(data[:3])

    authorization.file = ShortWrites()
    authorization.close()
    assert path.read_bytes() == original


@_POSIX
def test_authorization_at_the_size_limit_is_restored(home):
    path = home / ".ssh" / "authorized_keys"
    path.parent.mkdir(mode=0o700)
    original = b"#" + b"x" * (1024 * 1024 - 2) + b"\n"
    path.write_bytes(original)
    authorization = _authorization(home)
    try:
        authorization.install()
    finally:
        authorization.close()
    assert path.read_bytes() == original


@_POSIX
def test_cleanup_refuses_a_replaced_authorization_inode(home):
    authorization = _authorization(home)
    authorization.install()
    old = authorization.path.with_name("original")
    authorization.path.rename(old)
    authorization.path.write_bytes(b"replacement key\n")
    with raises(RuntimeError, match="replaced"):
        authorization.close()
    assert authorization.path.read_bytes() == b"replacement key\n"


@_POSIX
def test_fifo_authorization_is_rejected_without_blocking_cleanup(home):
    directory = home / ".ssh"
    directory.mkdir(mode=0o700)
    os.mkfifo(directory / "authorized_keys")
    source = str(Path(probe.__file__).resolve())
    script = """import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location('probe', sys.argv[1])
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
authorization = probe.AuthorizedKey(
    pathlib.Path(sys.argv[2]), os.getuid(), os.getgid(), 'key', 'test')
try:
    try:
        authorization.install()
    except ValueError:
        pass
    else:
        raise AssertionError('FIFO accepted')
finally:
    authorization.close()
"""
    result = subprocess.run(  # noqa: S603  # isolated fixture with a hard timeout for FIFO regressions
        [sys.executable, "-I", "-c", script, source, str(home)],
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_nonce_echo_survives_an_idle_accept_timeout():
    echo = probe.Echo(b"test nonce")
    try:
        probe.time.sleep(0.3)
        probe.round_trip(echo.port, b"test nonce")
    finally:
        echo.close()
    assert not echo.thread.is_alive()


def test_wrong_nonce_is_not_accepted():
    echo = probe.Echo(b"good nonce")
    try:
        with raises(ValueError, match="did not match"):
            probe.round_trip(echo.port, b"wrongnonce")
    finally:
        echo.close()


def test_echo_constructor_closes_its_socket_on_failure(monkeypatch):
    listener = Mock()
    listener.bind.side_effect = OSError("fixture bind failure")
    monkeypatch.setattr(probe.socket, "socket", lambda: listener)
    with raises(OSError, match="bind failure"):
        probe.Echo(b"nonce")
    listener.close.assert_called_once()


@mark.parametrize("exit_status", [0, 255])
def test_exited_ssh_cannot_satisfy_forwarding(monkeypatch, exit_status):
    process = Mock()
    process.poll.return_value = exit_status
    exchange = Mock()
    monkeypatch.setattr(probe, "round_trip", exchange)
    with raises(RuntimeError, match="exited before"):
        probe.wait_forwarding(process, 12345, b"nonce")
    exchange.assert_not_called()


def test_ssh_exit_after_nonce_exchange_is_rejected(monkeypatch):
    process = Mock()
    process.poll.side_effect = [None, 255]
    monkeypatch.setattr(probe, "round_trip", Mock())
    with raises(RuntimeError, match="exited during"):
        probe.wait_forwarding(process, 12345, b"nonce")


def test_refused_forwarding_is_retried_only_until_its_deadline(monkeypatch):
    clock = iter((0, 0, 31))
    sleep = Mock()
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=sleep))
    process = Mock()
    process.poll.return_value = None
    exchange = Mock(side_effect=ConnectionRefusedError())
    monkeypatch.setattr(probe, "round_trip", exchange)
    with raises(TimeoutError, match="did not become ready"):
        probe.wait_forwarding(process, 12345, b"nonce")
    exchange.assert_called_once_with(12345, b"nonce")
    sleep.assert_called_once_with(0.1)


def test_bad_forwarded_payload_is_not_retried(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    exchange = Mock(side_effect=ValueError("nonce mismatch"))
    monkeypatch.setattr(probe, "round_trip", exchange)
    with raises(ValueError, match="nonce mismatch"):
        probe.wait_forwarding(process, 12345, b"nonce")
    exchange.assert_called_once()


def test_ssh_cleanup_escalates_to_kill_and_reaps():
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("ssh", 5), 0]
    probe.stop_process(process)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_args_list == [call(timeout=5), call(timeout=5)]


@fixture
def lifecycle(monkeypatch, tmp_path):
    events = []
    account = SimpleNamespace(
        pw_uid=1000, pw_gid=1000, pw_shell="/sbin/nologin", pw_dir=str(tmp_path)
    )
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=lambda user: account))
    monkeypatch.setattr(probe.os, "geteuid", lambda: 0, raising=False)
    original_temporary = probe.tempfile.TemporaryDirectory
    monkeypatch.setattr(
        probe.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: original_temporary(prefix="probe-test-", dir=tmp_path),
    )

    @contextmanager
    def deadline(seconds):
        assert seconds == 420
        yield lambda: events.append("cleanup-start")

    monkeypatch.setattr(probe, "deadline", deadline)

    def checked(arguments, *, timeout=10):
        events.append((arguments, timeout))
        if arguments[:2] == ["/usr/sbin/sshd", "-T"]:
            return "\n".join(
                f"{key} {value}"
                for key, value in {"listenaddress": "127.0.0.1:22", **probe._SETTINGS}.items()
            )
        if arguments[0] == "ss":
            return "LISTEN 0 128 127.0.0.1:22 0.0.0.0:*"
        return ""

    monkeypatch.setattr(probe, "checked", checked)
    monkeypatch.setattr(probe, "public_key", lambda path: _PUBLIC)

    class Authorization:
        def __init__(self, *args):
            pass

        def install(self):
            events.append("authorize")

        def close(self):
            events.append("deauthorize")

    class Echo:
        port = 23456

        def __init__(self, nonce):
            pass

        def close(self):
            events.append("echo-close")

    monkeypatch.setattr(probe, "AuthorizedKey", Authorization)
    monkeypatch.setattr(probe, "Echo", Echo)
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(probe.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(probe, "stop_process", lambda child: events.append("ssh-stop"))
    exchange = Mock()
    monkeypatch.setattr(probe, "wait_forwarding", exchange)
    return SimpleNamespace(
        account=account, events=events, exchange=exchange, process=process, root=tmp_path
    )


def test_probe_cleans_owned_resources_after_forwarding(lifecycle):
    instance = probe.Probe("probe-id", "stlv-tunnel")
    instance.run()
    assert lifecycle.events[0] == (["cloud-init", "status", "--wait", "--long"], 300)
    assert lifecycle.events[-4:] == ["cleanup-start", "ssh-stop", "echo-close", "deauthorize"]
    assert not list(lifecycle.root.glob("probe-test-*"))
    arguments = probe.subprocess.Popen.call_args.args[0]
    assert arguments[:5] == ["ssh", "-F", "/dev/null", "-N", "-T"]
    assert "StrictHostKeyChecking=yes" in arguments
    assert "IdentityAgent=none" in arguments
    assert "HostKeyAlias=stelvio-bastion-probe-probe-id" in arguments
    local, target = arguments[arguments.index("-L") + 1].split(":")[1::2]
    assert int(local) == lifecycle.exchange.call_args.args[1]
    assert int(target) == 23456


@mark.parametrize(
    "failure",
    [ValueError("fixture forwarding failure"), TimeoutError("fixture forwarding timeout")],
)
def test_forwarding_failure_still_cleans_owned_resources(lifecycle, failure):
    lifecycle.exchange.side_effect = failure
    with raises(RuntimeError, match=r"forwarding (failure|timeout).*SSH stderr"):
        probe.Probe("probe-id", "stlv-tunnel").run()
    assert lifecycle.events[-4:] == ["cleanup-start", "ssh-stop", "echo-close", "deauthorize"]
    assert not list(lifecycle.root.glob("probe-test-*"))


@mark.parametrize(("uid", "shell"), [(0, "/sbin/nologin"), (1000, "/bin/bash")])
def test_probe_rejects_root_or_an_interactive_shell(lifecycle, uid, shell):
    lifecycle.account.pw_uid, lifecycle.account.pw_shell = uid, shell
    with raises(ValueError, match="nonroot with /sbin/nologin"):
        probe.Probe("probe-id", "stlv-tunnel").run()
    assert "authorize" not in lifecycle.events


def test_cloud_init_failure_stops_before_authorization(lifecycle, monkeypatch):
    command = Mock(side_effect=RuntimeError("cloud-init failed"))
    monkeypatch.setattr(probe, "checked", command)
    instance = probe.Probe("probe-id", "stlv-tunnel")
    with raises(RuntimeError, match="cloud-init failed"):
        instance.run()
    assert instance.stage == "cloud-init"
    assert command.call_count == 1
    assert lifecycle.events == ["cleanup-start"]


def test_sshd_syntax_failure_stops_before_authorization(lifecycle, monkeypatch):
    command = probe.checked

    def fail_syntax(arguments, **kwargs):
        if arguments == ["/usr/sbin/sshd", "-t"]:
            raise RuntimeError("fixture invalid sshd syntax")
        return command(arguments, **kwargs)

    monkeypatch.setattr(probe, "checked", fail_syntax)
    instance = probe.Probe("probe-id", "stlv-tunnel")
    with raises(RuntimeError, match="invalid sshd syntax"):
        instance.run()
    assert instance.stage == "sshd-config"
    assert "authorize" not in lifecycle.events
    assert lifecycle.events[-1] == "cleanup-start"


@_POSIX
def test_sigterm_raises_and_restores_signal_handlers():
    signals = (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)
    previous = {signum: signal.getsignal(signum) for signum in signals}
    with raises(TimeoutError, match="interrupted by signal"), probe.deadline(420):
        signal.raise_signal(signal.SIGTERM)
    assert {signum: signal.getsignal(signum) for signum in signals} == previous
    assert signal.alarm(0) == 0


@_POSIX
def test_deadline_is_disabled_during_bounded_cleanup():
    previous = signal.getsignal(signal.SIGTERM)
    with probe.deadline(420) as begin_cleanup:
        begin_cleanup()
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
        assert signal.alarm(0) == 0
    assert signal.getsignal(signal.SIGTERM) == previous
