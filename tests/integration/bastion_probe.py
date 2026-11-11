"""Standalone, standard-library-only probe sent to a bastion through Run Command."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Event, Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import BinaryIO

_SETTINGS = {
    "passwordauthentication": "no",
    "kbdinteractiveauthentication": "no",
    "permittty": "no",
    "maxsessions": "0",
    "allowtcpforwarding": "local",
    "permitopen": "any",
    "allowagentforwarding": "no",
    "x11forwarding": "no",
    "permittunnel": "no",
    "gatewayports": "no",
}


def checked(arguments: list[str], *, timeout: int = 10) -> str:
    result = subprocess.run(  # noqa: S603  # fixed probe commands, never a shell
        arguments, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-512:]
        raise RuntimeError(f"{arguments[0]} exited {result.returncode}: {detail}")
    return result.stdout


def validate_arguments(probe_id: str, user: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", probe_id):
        raise ValueError(
            "probe_id must contain 1-64 ASCII letters, digits, underscores or hyphens"
        )
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("SSH user must be a valid Linux account name")


def validate_config(output: str) -> None:
    settings: dict[str, list[str]] = {}
    for line in output.splitlines():
        key, _, value = line.partition(" ")
        settings.setdefault(key, []).append(value.strip())
    for key, expected in {"listenaddress": "127.0.0.1:22", **_SETTINGS}.items():
        if settings.get(key) != [expected]:
            raise ValueError(f"sshd {key}: expected {expected!r}, got {settings.get(key)!r}")


def validate_listeners(output: str) -> None:
    addresses = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] != "LISTEN":
            raise ValueError("ss returned an invalid SSH listener")
        addresses.append(fields[3])
    if addresses != ["127.0.0.1:22"]:
        raise ValueError(f"SSH listeners must be exactly 127.0.0.1:22, got {addresses!r}")


def public_key(path: Path) -> str:
    lines = path.read_text().splitlines()
    fields = lines[0].split() if len(lines) == 1 else []
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise ValueError(f"Expected one ed25519 public key in {path}")
    decoded = base64.b64decode(fields[1], validate=True)
    if len(decoded) != 51 or decoded[:19] != b"\0\0\0\x0bssh-ed25519\0\0\0\x20":
        raise ValueError(f"Invalid ed25519 public key in {path}")
    return " ".join(fields[:2])


def relabel(path: Path) -> None:
    restorecon = shutil.which("restorecon")
    if restorecon:
        checked([restorecon, "-F", str(path)])


class AuthorizedKey:
    """Temporarily append one key without replacing the existing inode or its labels."""

    def __init__(self, home: Path, uid: int, gid: int, key: str, probe_id: str) -> None:
        self.directory = home / ".ssh"
        self.path = self.directory / "authorized_keys"
        self.uid, self.gid = uid, gid
        self.line = f'from="127.0.0.1" {key} stelvio-bastion-probe-{probe_id}'.encode()
        self.file: BinaryIO | None = None
        self.original = b""
        self.before: os.stat_result | None = None
        self.created_directory = False
        self.created_file = False
        self.installed = False
        self.addition = b"\n" + self.line + b"\n"
        self.written = 0
        self.regular = False

    def install(self) -> None:
        if self.directory.parent.is_symlink():
            raise ValueError("SSH account home must not be a symlink")
        try:
            self.directory.mkdir(mode=0o700)
            self.created_directory = True
            os.chown(self.directory, self.uid, self.gid, follow_symlinks=False)
            relabel(self.directory)
        except FileExistsError:
            if self.directory.is_symlink() or not self.directory.is_dir():
                raise ValueError("SSH directory must be a real directory") from None
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            descriptor = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            self.created_file = True
        except FileExistsError:
            descriptor = os.open(self.path, flags)
        self.file = os.fdopen(descriptor, "r+b", buffering=0)
        self.before = os.fstat(self.file.fileno())
        self.regular = stat.S_ISREG(self.before.st_mode)
        if not self.regular or self.before.st_size > 1024 * 1024:
            raise ValueError("authorized_keys must be a regular file of at most 1 MiB")
        self._same_inode()
        if self.created_file:
            os.fchown(self.file.fileno(), self.uid, self.gid)
            relabel(self.path)
        self.original = self.file.read()
        self.installed = True
        while self.written < len(self.addition):
            count = self.file.write(self.addition[self.written :])
            if not count:
                raise OSError("Could not append the probe authorization")
            self.written += count
        self.file.flush()

    def _same_inode(self) -> None:
        current = self.path.lstat()
        if self.before is None or (current.st_dev, current.st_ino) != (
            self.before.st_dev,
            self.before.st_ino,
        ):
            raise RuntimeError("authorized_keys was replaced during the probe")

    def _restored(self, current: bytes) -> bytes:
        index = current.find(self.addition)
        if index >= 0:
            prefix, suffix = current[:index], current[index + len(self.addition) :]
        elif current.startswith(self.original) and self.addition.startswith(
            current[len(self.original) :]
        ):
            return self.original
        elif self.written and current.startswith(self.original + self.addition[: self.written]):
            prefix, suffix = self.original, current[len(self.original) + self.written :]
        else:
            return b"".join(
                line
                for line in current.splitlines(keepends=True)
                if line.rstrip(b"\r\n") != self.line
            )
        separator = b"\n" if prefix and not prefix.endswith(b"\n") and suffix else b""
        return prefix + separator + suffix

    def _rewrite(self, restored: bytes) -> None:
        import fcntl  # remote Linux file flags; Windows can import the probe

        self.file.seek(0)
        flags = fcntl.fcntl(self.file.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(self.file.fileno(), fcntl.F_SETFL, flags & ~os.O_APPEND)
        written = 0
        while written < len(restored):
            count = self.file.write(restored[written:])
            if not count:
                raise OSError("Could not restore the probe authorization")
            written += count
        self.file.truncate()
        self.file.flush()
        if restored == self.original and self.before is not None:
            os.utime(self.file.fileno(), ns=(self.before.st_atime_ns, self.before.st_mtime_ns))

    def close(self) -> None:
        try:
            if self.file is not None and self.regular and (self.installed or self.created_file):
                self._same_inode()
                self.file.seek(0)
                limit = 2 * 1024 * 1024 + len(self.addition)
                current = self.file.read(limit + 1)
                if len(current) > limit:
                    raise RuntimeError("authorized_keys grew beyond the probe's cleanup bound")
                if self.installed:
                    current = self._restored(current)
                    self._rewrite(current)
                if self.created_file and not current:
                    self.path.unlink()
        finally:
            if self.file is not None:
                self.file.close()
                self.file = None
            if self.created_directory:
                try:
                    self.directory.rmdir()
                except OSError:
                    if not self.directory.is_dir() or not any(self.directory.iterdir()):
                        raise


@contextmanager
def deadline(seconds: int) -> Iterator[Callable[[], None]]:
    signals = (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)
    previous = {signum: signal.getsignal(signum) for signum in signals}

    def interrupted(signum: int, _frame: object) -> None:
        raise TimeoutError(f"Probe interrupted by signal {signum}")

    def begin_cleanup() -> None:
        signal.alarm(0)
        for signum in signals:
            signal.signal(signum, signal.SIG_IGN)

    try:
        for signum in signals:
            signal.signal(signum, interrupted)
        signal.alarm(seconds)
        yield begin_cleanup
    finally:
        signal.alarm(0)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class Echo:
    def __init__(self, nonce: bytes) -> None:
        self.nonce = nonce
        self.stop = Event()
        self.listener = socket.socket()
        try:
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen()
            self.listener.settimeout(0.2)
            self.port = self.listener.getsockname()[1]
            self.thread = Thread(target=self._serve, daemon=True)
            self.thread.start()
        except BaseException:
            self.listener.close()
            raise

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:  # noqa: UP041  # AL2023 Python 3.9 has a separate socket timeout
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    data = b""
                    while len(data) < len(self.nonce):
                        part = connection.recv(len(self.nonce) - len(data))
                        if not part:
                            break
                        data += part
                    if data == self.nonce:
                        connection.sendall(b"stelvio-ok:" + self.nonce)
                except OSError:
                    continue

    def close(self) -> None:
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("Probe echo server did not stop")


def round_trip(port: int, nonce: bytes) -> None:
    expected = b"stelvio-ok:" + nonce
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(nonce)
        response = b""
        while len(response) <= len(expected):
            part = connection.recv(len(expected) + 1 - len(response))
            if not part:
                break
            response += part
    if response != expected:
        raise ValueError("Forwarded nonce response did not match")


def wait_forwarding(process: subprocess.Popen, port: int, nonce: bytes) -> None:
    end = time.monotonic() + 30
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError("SSH forwarding process exited before the nonce check")
        try:
            round_trip(port, nonce)
        except ConnectionRefusedError:
            time.sleep(0.1)
            continue
        if process.poll() is not None:
            raise RuntimeError("SSH forwarding process exited during the nonce check")
        return
    raise TimeoutError("SSH forwarding listener did not become ready")


class Probe:
    def __init__(self, probe_id: str, user: str) -> None:
        validate_arguments(probe_id, user)
        self.probe_id, self.user = probe_id, user
        self.stage = "initializing"

    def run(self) -> None:
        import pwd  # the module must remain importable on Windows

        if os.geteuid() != 0:
            raise PermissionError("The bastion probe must run as root through Run Command")
        with deadline(420) as begin_cleanup, ExitStack() as owned:
            try:
                self.stage = "cloud-init"
                checked(["cloud-init", "status", "--wait", "--long"], timeout=300)
                self.stage = "sshd-config"
                checked(["systemctl", "is-active", "--quiet", "sshd"])
                checked(["/usr/sbin/sshd", "-t"])
                validate_config(
                    checked(
                        [
                            "/usr/sbin/sshd",
                            "-T",
                            "-C",
                            f"user={self.user},host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport=22",
                        ]
                    )
                )
                self.stage = "account"
                account = pwd.getpwnam(self.user)
                if account.pw_uid == 0 or account.pw_shell != "/sbin/nologin":
                    raise ValueError("Tunnel account must be nonroot with /sbin/nologin")
                self.stage = "sshd-listener"
                validate_listeners(checked(["ss", "-H", "-ltnp", "sport = :22"]))
                work = Path(
                    owned.enter_context(
                        tempfile.TemporaryDirectory(prefix="stelvio-bastion-probe-", dir="/run")
                    )
                )
                self.stage = "authorization"
                key = work / "key"
                checked(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
                authorization = AuthorizedKey(
                    Path(account.pw_dir),
                    account.pw_uid,
                    account.pw_gid,
                    public_key(key.with_suffix(".pub")),
                    self.probe_id,
                )
                owned.callback(authorization.close)
                authorization.install()
                alias = f"stelvio-bastion-probe-{self.probe_id}"
                known_hosts = work / "known_hosts"
                known_hosts.write_text(
                    f"{alias} {public_key(Path('/etc/ssh/ssh_host_ed25519_key.pub'))}\n"
                )
                nonce = secrets.token_bytes(32)
                echo = Echo(nonce)
                owned.callback(echo.close)
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                self.stage = "forwarding"
                options = {
                    "BatchMode": "yes",
                    "IdentitiesOnly": "yes",
                    "IdentityAgent": "none",
                    "StrictHostKeyChecking": "yes",
                    "UserKnownHostsFile": str(known_hosts),
                    "GlobalKnownHostsFile": "/dev/null",
                    "KnownHostsCommand": "none",
                    "VerifyHostKeyDNS": "no",
                    "UpdateHostKeys": "no",
                    "HostKeyAlias": alias,
                    "HostKeyAlgorithms": "ssh-ed25519",
                    "PreferredAuthentications": "publickey",
                    "ExitOnForwardFailure": "yes",
                    "ConnectTimeout": "10",
                    "ConnectionAttempts": "1",
                    "ServerAliveInterval": "5",
                    "ServerAliveCountMax": "1",
                }
                arguments = ["ssh", "-F", "/dev/null", "-N", "-T", "-i", str(key)]
                for name, value in options.items():
                    arguments.extend(("-o", f"{name}={value}"))
                arguments.extend(
                    ("-L", f"127.0.0.1:{port}:127.0.0.1:{echo.port}", f"{self.user}@127.0.0.1")
                )
                error_log = owned.enter_context((work / "ssh.stderr").open("wb"))
                process = subprocess.Popen(  # noqa: S603  # fixed local SSH with pinned host trust
                    arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=error_log,
                    start_new_session=True,
                )
                owned.callback(stop_process, process)
                try:
                    wait_forwarding(process, port, nonce)
                except Exception as error:
                    detail = (work / "ssh.stderr").read_text(errors="replace")[-512:]
                    raise RuntimeError(f"{error}; SSH stderr: {detail}") from error
            finally:
                begin_cleanup()


def _from_arguments(arguments: list[str]) -> Probe:
    if len(arguments) != 2:
        raise ValueError("Expected PROBE_ID and SSH_USER")
    return Probe(*arguments)


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    probe = None
    probe_id = arguments[0][:64] if arguments else ""
    try:
        probe = _from_arguments(arguments)
        probe.run()
    except Exception as error:
        detail = " ".join(str(error).split())[:512]
        print(
            json.dumps(
                {
                    "probe_id": probe_id,
                    "status": "error",
                    "stage": probe.stage if probe else "arguments",
                    "error": detail,
                }
            )
        )
        return 1
    print(json.dumps({"probe_id": probe_id, "status": "ok", "stage": "complete"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
