import json
from dataclasses import dataclass, replace
from typing import Any, TypedDict, Unpack, final

import pulumi
from pulumi import Output
from pulumi_aws.lambda_ import EventSourceMapping
from pulumi_aws.sqs import Queue as SqsQueue

from stelvio import context
from stelvio.aws.function import Function, FunctionConfig, FunctionConfigDict
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import Link, LinkableMixin, LinkConfig

DEFAULT_QUEUE_BATCH_SIZE = 10
DEFAULT_QUEUE_DELAY = 0
DEFAULT_QUEUE_VISIBILITY_TIMEOUT = 60
DEFAULT_QUEUE_RETENTION = 345600  # 4 days in seconds
MAX_QUEUE_NAME_LENGTH = 80
MAX_FILTERS = 5  # AWS EventSourceMapping limit


@dataclass(frozen=True, kw_only=True)
class DlqConfig:
    """Dead-letter queue configuration.

    Args:
        queue: Dead-letter queue component.
        retry: Number of times a message is retried before being sent to DLQ (default: 3).
    """

    queue: "Queue | str"
    retry: int = 3


class DlqConfigDict(TypedDict, total=False):
    """Configuration for dead-letter queue settings."""

    queue: "Queue | str"
    retry: int


class SqsFilterDict(TypedDict, total=False):
    """SQS EventSourceMapping filter pattern.

    Filter on message body, attributes, or messageAttributes using AWS EventBridge syntax.
    See: https://docs.aws.amazon.com/lambda/latest/dg/invocation-eventfiltering.html
    """

    body: dict[str, Any]
    attributes: dict[str, Any]
    messageAttributes: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class QueueConfig:
    """Queue configuration."""

    fifo: bool = False
    delay: int = DEFAULT_QUEUE_DELAY
    visibility_timeout: int = DEFAULT_QUEUE_VISIBILITY_TIMEOUT
    retention: int = DEFAULT_QUEUE_RETENTION
    dlq: "Queue | str | DlqConfig | DlqConfigDict | None" = None


class QueueConfigDict(TypedDict, total=False):
    """Queue configuration dictionary."""

    fifo: bool
    delay: int
    visibility_timeout: int
    retention: int
    dlq: "Queue | str | DlqConfigDict | DlqConfig | None"


@final
@dataclass(frozen=True, kw_only=True)
class QueueResources:
    """Resources created for a Queue."""

    queue: SqsQueue


@final
@dataclass(frozen=True, kw_only=True)
class QueueSubscriptionResources:
    """Resources created for a QueueSubscription."""

    function: Function
    event_source_mapping: EventSourceMapping


@final
class QueueSubscription(Component[QueueSubscriptionResources]):
    """Lambda function subscription to an SQS queue."""

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        queue: "Queue",
        handler: str | FunctionConfig | FunctionConfigDict | None,
        batch_size: int | None,
        filters: list[SqsFilterDict] | None,
        opts: FunctionConfigDict,
    ):
        # Add suffix because we want to use 'name' for Function, avoiding component name conflicts
        super().__init__(f"{name}-subscription")
        self.queue = queue
        self.function_name = name  # Function gets the original name

        # Validate and store batch_size
        if batch_size is not None:
            max_batch = 10 if queue.config.fifo else 10000
            if not 1 <= batch_size <= max_batch:
                queue_type = "FIFO" if queue.config.fifo else "standard"
                raise ValueError(
                    f"batch_size must be between 1 and {max_batch} for {queue_type} queues, "
                    f"got {batch_size}"
                )
        self.batch_size = batch_size

        # Validate and store filters
        self._check_filter_rules(filters)
        self.filters = filters

        self.handler = self._create_handler_config(handler, opts)

    @staticmethod
    def _check_filter_rules(filters: list[SqsFilterDict] | None) -> None:
        """Validate SQS message filter rules.

        Args:
            filters: List of filter patterns to validate.

        Raises:
            ValueError: If more than 5 filters are provided.
            TypeError: If filters is not a list or if any filter is not a dict.
        """
        if filters is None:
            return

        if not isinstance(filters, list):
            raise TypeError(f"SQS message filters must be a list, got {type(filters).__name__}")

        if not filters:  # Empty list
            return

        if len(filters) > MAX_FILTERS:
            raise ValueError(
                f"SQS message filters cannot exceed {MAX_FILTERS} filters, "
                f"got {len(filters)} filters. "
                "See: https://docs.aws.amazon.com/lambda/latest/dg/invocation-eventfiltering.html"
            )

        for i, filter_rule in enumerate(filters):
            if not isinstance(filter_rule, dict):
                raise TypeError(
                    f"Each SQS message filter must be a dict, "
                    f"but filter at index {i} is {type(filter_rule).__name__}"
                )

    @staticmethod
    def _create_handler_config(
        handler: str | FunctionConfig | FunctionConfigDict | None,
        opts: FunctionConfigDict,
    ) -> FunctionConfig:
        if isinstance(handler, dict | FunctionConfig) and opts:
            raise ValueError(
                "Invalid configuration: cannot combine complete handler "
                "configuration with additional options"
            )

        if isinstance(handler, FunctionConfig):
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

    def _create_resources(self) -> QueueSubscriptionResources:
        # Create SQS link (mandatory for Lambda to poll SQS)
        sqs_link = self._create_sqs_link()

        # Merge SQS link with existing links from user's config
        merged_links = [sqs_link, *self.handler.links]

        # Create new config with merged links
        config_with_merged_links = replace(self.handler, links=merged_links)

        # Create function with merged permissions
        function = Function(self.function_name, config_with_merged_links)

        # Create EventSourceMapping for SQS
        mapping = EventSourceMapping(
            safe_name(context().prefix(), f"{self.name}-mapping", 128),
            event_source_arn=self.queue.arn,
            function_name=function.function_name,
            batch_size=self.batch_size or DEFAULT_QUEUE_BATCH_SIZE,
            filter_criteria=(
                {"filters": [{"pattern": json.dumps(f)} for f in self.filters]}
                if self.filters
                else None
            ),
            enabled=True,
        )

        return QueueSubscriptionResources(function=function, event_source_mapping=mapping)

    def _create_sqs_link(self) -> Link:
        """Create link with SQS permissions required for Lambda event source mapping."""
        return Link(
            f"{self.queue.name}-sqs",
            properties={},
            permissions=[
                AwsPermission(
                    actions=[
                        "sqs:ReceiveMessage",
                        "sqs:DeleteMessage",
                        "sqs:GetQueueAttributes",
                    ],
                    resources=[self.queue.arn],
                )
            ],
        )


@final
class Queue(Component[QueueResources], LinkableMixin):
    """AWS SQS Queue component.

    Args:
        name: Queue name
        config: Complete queue configuration as QueueConfig or dict
        **opts: Individual queue configuration parameters

    You can configure the queue in two ways:
        - Provide complete config:
            queue = Queue(
                "my-queue",
                config={"fifo": True, "delay": 5}
            )
        - Provide individual parameters:
            queue = Queue(
                "my-queue",
                fifo=True,
                delay=5
            )
    """

    _subscriptions: list[QueueSubscription]

    def __init__(
        self,
        name: str,
        /,
        *,
        config: QueueConfig | QueueConfigDict | None = None,
        **opts: Unpack[QueueConfigDict],
    ):
        super().__init__(name)
        self._config = self._parse_config(config, opts)
        self._subscriptions = []

    @staticmethod
    def _parse_config(
        config: QueueConfig | QueueConfigDict | None, opts: QueueConfigDict
    ) -> QueueConfig:
        """Parse configuration from either typed or dict form."""
        if config and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter with additional options "
                "- provide all settings either in 'config' or as separate options"
            )

        if config is None:
            config = QueueConfig(**opts)
        elif isinstance(config, QueueConfig):
            pass  # Already correct type
        elif isinstance(config, dict):
            config = QueueConfig(**config)
        else:
            raise TypeError(
                f"Invalid config type: expected QueueConfig or QueueConfigDict, "
                f"got {type(config).__name__}"
            )

        # Normalize DLQ config from dict
        if isinstance(config.dlq, dict):
            config = QueueConfig(
                fifo=config.fifo,
                delay=config.delay,
                visibility_timeout=config.visibility_timeout,
                retention=config.retention,
                dlq=DlqConfig(**config.dlq),
            )

        if isinstance(config.dlq, str | Queue):
            config = QueueConfig(
                fifo=config.fifo,
                delay=config.delay,
                visibility_timeout=config.visibility_timeout,
                retention=config.retention,
                dlq=DlqConfig(queue=config.dlq),
            )

        return config

    @property
    def arn(self) -> Output[str]:
        return self.resources.queue.arn

    @property
    def url(self) -> Output[str]:
        return self.resources.queue.url

    @property
    def queue_name(self) -> Output[str]:
        return self.resources.queue.name

    @property
    def config(self) -> QueueConfig:
        """Get the component configuration."""
        return self._config

    def _create_resources(self) -> QueueResources:
        suffix = ".fifo" if self.config.fifo else ""
        name = self.name.removesuffix(suffix)

        queue_name = safe_name(context().prefix(), name, MAX_QUEUE_NAME_LENGTH, suffix=suffix)

        # Build redrive policy for DLQ if configured
        redrive_policy = None
        dlq_arn = self._get_dlq_arn()
        if dlq_arn is not None:
            max_receive_count = self.config.dlq.retry
            redrive_policy = dlq_arn.apply(
                lambda arn: pulumi.Output.json_dumps(
                    {"deadLetterTargetArn": arn, "maxReceiveCount": max_receive_count}
                )
            )

        queue = SqsQueue(
            safe_name(context().prefix(), f"{self.name}", 128),
            name=queue_name,
            delay_seconds=self.config.delay,
            visibility_timeout_seconds=self.config.visibility_timeout,
            message_retention_seconds=self.config.retention,
            fifo_queue=self.config.fifo if self.config.fifo else None,
            content_based_deduplication=True if self.config.fifo else None,
            redrive_policy=redrive_policy,
        )

        pulumi.export(f"queue_{self.name}_arn", queue.arn)
        pulumi.export(f"queue_{self.name}_url", queue.url)
        pulumi.export(f"queue_{self.name}_name", queue.name)

        return QueueResources(queue=queue)

    def subscribe(
        self,
        name: str,
        handler: str | FunctionConfig | FunctionConfigDict | None = None,
        /,
        *,
        batch_size: int | None = None,
        filters: list[SqsFilterDict] | None = None,
        **opts: Unpack[FunctionConfigDict],
    ) -> QueueSubscription:
        """Subscribe a Lambda function to this SQS queue.

        Uses production-ready defaults: batch_size=10, enabled=True.

        Args:
            name: Name for the subscription (used in Lambda function naming)
            handler: Lambda handler specification. Can be:
                - Function handler path as string
                - Complete FunctionConfig object
                - FunctionConfigDict dictionary
                - None (if handler is specified in opts)
            batch_size: Maximum number of records to process per Lambda invocation (default: 10).
            filters: EventSourceMapping filter patterns for SQS messages (max 5).
                Each filter matches on message body, attributes, or messageAttributes.
                Multiple filters use OR logic. Within a filter, all conditions use AND logic.
                See: https://docs.aws.amazon.com/lambda/latest/dg/invocation-eventfiltering.html
            **opts: Lambda function configuration (memory, timeout, runtime, etc.)

        Raises:
            ValueError: If the configuration is ambiguous or incomplete
            ValueError: If more than 5 filters are provided
            TypeError: If handler is of invalid type
            TypeError: If filters is not a list or contains non-dict items
            ValueError: If a subscription with the same name already exists

        Examples:
            # Simple subscription
            orders_queue.subscribe("process-orders", "functions/orders.handler")

            # With function configuration
            orders_queue.subscribe(
                "process-orders", "functions/orders.handler", memory=256, timeout=60
            )

            # With batch size
            orders_queue.subscribe(
                "process-orders",
                "functions/orders.handler",
                batch_size=5
            )

            # Filter by order type in message body
            orders_queue.subscribe(
                "process-refunds",
                "functions/refunds.handler",
                filters=[{"body": {"orderType": ["refund"]}}]
            )

            # Filter by customer priority (multiple conditions with AND logic)
            orders_queue.subscribe(
                "process-vip-orders",
                "functions/vip.handler",
                filters=[{
                    "body": {
                        "customerTier": ["gold", "platinum"],
                        "priority": ["high"]
                    }
                }]
            )

            # Multiple filters with OR logic (process returns OR cancellations)
            orders_queue.subscribe(
                "process-exceptions",
                "functions/exceptions.handler",
                filters=[
                    {"body": {"orderType": ["return"]}},
                    {"body": {"orderType": ["cancellation"]}}
                ]
            )
        """
        function_name = f"{self.name}-{name}"
        expected_subscription_name = f"{function_name}-subscription"

        # Check for duplicate subscription names before creating the component
        if any(sub.name == expected_subscription_name for sub in self._subscriptions):
            raise ValueError(f"Subscription '{name}' already exists for queue '{self.name}'")

        subscription = QueueSubscription(function_name, self, handler, batch_size, filters, opts)

        self._subscriptions.append(subscription)
        return subscription

    def _get_dlq_arn(self) -> Output[str] | None:
        """Get the ARN of the dead-letter queue."""
        if self.config.dlq is None:
            return None

        # After _parse_config normalization, dlq is always a DlqConfig
        # but dlq.queue can be either a Queue component or a string ARN
        if isinstance(self.config.dlq.queue, str):
            return pulumi.Output.from_input(self.config.dlq.queue)

        return self.config.dlq.queue.resources.queue.arn


@link_config_creator(Queue)
def default_queue_link(queue_component: Queue) -> LinkConfig:
    """Default link configuration for Queue component.

    Grants permissions to send messages to the queue. For processing messages,
    use queue.subscribe() which automatically configures the necessary permissions.
    """
    queue = queue_component.resources.queue
    return LinkConfig(
        properties={
            "queue_url": queue.url,
            "queue_arn": queue.arn,
            "queue_name": queue.name,
        },
        permissions=[
            AwsPermission(
                actions=[
                    "sqs:SendMessage",
                    "sqs:GetQueueAttributes",
                ],
                resources=[queue.arn],
            ),
        ],
    )
