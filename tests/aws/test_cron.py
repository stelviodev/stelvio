import json

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.cron import Cron, CronResources
from stelvio.aws.function import Function, FunctionConfig

from .pulumi_mocks import PulumiTestMocks

# Test prefix
TP = "test-test-"


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


@pulumi.runtime.test
def test_cron_creates_event_rule(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler")
    _ = cron.resources

    # Assert
    def check_resources(_):
        rule_name = f"{TP}my-cron-rule"
        rules = pulumi_mocks.created_event_rules(rule_name)
        assert len(rules) == 1
        rule = rules[0]
        assert rule.typ == "aws:cloudwatch/eventRule:EventRule"
        assert rule.inputs["scheduleExpression"] == "rate(1 hour)"
        assert rule.inputs["state"] == "ENABLED"

    cron.resources.rule.id.apply(check_resources)


@pulumi.runtime.test
def test_cron_creates_event_target(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler")
    _ = cron.resources

    # Assert
    def check_resources(args):
        _, rule_name, fn_arn = args
        target_name = f"{TP}my-cron-target"
        targets = pulumi_mocks.created_event_targets(target_name)
        assert len(targets) == 1
        target = targets[0]
        assert target.typ == "aws:cloudwatch/eventTarget:EventTarget"
        assert target.inputs["rule"] == rule_name
        assert target.inputs["arn"] == fn_arn
        assert target.inputs.get("input") is None

    pulumi.Output.all(
        cron.resources.target.id,
        cron.resources.rule.name,
        cron.resources.function.resources.function.arn,
    ).apply(check_resources)


@pulumi.runtime.test
def test_cron_creates_lambda_permission(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler")
    _ = cron.resources

    # Assert - wait for multiple outputs to ensure all resources are created
    def check_resources(args):
        rule_arn, _, fn_name = args
        permission_name = f"{TP}my-cron-permission"
        permissions = pulumi_mocks.created_permissions(permission_name)
        assert len(permissions) == 1
        perm = permissions[0]
        assert perm.typ == "aws:lambda/permission:Permission"
        assert perm.inputs["action"] == "lambda:InvokeFunction"
        assert perm.inputs["principal"] == "events.amazonaws.com"
        assert perm.inputs["function"] == fn_name
        assert perm.inputs["sourceArn"] == rule_arn

    pulumi.Output.all(
        cron.resources.rule.arn,
        cron.resources.target.id,
        cron.resources.function.resources.function.name,
    ).apply(check_resources)


@pulumi.runtime.test
def test_cron_creates_function(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler")
    _ = cron.resources

    # Assert
    def check_resources(_):
        fn_name = f"{TP}my-cron-fn"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        fn = functions[0]
        assert fn.typ == "aws:lambda/function:Function"
        assert fn.inputs["handler"] == "simple.handler"
        assert fn.inputs["runtime"] == "python3.12"

    cron.resources.function.resources.function.id.apply(check_resources)


@pulumi.runtime.test
def test_cron_with_cron_expression(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("nightly", "cron(0 2 * * ? *)", "functions/simple.handler")
    _ = cron.resources

    # Assert
    def check_resources(_):
        rule_name = f"{TP}nightly-rule"
        rules = pulumi_mocks.created_event_rules(rule_name)
        assert len(rules) == 1
        assert rules[0].inputs["scheduleExpression"] == "cron(0 2 * * ? *)"

    cron.resources.rule.id.apply(check_resources)


@pulumi.runtime.test
def test_cron_disabled(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler", enabled=False)
    _ = cron.resources

    # Assert
    def check_resources(_):
        rule_name = f"{TP}my-cron-rule"
        rules = pulumi_mocks.created_event_rules(rule_name)
        assert len(rules) == 1
        assert rules[0].inputs["state"] == "DISABLED"

    cron.resources.rule.id.apply(check_resources)


@pulumi.runtime.test
def test_cron_with_payload(pulumi_mocks, project_cwd):
    # Arrange & Act
    payload = {"mode": "full", "count": 10}
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler", payload=payload)
    _ = cron.resources

    # Assert
    def check_resources(_):
        target_name = f"{TP}my-cron-target"
        targets = pulumi_mocks.created_event_targets(target_name)
        assert len(targets) == 1
        assert json.loads(targets[0].inputs["input"]) == payload

    cron.resources.target.id.apply(check_resources)


@pulumi.runtime.test
def test_cron_with_existing_function(pulumi_mocks, project_cwd):
    # Arrange
    existing_fn = Function("existing-fn", handler="functions/simple.handler")

    # Act
    cron = Cron("my-cron", "rate(1 hour)", existing_fn)
    _ = cron.resources

    # Assert
    def check_resources(_):
        # Should only have one function (the existing one), not a new one
        existing_fn_name = f"{TP}existing-fn"
        cron_fn_name = f"{TP}my-cron-fn"
        existing_functions = pulumi_mocks.created_functions(existing_fn_name)
        cron_functions = pulumi_mocks.created_functions(cron_fn_name)
        assert len(existing_functions) == 1
        assert len(cron_functions) == 0

    cron.resources.function.resources.function.id.apply(check_resources)


@pulumi.runtime.test
def test_cron_with_function_config(pulumi_mocks, project_cwd):
    # Arrange & Act
    config = FunctionConfig(handler="functions/simple.handler", memory=512, timeout=60)
    cron = Cron("my-cron", "rate(1 hour)", config)
    _ = cron.resources

    # Assert
    def check_resources(_):
        fn_name = f"{TP}my-cron-fn"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        assert functions[0].inputs["memorySize"] == 512
        assert functions[0].inputs["timeout"] == 60

    cron.resources.function.resources.function.id.apply(check_resources)


@pulumi.runtime.test
def test_cron_with_function_config_dict(pulumi_mocks, project_cwd):
    # Arrange & Act - passing a plain dict instead of FunctionConfig
    config_dict = {"handler": "functions/simple.handler", "memory": 512, "timeout": 60}
    cron = Cron("my-cron", "rate(1 hour)", config_dict)
    _ = cron.resources

    # Assert
    def check_resources(_):
        fn_name = f"{TP}my-cron-fn"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        assert functions[0].inputs["memorySize"] == 512
        assert functions[0].inputs["timeout"] == 60

    cron.resources.function.resources.function.id.apply(check_resources)


@pulumi.runtime.test
def test_cron_with_function_options(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler", memory=256, timeout=30)
    _ = cron.resources

    # Assert
    def check_resources(_):
        fn_name = f"{TP}my-cron-fn"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        assert functions[0].inputs["memorySize"] == 256
        assert functions[0].inputs["timeout"] == 30

    cron.resources.function.resources.function.id.apply(check_resources)


@pulumi.runtime.test
def test_cron_with_handler_in_opts(pulumi_mocks, project_cwd):
    # Arrange & Act - handler passed via opts (positional handler is None)
    cron = Cron("my-cron", "rate(1 hour)", handler="functions/simple.handler", memory=256)
    _ = cron.resources

    # Assert
    def check_resources(_):
        fn_name = f"{TP}my-cron-fn"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        assert functions[0].inputs["memorySize"] == 256

    cron.resources.function.resources.function.id.apply(check_resources)


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


@pulumi.runtime.test
def test_cron_with_empty_payload(pulumi_mocks, project_cwd):
    # Arrange & Act - empty dict should be serialized, not treated as None
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler", payload={})
    _ = cron.resources

    # Assert
    def check_resources(_):
        target_name = f"{TP}my-cron-target"
        targets = pulumi_mocks.created_event_targets(target_name)
        assert len(targets) == 1
        assert targets[0].inputs["input"] == "{}"

    cron.resources.target.id.apply(check_resources)


@pulumi.runtime.test
def test_cron_resources_are_properly_linked(pulumi_mocks, project_cwd):
    # Arrange & Act
    cron = Cron("my-cron", "rate(1 hour)", "functions/simple.handler")
    _ = cron.resources

    # Assert - verify all resources exist and are properly linked
    def check_resources(args):
        rule_arn, rule_name, _, fn_name, fn_arn = args

        # Verify EventTarget references the Rule and Function
        targets = pulumi_mocks.created_event_targets(f"{TP}my-cron-target")
        assert len(targets) == 1
        assert targets[0].inputs["rule"] == rule_name
        assert targets[0].inputs["arn"] == fn_arn

        # Verify Permission references both Rule and Function
        permissions = pulumi_mocks.created_permissions(f"{TP}my-cron-permission")
        assert len(permissions) == 1
        assert permissions[0].inputs["sourceArn"] == rule_arn
        assert permissions[0].inputs["function"] == fn_name

    pulumi.Output.all(
        cron.resources.rule.arn,
        cron.resources.rule.name,
        cron.resources.target.id,
        cron.resources.function.resources.function.name,
        cron.resources.function.resources.function.arn,
    ).apply(check_resources)
