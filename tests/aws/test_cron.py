import json

import pulumi
import pytest

from stelvio.aws.cron import Cron, CronResources
from stelvio.aws.function import Function, FunctionConfig

from ..conftest import TP
from .pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tn


def test_cron_creates_event_rule(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res(
        "my-cron-rule", R.EVENT_RULE, {"scheduleExpression": "rate(1 hour)", "state": "ENABLED"}
    )
    pulumi_mocks.assert_res_counts(
        {
            R.EVENT_RULE: 1,
            R.EVENT_TARGET: 1,
            R.LAMBDA_PERMISSION: 1,
            R.FUNCTION: 1,
            R.ROLE: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
        }
    )


def test_cron_creates_event_target(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert - full compare: no input without a payload
    pulumi_mocks.assert_res(
        "my-cron-target",
        R.EVENT_TARGET,
        {
            "rule": tn(TP + "my-cron-rule"),
            "arn": (
                f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{tn(TP + 'my-cron-fn')}"
            ),
        },
    )


def test_cron_creates_lambda_permission(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res(
        "my-cron-permission",
        R.LAMBDA_PERMISSION,
        {
            "action": "lambda:InvokeFunction",
            "principal": "events.amazonaws.com",
            "function": tn(TP + "my-cron-fn"),
            "sourceArn": (
                f"arn:aws:events:{DEFAULT_REGION}:{ACCOUNT_ID}:rule/{tn(TP + 'my-cron-rule')}"
            ),
        },
    )


def test_cron_creates_function(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res(
        "my-cron-fn",
        R.FUNCTION,
        {"handler": "simple.handler", "runtime": "python3.12"},
        partial=True,
    )


def test_cron_with_cron_expression(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("nightly", "cron(0 2 * * ? *)", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res(
        "nightly-rule", R.EVENT_RULE, {"scheduleExpression": "cron(0 2 * * ? *)"}, partial=True
    )


def test_cron_disabled(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler", enabled=False)

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res("my-cron-rule", R.EVENT_RULE, {"state": "DISABLED"}, partial=True)


def test_cron_with_payload(pulumi_mocks, project_cwd):
    # Arrange & Act
    payload = {"mode": "full", "count": 10}
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler", payload=payload)

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    target = pulumi_mocks.assert_res("my-cron-target", R.EVENT_TARGET)
    assert json.loads(target.inputs["input"]) == payload


def test_cron_with_existing_function(pulumi_mocks, project_cwd):
    # Arrange
    existing_fn = Function("existing-fn", handler="functions/simple.handler")

    # Act
    cron = Cron("my-cron", "rate(1 hour)", existing_fn)

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    # Only the existing function, no my-cron-fn
    assert [f.name for f in pulumi_mocks.created(R.FUNCTION)] == [f"{TP}existing-fn"]


def test_cron_with_function_config(pulumi_mocks, project_cwd):
    # Arrange & Act
    config = FunctionConfig(handler="functions/simple.handler", memory=512, timeout=60)
    cron = Cron("my-cron", "rate(1 hour)", config)

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res(
        "my-cron-fn", R.FUNCTION, {"memorySize": 512, "timeout": 60}, partial=True
    )


def test_cron_with_function_config_dict(pulumi_mocks, project_cwd):
    # Arrange & Act - passing a plain dict instead of FunctionConfig
    config_dict = {"handler": "functions/simple.handler", "memory": 512, "timeout": 60}
    cron = Cron("my-cron", "rate(1 hour)", config_dict)

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res(
        "my-cron-fn", R.FUNCTION, {"memorySize": 512, "timeout": 60}, partial=True
    )


def test_cron_with_function_options(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler", memory=256, timeout=30)

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res(
        "my-cron-fn", R.FUNCTION, {"memorySize": 256, "timeout": 30}, partial=True
    )


def test_cron_with_handler_in_opts(pulumi_mocks, project_cwd):
    # Arrange & Act - handler passed via opts (positional handler is None)
    cron = Cron("my-cron", "rate(1 hour)", handler="functions/simple.handler", memory=256)

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res("my-cron-fn", R.FUNCTION, {"memorySize": 256}, partial=True)


def test_cron_invalid_schedule_expression():
    with pytest.raises(ValueError, match="must start with 'rate\\(' or 'cron\\('"):
        Cron("my-cron", "every(1 hour)", "functions/simple.handler")


def test_cron_rate_missing_closing_paren():
    with pytest.raises(ValueError, match="missing closing parenthesis"):
        Cron("my-cron", "rate(1 hour", "functions/simple.handler")


def test_cron_rate_invalid_value():
    with pytest.raises(ValueError, match="value must be a positive integer"):
        Cron("my-cron", "rate(0 hours)", "functions/simple.handler")


def test_cron_rate_invalid_unit():
    with pytest.raises(ValueError, match="unit must be one of"):
        Cron("my-cron", "rate(1 weeks)", "functions/simple.handler")


def test_cron_rate_missing_unit():
    with pytest.raises(ValueError, match="expected 'rate\\(value unit\\)'"):
        Cron("my-cron", "rate(1)", "functions/simple.handler")


def test_cron_expression_wrong_field_count():
    with pytest.raises(ValueError, match="expected 6 fields"):
        Cron("my-cron", "cron(0 12 * * *)", "functions/simple.handler")


def test_cron_expression_missing_closing_paren():
    with pytest.raises(ValueError, match="missing closing parenthesis"):
        Cron("my-cron", "cron(0 12 * * ? *", "functions/simple.handler")


def test_cron_rate_empty_content():
    with pytest.raises(ValueError, match="empty content"):
        Cron("my-cron", "rate()", "functions/simple.handler")


def test_cron_expression_empty_content():
    with pytest.raises(ValueError, match="empty content"):
        Cron("my-cron", "cron()", "functions/simple.handler")


def test_cron_missing_handler():
    with pytest.raises(ValueError, match="Missing handler configuration"):
        Cron("my-cron", "rate(1 hour)")


def test_cron_ambiguous_handler():
    with pytest.raises(ValueError, match="Ambiguous handler configuration"):
        Cron(
            "my-cron",
            "rate(1 hour)",
            "functions/simple.handler",
            handler="other.handler",
        )


def test_cron_cannot_combine_config_with_opts():
    config = FunctionConfig(handler="functions/simple.handler")
    with pytest.raises(ValueError, match="cannot combine complete handler"):
        Cron("my-cron", "rate(1 hour)", config, memory=256)


def test_cron_cannot_combine_function_with_opts():
    function = Function("my-fn", handler="functions/simple.handler")
    with pytest.raises(
        ValueError, match="Cannot combine a Function handler with function options"
    ):
        Cron("my-cron", "rate(1 hour)", function, memory=256)


def test_cron_resources_dataclass():
    # Test that CronResources is a proper frozen dataclass
    import dataclasses

    assert dataclasses.is_dataclass(CronResources)
    assert CronResources.__dataclass_params__.frozen
    assert "rule" in CronResources.__dataclass_fields__
    assert "target" in CronResources.__dataclass_fields__
    assert "function" in CronResources.__dataclass_fields__


def test_cron_invalid_handler_type():
    with pytest.raises(TypeError, match="Invalid handler type"):
        Cron("my-cron", "rate(1 hour)", 123)  # type: ignore[arg-type]


def test_cron_with_empty_payload(pulumi_mocks, project_cwd):
    # Arrange & Act - empty dict should be serialized, not treated as None
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler", payload={})

    @pulumi.runtime.test
    def deploy():
        return cron.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res("my-cron-target", R.EVENT_TARGET, {"input": "{}"}, partial=True)
