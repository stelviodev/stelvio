import json
import os
import subprocess
import sys

import pytest
from click.testing import CliRunner
from conftest import commit

from traforo_acceptance import candidate, process, report
from traforo_acceptance.cli import main, timeouts
from traforo_acceptance.host import stable_dns, stable_routes
from traforo_acceptance.runner import environment, verify
from traforo_acceptance.stages import STAGES, select
from traforo_acceptance.store import Store, digest, event, events, redact


def test_selection_dependencies_and_groups():
    assert select((), ()) == list(STAGES)
    assert select(("interop",), ()) == ["prepare", "build", "distribution", "interop"]
    assert "native" not in select(("aws-dev",), ())
    assert not any(s.startswith("aws-") for s in select((), ("aws",)))
    with pytest.raises(ValueError, match="requires excluded"):
        select(("interop",), ("build",))
    with pytest.raises(ValueError, match="Unknown"):
        select(("unknown",), ())


def test_private_store_and_lock(run_root):
    store = Store(run_root)
    with store.locked(), pytest.raises(RuntimeError, match="Another process"):
        with Store(run_root).locked():
            pass
    run_root.chmod(0o755)
    with pytest.raises(RuntimeError, match="private"):
        Store(run_root)


def test_redaction(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "abcdefgh123456")
    assert "abcdefgh123456" not in redact("Bearer abcdefgh123456")
    assert "topsecret" not in redact('"Password": "topsecret"')


def test_command_evidence_and_failed_exit(run_root):
    with pytest.raises(process.CommandFailure, match="exit 7"):
        process.run(
            run_root,
            "test",
            [sys.executable, "-c", "print('failure-line');exit(7)"],
            run_root,
            dict(os.environ),
            5,
        )
    record = json.loads(next((run_root / "commands").glob("*/command.json")).read_text())
    assert record["exit_code"] == 7
    assert record["runtime_seconds"] >= 0
    assert "failure-line" in next((run_root / "commands").glob("*/output.log")).read_text()
    assert any(r["kind"] == "command_end" for r in events(run_root))


def test_command_timeout(run_root):
    with pytest.raises(process.CommandFailure, match="timeout"):
        process.run(
            run_root,
            "slow",
            [sys.executable, "-c", "import time;time.sleep(5)"],
            run_root,
            dict(os.environ),
            0.1,
        )
    assert next(r for r in events(run_root) if r["kind"] == "command_end")["stopped"] == "timeout"


def test_refuse_signal_changed_identity(monkeypatch):
    monkeypatch.setattr(
        process, "identity", lambda pid: {"pid": pid, "created": 2, "uid": os.getuid()}
    )
    with pytest.raises(RuntimeError, match="identity changed"):
        process.signal_owned({"pid": 123, "created": 1, "uid": os.getuid()}, 2)


def test_checkout_patch_and_caller_untouched(repo, tmp_path):
    base = candidate.git(repo, "rev-parse", "HEAD")
    (repo / "file").write_text("patched\n")
    commit(repo, "patch")
    patch = candidate.git(repo, "rev-parse", "HEAD")
    destination = tmp_path / "candidate"
    identity = candidate.checkout(repo, base, (patch,), destination)
    assert identity["commit"] == base
    assert identity["patches"] == [patch]
    assert (destination / "file").read_text() == "patched\n"
    assert candidate.git(repo, "rev-parse", "HEAD") == patch
    assert candidate.git(repo, "status", "--porcelain") == ""


def test_conflicting_patch_retains_evidence(repo, tmp_path):
    (repo / "file").write_text("first\n")
    commit(repo, "first")
    patch = candidate.git(repo, "rev-parse", "HEAD")
    with pytest.raises(subprocess.CalledProcessError):
        candidate.checkout(repo, patch, (patch,), tmp_path / "candidate")
    assert list(tmp_path.glob("patch-*.diff"))


def test_empty_patch_rejected(repo, tmp_path):
    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "empty"], check=True)
    patch = candidate.git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="Empty patch"):
        candidate.checkout(repo, "HEAD", (patch,), tmp_path / "candidate")


def test_source_and_artifact_reuse(repo, run_root):
    identity = candidate.checkout(repo, "HEAD", (), run_root / "source")
    image = run_root / "image"
    image.write_bytes(b"image")
    data = {
        "identity": identity,
        "artifacts": {"binary": {"path": str(image), "sha256": digest(image)}},
    }
    verify(run_root, data)
    image.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="Artifact changed"):
        verify(run_root, data)
    data["artifacts"] = {}
    (run_root / "source/file").write_text("changed")
    with pytest.raises(RuntimeError, match="Frozen candidate"):
        verify(run_root, data)


def test_environment_forces_local_selection(run_root, monkeypatch):
    monkeypatch.setenv("STLV_TEST_TRAFORO_RELEASE_DOWNLOADS", "1")
    monkeypatch.setenv("STLV_TRAFORO_DEV_BINARY", "/wrong")
    data = Store(run_root).data
    data["artifacts"] = {"binary": {"path": "/frozen/image"}}
    env = environment(run_root, data)
    assert env["STLV_TRAFORO_DEV_BINARY"] == env["STLV_TEST_TRAFORO_IMAGE"] == "/frozen/image"
    assert "STLV_TEST_TRAFORO_RELEASE_DOWNLOADS" not in env
    assert env["GOTOOLCHAIN"] == "go1.27.3"


def test_report_deterministic_and_cleanup_does_not_erase_failure(run_root):
    store = Store(run_root)
    data = store.data
    data["results"] = {"prepare": {"outcome": "passed", "runtime": 1}}
    data["audit"] = {"outcome": "certified"}
    store.save(data)
    event(
        run_root, "test_result", nodeid="case", phase="teardown", outcome="failed", trace="line 3"
    )
    report.generate(run_root)
    first = (run_root / "report.json").read_bytes()
    assert report.generate(run_root)["outcome"] == "failed-or-incomplete"
    assert first == (run_root / "report.json").read_bytes()
    assert "line 3" in (run_root / "report.md").read_text()


def test_partial_success_and_implicit_skip(run_root):
    store = Store(run_root)
    data = store.data
    data.update(results={"prepare": {"outcome": "passed"}}, audit={"outcome": "certified"})
    store.save(data)
    assert report.generate(run_root)["outcome"] == "partial-passed"
    event(run_root, "test_result", stage="native", outcome="skipped", phase="setup")
    assert report.generate(run_root)["outcome"] == "failed-or-incomplete"


def test_dns_and_routes_normalization():
    assert stable_dns("resolver #1\n reach : 1\n domain : a") == stable_dns(
        "resolver #99\n reach : 9\n domain : a"
    )
    assert stable_dns("domain : a\ndomain : b") != stable_dns("domain : b\ndomain : a")
    assert stable_routes("Destination Gateway Flags Netif\n10/8 x UG utun1\n1 x UHL en0") == [
        ["10/8", "x", "UG", "utun1"]
    ]


def test_cli_dry_run_does_not_create_environment(repo):
    result = CliRunner().invoke(
        main, ["run", "HEAD", "--repo", str(repo), "--only", "build", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["stages"] == ["prepare", "build"]
    assert CliRunner().invoke(main, ["stages"]).exit_code == 0


def test_bad_timeout():
    with pytest.raises(Exception, match="POSITIVE_SECONDS"):
        timeouts(("native=-1",))


def test_plugin_emits_phase_failure_immediately(run_root, monkeypatch):
    import importlib
    from types import SimpleNamespace

    from traforo_acceptance import pytest_plugin

    monkeypatch.setenv("TRA_ACCEPTANCE_RUN", str(run_root))
    plugin = importlib.reload(pytest_plugin)
    plugin.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid="case",
            when="teardown",
            outcome="failed",
            duration=1.5,
            longrepr="assertion at fixture.py:20",
            location=("fixture.py", 19, "case"),
        )
    )
    assert events(run_root)[-1]["phase"] == "teardown"
    assert "fixture.py:20" in events(run_root)[-1]["trace"]


def test_observer_survives_candidate_os_mocks(run_root, monkeypatch):
    import fcntl

    def busy(*args, **kwargs):
        raise BlockingIOError("candidate fixture mock")

    monkeypatch.setattr(fcntl, "flock", busy)
    monkeypatch.setattr(os, "open", busy)
    event(run_root, "test_result", outcome="failed", phase="call")
    assert events(run_root)[-1]["outcome"] == "failed"


def test_external_pytest_records_teardown_before_session_exit(run_root):
    case = run_root / "test_case.py"
    case.write_text("""import fcntl
import pytest
@pytest.fixture
def resource(monkeypatch):
    monkeypatch.setattr(fcntl, "flock", lambda *a: (_ for _ in ()).throw(BlockingIOError()))
    yield
    assert False, "teardown evidence"
def test_body(resource):
    assert True
""")
    env = dict(os.environ, TRA_ACCEPTANCE_RUN=str(run_root), TRA_ACCEPTANCE_STAGE="interop")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "traforo_acceptance.pytest_plugin", str(case), "-q"],
        cwd=run_root,
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 1, completed.stdout
    results = [r for r in events(run_root) if r["kind"] == "test_result"]
    assert [r["outcome"] for r in results] == ["passed", "passed", "failed"]
    assert results[-1]["failure_location"]["path"].endswith("test_case.py")
    assert events(run_root)[-1]["kind"] == "pytest_end"


def test_full_pass_requires_every_stage_and_clean_audit(run_root):
    store = Store(run_root)
    data = store.data
    data.update(
        selected=list(STAGES),
        results={n: {"outcome": "passed"} for n in STAGES},
        audit={"outcome": "certified"},
    )
    store.save(data)
    event(run_root, "test_result", stage="python", phase="setup", outcome="skipped")
    assert report.generate(run_root)["outcome"] == "passed"
    data["audit"] = {"outcome": "unknown"}
    store.save(data)
    assert report.generate(run_root)["outcome"] == "failed-or-incomplete"


def test_archived_tool_and_artifact_reuse_are_bound(repo, run_root):
    data = Store(run_root).data
    data["identity"] = candidate.checkout(repo, "HEAD", (), run_root / "source")
    data["tool_fingerprint"] = candidate.freeze_tool(run_root)
    verify(run_root, data)
    (run_root / "observer/traforo_acceptance/__init__.py").write_text("changed")
    with pytest.raises(RuntimeError, match="observer changed"):
        verify(run_root, data)


def test_ssh_test_location_rejects_writable_ancestors(run_root):
    from traforo_acceptance.runner import safe_ssh_path

    # The parent is test-owned; no system directory is changed.
    run_root.parent.chmod(0o777)
    with pytest.raises(RuntimeError, match="StrictModes"):
        safe_ssh_path(run_root)


def test_redaction_never_corrupts_json_or_password_test_names(run_root):
    from traforo_acceptance.store import write_json

    value = {
        "nodeid": "test_manage_master_user_password",
        "location": ["test_password"],
        "Password": "topsecret",
        "trace": r'"password": "secret-value"',
    }
    event(run_root, "test_start", **value)
    record = events(run_root)[-1]
    assert record["kind"] == "test_start"
    assert record["nodeid"] == value["nodeid"]
    assert record["location"] == value["location"]
    assert "secret-value" not in record["trace"]
    write_json(run_root / "redacted.json", value)
    assert json.loads((run_root / "redacted.json").read_text())["Password"] == "[REDACTED]"
