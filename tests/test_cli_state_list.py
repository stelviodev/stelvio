import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


def _state_with_grouped_resources() -> dict:
    stack_urn = "urn:pulumi:dev::myapp::pulumi:pulumi:Stack::myapp-dev"
    component_urn = "urn:pulumi:dev::myapp::stelvio:aws:Function::api"
    lambda_urn = "urn:pulumi:dev::myapp::aws:lambda/function:Function::myapp-dev-api"
    role_urn = "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-api-r"
    provider_urn = "urn:pulumi:dev::myapp::pulumi:providers:aws::default_6_78_0"
    return {
        "checkpoint": {
            "latest": {
                "resources": [
                    {"urn": stack_urn, "type": "pulumi:pulumi:Stack"},
                    {
                        "urn": component_urn,
                        "type": "stelvio:aws:Function",
                        "parent": stack_urn,
                    },
                    {
                        "urn": lambda_urn,
                        "type": "aws:lambda/function:Function",
                        "parent": component_urn,
                        "dependencies": [role_urn],
                    },
                    {
                        "urn": role_urn,
                        "type": "aws:iam/role:Role",
                        "parent": component_urn,
                    },
                    {"urn": provider_urn, "type": "pulumi:providers:aws"},
                ]
            }
        }
    }


class _FakeRun:
    def __init__(self, state: dict):
        self.has_deployed = True
        self._state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def load_state(self) -> dict:
        return self._state


def _import_cli_module():
    with (
        patch("platformdirs.user_log_dir", return_value=str(Path.cwd() / ".tmp-test-logs")),
        patch("logging.handlers.TimedRotatingFileHandler"),
    ):
        return importlib.import_module("stelvio.cli")


def _import_cli_commands_module():
    _import_cli_module()
    return importlib.import_module("stelvio.cli.commands")


def test_state_list_command_accepts_json_flag() -> None:
    cli_module = _import_cli_module()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(cli_module, "determine_env", return_value="dev"),
        patch.object(cli_module, "run_state_list") as run_state_list_mock,
    ):
        result = cli_module.state_list.main(["--json"], standalone_mode=False)

    assert result is None
    run_state_list_mock.assert_called_once_with("dev", json_output=True)


def test_run_state_list_prints_grouped_tree_in_human_mode() -> None:
    commands_module = _import_cli_commands_module()
    printed: list[str] = []
    fake_console = SimpleNamespace(
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
        print=lambda *args, **_kwargs: printed.append(str(args[0])),
        print_json=Mock(),
    )

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun(_state_with_grouped_resources()),
        ),
    ):
        commands_module.run_state_list("dev")

    assert printed == [
        "[bold]Resources (5):[/bold]\n",
        "[bold]Stack[/bold]  myapp-dev",
        "  [bold]Function[/bold]  api",
        "    Type: stelvio:aws:Function",
        "    [cyan]myapp-dev-api[/cyan]",
        "      Type: aws:lambda/function:Function",
        "      Depends on: myapp-dev-api-r",
        "    [cyan]myapp-dev-api-r[/cyan]",
        "      Type: aws:iam/role:Role",
        "",
        "[bold]Providers[/bold]",
        "  [cyan]default_6_78_0[/cyan]",
        "    Type: pulumi:providers:aws",
    ]


def test_run_state_list_prints_grouped_json() -> None:
    commands_module = _import_cli_commands_module()
    fake_console = SimpleNamespace(
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
        print=Mock(),
        print_json=Mock(),
    )

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun(_state_with_grouped_resources()),
        ),
    ):
        commands_module.run_state_list("dev", json_output=True)

    fake_console.print_json.assert_called_once_with(
        data={
            "stack": {
                "name": "myapp-dev",
                "urn": "urn:pulumi:dev::myapp::pulumi:pulumi:Stack::myapp-dev",
                "type": "pulumi:pulumi:Stack",
                "parent": None,
                "dependencies": [],
            },
            "components": [
                {
                    "name": "api",
                    "urn": "urn:pulumi:dev::myapp::stelvio:aws:Function::api",
                    "type": "stelvio:aws:Function",
                    "parent": "urn:pulumi:dev::myapp::pulumi:pulumi:Stack::myapp-dev",
                    "dependencies": [],
                    "component_type": "Function",
                    "children": [
                        {
                            "name": "myapp-dev-api",
                            "urn": (
                                "urn:pulumi:dev::myapp::aws:lambda/function:Function::"
                                "myapp-dev-api"
                            ),
                            "type": "aws:lambda/function:Function",
                            "parent": "urn:pulumi:dev::myapp::stelvio:aws:Function::api",
                            "dependencies": [
                                "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-api-r"
                            ],
                            "children": [],
                        },
                        {
                            "name": "myapp-dev-api-r",
                            "urn": "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-api-r",
                            "type": "aws:iam/role:Role",
                            "parent": "urn:pulumi:dev::myapp::stelvio:aws:Function::api",
                            "dependencies": [],
                            "children": [],
                        },
                    ],
                }
            ],
            "providers": [
                {
                    "name": "default_6_78_0",
                    "urn": "urn:pulumi:dev::myapp::pulumi:providers:aws::default_6_78_0",
                    "type": "pulumi:providers:aws",
                    "parent": None,
                    "dependencies": [],
                    "children": [],
                }
            ],
        }
    )


def test_run_state_list_json_with_empty_state_returns_structured_empty_json() -> None:
    commands_module = _import_cli_commands_module()
    fake_console = SimpleNamespace(
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
        print=Mock(),
        print_json=Mock(),
    )

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(
            commands_module,
            "CommandRun",
            return_value=_FakeRun({"checkpoint": {"latest": {"resources": []}}}),
        ),
    ):
        commands_module.run_state_list("dev", json_output=True)

    fake_console.print_json.assert_called_once_with(data={"components": []})


def test_run_state_list_wraps_long_dependency_lines_with_tree_indent() -> None:
    commands_module = _import_cli_commands_module()
    state = _state_with_grouped_resources()
    state["checkpoint"]["latest"]["resources"][2]["dependencies"] = [
        "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-api-r",
        "urn:pulumi:dev::myapp::aws:iam/policy:Policy::myapp-dev-api-p",
        "urn:pulumi:dev::myapp::aws:sqs/queue:Queue::myapp-dev-tasks",
    ]
    printed: list[str] = []
    fake_console = SimpleNamespace(
        size=SimpleNamespace(width=45),
        status=lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None),
        print=lambda *args, **_kwargs: printed.append(str(args[0])),
        print_json=Mock(),
    )

    with (
        patch.object(commands_module, "console", fake_console),
        patch.object(commands_module, "CommandRun", return_value=_FakeRun(state)),
    ):
        commands_module.run_state_list("dev")

    dependency_lines = [
        line for line in printed if "Depends on:" in line or "myapp-dev-tasks" in line
    ]
    assert dependency_lines[0].startswith("      Depends on: ")
    assert len(dependency_lines) > 1
    assert all(line.startswith("                  ") for line in dependency_lines[1:])
