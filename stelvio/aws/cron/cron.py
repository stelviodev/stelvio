"""Cron component for scheduling Lambda functions using EventBridge Rules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict, Unpack, final

from pulumi_aws import cloudwatch, lambda_

from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict, resolve_handler
from stelvio.component import Component, resource_name
from stelvio.provider import ProviderStore

if TYPE_CHECKING:
    from pulumi_aws.cloudwatch import EventRuleArgs, EventTargetArgs
    from pulumi_aws.lambda_ import PermissionArgs

    from stelvio.aws.function.function import FunctionCustomizationDict
    from stelvio.customize import ChildCustomization, Customization


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


@final
@dataclass(frozen=True)
class CronResources:
    """Resources created by a Cron component."""

    rule: cloudwatch.EventRule
    target: cloudwatch.EventTarget
    permission: lambda_.Permission
    function: Function


class CronCustomizationDict(TypedDict, total=False):
    rule: Customization[EventRuleArgs]
    target: Customization[EventTargetArgs]
    permission: Customization[PermissionArgs]
    function: ChildCustomization[FunctionCustomizationDict]


@final
class Cron(Component[CronResources, CronCustomizationDict]):
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

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        schedule: str,
        handler: str | FunctionConfig | FunctionConfigDict | Function | None = None,
        /,
        *,
        enabled: bool | None = None,
        payload: dict[str, Any] | None = None,
        tags: dict[str, str] | None = None,
        customize: CronCustomizationDict | None = None,
        **opts: Unpack[FunctionConfigDict],
    ):
        super().__init__(
            ProviderStore.aws(), "stelvio:aws:Cron", name, tags=tags, customize=customize
        )

        # Validate and parse inputs using pure functions
        _validate_schedule(schedule)
        handler_config = resolve_handler(handler, opts)

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
            stelvio_function = Function(
                f"{self.name}-fn",
                config=self._handler_config,
                tags=self.tags,
                customize=self._customize.get("function"),
                parent=self,
            )

        lambda_function = stelvio_function.resources.function

        # Create EventBridge Rule with schedule
        rule = cloudwatch.EventRule(
            resource_name(f"{self.name}-rule", limit=64),
            **self._customizer(
                "rule",
                {
                    "schedule_expression": self._schedule,
                    "state": {True: "ENABLED", False: "DISABLED"}.get(self._enabled),
                },
                {"state": "ENABLED"},
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

        # Create EventBridge Target linking rule to Lambda
        target = cloudwatch.EventTarget(
            resource_name(f"{self.name}-target", limit=64),
            **self._customizer(
                "target",
                {"input": json.dumps(self._payload) if self._payload is not None else None},
                {"rule": rule.name, "arn": lambda_function.arn},
            ),
            opts=self._resource_opts(),
        )

        # Create Lambda Permission for EventBridge to invoke the function
        permission = lambda_.Permission(
            resource_name(f"{self.name}-permission", limit=64),
            **self._customizer(
                "permission",
                {},
                {
                    "action": "lambda:InvokeFunction",
                    "function": lambda_function.name,
                    "principal": "events.amazonaws.com",
                    "source_arn": rule.arn,
                },
            ),
            opts=self._resource_opts(),
        )

        return CronResources(
            rule=rule, target=target, permission=permission, function=stelvio_function
        )
