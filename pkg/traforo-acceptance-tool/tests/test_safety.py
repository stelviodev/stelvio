from types import SimpleNamespace

import pytest

from traforo_acceptance import aws, recovery, runner
from traforo_acceptance.host import compare
from traforo_acceptance.report import generate
from traforo_acceptance.store import Store, event, events, write_json

PROOF = {
    "app": "stlv-tunnel-01234567",
    "env": "tunnel-proof",
    "account": "123456789012",
    "profile": "default",
    "region": "us-east-1",
}


class SDK:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def client(self, service, **kwargs):
        parent = self

        class Client:
            def __getattr__(self, operation):
                def call(**arguments):
                    parent.calls.append((service, operation, arguments))
                    response = parent.responses.get((service, operation), {})
                    if isinstance(response, Exception):
                        raise response
                    return response(arguments) if callable(response) else response

                return call

        return Client()


def sdk(**responses):
    return SDK({("sts", "get_caller_identity"): {"Account": PROOF["account"]}, **responses})


def test_reader_bounds_calls_and_pagination():
    reader = aws.Reader(SDK(), calls=1)
    reader.call("sts", "get_caller_identity")
    with pytest.raises(TimeoutError, match="budget"):
        reader.call("sts", "get_caller_identity")
    client = SDK({("x", "list"): {"Items": [], "NextToken": "same"}})
    with pytest.raises(RuntimeError, match="Repeated pagination"):
        list(aws.Reader(client).pages("x", "list", "Items"))
    with pytest.raises(TimeoutError):
        aws.Reader(SDK(), seconds=0).call("sts", "get_caller_identity")


def test_capture_account_mismatch_never_certifies_absence():
    client = SDK({("sts", "get_caller_identity"): {"Account": "999999999999"}})
    with pytest.raises(RuntimeError, match="identity changed"):
        aws.capture(client, PROOF)
    assert len(client.calls) == 1


def test_capture_terminal_state_and_untagged_volume():
    client = sdk()
    client.responses.update(
        {
            ("ec2", "describe_instances"): {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-proof",
                                "State": {"Name": "terminated"},
                                "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-untagged"}}],
                            }
                        ]
                    }
                ]
            },
            ("ec2", "describe_volumes"): lambda args: (
                {"Volumes": [{"VolumeId": "vol-untagged"}]} if "VolumeIds" in args else {}
            ),
            ("ssm", "describe_sessions"): lambda args: (
                {"Sessions": [{"SessionId": "ssm-proof", "Status": "Terminating"}]}
                if args["State"] == "History"
                else {}
            ),
        }
    )
    result = aws.capture(client, PROOF)
    assert result["remaining"] == {"captured_volumes": ["vol-untagged"]}
    assert result["ssm_history"][0]["Status"] == "Terminating"
    assert result["volumes"] == ["vol-untagged"]


def test_capture_retains_secret_identity_before_cluster_disappears():
    client = sdk()
    client.responses[("docdb", "describe_db_clusters")] = {
        "DBClusters": [
            {
                "DBClusterIdentifier": f"{PROOF['app']}-{PROOF['env']}-db",
                "MasterUserSecret": {"SecretArn": "arn:secret:proof"},
            }
        ]
    }
    result = aws.capture(client, PROOF)
    assert result["secrets"] == ["arn:secret:proof"]
    assert "secrets" in result["remaining"]
    assert any(args.get("SecretId") == "arn:secret:proof" for _, _, args in client.calls)


def test_unowned_proof_rejected():
    with pytest.raises(RuntimeError, match="namespace"):
        aws.capture(SDK(), {**PROOF, "app": "production"})


def test_read_failure_is_unknown_not_absent(run_root, monkeypatch):
    event(run_root, "proof_intent", **PROOF, project=str(run_root / "app"))
    monkeypatch.setattr(recovery, "sdk_for", lambda p: SDK())
    monkeypatch.setattr(aws, "capture", lambda *a: (_ for _ in ()).throw(TimeoutError("IAM read")))
    result = recovery.audit(run_root, None, {})
    assert result["outcome"] == "unknown-or-remaining"
    assert result["findings"][PROOF["app"]]["outcome"] == "unknown"


def test_cleanup_refuses_foreign_project(run_root):
    event(run_root, "proof_intent", **PROOF, project="/outside/foreign")
    called = []
    result = recovery.recover(run_root, lambda *args: called.append(args), {})
    assert not called
    assert "outside" in result[0]["error"]


def test_partial_deployment_destroy_uses_registered_identity(run_root, monkeypatch):
    project = run_root / "app"
    project.mkdir()
    proof = {**PROOF, "project": str(project)}
    write_json(project / ".acceptance-owner.json", proof)
    event(run_root, "proof_intent", **proof)
    monkeypatch.setattr(recovery, "sdk_for", lambda p: sdk())
    snapshots = iter(
        [
            {"remaining": {"Vpcs": ["vpc-proof"]}, "functions": [], "volumes": []},
            {"remaining": {}, "functions": []},
        ]
    )
    monkeypatch.setattr(aws, "capture", lambda *args: next(snapshots))
    commands = []
    result = recovery.recover(run_root, lambda *args: commands.append(args), {})
    assert result[0]["outcome"] == "completed"
    assert commands[0][1][-3:] == ["destroy", "tunnel-proof", "--yes"]
    assert commands[0][2] == project
    assert events(run_root)[1]["kind"] == "proof_inventory"


def test_authorization_failure_retained(run_root, monkeypatch):
    image = run_root / "original"
    image.write_text("image")
    from traforo_acceptance.store import digest

    event(run_root, "host_intent", backup=str(image), digest=digest(image))

    def refuse(*args):
        raise TimeoutError("Administrator dialog exceeded 180s")

    result = recovery.recover(run_root, refuse, {})
    assert result[0]["outcome"] == "failed"
    assert "180s" in result[0]["error"]
    assert image.read_text() == "image"


def test_cleanup_gap_not_hidden_by_empty_namespace(run_root):
    event(run_root, "inventory_error", **PROOF, error="Cannot capture root volume")
    result = recovery.audit(run_root, None, {})
    assert result["findings"]["inventory_gaps"]["outcome"] == "unknown"


def test_runner_stops_before_live_effects_after_local_failure(run_root, monkeypatch):
    store = Store(run_root)
    data = store.data
    data.update(selected=["prepare", "native"], identity={"fingerprint": "frozen"})
    store.save(data)
    (run_root / "source").mkdir()
    monkeypatch.setattr(runner, "verify", lambda *a: None)
    monkeypatch.setattr(runner.candidate, "validate", lambda *a: None)
    monkeypatch.setattr(
        runner.process, "run", lambda *a: (_ for _ in ()).throw(RuntimeError("test failure"))
    )
    monkeypatch.setattr(
        runner, "authorize", lambda *a: pytest.fail("Must not authorize host changes")
    )
    result = runner.execute_stages(run_root)
    assert result["results"]["prepare"]["outcome"] == "failed"
    assert "native" not in result["results"]
    assert result["outcome"] == "failed-or-incomplete"


def test_noninteractive_live_requires_confirmation(run_root, monkeypatch):
    store = Store(run_root)
    data = store.data
    data.update(selected=["native"], artifacts={"binary": {"path": "/local", "sha256": "abc"}})
    monkeypatch.setattr(runner.candidate, "validate", lambda *a: None)
    monkeypatch.setattr(
        runner.host,
        "snapshot",
        lambda: {
            "os": "15.7.5",
            "helper": None,
            "service": None,
            "state_exists": False,
            "administration_remnants": [],
        },
    )
    with pytest.raises(RuntimeError, match="expected-account"):
        runner.authorize(run_root, data, {}, lambda *a: pytest.fail("No effects allowed"), True)
    assert not (run_root / "host-before.json").exists()


def test_host_audit_preserves_foreign_resolver_and_order():
    baseline = {
        "interfaces": ["en0"],
        "routes": "",
        "dns": "domain : a\ndomain : b",
        "resolvers": {"/etc/resolver/a": {"inode": 1}},
        "helper": None,
        "service": None,
        "processes": "PID PPID UID COMM\n",
        "state_exists": False,
    }
    after = {
        **baseline,
        "resolvers": {"/etc/resolver/a": {"inode": 2}},
        "dns": "domain : b\ndomain : a",
        "administration_remnants": ["foreign"],
    }
    checks = compare(baseline, after)
    assert not checks["resolvers"] and not checks["dns"] and not checks["administration"]


def test_audit_history_not_erased(run_root, monkeypatch):
    # Failed audits must remain in the event stream even after a later successful read.
    store = Store(run_root)
    data = store.data
    data["audit"] = {"outcome": "unknown-or-remaining"}
    store.save(data)
    recovery.audit(run_root, None, {})
    assert store.data["audit"]["outcome"] == "certified"
    # Report still distinguishes missing acceptance results from clean cleanup.
    assert generate(run_root)["outcome"] == "failed-or-incomplete"


def test_inventory_requests_match_actual_aws_api_models():
    import botocore.session
    from botocore import xform_name
    from botocore.validate import validate_parameters

    models = botocore.session.get_session()
    client = sdk()
    original_client = client.client

    def validated_client(service, **kwargs):
        wrapped = original_client(service, **kwargs)
        model = models.get_service_model(service)

        class Validated:
            def __getattr__(self, name):
                operation = next(op for op in model.operation_names if xform_name(op) == name)

                def call(**arguments):
                    validate_parameters(arguments, model.operation_model(operation).input_shape)
                    return getattr(wrapped, name)(**arguments)

                return call

        return Validated()

    client.client = validated_client
    assert aws.capture(client, PROOF)["remaining"] == {}


def test_idle_helper_restore_uses_production_transaction(tmp_path):
    from traforo_acceptance.admin_adapter import restore_images
    from traforo_acceptance.store import digest

    original = tmp_path / "original"
    current = tmp_path / "installed"
    original.write_text("original")
    current.write_text("candidate")
    calls = []
    i = SimpleNamespace(
        INSTALLED_HELPER=current,
        _installed_digest=lambda: digest(current),
        _bootstrap=lambda p, sha: (p, sha),
        _wait_ready=lambda: None,
    )

    def authorize(script):
        calls.append(script)
        current.write_bytes(script[0].read_bytes())

    i._authorize = authorize

    def idle():
        return SimpleNamespace(owned=False, uncertain=False, mutating=False, units=[])

    restore_images(
        i,
        idle,
        str(original),
        digest(original),
        {digest(current): str(current), digest(original): str(original)},
    )
    assert len(calls) == 1
    assert current.read_text() == "original"


def test_live_and_unknown_images_refuse_recovery(tmp_path):
    from traforo_acceptance.admin_adapter import restore_images
    from traforo_acceptance.store import digest

    current = tmp_path / "installed"
    current.write_text("candidate")
    i = SimpleNamespace(INSTALLED_HELPER=current, _installed_digest=lambda: digest(current))

    def idle():
        return SimpleNamespace(owned=True, uncertain=False, mutating=False, units=[])

    with pytest.raises(RuntimeError, match="not a recorded"):
        restore_images(i, idle, "", "", {})
    with pytest.raises(RuntimeError, match="Live or uncertain"):
        restore_images(i, idle, "", "", {digest(current): str(current)})


def test_original_absence_uninstalls_only_an_existing_image(tmp_path):
    from traforo_acceptance.admin_adapter import restore_images
    from traforo_acceptance.store import digest

    current = tmp_path / "installed"
    current.write_text("candidate")
    called = []
    i = SimpleNamespace(
        INSTALLED_HELPER=current,
        _installed_digest=lambda: digest(current) if current.exists() else None,
        uninstall_helper=lambda: called.append(True),
    )

    def idle():
        return SimpleNamespace(owned=False, uncertain=False, mutating=False, units=[])

    restore_images(i, idle, "", "", {digest(current): str(current)})
    assert called == [True]
    current.unlink()
    called.clear()
    restore_images(i, idle, "", "", {})
    assert not called  # never take uninstall_helper's download fallback


def test_interrupted_replacement_retains_unknown_stage(tmp_path):
    from traforo_acceptance.admin_adapter import restore_images

    (tmp_path / ".stelvio-traforo.bootstrap.foreign").write_text("foreign")
    i = SimpleNamespace(INSTALLED_HELPER=tmp_path / "missing", _installed_digest=lambda: None)
    with pytest.raises(RuntimeError, match="Unsafe administration|Unrecorded staged"):
        restore_images(i, lambda: None, "", "", {})
    assert (tmp_path / ".stelvio-traforo.bootstrap.foreign").read_text() == "foreign"


def test_interrupted_owned_replacement_finishes_before_original_restore(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    from traforo_acceptance.admin_adapter import restore_images
    from traforo_acceptance.store import digest

    original, candidate, installed = (
        tmp_path / name for name in ("original", "candidate", "installed")
    )
    original.write_bytes(b"original")
    candidate.write_bytes(b"candidate")
    candidate_digest = digest(candidate)
    stage = tmp_path / f".stelvio-traforo.bootstrap.{candidate_digest}"
    stage.write_bytes(b"cand")  # interrupted copy, completed by the production transaction
    real_lstat = Path.lstat

    def root_owned(path):
        value = real_lstat(path)
        if path == stage:
            fields = list(value)
            fields[4] = 0
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, "lstat", root_owned)
    calls = []

    def authorize(script):
        image, expected = script
        assert digest(image) == expected
        calls.append(expected)
        installed.write_bytes(image.read_bytes())
        stage.unlink(missing_ok=True)

    i = SimpleNamespace(
        INSTALLED_HELPER=installed,
        _installed_digest=lambda: digest(installed) if installed.exists() else None,
        _bootstrap=lambda path, sha: (path, sha),
        _authorize=authorize,
        _wait_ready=lambda: None,
    )
    restore_images(
        i,
        lambda: SimpleNamespace(owned=False, uncertain=False, mutating=False, units=[]),
        str(original),
        digest(original),
        {digest(original): str(original), candidate_digest: str(candidate)},
    )
    assert calls == [candidate_digest, digest(original)]
    assert installed.read_bytes() == b"original"


def test_source_drift_blocks_automatic_recovery_execution(run_root, monkeypatch):
    store = Store(run_root)
    data = store.data
    data["live_authorized"] = "previous consent"
    store.save(data)
    (run_root / "source").mkdir()

    def changed(*args):
        raise RuntimeError("Frozen candidate changed")

    monkeypatch.setattr(runner, "verify", changed)
    monkeypatch.setattr(
        recovery, "recover", lambda *a: pytest.fail("Do not execute changed source")
    )
    result = runner.execute_stages(run_root)
    assert result["outcome"] == "failed-or-incomplete"
    assert any("Frozen candidate" in e.get("error", "") for e in result["failures"])


def test_external_live_adapter_records_before_deploy_and_keeps_cleanup_signature(
    run_root, monkeypatch
):
    import importlib
    import sys
    from types import ModuleType

    from traforo_acceptance import pytest_plugin

    monkeypatch.setenv("TRA_ACCEPTANCE_RUN", str(run_root))
    plugin = importlib.reload(pytest_plugin)

    class Session:
        def deploy(self):
            raise RuntimeError("partial deploy failure")

        def destroy(self):
            pass

        def _start(self, arguments, *, mode):
            pass

    class Host:
        def save(self, name, value):
            pass

    sessions = ModuleType("tests.integration.cli_session")
    sessions.StandaloneCliSession = Session
    hosts = ModuleType("tests.integration.macos_host")
    hosts.MacOSHost = Host
    tunnel = ModuleType("stelvio.tunnel")
    tunnel.installation = SimpleNamespace(
        _bootstrap=lambda asset, digest, *, cleanup=False: cleanup
    )
    monkeypatch.setitem(sys.modules, "tests.integration.cli_session", sessions)
    monkeypatch.setitem(sys.modules, "tests.integration.macos_host", hosts)
    monkeypatch.setitem(sys.modules, "stelvio.tunnel", tunnel)
    plugin.pytest_configure(SimpleNamespace(getoption=lambda *a, **kw: True))
    assert tunnel.installation._bootstrap(run_root / "image", "digest", cleanup=True) is True
    session = Session()
    for key, value in {**PROOF, "environment_name": PROOF["env"], "project": run_root}.items():
        setattr(session, key, value)
    with pytest.raises(RuntimeError, match="partial deploy failure"):
        session.deploy()
    records = events(run_root)
    assert [r["kind"] for r in records][-2:] == ["proof_intent", "proof_outputs"]
    assert (run_root / ".acceptance-owner.json").exists()


def test_independent_host_audit_rejects_changed_admin_lock():
    baseline = {
        "interfaces": [],
        "routes": "",
        "dns": "",
        "resolvers": {},
        "helper": None,
        "service": None,
        "processes": "",
        "state_exists": False,
        "administration_lock": {"valid": True, "identity": [1, 2]},
    }
    current = {
        **baseline,
        "administration_remnants": [],
        "administration_lock": {"valid": True, "identity": [1, 3]},
    }
    assert not compare(baseline, current)["administration_lock"]
    current["administration_lock"] = {"valid": False, "identity": [1, 2]}
    assert not compare(baseline, current)["administration_lock"]
