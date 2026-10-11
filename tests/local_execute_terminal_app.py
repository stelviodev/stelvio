"""Mock deployment process used by the interactive terminal regressions."""

import asyncio
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import click
import pulumi
from pulumi.automation import OpType, OutputValue

from stelvio import signals
from stelvio.app import StelvioApp
from stelvio.aws.s3 import Bucket
from stelvio.config import AwsConfig, StelvioAppConfig
from tests.aws.pulumi_mocks import PulumiTestMocks
from tests.cli_test_helpers import import_cli_commands_module, import_cli_module
from tests.test_local_execute import PulumiStack
from tests.test_phase_signals import MemoryHome
from tests.test_rich_deployment_handler import (
    _diagnostic_event,
    _outputs_event,
    _pre_event,
    _summary_event,
)


class TerminalStack(PulumiStack):
    def __init__(self, program, unchanged):
        super().__init__(program)
        self.unchanged = unchanged

    def up(self, *, on_event, **kwargs):
        super().up(**kwargs)
        operation = OpType.SAME if self.unchanged else OpType.CREATE
        component_type = "stelvio:aws:Bucket"
        component_urn = f"urn:pulumi:test::demo::{component_type}::my-bucket"
        resources = [
            (component_type, component_urn, ""),
            (
                "aws:s3/bucket:Bucket",
                f"urn:pulumi:test::demo::{component_type}$aws:s3/bucket:Bucket::my-bucket",
                component_urn,
            ),
            (
                "aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock",
                f"urn:pulumi:test::demo::{component_type}$"
                "aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock::access-block",
                component_urn,
            ),
        ]
        for resource_type, urn, parent in resources:
            on_event(_pre_event(urn, resource_type, operation, parent_urn=parent))
            on_event(_outputs_event(urn, resource_type, operation, parent_urn=parent))
        if not self.unchanged:
            on_event(_diagnostic_event("terminal warning", severity="warning"))
        on_event(_summary_event())

    def outputs(self):
        return {"result": OutputValue("terminal-output", secret=False)}


def deploy_arguments(scenario):
    arguments = ["test", "--yes"]
    if scenario.startswith("repl"):
        arguments.append("--repl")
    if scenario == "repl-skip":
        arguments.append("--skip-local-execute")
    if scenario == "repl-json":
        arguments.append("--json")
    elif scenario == "repl-stream":
        arguments.append("--stream")
    return [*arguments, "--", "terminal-argument"]


def write_report(project, home, report):
    records = [json.loads(data) for key, data in home.files.items() if key.startswith("update/")]
    report["lock_exists"] = home.file_exists("lock/demo/test.json")
    report["workdirs"] = [path.name for path in (project / "work").iterdir()]
    report["completed"] = all(record["time_completed"] is not None for record in records)
    report["update_errors"] = [record["errors"] for record in records]
    (project / "report.json").write_text(json.dumps(report))


def main():
    # Give the child its controlling terminal so writing Ctrl+C delivers a real SIGINT.
    import fcntl
    import termios

    fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)
    project = Path(sys.argv[1])
    scenario = sys.argv[2]
    unchanged = "unchanged" in scenario
    repl = scenario.startswith("repl")
    no_callbacks = scenario == "repl-no-callback"
    asyncio.set_event_loop(asyncio.new_event_loop())
    pulumi.runtime.set_mocks(PulumiTestMocks())
    app = StelvioApp("demo")
    app.config(
        lambda env: StelvioAppConfig(environments=["test"], aws=AwsConfig(region="us-east-1"))
    )
    app.run(lambda: Bucket("my-bucket"))
    home = MemoryHome()
    report = {"input": None, "cancellations": [], "errors": [], "following": 0}

    def prompt(components, argv):
        assert argv == [sys.argv[0], "terminal-argument"]
        answer = input("test:")
        report["input"] = answer
        print(f"Bucket ready: {components.my_bucket.bucket_name}", file=sys.stderr)
        print(f"input={answer}")

    def following(components):
        report["following"] += 1

    if not no_callbacks:
        app.local_execute(prompt)
        app.local_execute(following)

    @app.on(signals.on_cancel)
    def cancelled(event):
        report["cancellations"].append([event.reason, event.phase])

    app.on(signals.on_error)(lambda event: report["errors"].append(str(event.error)))
    commands = import_cli_commands_module()
    cli = import_cli_module()
    with ExitStack() as patches:
        replacements = {
            "stelvio.command_run.import_module": Mock(),
            "stelvio.command_run.get_project_root": lambda: project,
            "stelvio.command_run.get_user_env": lambda: "test",
            "stelvio.command_run.AwsHome": lambda *args: home,
            "stelvio.command_run._init_storage": Mock(),
            "stelvio.command_run._get_or_create_passphrase": lambda *args: "passphrase",
            "stelvio.command_run.get_dot_stelvio_dir": lambda: project / "work",
            "stelvio.command_run._create_stack": lambda *args: TerminalStack(
                app._get_pulumi_program_func(), unchanged
            ),
            "stelvio._local_execute.get_project_root": lambda: project,
            "stelvio.cli.commands.clean_stale_dependency_caches": Mock(),
            "stelvio.cli.ensure_pulumi": Mock(),
            "stelvio.cli.run_deploy": commands.run_deploy,
        }
        for target, replacement in replacements.items():
            patches.enter_context(patch(target, replacement))
        if repl:
            patches.enter_context(patch.dict(sys.modules, {"IPython": None}))
        exit_code = 0
        try:
            cli.deploy.main(args=deploy_arguments(scenario), standalone_mode=False)
        except (KeyboardInterrupt, click.Abort):
            exit_code = 130
        finally:
            write_report(project, home, report)
            print("terminal-done", flush=True)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
