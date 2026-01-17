import json
from dataclasses import dataclass
from typing import Any, TypedDict, Unpack, final

import pulumi
from pulumi import Input, Output, ResourceOptions
from pulumi_aws import lambda_, sns, sqs

from stelvio import context
from stelvio.aws.function import (
    Function,
    FunctionConfig,
    FunctionConfigDict,
    FunctionCustomizationDict,
    parse_handler_config,
)
from stelvio.aws.permission import AwsPermission
from stelvio.aws.queue import Queue
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import LinkableMixin, LinkConfig

MAX_TOPIC_NAME_LENGTH = 256
FIFO_SUFFIX = ".fifo"


@final
@dataclass(frozen=True)
class TopicResources:
    """Resources created for a Topic."""

    topic: sns.Topic


@final
@dataclass(frozen=True)
class TopicSubscriptionResources:
    """Resources created for a TopicSubscription (Lambda)."""

    function: Function
    subscription: sns.TopicSubscription
    permission: lambda_.Permission


@final
@dataclass(frozen=True)
class TopicQueueSubscriptionResources:
    """Resources created for a TopicQueueSubscription (SQS)."""

    subscription: sns.TopicSubscription
    queue_policy: sqs.QueuePolicy | None  # None if ARN string was passed


class TopicSubscriptionCustomizationDict(TypedDict):
    function: FunctionCustomizationDict | dict[str, Any] | None
    subscription: sns.TopicSubscriptionArgs | dict[str, Any] | None
    permission: lambda_.PermissionArgs | dict[str, Any] | None


@final
class TopicSubscription(Component[TopicSubscriptionResources, TopicSubscriptionCustomizationDict]):
    """Lambda function subscription to an SNS topic."""

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        topic: "Topic",
        handler: str | FunctionConfig | FunctionConfigDict | None,
        filter_: dict[str, list] | None,
        opts: FunctionConfigDict,
        customize: TopicSubscriptionCustomizationDict | None = None,
    ):
        super().__init__(f"{name}-subscription", customize=customize)
        self.topic = topic
        self.function_name = name
        self.filter_ = filter_
        self.handler = parse_handler_config(handler, opts)

    def _create_resources(self) -> TopicSubscriptionResources:
        function = Function(self.function_name, self.handler)

        subscription = sns.TopicSubscription(
            safe_name(context().prefix(), self.name, MAX_TOPIC_NAME_LENGTH),
            topic=self.topic.arn,
            protocol="lambda",
            endpoint=function.resources.function.arn,
            filter_policy=json.dumps(self.filter_) if self.filter_ else None,
        )

        permission = lambda_.Permission(
            safe_name(context().prefix(), f"{self.name}-perm", 100),
            action="lambda:InvokeFunction",
            function=function.function_name,
            principal="sns.amazonaws.com",
            source_arn=self.topic.arn,
        )

        return TopicSubscriptionResources(
            function=function,
            subscription=subscription,
            permission=permission,
        )


class TopicQueueSubscriptionCustomizationDict(TypedDict):
    subscription: sns.TopicSubscriptionArgs | dict[str, Any] | None
    queue_policy: sqs.QueuePolicyArgs | dict[str, Any] | None


@final
class TopicQueueSubscription(
    Component[TopicQueueSubscriptionResources, TopicQueueSubscriptionCustomizationDict]
):
    """SQS queue subscription to an SNS topic."""

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        topic: "Topic",
        queue: Queue | Input[str],
        filter_: dict[str, list] | None,
        raw_message_delivery: bool,
        customize: TopicQueueSubscriptionCustomizationDict | None = None,
    ):
        super().__init__(name, customize=customize)
        self.topic = topic
        self.queue = queue
        self.filter_ = filter_
        self.raw_message_delivery = raw_message_delivery

    def _create_resources(self) -> TopicQueueSubscriptionResources:
        is_queue_component = isinstance(self.queue, Queue)
        queue_arn = self.queue.arn if is_queue_component else self.queue

        queue_policy = None
        if is_queue_component:
            queue_policy = self._create_queue_policy()

        subscription = sns.TopicSubscription(
            safe_name(context().prefix(), self.name, MAX_TOPIC_NAME_LENGTH),
            topic=self.topic.arn,
            protocol="sqs",
            endpoint=queue_arn,
            filter_policy=json.dumps(self.filter_) if self.filter_ else None,
            raw_message_delivery=self.raw_message_delivery,
            opts=ResourceOptions(depends_on=[queue_policy]) if queue_policy else None,
        )

        return TopicQueueSubscriptionResources(
            subscription=subscription,
            queue_policy=queue_policy,
        )

    def _create_queue_policy(self) -> sqs.QueuePolicy:
        """Create SQS policy allowing SNS to send messages to the queue."""
        queue = self.queue  # Already verified as Queue in _create_resources
        account_id = queue.arn.apply(lambda arn: arn.split(":")[4])

        policy_document = pulumi.Output.all(
            queue.arn,
            account_id,
        ).apply(
            lambda args: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "sns.amazonaws.com"},
                            "Action": "sqs:SendMessage",
                            "Resource": args[0],
                            "Condition": {"StringEquals": {"aws:SourceAccount": args[1]}},
                        }
                    ],
                }
            )
        )

        return sqs.QueuePolicy(
            safe_name(
                context().prefix(),
                f"{queue.name}-{self.topic.name}-sns-policy",
                MAX_TOPIC_NAME_LENGTH,
            ),
            queue_url=queue.url,
            policy=policy_document,
        )


class TopicCustomizationDict(TypedDict):
    topic: sns.TopicArgs | dict[str, Any] | None


@final
class Topic(Component[TopicResources, TopicCustomizationDict], LinkableMixin):
    """AWS SNS Topic component.

    Args:
        name: Topic name
        fifo: Whether this is a FIFO topic (default: False)
        customize: Customization dictionary

    Examples:
        # Standard topic
        notifications = Topic("notifications")

        # FIFO topic
        orders = Topic("orders", fifo=True)

        # Subscribe Lambda
        notifications.subscribe("handler", "functions/notify.handler")

        # Subscribe SQS queue
        notifications.subscribe_queue("analytics", analytics_queue)
    """

    _subscriptions: list[TopicSubscription]
    _queue_subscriptions: list[TopicQueueSubscription]

    def __init__(
        self,
        name: str,
        /,
        *,
        fifo: bool = False,
        customize: TopicCustomizationDict | None = None,
    ):
        super().__init__(name, customize=customize)
        self._fifo = fifo
        self._subscriptions = []
        self._queue_subscriptions = []

    @property
    def fifo(self) -> bool:
        """Whether this is a FIFO topic."""
        return self._fifo

    @property
    def arn(self) -> Output[str]:
        """Topic ARN."""
        return self.resources.topic.arn

    @property
    def topic_name(self) -> Output[str]:
        """Topic name in AWS."""
        return self.resources.topic.name

    def _create_resources(self) -> TopicResources:
        suffix = FIFO_SUFFIX if self._fifo else ""
        name = self.name.removesuffix(suffix)
        topic_name = safe_name(context().prefix(), name, MAX_TOPIC_NAME_LENGTH, suffix=suffix)

        topic = sns.Topic(
            topic_name,
            name=topic_name,
            fifo_topic=self._fifo if self._fifo else None,
            content_based_deduplication=self._fifo if self._fifo else None,
        )

        pulumi.export(f"topic_{self.name}_arn", topic.arn)
        pulumi.export(f"topic_{self.name}_name", topic.name)

        return TopicResources(topic=topic)

    def subscribe(
        self,
        name: str,
        handler: str | FunctionConfig | FunctionConfigDict | None = None,
        /,
        *,
        filter_: dict[str, list] | None = None,
        customize: TopicSubscriptionCustomizationDict | None = None,
        **opts: Unpack[FunctionConfigDict],
    ) -> TopicSubscription:
        """Subscribe a Lambda function to this topic.

        Args:
            name: Name for the subscription (used in Lambda function naming)
            handler: Lambda handler specification
            filter_: SNS filter policy for message filtering
            customize: Customization dictionary
            **opts: Lambda function configuration (memory, timeout, etc.)

        Raises:
            ValueError: If called on a FIFO topic (Lambda can't subscribe to FIFO)
            ValueError: If a subscription with the same name already exists
        """
        if self._fifo:
            raise ValueError(
                f"Cannot subscribe Lambda to FIFO topic '{self.name}'. "
                "Lambda functions cannot subscribe to FIFO topics. "
                "Use subscribe_queue() with an SQS FIFO queue instead."
            )

        function_name = f"{self.name}-{name}"
        subscription_name = f"{function_name}-subscription"

        if any(sub.name == subscription_name for sub in self._subscriptions):
            raise ValueError(f"Subscription '{name}' already exists for topic '{self.name}'")

        subscription = TopicSubscription(
            function_name,
            self,
            handler,
            filter_,
            opts,
            customize=customize,
        )
        self._subscriptions.append(subscription)
        return subscription

    def subscribe_queue(
        self,
        name: str,
        queue: Queue | Input[str],
        /,
        *,
        filter_: dict[str, list] | None = None,
        raw_message_delivery: bool = False,
        customize: TopicQueueSubscriptionCustomizationDict | None = None,
    ) -> TopicQueueSubscription:
        """Subscribe an SQS queue to this topic.

        FIFO topics support both FIFO and standard queues.

        Args:
            name: Name for the subscription
            queue: Queue component or queue ARN
            filter_: SNS filter policy for message filtering
            raw_message_delivery: If True, send raw message without SNS envelope
            customize: Customization dictionary

        Raises:
            ValueError: If a subscription with the same name already exists
        """
        subscription_name = f"{self.name}-{name}-queue-subscription"

        if any(sub.name == subscription_name for sub in self._queue_subscriptions):
            raise ValueError(f"Queue subscription '{name}' already exists for topic '{self.name}'")

        subscription = TopicQueueSubscription(
            subscription_name,
            self,
            queue,
            filter_,
            raw_message_delivery,
            customize=customize,
        )
        self._queue_subscriptions.append(subscription)
        return subscription


@link_config_creator(Topic)
def default_topic_link(topic: Topic) -> LinkConfig:
    """Default link configuration for Topic component.

    Grants permissions to publish messages to the topic.
    """
    t = topic.resources.topic
    return LinkConfig(
        properties={
            "topic_arn": t.arn,
            "topic_name": t.name,
        },
        permissions=[
            AwsPermission(
                actions=["sns:Publish"],
                resources=[t.arn],
            ),
        ],
    )
