"""Pure validation and reporting for the standalone bastion probe."""

import base64
import json
import struct
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

from pytest import mark, param, raises

from stelvio.network import SSH_USER

from . import bastion_probe

PROBE_ID = "e" * 32
SSHD_CONFIG = """port 22
addressfamily any
listenaddress 127.0.0.1:22
usepam yes
passwordauthentication no
kbdinteractiveauthentication no
permittty no
maxsessions 0
allowtcpforwarding local
permitopen any
allowagentforwarding no
x11forwarding no
permittunnel no
gatewayports no
"""
LISTENER = 'LISTEN 0 128 127.0.0.1:22 0.0.0.0:* users:(("sshd",pid=123,fd=3))'
KEY_BLOB = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(range(32))
KEY_DATA = base64.b64encode(KEY_BLOB).decode()
PUBLIC_KEY = f"ssh-ed25519 {KEY_DATA}"


@mark.parametrize("probe_id", [PROBE_ID, "A", "a" * 64, "proof_9-a"])
def test_valid_probe_arguments_accept_the_tunnel_account(probe_id):
    bastion_probe.validate_arguments(probe_id, SSH_USER)


@mark.parametrize("probe_id", ["", "a" * 65, "-probe", "../probe", "two words", "pröbe", "a\nb"])
def test_invalid_probe_identity_is_rejected(probe_id):
    with raises(ValueError, match="probe_id"):
        bastion_probe.validate_arguments(probe_id, SSH_USER)


@mark.parametrize("user", ["", "AUser", "a" * 33, "user/name", "two users", "user;id", "-user"])
def test_invalid_ssh_account_is_rejected(user):
    with raises(ValueError, match="SSH user"):
        bastion_probe.validate_arguments(PROBE_ID, user)


def test_effective_sshd_configuration_allows_only_the_intended_tunnel_policy():
    bastion_probe.validate_config(SSHD_CONFIG)


@mark.parametrize(
    ("field", "value", "bad_value"),
    [
        ("listenaddress", "127.0.0.1:22", "0.0.0.0:22"),
        ("passwordauthentication", "no", "yes"),
        ("kbdinteractiveauthentication", "no", "yes"),
        ("permittty", "no", "yes"),
        ("maxsessions", "0", "10"),
        ("allowtcpforwarding", "local", "yes"),
        ("permitopen", "any", "none"),
        ("allowagentforwarding", "no", "yes"),
        ("x11forwarding", "no", "yes"),
        ("permittunnel", "no", "yes"),
        ("gatewayports", "no", "yes"),
    ],
)
def test_changed_sshd_policy_names_the_bad_field(field, value, bad_value):
    config = SSHD_CONFIG.replace(f"{field} {value}\n", f"{field} {bad_value}\n")

    with raises(ValueError, match=f"sshd {field}"):
        bastion_probe.validate_config(config)


def test_missing_sshd_policy_is_rejected():
    with raises(ValueError, match="sshd maxsessions"):
        bastion_probe.validate_config(SSHD_CONFIG.replace("maxsessions 0\n", ""))


def test_duplicate_sshd_listener_configuration_is_rejected():
    with raises(ValueError, match="sshd listenaddress"):
        bastion_probe.validate_config(SSHD_CONFIG + "listenaddress 127.0.0.1:22\n")


def test_one_live_ipv4_loopback_ssh_listener_is_accepted():
    bastion_probe.validate_listeners(LISTENER)


@mark.parametrize(
    "output",
    [
        param("", id="none"),
        param(LISTENER.replace("127.0.0.1:22", "*:22"), id="wildcard"),
        param(LISTENER.replace("127.0.0.1:22", "0.0.0.0:22"), id="all-ipv4"),
        param(LISTENER.replace("127.0.0.1:22", "[::]:22"), id="all-ipv6"),
        param(LISTENER.replace("127.0.0.1:22", "[::1]:22"), id="ipv6-loopback"),
        param(LISTENER.replace("127.0.0.1:22", "198.51.100.10:22"), id="public-address"),
        param(LISTENER + "\n" + LISTENER, id="duplicate"),
        param("LISTEN", id="malformed"),
    ],
)
def test_unexpected_live_ssh_listener_is_rejected(output):
    with raises(ValueError, match="SSH listener"):
        bastion_probe.validate_listeners(output)


def test_ed25519_public_key_is_normalized_without_its_comment(tmp_path):
    path = tmp_path / "key.pub"
    path.write_text(PUBLIC_KEY + " test-only public key\n")

    assert bastion_probe.public_key(path) == PUBLIC_KEY


@mark.parametrize(
    "source",
    [
        param("", id="empty"),
        param(PUBLIC_KEY + "\n" + PUBLIC_KEY, id="multiple-keys"),
        param("ssh-rsa " + KEY_DATA, id="wrong-algorithm"),
        param("ssh-ed25519 not-base64!", id="bad-base64"),
        param("ssh-ed25519 " + base64.b64encode(b"short").decode(), id="short-wire-key"),
        param("ssh-ed25519 " + base64.b64encode(bytes(51)).decode(), id="wrong-wire-header"),
        param(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nPRIVATE-MATERIAL-MUST-NOT-APPEAR\n"
            "-----END OPENSSH PRIVATE KEY-----\n",
            id="private-key-file",
        ),
    ],
)
def test_malformed_public_keys_do_not_expose_file_contents(tmp_path, source):
    path = tmp_path / "key.pub"
    path.write_text(source)

    with raises(ValueError, match=r"public key|base64") as failure:
        bastion_probe.public_key(path)

    assert "PRIVATE-MATERIAL-MUST-NOT-APPEAR" not in str(failure.value)


def test_checked_command_returns_stdout_and_bounds_execution(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="observed\n", stderr=""))
    monkeypatch.setattr(bastion_probe.subprocess, "run", run)

    assert bastion_probe.checked(["/usr/sbin/sshd", "-t"], timeout=3) == "observed\n"
    run.assert_called_once_with(
        ["/usr/sbin/sshd", "-t"], capture_output=True, text=True, timeout=3, check=False
    )


@mark.parametrize(
    ("stderr", "expected"),
    [("", "y" * 32), ("stderr-marker:" + "x" * 10_000, "x" * 32)],
)
def test_checked_command_failure_is_bounded_and_names_program_and_exit(
    monkeypatch, stderr, expected
):
    run = Mock(
        return_value=SimpleNamespace(
            returncode=9, stdout="stdout-marker:" + "y" * 10_000, stderr=stderr
        )
    )
    monkeypatch.setattr(bastion_probe.subprocess, "run", run)

    with raises(RuntimeError) as failure:
        bastion_probe.checked(["/usr/sbin/sshd", "-t"])

    message = str(failure.value)
    assert "/usr/sbin/sshd" in message
    assert "9" in message
    assert expected in message
    assert len(message) < 600


def test_checked_command_timeout_is_not_silently_accepted(monkeypatch):
    monkeypatch.setattr(
        bastion_probe.subprocess,
        "run",
        Mock(side_effect=subprocess.TimeoutExpired(["cloud-init", "status"], 300)),
    )

    with raises(subprocess.TimeoutExpired):
        bastion_probe.checked(["cloud-init", "status"], timeout=300)


def test_successful_main_emits_only_the_current_proof_schema(monkeypatch, capsys):
    probe = SimpleNamespace(stage="initializing", run=Mock())
    constructor = Mock(return_value=probe)
    monkeypatch.setattr(bastion_probe, "Probe", constructor)

    assert bastion_probe.main([PROBE_ID, SSH_USER]) == 0

    constructor.assert_called_once_with(PROBE_ID, SSH_USER)
    probe.run.assert_called_once_with()
    assert json.loads(capsys.readouterr().out) == {
        "probe_id": PROBE_ID,
        "status": "ok",
        "stage": "complete",
    }


def test_failed_main_emits_current_stage_and_bounded_diagnostics(monkeypatch, capsys):
    probe = SimpleNamespace(
        stage="ssh-forwarding", run=Mock(side_effect=RuntimeError("failed\n" + "x" * 10_000))
    )
    monkeypatch.setattr(bastion_probe, "Probe", Mock(return_value=probe))

    assert bastion_probe.main([PROBE_ID, SSH_USER]) == 1

    proof = json.loads(capsys.readouterr().out)
    assert set(proof) == {"probe_id", "status", "stage", "error"}
    assert proof["probe_id"] == PROBE_ID
    assert proof["status"] == "error"
    assert proof["stage"] == "ssh-forwarding"
    assert len(proof["error"]) <= 512
    assert "\n" not in proof["error"]


def test_invalid_probe_identity_is_bounded_in_main_failure_report(capsys):
    assert bastion_probe.main(["x" * 1000, SSH_USER]) == 1

    proof = json.loads(capsys.readouterr().out)
    assert proof["probe_id"] == "x" * 64
    assert proof["status"] == "error"
    assert proof["stage"] == "arguments"


@mark.parametrize("arguments", [[], [PROBE_ID], [PROBE_ID, SSH_USER, "unexpected"]])
def test_main_rejects_wrong_argument_count_before_starting_probe(monkeypatch, capsys, arguments):
    constructor = Mock()
    monkeypatch.setattr(bastion_probe, "Probe", constructor)

    assert bastion_probe.main(arguments) == 1

    constructor.assert_not_called()
    proof = json.loads(capsys.readouterr().out)
    assert proof["status"] == "error"
    assert proof["stage"] == "arguments"
