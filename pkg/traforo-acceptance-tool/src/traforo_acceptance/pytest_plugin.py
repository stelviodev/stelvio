"""External pytest observer, loaded from this package, never from modified candidate code."""

from __future__ import annotations

import functools
import os
from pathlib import Path

from .store import digest, event, write_json

ROOT = Path(os.environ.get("TRA_ACCEPTANCE_RUN", "/nonexistent"))


def pytest_runtest_logstart(nodeid, location):
    event(
        ROOT,
        "test_start",
        stage=os.environ.get("TRA_ACCEPTANCE_STAGE"),
        nodeid=nodeid,
        location=location,
    )


def pytest_runtest_logreport(report):
    crash = getattr(report.longrepr, "reprcrash", None)
    event(
        ROOT,
        "test_result",
        stage=os.environ.get("TRA_ACCEPTANCE_STAGE"),
        nodeid=report.nodeid,
        phase=report.when,
        outcome=report.outcome,
        duration=report.duration,
        trace=str(report.longrepr) if report.longrepr else None,
        location=report.location,
        failure_location={"path": str(crash.path), "line": crash.lineno, "message": crash.message}
        if crash
        else None,
    )


def pytest_sessionfinish(session, exitstatus):
    event(ROOT, "pytest_end", exit_code=int(exitstatus), collected=session.testscollected)


def pytest_configure(config):
    if not (
        config.getoption("integration_macos_privileged", default=False)
        or config.getoption("integration_tunnel", default=False)
    ):
        return
    from stelvio.tunnel import installation
    from tests.integration.cli_session import StandaloneCliSession
    from tests.integration.macos_host import MacOSHost

    original_bootstrap = installation._bootstrap

    @functools.wraps(original_bootstrap)
    def bootstrap(asset, digest, *, cleanup=False):
        event(ROOT, "admin_image_intent", path=str(asset), digest=digest)
        return original_bootstrap(asset, digest, cleanup=cleanup)

    installation._bootstrap = bootstrap
    original_save = MacOSHost.save

    @functools.wraps(original_save)
    def save(host, name, value):
        if name == "baseline":
            original = getattr(host, "original", None)
            backup = None
            if original:
                backup = ROOT / f"original-helper-{os.getpid()}"
                backup.write_bytes(original.read_bytes())
                backup.chmod(0o700)
            # This is fsynced before install_helper, including fresh-install absence.
            event(
                ROOT,
                "host_intent",
                baseline=value,
                backup=str(backup) if backup else None,
                digest=digest(backup) if backup else None,
            )
        return original_save(host, name, value)

    MacOSHost.save = save
    original_start = StandaloneCliSession._start
    original_deploy = StandaloneCliSession.deploy
    original_destroy = StandaloneCliSession.destroy

    def registration(session):
        return {
            **({"suite": "lambda-vpc"} if getattr(session, "_acceptance_lambda", False) else {}),
            "app": session.app,
            "env": session.environment_name,
            "project": str(session.project.resolve()),
            "profile": session.profile,
            "account": session.account,
            "region": session.region,
        }

    @functools.wraps(original_deploy)
    def deploy(session):
        if getattr(session, "_acceptance_lambda", False):
            return original_deploy(session)
        owned = registration(session)
        write_json(session.project / ".acceptance-owner.json", owned)
        event(ROOT, "proof_intent", **owned)
        try:
            result = original_deploy(session)
        finally:
            event(ROOT, "proof_outputs", **owned, outputs=getattr(session, "_outputs", {}))
        from .aws import capture

        try:
            event(ROOT, "proof_inventory", **owned, snapshot=capture(session.aws, owned))
        except Exception as error:
            event(ROOT, "inventory_error", **owned, error=str(error))
        return result

    @functools.wraps(original_destroy)
    def destroy(session):
        if getattr(session, "_acceptance_lambda", False):
            return original_destroy(session)
        from .aws import capture

        # Capture untagged root volume and managed-secret identities before destruction.
        try:
            snapshot = capture(session.aws, registration(session))
            event(ROOT, "proof_inventory", **registration(session), snapshot=snapshot)
        except Exception as error:
            event(ROOT, "inventory_error", **registration(session), error=str(error))
        result = original_destroy(session)
        event(ROOT, "proof_destroyed", **registration(session))
        return result

    @functools.wraps(original_start)
    def start(session, *args, **kwargs):
        from .ownership import process_record

        try:
            return original_start(session, *args, **kwargs)
        finally:
            if session.process is not None:
                event(
                    ROOT,
                    "proof_command",
                    **registration(session),
                    pid=session.process.pid,
                    arguments=getattr(session.process, "args", None),
                    mode=kwargs.get("mode"),
                    exit_code=session.process.poll(),
                )
            if session.process is not None and session.process.poll() is None:
                event(
                    ROOT,
                    "proof_process",
                    **registration(session),
                    process=process_record(session.process.pid),
                )

    StandaloneCliSession._start = start
    StandaloneCliSession.deploy = deploy
    StandaloneCliSession.destroy = destroy

    if os.environ.get("TRA_ACCEPTANCE_STAGE") == "aws-lambda-vpc":
        from tests.integration.lambda_tunnel_session import LambdaTunnelSession, PrivateApi

        from .lambda_observer import install

        install(ROOT, LambdaTunnelSession, PrivateApi, StandaloneCliSession, registration)
