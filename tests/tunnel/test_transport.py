"""Transport validates deployed ownership and content before local SSH execution."""

import os
import select
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock

from botocore.exceptions import ClientError
from pytest import fixture, mark, raises

from stelvio.tunnel.processes import ProcessIdentity, identity
from stelvio.tunnel.socks import TransportInterruptedError
from stelvio.tunnel.transport import (
    SshTransport,
    TransportArtifactError,
    TransportAuthorizationError,
    TransportCleanupError,
    TransportRetryError,
    classify_aws,
    terminate_owned_session,
)


def test_ssm_bootstrap_checks_availability_without_document_or_run_command(transport_world):
    transport, clients, *_ = transport_world
    transport._bootstrap()
    for operation in (
        "get_document",
        "describe_document",
        "list_tags_for_resource",
        "send_command",
        "get_command_invocation",
    ):
        getattr(clients["ssm"], operation).assert_not_called()
    clients["ssm"].describe_instance_information.assert_called_once()


@mark.skipif(sys.platform != "darwin", reason="requires the supported macOS host")
def test_actual_child_birth_registration_finishes_before_shutdown_fence(  # noqa: PLR0915 - full concurrent child lifecycle
    transport_world, monkeypatch
):
    transport, clients, _, _ = transport_world
    transport.pin = None
    monkeypatch.setattr(transport, "_bootstrap", lambda: "fixture authenticated pin")
    monkeypatch.setattr("stelvio.tunnel.transport.installed_plugin", lambda: Path("/usr/bin/true"))
    clients["ssm"].start_session.return_value = {"SessionId": "owned-fixture"}
    clients["ssm"].meta.endpoint_url = "https://ssm.us-east-1.amazonaws.com"
    eic = Mock()
    eic.send_ssh_public_key.return_value = {"Success": True}
    original_client = transport.session.client.side_effect
    transport.session.client.side_effect = lambda name, **kwargs: (
        eic if name == "ec2-instance-connect" else original_client(name, **kwargs)
    )
    original_popen = subprocess.Popen
    spawned, release, fenced = Event(), Event(), Event()
    children, failures = [], []

    def launch(arguments, **options):
        if arguments[0] != "/usr/bin/ssh":
            return original_popen(arguments, **options)
        assert options["env"]["STLV_SSM_PLUGIN"] == "/usr/bin/true"
        child = original_popen(
            [sys.executable, "-I", "-S", "-c", "import time;time.sleep(60)"],
            start_new_session=True,
            stderr=subprocess.PIPE,
        )
        children.append(child)
        spawned.set()
        assert release.wait(5)
        return child

    def start():
        try:
            transport.start()
        except Exception as error:
            failures.append(error)

    def fence():
        transport.stop_local()
        fenced.set()

    monkeypatch.setattr("stelvio.tunnel.transport.subprocess.Popen", launch)
    attempt = Thread(target=start)
    stopping = Thread(target=fence)
    attempt.start()
    try:
        assert spawned.wait(5)
        transport.stop.set()
        stopping.start()
        assert not fenced.wait(0.05)
        release.set()
        attempt.join(5)
        stopping.join(5)
        assert not attempt.is_alive()
        assert not stopping.is_alive()
        assert fenced.is_set()
        assert len(failures) == 1
        assert isinstance(failures[0], TransportInterruptedError)
        assert len(children) == 1
        assert children[0].returncode is not None
        assert identity(children[0].pid) is None
        transport.close()
        assert clients["ssm"].terminate_session.call_args.kwargs == {"SessionId": "owned-fixture"}
        with raises(TransportInterruptedError):
            transport.start()
        assert len(children) == 1
    finally:
        release.set()
        attempt.join(5)
        if stopping.ident is not None:
            stopping.join(5)
        for child in children:
            if child.returncode is None:
                os.killpg(child.pid, 9)
                child.wait(timeout=5)
        transport.close()


@mark.skipif(sys.platform != "darwin", reason="requires the supported macOS host")
def test_birth_identity_mismatch_refuses_to_signal_actual_child(transport_world, monkeypatch):
    transport = transport_world[0]
    child = subprocess.Popen(  # noqa: S603 - fixed owned child fixture
        [sys.executable, "-I", "-S", "-c", "import time;time.sleep(60)"], start_new_session=True
    )
    actual = None
    try:
        actual = identity(child.pid)
        assert actual is not None
        transport.process = child
        transport._process_identity = replace(actual, birth_seconds=actual.birth_seconds + 1)
        with raises(RuntimeError, match="identity changed"):
            transport.stop_local()
        assert identity(child.pid).same_process(actual)
        transport._process_identity = actual
        transport.stop_local()
        assert child.returncode is not None
    finally:
        if actual is None:
            child.kill()
            child.wait(timeout=5)
        else:
            transport._process_identity = actual
            transport.stop_local()


@mark.skipif(sys.platform != "darwin", reason="requires the supported macOS host")
def test_exited_leader_does_not_leave_its_term_ignoring_descendant(transport_world):
    transport = transport_world[0]
    script = """import subprocess,sys,time
child=subprocess.Popen([sys.executable,'-I','-S','-c',
    'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
    'print("ready",flush=True);time.sleep(60)'],stdout=subprocess.PIPE)
assert child.stdout.readline()==b'ready\\n'
print(child.pid,flush=True)
time.sleep(60)
"""
    child = subprocess.Popen(  # noqa: S603 - fixed descendant fixture
        [sys.executable, "-I", "-S", "-c", script], start_new_session=True, stdout=subprocess.PIPE
    )
    transport.process = child
    transport._process_identity = identity(child.pid)
    try:
        assert select.select([child.stdout], [], [], 5)[0]
        descendant_pid = int(child.stdout.readline())
        descendant = identity(descendant_pid)
        assert descendant is not None
        assert descendant.group == child.pid
        transport.stop_local()
        assert child.returncode is not None
        assert identity(descendant_pid) is None
    finally:
        transport.stop_local()
        child.stdout.close()


@fixture
def shutdown_world(transport_world, monkeypatch):
    transport = transport_world[0]
    child = Mock(pid=123)
    owner = ProcessIdentity(123, 1, 123, os.geteuid(), 1, 0, 5)
    transport.process, transport._process_identity = child, owner
    transport._temporary = Mock()
    monkeypatch.setattr("stelvio.tunnel.transport.identity", lambda pid: owner)
    monkeypatch.setattr("stelvio.tunnel.transport.unreaped_child", lambda pid: True)
    signals = []
    monkeypatch.setattr("stelvio.tunnel.transport.os.killpg", lambda *args: signals.append(args))
    clock = [0.0]
    monkeypatch.setattr("stelvio.tunnel.transport.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "stelvio.tunnel.transport.time.sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    return transport, child, owner, signals, clock


def test_shutdown_confirms_group_exit_before_reaping_leader(shutdown_world, monkeypatch):
    transport, child, _, signals, clock = shutdown_world
    checks = []

    def group_empty(group, *, timeout):
        child.wait.assert_not_called()
        assert group == child.pid
        assert 0 < timeout <= 3
        checks.append(timeout)
        return len(checks) == 3

    monkeypatch.setattr("stelvio.tunnel.transport.process_group_empty", group_empty)
    transport.stop_local()
    assert signals == [(123, 15), (123, 9)]
    assert len(checks) == 3
    assert clock[0] == 0.02
    child.wait.assert_called_once_with(timeout=3)
    assert transport.process is None
    assert transport._process_identity is None
    transport.stop_local()
    assert len(signals) == 2


@mark.parametrize("failure", ["deadline", "inventory", "inventory-timeout"])
def test_unconfirmed_shutdown_retains_ownership_and_can_retry(
    shutdown_world, monkeypatch, failure
):
    transport, child, owner, _, clock = shutdown_world
    temporary = transport._temporary

    def group_empty(group, *, timeout):
        if failure == "inventory":
            raise RuntimeError("cannot inspect process identity")
        if failure == "inventory-timeout":
            raise subprocess.TimeoutExpired("ps", timeout)
        clock[0] += timeout
        return False

    monkeypatch.setattr("stelvio.tunnel.transport.process_group_empty", group_empty)
    with raises(TransportCleanupError, match="retain cleanup ownership"):
        transport.stop_local()
    child.wait.assert_not_called()
    assert transport.process is child
    assert transport._process_identity is owner
    assert transport._temporary is temporary
    temporary.cleanup.assert_not_called()
    monkeypatch.setattr("stelvio.tunnel.transport.process_group_empty", lambda *a, **k: True)
    transport.stop_local()
    child.wait.assert_called_once_with(timeout=3)
    assert transport.process is None


@mark.parametrize(
    "message",
    ["missing", "corrupt digest", "unsafe ownership", "version check failed"],
)
def test_invalid_private_plugin_is_terminal_at_transport_boundary(
    transport_world, monkeypatch, message
):
    transport, clients, *_ = transport_world
    transport.pin = None
    monkeypatch.setattr(transport, "_bootstrap", lambda: "fixture authenticated pin")

    def missing_plugin():
        from stelvio.tunnel.plugin import PluginAssetError

        raise PluginAssetError(f"Private AWS plugin {message}")

    monkeypatch.setattr("stelvio.tunnel.transport.installed_plugin", missing_plugin)
    with raises(TransportArtifactError, match=message):
        transport.start()
    clients["ssm"].start_session.assert_not_called()
    assert transport.session_id is None
    assert transport._temporary is None


@mark.parametrize(
    ("code", "error_type"),
    [
        ("AccessDeniedException", TransportAuthorizationError),
        ("InvalidInstanceID.NotFound", TransportAuthorizationError),
        ("ThrottlingException", TransportRetryError),
    ],
)
def test_aws_failures_are_sanitized_and_classified(code, error_type):
    error = ClientError(
        {"Error": {"Code": code, "Message": "private response detail"}}, "DescribeFixture"
    )
    classified = classify_aws(error)
    assert isinstance(classified, error_type)
    assert "private response detail" not in str(classified)


def test_partial_start_failure_terminates_session_and_cleans_temporary_key(
    transport_world, monkeypatch
):
    transport, clients, *_ = transport_world
    transport.pin = None
    monkeypatch.setattr(transport, "_bootstrap", lambda: "fixture authenticated pin")
    monkeypatch.setattr("stelvio.tunnel.transport.installed_plugin", lambda: Path("/usr/bin/true"))
    clients["ssm"].start_session.return_value = {"SessionId": "owned-fixture"}
    instance_connect = Mock()
    instance_connect.send_ssh_public_key.return_value = {"Success": False}
    clients["ec2-instance-connect"] = instance_connect

    def fake_run(arguments, **kwargs):
        assert arguments[0] == "/usr/bin/ssh-keygen"
        private_key = Path(arguments[arguments.index("-f") + 1])
        private_key.write_text("private-key fixture")
        private_key.with_suffix(".pub").write_text("ssh-ed25519 public-key-fixture")
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("stelvio.tunnel.transport.subprocess.run", fake_run)
    with raises(TransportAuthorizationError, match="rejected the client key"):
        transport.start()
    instance_connect.send_ssh_public_key.assert_called_once()
    clients["ssm"].terminate_session.assert_called_once_with(SessionId="owned-fixture")
    assert transport.session_id is None
    assert transport._reason is None
    assert transport._temporary is None


def test_lost_start_session_reply_recovers_only_one_exact_owned_session(
    transport_world, monkeypatch
):
    transport, clients, *_ = transport_world
    transport.pin = None
    monkeypatch.setattr(transport, "_bootstrap", lambda: "fixture authenticated pin")
    monkeypatch.setattr("stelvio.tunnel.transport.installed_plugin", lambda: Path("/usr/bin/true"))
    clients["ssm"].start_session.side_effect = ClientError(
        {"Error": {"Code": "InternalError", "Message": "response lost"}}, "StartSession"
    )

    def active_sessions(**kwargs):
        return [
            {
                "Sessions": [
                    {
                        "SessionId": "owned-recovered",
                        "Target": "i-12345678",
                        "Reason": transport._reason,
                    }
                ]
            }
        ]

    clients["ssm"].get_paginator.return_value.paginate.side_effect = active_sessions

    def fake_run(arguments, **kwargs):
        private_key = Path(arguments[arguments.index("-f") + 1])
        private_key.write_text("private-key fixture")
        private_key.with_suffix(".pub").write_text("ssh-ed25519 public-key-fixture")
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("stelvio.tunnel.transport.subprocess.run", fake_run)
    with raises(TransportRetryError, match="temporarily unavailable"):
        transport.start()
    clients["ssm"].terminate_session.assert_called_once_with(SessionId="owned-recovered")
    assert transport.session_id is None
    assert transport._reason is None
    assert transport._temporary is None


def test_uncertain_start_outcome_retains_recovery_owner_and_blocks_retry(transport_world):
    transport, clients, *_ = transport_world
    transport._reason = "stelvio/owner/receipt"
    transport._receipt = "receipt"
    transport._session_start_attempted = True
    clients["ssm"].get_paginator.return_value.paginate.return_value = [{"Sessions": []}]
    with raises(TransportCleanupError, match="retain recovery ownership"):
        transport.close()
    with raises(RuntimeError, match="Dispose the previous"):
        transport.start()


@mark.parametrize(
    ("field", "value"),
    [
        ("Target", "i-foreign"),
        ("Reason", "another-owner"),
        ("Status", "Failed"),
        ("SessionId", "foreign-session"),
    ],
)
def test_external_session_termination_requires_exact_history_ownership(field, value):
    ssm = Mock()
    ssm.terminate_session.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "not a valid state"}},
        "TerminateSession",
    )
    history = {
        "SessionId": "known",
        "Target": "i-owned",
        "Reason": "reason-owned",
        "Status": "Terminated",
    }
    ssm.get_paginator.return_value.paginate.return_value = [{"Sessions": [history]}]
    terminate_owned_session(ssm, "known", "i-owned", "reason-owned")
    history[field] = value
    with raises(TransportRetryError, match="not certified"):
        terminate_owned_session(ssm, "known", "i-owned", "reason-owned")


@mark.parametrize(
    ("target", "field", "value", "message"),
    [
        ("node", "VpcId", "vpc-other", "instance ownership changed"),
        ("node", "Tags", [], "instance ownership changed"),
        ("group", "OwnerId", "999999999999", "security group ownership changed"),
        (
            "group",
            "IpPermissions",
            [{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22}],
            "admits inbound SSH",
        ),
    ],
)
def test_changed_access_ownership_never_starts_a_session(
    transport_world, target, field, value, message
):
    transport, clients, node, group = transport_world
    {"node": node, "group": group}[target][field] = value
    with raises(TransportAuthorizationError, match=message):
        transport.start()
    clients["ssm"].start_session.assert_not_called()
    assert transport.process is None
    assert transport.session_id is None
    transport.close()


def test_new_transport_reuses_persistent_pin_and_rejects_changed_key(ssh_world):
    from stelvio.tunnel.host_trust import fingerprint
    from stelvio.tunnel.transport import ServerIdentityError

    transport, server, key = ssh_world
    assert transport.start().proxy_port
    transport.close()
    fresh = SshTransport(
        transport.network, transport.session, Event(), ownership=transport.ownership
    )
    try:
        assert fresh.start().proxy_port
        assert server.calls[-1][0]["StrictHostKeyChecking"] == "yes"
        fresh.close()
        server.key = server.changed_key
        with raises(ServerIdentityError) as error:
            fresh.start()
        assert "i-12345678" in str(error.value)
        assert fingerprint(key) in str(error.value)
        assert fingerprint(server.key) in str(error.value)
        assert "trust reset" in str(error.value)
        assert fresh.session_id is None
        server.key = key
        assert fresh.start().proxy_port
    finally:
        fresh.close()
    ssm = transport.ssm
    for name in ("send_command", "get_command_invocation", "get_document", "describe_document"):
        getattr(ssm, name).assert_not_called()


def test_failed_first_authentication_does_not_publish_host_trust(ssh_world):
    from stelvio.tunnel.host_trust import HostIdentity, locked_host_trust

    transport, server, _ = ssh_world
    server.fail_auth = True
    with raises(TransportRetryError, match="before authentication"):
        transport.start()
    with locked_host_trust(
        HostIdentity("aws", "123456789012", "us-east-1", "i-12345678"), Event()
    ) as entry:
        assert entry.key is None
    assert transport.session_id is None
    assert transport._temporary is None


def test_ssh_is_bound_to_ssm_target_and_isolated_known_hosts(ssh_world):
    transport, server, _ = ssh_world
    transport.start()
    options, parameters = server.calls[0]
    assert options["GlobalKnownHostsFile"] == os.devnull
    assert options["KnownHostsCommand"] == "none"
    assert options["VerifyHostKeyDNS"] == "no"
    assert options["UpdateHostKeys"] == "no"
    assert options["HostKeyAlgorithms"] == "ssh-ed25519"
    assert parameters["env"]["STLV_SSM_TARGET"] == "i-12345678"
    assert parameters["env"]["LC_ALL"] == "C"
    transport.ssm.start_session.assert_called_once_with(
        Target="i-12345678",
        DocumentName="AWS-StartSSHSession",
        Parameters={"portNumber": ["22"]},
        Reason=transport._reason,
    )


@mark.skipif(
    sys.platform != "darwin" or os.environ.get("STLV_TEST_SSH_INTEROP") != "1",
    reason="set STLV_TEST_SSH_INTEROP=1 on macOS for local OpenSSH interoperability",
)
def test_actual_openssh_first_trust_restart_mismatch_and_reset(  # noqa: PLR0915 - bounded local SSH lifecycle
    transport_world, tmp_path, monkeypatch
):
    """Use real ssh/sshd over a test-owned stand-in for the SSM byte stream."""
    import getpass
    import json
    import socket
    import time

    from stelvio.tunnel.host_trust import HostIdentity, fingerprint, locked_host_trust
    from stelvio.tunnel.transport import ServerIdentityError

    transport, clients, *_ = transport_world
    directory = tmp_path.resolve()
    host = directory / "host"
    authorized = directory / "authorized"
    authorized.touch(mode=0o600)
    for executable in ("/usr/sbin/sshd", "/usr/bin/ssh", "/usr/bin/ssh-keygen"):
        if not Path(executable).is_file():
            from pytest import skip

            skip(f"OpenSSH interoperability prerequisite missing: {executable}")
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    config = directory / "sshd_config"
    config.write_text(
        f"ListenAddress 127.0.0.1\nPort {port}\nHostKey {host}\n"
        f"PidFile {directory / 'sshd.pid'}\nAuthorizedKeysFile {authorized}\n"
        f"AllowUsers {getpass.getuser()}\nPasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\nUsePAM no\nStrictModes yes\n"
        "AllowTcpForwarding local\nMaxSessions 0\nLogLevel VERBOSE\n"
    )
    plugin = directory / "test-ssm-plugin"
    plugin.write_text(
        f"#!{sys.executable}\n"
        "import os,socket,threading\n"
        f"s=socket.create_connection(('127.0.0.1',{port}),timeout=5)\ns.settimeout(None)\n"
        "def upload():\n"
        " try:\n"
        "  while data:=os.read(0,65536): s.sendall(data)\n"
        " finally:\n"
        "  try:s.shutdown(socket.SHUT_WR)\n"
        "  except OSError:pass\n"
        "threading.Thread(target=upload,daemon=True).start()\n"
        "while data:=s.recv(65536):\n"
        " while data:\n"
        "  written=os.write(1,data);data=data[written:]\n"
    )
    plugin.chmod(0o700)
    monkeypatch.setattr("stelvio.tunnel.transport.installed_plugin", lambda: plugin)
    monkeypatch.setattr("stelvio.tunnel.transport.SSH_USER", getpass.getuser())
    clients["ssm"].start_session.return_value = {"SessionId": "test-owned-session"}
    clients["ssm"].meta.endpoint_url = "https://ssm.us-east-1.amazonaws.com"
    eic = Mock()
    clients["ec2-instance-connect"] = eic

    def authorize(**kwargs):
        authorized.write_text(kwargs["SSHPublicKey"] + "\n")
        return {"Success": True}

    eic.send_ssh_public_key.side_effect = authorize
    servers = []

    def start_server():
        subprocess.run(  # noqa: S603 - fixed executable and test-owned key path
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host)],
            check=True,
            timeout=10,
        )
        stream = (directory / f"sshd-{len(servers)}.log").open("wb")
        server = subprocess.Popen(  # noqa: S603 - fixed executable and test-owned config
            ["/usr/sbin/sshd", "-D", "-e", "-f", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=stream,
        )
        servers.append((server, stream))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            assert server.poll() is None, (
                f"sshd exited: {(directory / f'sshd-{len(servers) - 1}.log').read_text()}"
            )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return server
            except OSError:
                time.sleep(0.05)
        raise AssertionError("Test sshd did not become ready")

    target = HostIdentity("aws", "123456789012", "us-east-1", "i-12345678")
    fresh = None
    try:
        server = start_server()
        assert transport.start().proxy_port > 0
        transport.close()
        fresh = SshTransport(
            transport.network, transport.session, Event(), ownership=transport.ownership
        )
        assert fresh.start().proxy_port > 0
        fresh.close()
        server.terminate()
        server.wait(timeout=5)
        host.unlink()
        host.with_suffix(".pub").unlink()
        start_server()
        with raises(ServerIdentityError, match="i-12345678"):
            fresh.start()
        # A new transport object cannot bypass the persisted key either.
        another = SshTransport(
            transport.network, transport.session, Event(), ownership=transport.ownership
        )
        with raises(ServerIdentityError):
            another.start()
        with locked_host_trust(target, Event()) as entry:
            entry.reset(fingerprint(entry.key))
        reset = SshTransport(
            transport.network, transport.session, Event(), ownership=transport.ownership
        )
        try:
            assert reset.start().proxy_port > 0
        finally:
            reset.close()
        assert clients["ssm"].terminate_session.call_count == 5
        clients["ssm"].send_command.assert_not_called()
        assert json.loads(
            (directory / "cache" / "ssh-host-trust" / (target.alias + ".json")).read_text()
        )["key"] == " ".join(host.with_suffix(".pub").read_text().split()[:2])
    finally:
        transport.close()
        if fresh is not None:
            fresh.close()
        for server, stream in servers:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=5)
            stream.close()


@mark.parametrize(
    "response",
    [None, [], "known", {}, {"SessionId": None}, {"SessionId": 1}, {"SessionId": "foreign"}],
)
def test_termination_rejects_invalid_acknowledgement(response):
    ssm = Mock()
    ssm.terminate_session.return_value = response
    with raises(TransportRetryError, match="not certified"):
        terminate_owned_session(ssm, "known", "i-owned", "reason-owned")
    ssm.get_paginator.assert_not_called()


def test_termination_accepts_exact_acknowledgement_without_waiting_for_history():
    ssm = Mock()
    ssm.terminate_session.return_value = {"SessionId": "known"}
    terminate_owned_session(ssm, "known", "i-owned", "reason-owned")
    ssm.terminate_session.assert_called_once_with(SessionId="known")
    ssm.get_paginator.assert_not_called()


@mark.parametrize("failure", ["invalid-response", "api-error"])
def test_failed_termination_retains_owner_and_never_records_completion(transport_world, failure):
    transport, clients, *_ = transport_world
    transport.session_id = "known"
    transport._reason = "reason-owned"
    transport._receipt = "receipt-owned"
    transport._session_start_attempted = True
    transport.record = Mock()
    if failure == "api-error":
        clients["ssm"].terminate_session.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "TerminateSession"
        )
    else:
        clients["ssm"].terminate_session.side_effect = None
        clients["ssm"].terminate_session.return_value = {"SessionId": "foreign"}
    with raises(TransportCleanupError):
        transport.close()
    assert transport.session_id == "known"
    assert transport._reason == "reason-owned"
    assert transport._receipt == "receipt-owned"
    assert transport._session_start_attempted
    assert not transport._terminated
    transport.record.assert_not_called()


def test_valid_termination_records_completion_and_releases_owner(transport_world):
    transport, *_ = transport_world
    transport.session_id = "known"
    transport._reason = "reason-owned"
    transport._receipt = "receipt-owned"
    transport._session_start_attempted = True
    transport.record = Mock()
    transport.close()
    transport.record.assert_called_once_with(
        "transport-stop-receipt-owned.json", {"complete": True}
    )
    assert transport.session_id is None
    assert transport._reason is None
    assert transport._receipt is None
