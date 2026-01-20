from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, final, get_args

import pulumi
import pulumi_aws
from pulumi_aws import lambda_, sns, sqs

from stelvio import context
from stelvio.aws.function import (
    Function,
    FunctionConfig,
    FunctionConfigDict,
    parse_handler_config,
)
from stelvio.aws.permission import AwsPermission
from stelvio.aws.queue import Queue
from stelvio.aws.topic import Topic
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import Link, Linkable, LinkableMixin, LinkConfig

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stelvio.aws.function.function import FunctionCustomizationDict

# All valid S3 event types
S3EventType = Literal[
    "s3:ObjectCreated:*",
    "s3:ObjectCreated:Put",
    "s3:ObjectCreated:Post",
    "s3:ObjectCreated:Copy",
    "s3:ObjectCreated:CompleteMultipartUpload",
    "s3:ObjectRemoved:*",
    "s3:ObjectRemoved:Delete",
    "s3:ObjectRemoved:DeleteMarkerCreated",
    "s3:ObjectRestore:*",
    "s3:ObjectRestore:Post",
    "s3:ObjectRestore:Completed",
    "s3:ObjectRestore:Delete",
    "s3:ReducedRedundancyLostObject",
    "s3:Replication:*",
    "s3:Replication:OperationFailedReplication",
    "s3:Replication:OperationMissedThreshold",
    "s3:Replication:OperationReplicatedAfterThreshold",
    "s3:Replication:OperationNotTracked",
    "s3:LifecycleExpiration:*",
    "s3:LifecycleExpiration:Delete",
    "s3:LifecycleExpiration:DeleteMarkerCreated",
    "s3:LifecycleTransition",
    "s3:IntelligentTiering",
    "s3:ObjectTagging:*",
    "s3:ObjectTagging:Put",
    "s3:ObjectTagging:Delete",
    "s3:ObjectAcl:Put",
]

VALID_S3_EVENTS: tuple[S3EventType, ...] = get_args(S3EventType)
VALID_S3_EVENTS_SET: frozenset[S3EventType] = frozenset(VALID_S3_EVENTS)


class BucketNotificationResourceDict(TypedDict):
    """Internal dictionary for bucket notification resource configuration."""

    events: list[S3EventType]
    filter_prefix: str | None
    filter_suffix: str | None
    target_arn: pulumi.Output[str]
    target_type: Literal["lambda", "queue", "topic"]


@final
@dataclass(frozen=True, kw_only=True)
class BucketNotifySubscriptionResources:
    """Resources created for a BucketNotifySubscription."""

    function: FunctionCustomizationDict | dict[str, Any] | None = None
    permission: lambda_.PermissionArgs | dict[str, Any] | None = None
    queue_policy: sqs.QueuePolicyArgs | dict[str, Any] | None = None
    topic_policy: sns.TopicPolicyArgs | dict[str, Any] | None = None


class BucketNotifySubscriptionCustomizationDict(TypedDict, total=False):
    function: dict[str, Any] | None
    permission: dict[str, Any] | None
    queue_policy: dict[str, Any] | None
    topic_policy: dict[str, Any] | None


@final
class BucketNotifySubscription(Component[BucketNotifySubscriptionResources, None]):
    """Lambda/SQS/SNS subscription to S3 bucket event notifications."""

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        bucket: Bucket,
        events: list[S3EventType],
        filter_prefix: str | None,
        filter_suffix: str | None,
        function_config: FunctionConfig | None,
        queue_ref: Queue | str | None,
        topic_ref: Topic | str | None,
        links: Sequence[Link | Linkable],
        customize: BucketNotifySubscriptionCustomizationDict | None = None,
    ):
        super().__init__(f"{name}-subscription", customize=customize)
        self.bucket = bucket
        self.function_name = name  # Function gets the original name
        self.events = events
        self.filter_prefix = filter_prefix
        self.filter_suffix = filter_suffix
        self.function_config = function_config
        self.queue_ref = queue_ref
        self.topic_ref = topic_ref
        self.links = links
        # This will be set by Bucket._create_notification_resources before triggering resources
        self._bucket_arn: pulumi.Output[str] | None = None
        # Set to True if another subscription already created a policy for this queue/topic
        self._skip_policy_creation: bool = False

    def _create_resources(self) -> BucketNotifySubscriptionResources:
        if self._bucket_arn is None:
            raise RuntimeError(
                "BucketNotifySubscription._bucket_arn must be set before creating resources. "
                "This is an internal error - subscription resources should only be created "
                "through Bucket._create_notification_resources."
            )

        function: Function | None = None
        permission: lambda_.Permission | None = None
        queue_policy: sqs.QueuePolicy | None = None
        topic_policy: sns.TopicPolicy | None = None

        if self.function_config is not None:
            # Merge links from notification with existing links from function config
            merged_links = [*self.links, *self.function_config.links]
            config_with_merged_links = replace(self.function_config, links=merged_links)

            # Create Lambda function for this notification
            function = Function(self.function_name, config=config_with_merged_links)

            # Create Lambda Permission for S3 to invoke the function
            permission = lambda_.Permission(
                safe_name(context().prefix(), f"{self.name}-perm", 64),
                action="lambda:InvokeFunction",
                function=function.resources.function.name,
                principal="s3.amazonaws.com",
                source_arn=self._bucket_arn,
            )

        elif self.queue_ref is not None:
            # Only create policy if another subscription hasn't already created one
            if not self._skip_policy_creation:
                queue_policy = self._create_queue_policy()

        elif self.topic_ref is not None:
            # Only create policy if another subscription hasn't already created one
            if not self._skip_policy_creation:
                topic_policy = self._create_topic_policy()

        return BucketNotifySubscriptionResources(
            function=function,
            permission=permission,
            queue_policy=queue_policy,
            topic_policy=topic_policy,
        )

    def _create_queue_policy(self) -> sqs.QueuePolicy | None:
        """Create SQS queue policy to allow S3 to send messages.

        Returns None if queue_ref is a string ARN (external queue).
        """
        if not isinstance(self.queue_ref, Queue):
            return None

        queue_arn = self.queue_ref.arn
        queue_url = self.queue_ref.url
        account_id = queue_arn.apply(lambda arn: arn.split(":")[4])

        policy_document = pulumi.Output.all(queue_arn, account_id).apply(
            lambda args: pulumi.Output.json_dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "s3.amazonaws.com"},
                            "Action": "sqs:SendMessage",
                            "Resource": args[0],
                            "Condition": {"StringEquals": {"aws:SourceAccount": args[1]}},
                        }
                    ],
                }
            )
        )

        return sqs.QueuePolicy(
            safe_name(context().prefix(), f"{self.name}-qp", 64),
            **self._customizer(
                "queue_policy",
                {
                    "queue_url": queue_url,
                    "policy": policy_document,
                },
            ),
        )

    def _create_topic_policy(self) -> sns.TopicPolicy | None:
        """Create SNS topic policy to allow S3 to publish messages.

        Returns None if topic_ref is a string ARN (external topic).
        """
        if not isinstance(self.topic_ref, Topic):
            return None

        topic_arn = self.topic_ref.arn
        account_id = topic_arn.apply(lambda arn: arn.split(":")[4])

        policy_document = pulumi.Output.all(topic_arn, account_id).apply(
            lambda args: pulumi.Output.json_dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "s3.amazonaws.com"},
                            "Action": "sns:Publish",
                            "Resource": args[0],
                            "Condition": {"StringEquals": {"aws:SourceAccount": args[1]}},
                        }
                    ],
                }
            )
        )

        return sns.TopicPolicy(
            safe_name(context().prefix(), f"{self.name}-tp", 64),
            **self._customizer(
                "topic_policy",
                {
                    "arn": topic_arn,
                    "policy": policy_document,
                },
            ),
        )

    def get_notification_config(self) -> BucketNotificationResourceDict:
        """Get the notification configuration for this subscription.

        Returns a dictionary containing the events, filters, target ARN,
        and target type for use in BucketNotification resource.
        """
        target_arn: pulumi.Output[str]
        target_type: Literal["lambda", "queue", "topic"]

        if self.function_config is not None:
            function = self.resources.function
            if function is None:
                raise RuntimeError(
                    "BucketNotifySubscription expected to create a Function for a "
                    "function target, but got None. This is an internal error."
                )

            target_arn = function.resources.function.arn
            target_type = "lambda"
        elif self.queue_ref is not None:
            target_arn = self._resolve_queue_arn()
            target_type = "queue"
        else:
            target_arn = self._resolve_topic_arn()
            target_type = "topic"

        return {
            "events": self.events,
            "filter_prefix": self.filter_prefix,
            "filter_suffix": self.filter_suffix,
            "target_arn": target_arn,
            "target_type": target_type,
        }

    def _resolve_queue_arn(self) -> pulumi.Output[str]:
        """Resolve queue reference to ARN."""
        if isinstance(self.queue_ref, Queue):
            return self.queue_ref.arn
        return pulumi.Output.from_input(self.queue_ref)

    def _resolve_topic_arn(self) -> pulumi.Output[str]:
        """Resolve topic reference to ARN."""
        if isinstance(self.topic_ref, Topic):
            return self.topic_ref.arn
        return pulumi.Output.from_input(self.topic_ref)


def _validate_events(events: list[S3EventType]) -> None:
    """Validate that events list is non-empty and contains only valid event types.

    Raises:
        ValueError: If events is empty or contains invalid event types.
    """
    if not events:
        raise ValueError("events list cannot be empty - at least one event type is required.")

    invalid_events = [e for e in events if e not in VALID_S3_EVENTS_SET]
    if invalid_events:
        raise ValueError(
            f"Invalid S3 event type(s): {invalid_events}. "
            f"Valid events are: {sorted(VALID_S3_EVENTS_SET)}"
        )


@dataclass(frozen=False, kw_only=True)
class _NotificationConfigs:
    """Mutable accumulator for bucket notification configurations."""

    lambda_functions: list[pulumi_aws.s3.BucketNotificationLambdaFunctionArgs] = field(
        default_factory=list
    )
    queues: list[pulumi_aws.s3.BucketNotificationQueueArgs] = field(default_factory=list)
    topics: list[pulumi_aws.s3.BucketNotificationTopicArgs] = field(default_factory=list)
    depends_on: list[pulumi.Resource] = field(default_factory=list)


@final
@dataclass(frozen=True, kw_only=True)
class S3BucketResources:
    bucket: pulumi_aws.s3.Bucket
    public_access_block: pulumi_aws.s3.BucketPublicAccessBlock
    bucket_policy: pulumi_aws.s3.BucketPolicy | None
    # Notification-related resources
    bucket_notification: pulumi_aws.s3.BucketNotification | None = None
    subscriptions: list[BucketNotifySubscription] = field(default_factory=list)


class S3BucketCustomizationDict(TypedDict, total=False):
    bucket: pulumi_aws.s3.BucketArgs | dict[str, Any] | None
    public_access_block: pulumi_aws.s3.BucketPublicAccessBlockArgs | dict[str, Any] | None
    bucket_policy: pulumi_aws.s3.BucketPolicyArgs | dict[str, Any] | None
    bucket_notification: pulumi_aws.s3.BucketNotificationArgs | dict[str, Any] | None
    subscriptions: BucketNotifySubscriptionCustomizationDict | dict[str, Any] | None


@final
class Bucket(Component[S3BucketResources, S3BucketCustomizationDict], LinkableMixin):
    _subscriptions: list[BucketNotifySubscription]

    def __init__(
        self,
        name: str,
        versioning: bool = False,
        access: Literal["public"] | None = None,
        customize: S3BucketCustomizationDict | None = None,
    ):
        super().__init__(name, customize=customize)
        self.versioning = versioning
        self.access = access
        self._resources = None
        self._subscriptions = []

    def _create_resources(self) -> S3BucketResources:
        bucket = pulumi_aws.s3.Bucket(
            context().prefix(self.name),
            **self._customizer(
                "bucket",
                {
                    "bucket": context().prefix(self.name),
                    "versioning": {"enabled": self.versioning},
                },
            ),
        )

        # Configure public access block
        if self.access == "public":
            # setup readonly configuration
            public_access_block = pulumi_aws.s3.BucketPublicAccessBlock(
                context().prefix(f"{self.name}-pab"),
                **self._customizer(
                    "public_access_block",
                    {
                        "bucket": bucket.id,
                        "block_public_acls": False,
                        "block_public_policy": False,
                        "ignore_public_acls": False,
                        "restrict_public_buckets": False,
                    },
                ),
            )
            public_read_policy = pulumi_aws.iam.get_policy_document(
                statements=[
                    {
                        "effect": "Allow",
                        "principals": [
                            {
                                "type": "*",
                                "identifiers": ["*"],
                            }
                        ],
                        "actions": ["s3:GetObject"],
                        "resources": [bucket.arn.apply(lambda arn: f"{arn}/*")],
                    }
                ]
            )
            bucket_policy = pulumi_aws.s3.BucketPolicy(
                context().prefix(f"{self.name}-policy"),
                **self._customizer(
                    "bucket_policy",
                    {
                        "bucket": bucket.id,
                        "policy": public_read_policy.json,
                    },
                ),
            )
            pulumi.export(f"s3bucket_{self.name}_policy_id", bucket_policy.id)
        else:
            public_access_block = pulumi_aws.s3.BucketPublicAccessBlock(
                context().prefix(f"{self.name}-pab"),
                **self._customizer(
                    "public_access_block",
                    {
                        "bucket": bucket.id,
                        "block_public_acls": True,
                        "block_public_policy": True,
                        "ignore_public_acls": True,
                        "restrict_public_buckets": True,
                    },
                ),
            )
            bucket_policy = None

        pulumi.export(f"s3bucket_{self.name}_arn", bucket.arn)
        pulumi.export(f"s3bucket_{self.name}_name", bucket.bucket)
        pulumi.export(f"s3bucket_{self.name}_public_access_block_id", public_access_block.id)

        # Create notification resources if any subscriptions configured
        bucket_notification = self._create_notification_resources(bucket)

        return S3BucketResources(
            bucket=bucket,
            public_access_block=public_access_block,
            bucket_policy=bucket_policy,
            bucket_notification=bucket_notification,
            subscriptions=self._subscriptions,
        )

    def _add_lambda_notification_config(
        self,
        sub_resources: BucketNotifySubscriptionResources,
        config: BucketNotificationResourceDict,
        configs: _NotificationConfigs,
    ) -> None:
        """Add Lambda notification configuration to the accumulator."""
        if sub_resources.permission:
            configs.depends_on.append(sub_resources.permission)

        configs.lambda_functions.append(
            pulumi_aws.s3.BucketNotificationLambdaFunctionArgs(
                **self._customizer(
                    "function",
                    {
                        "lambda_function_arn": config["target_arn"],
                        "events": config["events"],
                        "filter_prefix": config["filter_prefix"],
                        "filter_suffix": config["filter_suffix"],
                    },
                )
            )
        )

    def _add_queue_notification_config(
        self,
        sub_resources: BucketNotifySubscriptionResources,
        config: BucketNotificationResourceDict,
        configs: _NotificationConfigs,
    ) -> None:
        """Add SQS queue notification configuration to the accumulator."""
        if sub_resources.queue_policy:
            configs.depends_on.append(sub_resources.queue_policy)

        configs.queues.append(
            pulumi_aws.s3.BucketNotificationQueueArgs(
                queue_arn=config["target_arn"],
                events=config["events"],
                filter_prefix=config["filter_prefix"],
                filter_suffix=config["filter_suffix"],
            )
        )

    def _add_topic_notification_config(
        self,
        sub_resources: BucketNotifySubscriptionResources,
        config: BucketNotificationResourceDict,
        configs: _NotificationConfigs,
    ) -> None:
        """Add SNS topic notification configuration to the accumulator."""
        if sub_resources.topic_policy:
            configs.depends_on.append(sub_resources.topic_policy)

        configs.topics.append(
            pulumi_aws.s3.BucketNotificationTopicArgs(
                **self._customizer(
                    "topic",
                    {
                        "topic_arn": config["target_arn"],
                        "events": config["events"],
                        "filter_prefix": config["filter_prefix"],
                        "filter_suffix": config["filter_suffix"],
                    },
                )
            )
        )

    def _prepare_subscriptions(self, bucket: pulumi_aws.s3.Bucket) -> None:
        """Set bucket ARN and skip flags on subscriptions before resource creation.

        This must be called before triggering subscription resource creation to:
        1. Provide the bucket ARN needed by Lambda permissions
        2. Mark duplicate Queue/Topic subscriptions to skip policy creation
        """
        processed_queues: set[Queue] = set()
        processed_topics: set[Topic] = set()

        for subscription in self._subscriptions:
            subscription._bucket_arn = bucket.arn  # noqa: SLF001

            # Determine if we should skip policy creation for this subscription
            if isinstance(subscription.queue_ref, Queue):
                if subscription.queue_ref in processed_queues:
                    subscription._skip_policy_creation = True  # noqa: SLF001
                else:
                    processed_queues.add(subscription.queue_ref)
            elif isinstance(subscription.topic_ref, Topic):
                if subscription.topic_ref in processed_topics:
                    subscription._skip_policy_creation = True  # noqa: SLF001
                else:
                    processed_topics.add(subscription.topic_ref)

    def _create_notification_resources(
        self, bucket: pulumi_aws.s3.Bucket
    ) -> pulumi_aws.s3.BucketNotification | None:
        """Create all notification-related resources."""
        if not self._subscriptions:
            return None

        configs = _NotificationConfigs()

        # First pass: set bucket ARN and skip flags before triggering resource creation
        self._prepare_subscriptions(bucket)

        # Second pass: trigger resource creation and collect configs
        for subscription in self._subscriptions:
            sub_resources = subscription.resources
            config = subscription.get_notification_config()
            target_type = config["target_type"]

            if target_type == "lambda":
                self._add_lambda_notification_config(sub_resources, config, configs)
            elif target_type == "queue":
                self._add_queue_notification_config(sub_resources, config, configs)
            elif target_type == "topic":
                self._add_topic_notification_config(sub_resources, config, configs)

        # Create single BucketNotification resource with all configurations
        return pulumi_aws.s3.BucketNotification(
            context().prefix(f"{self.name}-notifications"),
            **self._customizer(
                "bucket_notification",
                {
                    "bucket": bucket.id,
                    "lambda_functions": configs.lambda_functions
                    if configs.lambda_functions
                    else None,
                    "queues": configs.queues if configs.queues else None,
                    "topics": configs.topics if configs.topics else None,
                },
            ),
            opts=pulumi.ResourceOptions(depends_on=configs.depends_on)
            if configs.depends_on
            else None,
        )

    def _check_can_add_notification(self, name: str) -> str:
        """Check that notification can be added and return the subscription name.

        Raises:
            RuntimeError: If called after bucket resources have been created.
            ValueError: If a notification with the same name already exists.
        """
        if self._resources is not None:
            raise RuntimeError(
                "Cannot add notifications after Bucket resources have been created."
            )

        # Build subscription name following Queue/Topic pattern
        subscription_name = f"{self.name}-{name}"
        expected_subscription_name = f"{subscription_name}-subscription"

        # Check for duplicate subscription names
        if any(sub.name == expected_subscription_name for sub in self._subscriptions):
            raise ValueError(f"Notification '{name}' already exists for bucket '{self.name}'.")

        return subscription_name

    def notify_function(  # noqa: PLR0913
        self,
        name: str,
        /,
        *,
        events: list[S3EventType],
        filter_prefix: str | None = None,
        filter_suffix: str | None = None,
        function: str | FunctionConfig | FunctionConfigDict | None = None,
        links: Sequence[Link | Linkable] | None = None,
        **opts: Unpack[FunctionConfigDict],
    ) -> BucketNotifySubscription:
        """Subscribe a Lambda function to event notifications from this bucket.

        Args:
            name: Unique name for this notification subscription.
            events: List of S3 event types to subscribe to (required).
            filter_prefix: Filter notifications by object key prefix.
            filter_suffix: Filter notifications by object key suffix.
            function: Lambda function handler to invoke. Can be:
                - str: Handler path (e.g., "functions/handler.process")
                - FunctionConfig: Complete function configuration
                - FunctionConfigDict: Function configuration dictionary
            links: List of links to grant the notification function access to other
                resources (e.g., DynamoDB tables, S3 buckets, queues).
            **opts: Additional function configuration options (memory, timeout, etc.)
                when function is specified as a string.

        Returns:
            BucketNotifySubscription: The created subscription component.

        Raises:
            RuntimeError: If called after bucket resources have been created.
            ValueError: If events list is empty or contains invalid event types.
            ValueError: If a notification with the same name already exists.
        """
        subscription_name = self._check_can_add_notification(name)
        _validate_events(events)

        # Normalize empty filter strings to None
        normalized_filter_prefix = filter_prefix if filter_prefix else None
        normalized_filter_suffix = filter_suffix if filter_suffix else None

        # Resolve function config
        function_config = parse_handler_config(function, opts)

        # Create subscription component
        subscription = BucketNotifySubscription(
            subscription_name,
            self,
            events,
            normalized_filter_prefix,
            normalized_filter_suffix,
            function_config,
            None,  # queue_ref
            None,  # topic_ref
            links or [],
            customize=self._customize.get("subscriptions"),
        )

        self._subscriptions.append(subscription)
        return subscription

    def notify_queue(
        self,
        name: str,
        /,
        *,
        events: list[S3EventType],
        filter_prefix: str | None = None,
        filter_suffix: str | None = None,
        queue: Queue | str | None = None,
    ) -> BucketNotifySubscription:
        """Subscribe an SQS queue to event notifications from this bucket.

        Args:
            name: Unique name for this notification subscription.
            events: List of S3 event types to subscribe to (required).
            filter_prefix: Filter notifications by object key prefix.
            filter_suffix: Filter notifications by object key suffix.
            queue: SQS queue to send notifications to. Can be:
                - Queue: Queue component instance
                - str: Queue ARN (e.g., "arn:aws:sqs:us-east-1:123456789:my-queue")

        Returns:
            BucketNotifySubscription: The created subscription component.

        Raises:
            RuntimeError: If called after bucket resources have been created.
            ValueError: If events list is empty or contains invalid event types.
            ValueError: If a notification with the same name already exists.
        """
        subscription_name = self._check_can_add_notification(name)
        _validate_events(events)

        # Normalize empty filter strings to None
        normalized_filter_prefix = filter_prefix if filter_prefix else None
        normalized_filter_suffix = filter_suffix if filter_suffix else None

        # Create subscription component
        subscription = BucketNotifySubscription(
            subscription_name,
            self,
            events,
            normalized_filter_prefix,
            normalized_filter_suffix,
            None,  # function_config
            queue,
            None,  # topic_ref
            [],  # links
            customize=self._customize.get("subscriptions"),
        )

        self._subscriptions.append(subscription)
        return subscription

    def notify_topic(
        self,
        name: str,
        /,
        *,
        events: list[S3EventType],
        filter_prefix: str | None = None,
        filter_suffix: str | None = None,
        topic: Topic | str | None = None,
    ) -> BucketNotifySubscription:
        """Subscribe an SNS topic to event notifications from this bucket.

        Args:
            name: Unique name for this notification subscription.
            events: List of S3 event types to subscribe to (required).
            filter_prefix: Filter notifications by object key prefix.
            filter_suffix: Filter notifications by object key suffix.
            topic: SNS topic to send notifications to. Can be:
                - Topic: Topic component instance
                - str: Topic ARN (e.g., "arn:aws:sns:us-east-1:123456789:my-topic")

        Returns:
            BucketNotifySubscription: The created subscription component.

        Raises:
            RuntimeError: If called after bucket resources have been created.
            ValueError: If events list is empty or contains invalid event types.
            ValueError: If a notification with the same name already exists.
        """
        subscription_name = self._check_can_add_notification(name)
        _validate_events(events)

        # Normalize empty filter strings to None
        normalized_filter_prefix = filter_prefix if filter_prefix else None
        normalized_filter_suffix = filter_suffix if filter_suffix else None

        # Create subscription component
        subscription = BucketNotifySubscription(
            subscription_name,
            self,
            events,
            normalized_filter_prefix,
            normalized_filter_suffix,
            None,  # function_config
            None,  # queue_ref
            topic,
            [],  # links
            customize=self._customize.get("subscriptions"),
        )

        self._subscriptions.append(subscription)
        return subscription

    @property
    def arn(self) -> pulumi.Output[str]:
        """Get the ARN of the S3 bucket."""
        return self.resources.bucket.arn


@link_config_creator(Bucket)
def default_bucket_link(bucket_component: Bucket) -> LinkConfig:
    bucket = bucket_component.resources.bucket
    return LinkConfig(
        properties={"bucket_arn": bucket.arn, "bucket_name": bucket.bucket},
        permissions=[
            AwsPermission(
                actions=["s3:ListBucket"],
                resources=[bucket.arn],
            ),
            AwsPermission(
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[bucket.arn.apply(lambda arn: f"{arn}/*")],
            ),
        ],
    )
