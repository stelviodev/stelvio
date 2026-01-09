"""Cron component for scheduling Lambda functions using EventBridge Rules."""

import json
from dataclasses import dataclass
from typing import Any, Unpack, final

import pulumi
from pulumi_aws import cloudwatch, lambda_

from stelvio import context
from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict
from stelvio.component import Component, safe_name


def _validate_rate_expression(schedule: str) -> None:
    """Validate rate expression format: rate(value unit)."""
    if not schedule.endswith(")"):
        raise ValueError(f"Invalid rate expression: missing closing parenthesis: {schedule}")

    content = schedule[5:-1].strip()
    if not content:
        raise ValueError(f"Invalid rate expression: empty content: {schedule}")

    parts = content.split()
    expected_parts = 2
    if len(parts) != expected_parts:
        raise ValueError(f"Invalid rate expression: expected 'rate(value unit)', got: {schedule}")

    value, unit = parts
    if not value.isdigit() or int(value) < 1:
        raise ValueError(
            f"Invalid rate expression: value must be a positive integer, got: {value}"
        )

    valid_units = ("minute", "minutes", "hour", "hours", "day", "days")
    if unit not in valid_units:
        raise ValueError(
            f"Invalid rate expression: unit must be one of {valid_units}, got: {unit}"
        )


def _validate_cron_expression(schedule: str) -> None:
    """Validate cron expression format: cron(min hour dom month dow year)."""
    if not schedule.endswith(")"):
        raise ValueError(f"Invalid cron expression: missing closing parenthesis: {schedule}")

    content = schedule[5:-1].strip()
    if not content:
        raise ValueError(f"Invalid cron expression: empty content: {schedule}")

    parts = content.split()
    expected_fields = 6
    if len(parts) != expected_fields:
        raise ValueError(
            f"Invalid cron expression: expected 6 fields "
            f"(minutes hours day-of-month month day-of-week year), "
            f"got {len(parts)} fields: {schedule}"
        )


def _validate_schedule(schedule: str) -> None:
    """Validate schedule expression format."""
    if schedule.startswith("rate("):
        _validate_rate_expression(schedule)
    elif schedule.startswith("cron("):
        _validate_cron_expression(schedule)
    else:
        raise ValueError(
            f"Invalid schedule expression: must start with 'rate(' or 'cron(', got: {schedule}"
        )


def _parse_handler(
    handler: str | FunctionConfig | FunctionConfigDict | Function | None,
    opts: FunctionConfigDict,
) -> FunctionConfig | Function:
    """Parse handler input into FunctionConfig or Function."""
    if isinstance(handler, dict | FunctionConfig | Function) and opts:
        raise ValueError(
            "Invalid configuration: cannot combine complete handler "
            "configuration with additional options"
        )

    if isinstance(handler, FunctionConfig | Function):
        return handler

    if isinstance(handler, dict):
        return FunctionConfig(**handler)

    if isinstance(handler, str):
        if "handler" in opts:
            raise ValueError(
                "Ambiguous handler configuration: handler is specified both as positional "
                "argument and in options"
            )
        return FunctionConfig(handler=handler, **opts)

    if handler is None:
        if "handler" not in opts:
            raise ValueError(
                "Missing handler configuration: when handler argument is None, "
                "'handler' option must be provided"
            )
        return FunctionConfig(**opts)

    raise TypeError(f"Invalid handler type: {type(handler).__name__}")


@final
@dataclass(frozen=True)
class CronResources:
    """Resources created by a Cron component."""

    rule: cloudwatch.EventRule
    target: cloudwatch.EventTarget
    function: lambda_.Function


class Cron(Component[CronResources]):
    """Schedule Lambda function execution using EventBridge Rules.

    Creates an EventBridge Rule with a schedule expression (rate or cron) that
    triggers a Lambda function.

    Args:
        name: Unique name for the cron job
        schedule: Schedule expression - either rate() or cron()
            - rate: "rate(1 hour)", "rate(5 minutes)", "rate(1 day)"
            - cron: "cron(0 12 * * ? *)" (UTC)
        handler: Lambda function to invoke - can be:
            - str: Handler path (creates new Function)
            - FunctionConfig: Complete function configuration
            - dict: FunctionConfigDict
            - Function: Existing Function instance
        enabled: Whether the schedule is active (default: True)
        payload: Custom JSON payload to pass to the Lambda (default: None)
        **opts: Additional function options when handler is a string

    Examples:
        # Simple rate expression
        Cron("hourly-cleanup", "rate(1 hour)", "tasks/cleanup.handler")

        # Cron expression with function options
        Cron("nightly-report",
            "cron(0 2 * * ? *)",
            "tasks/report.handler",
            memory=512,
            timeout=60
        )

        # Using existing Function
        fn = Function("my-fn", handler="tasks/process.handler")
        Cron("process-job", "rate(1 day)", fn)

        # With custom payload
        Cron("batch-job",
            "rate(1 hour)",
            "tasks/batch.handler",
            payload={"mode": "full"}
        )
    """

    def __init__(
        self,
        name: str,
        schedule: str,
        handler: str | FunctionConfig | FunctionConfigDict | Function | None = None,
        /,
        *,
        enabled: bool = True,
        payload: dict[str, Any] | None = None,
        customize: dict[str, dict] | None = None,
        **opts: Unpack[FunctionConfigDict],
    ):
        super().__init__(name, customize=customize)

        # Validate and parse inputs using pure functions
        _validate_schedule(schedule)
        handler_config = _parse_handler(handler, opts)

        # Set immutable state
        self._schedule = schedule
        self._enabled = enabled
        self._payload = payload
        self._handler_config = handler_config

    def _create_resources(self) -> CronResources:
        # Get or create function
        if isinstance(self._handler_config, Function):
            stelvio_function = self._handler_config
        else:
            stelvio_function = Function(f"{self.name}-fn", config=self._handler_config, customize=self._customize)

        lambda_function = stelvio_function.resources.function

        # Create EventBridge Rule with schedule
        rule = cloudwatch.EventRule(
            safe_name(context().prefix(), f"{self.name}-rule", 64),
            schedule_expression=self._schedule,
            **self._customizer("rule", dict(
                state="ENABLED" if self._enabled else "DISABLED",
            )),
        )

        # Create EventBridge Target linking rule to Lambda
        target = cloudwatch.EventTarget(
            safe_name(context().prefix(), f"{self.name}-target", 64),
            **self._customizer("target", dict(
                rule=rule.name,
                arn=lambda_function.arn,
                input=json.dumps(self._payload) if self._payload is not None else None,
            )),
        )

        # Create Lambda Permission for EventBridge to invoke the function
        lambda_.Permission(
            safe_name(context().prefix(), f"{self.name}-permission", 64),
            action="lambda:InvokeFunction",
            function=lambda_function.name,
            principal="events.amazonaws.com",
            source_arn=rule.arn,
        )

        # Pulumi exports
        pulumi.export(f"cron_{self.name}_rule_arn", rule.arn)
        pulumi.export(f"cron_{self.name}_rule_name", rule.name)

        return CronResources(rule=rule, target=target, function=lambda_function)
