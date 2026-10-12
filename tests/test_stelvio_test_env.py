import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from pytest import fixture, mark, param, raises

from stelvio.app import StelvioApp
from tests.integration import stelvio_test_env as env_module
from tests.integration.stelvio_test_env import StelvioTestEnv


class FakeAutomationStack:
    def __init__(self):
        self.destroy_outcomes: list[Exception | None] = []
        self.refresh_error: Exception | None = None
        self.state_path: Path | None = None
        self.needs_refresh = False

    def up(self, *, on_output):
        return SimpleNamespace(outputs={"ready": SimpleNamespace(value="deployed")})

    def destroy(self, *, on_output):
        if self.needs_refresh:
            raise RuntimeError("stack state needs refresh before another destroy")
        outcome = self.destroy_outcomes.pop(0) if self.destroy_outcomes else None
        if outcome is not None:
            self.needs_refresh = True
            raise outcome

    def refresh(self, *, on_output):
        if self.refresh_error is not None:
            raise self.refresh_error
        self.needs_refresh = False


@fixture
def fake_automation(hermetic_aws, monkeypatch, tmp_path):
    stack = FakeAutomationStack()

    def create_stack(*, opts, **_kwargs):
        backend = Path(unquote(urlsplit(opts.project_settings.backend.url).path))
        stack.state_path = backend / "checkpoint.json"
        stack.state_path.write_text("recoverable stack state")
        return stack

    monkeypatch.setattr(env_module, "create_or_select_stack", create_stack)
    monkeypatch.setattr(env_module, "_get_pulumi_command", lambda: object())
    monkeypatch.setattr(env_module, "get_stelvio_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        env_module,
        "tempfile",
        SimpleNamespace(mkdtemp=lambda **kwargs: tempfile.mkdtemp(dir=tmp_path, **kwargs)),
    )
    return stack


@mark.parametrize("retry", [False, True], ids=["first-attempt", "after-refresh"])
def test_destroy_removes_state_after_success(fake_automation, retry):
    if retry:
        fake_automation.destroy_outcomes = [RuntimeError("initial destroy failure"), None]
    env = StelvioTestEnv("cleanup-success")
    assert env.deploy(lambda: None) == {"ready": "deployed"}
    state_path = fake_automation.state_path
    assert state_path is not None
    assert state_path.read_text() == "recoverable stack state"

    env.destroy()

    assert not state_path.parent.exists()
    # A fresh public app must work immediately after cleanup, including a retry.
    app = StelvioApp("after-cleanup")
    assert StelvioApp.get_instance() is app
    StelvioTestEnv("unused").destroy()


@mark.parametrize(
    "failure",
    [param("refresh", id="refresh-fails"), param("destroy", id="retry-destroy-fails")],
)
def test_destroy_raises_and_retains_state_when_recovery_fails(fake_automation, failure):
    initial_error = RuntimeError("initial destroy failure")
    recovery_error = RuntimeError(f"{failure} failure")
    fake_automation.destroy_outcomes = [initial_error]
    if failure == "refresh":
        fake_automation.refresh_error = recovery_error
    else:
        fake_automation.destroy_outcomes.append(recovery_error)
    env = StelvioTestEnv("cleanup-failure")
    assert env.deploy(lambda: None) == {"ready": "deployed"}
    state_path = fake_automation.state_path
    assert state_path is not None

    with raises(RuntimeError, match="Could not destroy integration stack") as error:
        env.destroy()

    assert str(error.value) == (
        "Could not destroy integration stack 'integ-cleanup-failure' after refresh/retry.\n"
        "First error: initial destroy failure\n"
        f"Retry error: {failure} failure\n"
        f"State dir: {state_path.parent}\n"
        "Manual cleanup required; preserved state can be used to recover the stack."
    )
    assert error.value.__cause__ is recovery_error
    assert state_path.read_text() == "recoverable stack state"
    app = StelvioApp("after-cleanup-failure")
    assert StelvioApp.get_instance() is app
    StelvioTestEnv("unused").destroy()


def test_destroy_without_deployment_is_safe():
    env = StelvioTestEnv("not-deployed")
    StelvioApp("before-unused-cleanup")
    env.destroy()
    app = StelvioApp("after-unused-cleanup")
    assert StelvioApp.get_instance() is app
    env.destroy()


def test_cleanup_failure_makes_a_passing_pytest_body_fail(tmp_path):
    # Use the real integration fixture in an isolated pytest session so its
    # teardown error and exit status are observed after the body has passed.
    (tmp_path / "conftest.py").write_text(
        """
import json
from pathlib import Path

from tests.conftest import _hermetic_home, hermetic_aws
from tests.test_stelvio_test_env import fake_automation
from tests.integration.conftest import stelvio_env

outcomes = []

def pytest_runtest_logreport(report):
    outcomes.append([report.when, report.outcome])

def pytest_sessionfinish(session, exitstatus):
    Path(__file__).with_name("outcomes.json").write_text(json.dumps(outcomes))
"""
    )
    (tmp_path / "test_cleanup.py").write_text(
        """
def test_body_passes(fake_automation, stelvio_env):
    fake_automation.destroy_outcomes = [
        RuntimeError("initial destroy failure"),
        RuntimeError("retry destroy failure"),
    ]
    assert stelvio_env.deploy(lambda: None) == {"ready": "deployed"}
"""
    )
    result = subprocess.run(  # noqa: S603 — fixed interpreter and generated test only
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path / "test_cleanup.py"),
            f"--rootdir={tmp_path}",
            f"--basetemp={tmp_path / 'pytest-temp'}",
            "-q",
            "--disable-warnings",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads((tmp_path / "outcomes.json").read_text()) == [
        ["setup", "passed"],
        ["call", "passed"],
        ["teardown", "failed"],
    ]
    states = list((tmp_path / "pytest-temp").rglob("checkpoint.json"))
    assert len(states) == 1
    assert states[0].read_text() == "recoverable stack state"
