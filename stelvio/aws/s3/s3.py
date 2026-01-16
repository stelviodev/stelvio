from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack, final

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
from stelvio.component import Component, link_config_creator, safe_name
from stelvio.link import Link, Linkable, LinkableMixin, LinkConfig

if TYPE_CHECKING:
    from stelvio.aws.queue import Queue
    from stelvio.aws.topic import Topic

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

VALID_S3_EVENTS: set[str] = set(S3EventType.__args__)

# SQS ARN has 6 colon-separated parts: arn:aws:sqs:<region>:<account-id>:<queue-name>
_SQS_ARN_PARTS = 6


def _arn_to_sqs_url(arn: str) -> str:
    """Convert an SQS ARN to its corresponding URL.

    ARN format: arn:aws:sqs:<region>:<account-id>:<queue-name>
    URL format: https://sqs.<region>.amazonaws.com/<account-id>/<queue-name>
    """
    parts = arn.split(":")
    if len(parts) != _SQS_ARN_PARTS or parts[2] != "sqs":
        raise ValueError(f"Invalid SQS ARN format: {arn}")
    region = parts[3]
    account_id = parts[4]
    queue_name = parts[5]
    return f"https://sqs.{region}.amazonaws.com/{account_id}/{queue_name}"


@dataclass(frozen=True, kw_only=True)
class BucketNotifyConfig:
    """Configuration for S3 bucket event notifications.

    Args:
        events: List of S3 event types to subscribe to (required).
        filter_prefix: Filter notifications by object key prefix.
        filter_suffix: Filter notifications by object key suffix.
        function: Lambda function handler to invoke.
        queue: SQS queue to send notifications to.
        topic: SNS topic to send notifications to.
        links: List of links to grant the notification function access to other resources.
    """

    events: list[S3EventType]
    filter_prefix: str | None = None
    filter_suffix: str | None = None
    function: str | FunctionConfig | FunctionConfigDict | None = None
    queue: Queue | str | None = None
    topic: Topic | str | None = None
    links: list[Link | Linkable] = field(default_factory=list)


class BucketNotifyConfigDict(TypedDict, total=False):
    """Configuration dictionary for S3 bucket event notifications."""

    events: list[S3EventType]
    filter_prefix: str
    filter_suffix: str
    function: str | FunctionConfig | FunctionConfigDict
    queue: Queue | str
    topic: Topic | str
    links: list[Link | Linkable]


@dataclass(frozen=True, kw_only=True)
class _BucketNotification:
    """Internal representation of a validated bucket notification."""

    name: str
    events: list[S3EventType]
    filter_prefix: str | None
    filter_suffix: str | None
    links: list[Link | Linkable] = field(default_factory=list)
    # Exactly one of these will be set
    function_config: FunctionConfig | None = None
    queue_ref: Queue | str | None = None
    topic_ref: Topic | str | None = None


@final
@dataclass(frozen=True, kw_only=True)
class S3BucketResources:
    bucket: pulumi_aws.s3.Bucket
    public_access_block: pulumi_aws.s3.BucketPublicAccessBlock
    bucket_policy: pulumi_aws.s3.BucketPolicy | None
    # Notification-related resources
    bucket_notification: pulumi_aws.s3.BucketNotification | None = None
    notification_functions: list[Function] = field(default_factory=list)
    notification_permissions: list[lambda_.Permission] = field(default_factory=list)
    queue_policies: list[sqs.QueuePolicy] = field(default_factory=list)
    topic_policies: list[sns.TopicPolicy] = field(default_factory=list)


@final
class Bucket(Component[S3BucketResources], LinkableMixin):
    _notifications: list[_BucketNotification]

    def __init__(
        self, name: str, versioning: bool = False, access: Literal["public"] | None = None
    ):
        super().__init__(name)
        self.versioning = versioning
        self.access = access
        self._resources = None
        self._notifications = []

    def _create_resources(self) -> S3BucketResources:
        bucket = pulumi_aws.s3.Bucket(
            context().prefix(self.name),
            bucket=context().prefix(self.name),
            versioning={"enabled": self.versioning},
        )

        # Configure public access block
        if self.access == "public":
            # setup readonly configuration
            public_access_block = pulumi_aws.s3.BucketPublicAccessBlock(
                context().prefix(f"{self.name}-pab"),
                bucket=bucket.id,
                block_public_acls=False,
                block_public_policy=False,
                ignore_public_acls=False,
                restrict_public_buckets=False,
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
                bucket=bucket.id,
                policy=public_read_policy.json,
            )
            pulumi.export(f"s3bucket_{self.name}_policy_id", bucket_policy.id)
        else:
            public_access_block = pulumi_aws.s3.BucketPublicAccessBlock(
                context().prefix(f"{self.name}-pab"),
                bucket=bucket.id,
                block_public_acls=True,
                block_public_policy=True,
                ignore_public_acls=True,
                restrict_public_buckets=True,
            )
            bucket_policy = None

        pulumi.export(f"s3bucket_{self.name}_arn", bucket.arn)
        pulumi.export(f"s3bucket_{self.name}_name", bucket.bucket)
        pulumi.export(f"s3bucket_{self.name}_public_access_block_id", public_access_block.id)

        # Create notification resources if any notifications configured
        (
            bucket_notification,
            notification_functions,
            notification_permissions,
            queue_policies,
            topic_policies,
        ) = self._create_notification_resources(bucket)

        return S3BucketResources(
            bucket=bucket,
            public_access_block=public_access_block,
            bucket_policy=bucket_policy,
            bucket_notification=bucket_notification,
            notification_functions=notification_functions,
            notification_permissions=notification_permissions,
            queue_policies=queue_policies,
            topic_policies=topic_policies,
        )

    def _create_notification_resources(
        self, bucket: pulumi_aws.s3.Bucket
    ) -> tuple[
        pulumi_aws.s3.BucketNotification | None,
        list[Function],
        list[lambda_.Permission],
        list[sqs.QueuePolicy],
        list[sns.TopicPolicy],
    ]:
        """Create all notification-related resources."""
        if not self._notifications:
            return None, [], [], [], []

        # Import here to avoid circular imports
        from stelvio.aws.queue import Queue
        from stelvio.aws.topic import Topic

        lambda_function_configs: list[pulumi_aws.s3.BucketNotificationLambdaFunctionArgs] = []
        queue_configs: list[pulumi_aws.s3.BucketNotificationQueueArgs] = []
        topic_configs: list[pulumi_aws.s3.BucketNotificationTopicArgs] = []
        notification_functions: list[Function] = []
        notification_permissions: list[lambda_.Permission] = []
        queue_policies: list[sqs.QueuePolicy] = []
        topic_policies: list[sns.TopicPolicy] = []

        # Track processed queues/topics to avoid duplicate policies
        processed_queues: set[Queue] = set()
        processed_topics: set[Topic] = set()

        for notification in self._notifications:
            if notification.function_config is not None:
                # Merge links from notification with existing links from function config
                merged_links = [*notification.links, *notification.function_config.links]
                config_with_merged_links = replace(
                    notification.function_config, links=merged_links
                )

                # Create Lambda function for this notification
                function = Function(
                    f"{self.name}-{notification.name}",
                    config=config_with_merged_links,
                )
                notification_functions.append(function)

                lambda_function = function.resources.function

                # Create Lambda Permission for S3 to invoke the function
                permission = lambda_.Permission(
                    safe_name(context().prefix(), f"{self.name}-{notification.name}-perm", 64),
                    action="lambda:InvokeFunction",
                    function=lambda_function.name,
                    principal="s3.amazonaws.com",
                    source_arn=bucket.arn,
                )
                notification_permissions.append(permission)

                lambda_function_configs.append(
                    pulumi_aws.s3.BucketNotificationLambdaFunctionArgs(
                        lambda_function_arn=lambda_function.arn,
                        events=notification.events,
                        filter_prefix=notification.filter_prefix,
                        filter_suffix=notification.filter_suffix,
                    )
                )

            elif notification.queue_ref is not None:
                # Handle queue notification
                queue_arn, queue_url = self._resolve_queue(notification.queue_ref)

                # Only create policy for Queue components that we haven't processed yet
                # We skip string ARNs (external queues) to avoid overwriting their policies
                if (
                    isinstance(notification.queue_ref, Queue)
                    and notification.queue_ref not in processed_queues
                ):
                    processed_queues.add(notification.queue_ref)

                    # Create SQS queue policy to allow S3 to send messages
                    policy_document = pulumi.Output.all(queue_arn, bucket.arn).apply(
                        lambda args: pulumi.Output.json_dumps(
                            {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Principal": {"Service": "s3.amazonaws.com"},
                                        "Action": "sqs:SendMessage",
                                        "Resource": args[0],
                                        "Condition": {"ArnEquals": {"aws:SourceArn": args[1]}},
                                    }
                                ],
                            }
                        )
                    )

                    queue_policy = sqs.QueuePolicy(
                        safe_name(
                            context().prefix(),
                            f"{self.name}-{notification.queue_ref.name}-qp",
                            64,
                        ),
                        queue_url=queue_url,
                        policy=policy_document,
                    )
                    queue_policies.append(queue_policy)

                queue_configs.append(
                    pulumi_aws.s3.BucketNotificationQueueArgs(
                        queue_arn=queue_arn,
                        events=notification.events,
                        filter_prefix=notification.filter_prefix,
                        filter_suffix=notification.filter_suffix,
                    )
                )

            elif notification.topic_ref is not None:
                # Handle topic notification
                topic_arn = self._resolve_topic(notification.topic_ref)

                # Only create policy for Topic components that we haven't processed yet
                # We skip string ARNs (external topics) to avoid overwriting their policies
                if (
                    isinstance(notification.topic_ref, Topic)
                    and notification.topic_ref not in processed_topics
                ):
                    processed_topics.add(notification.topic_ref)

                    # Create SNS topic policy to allow S3 to publish messages
                    policy_document = pulumi.Output.all(topic_arn, bucket.arn).apply(
                        lambda args: pulumi.Output.json_dumps(
                            {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Principal": {"Service": "s3.amazonaws.com"},
                                        "Action": "sns:Publish",
                                        "Resource": args[0],
                                        "Condition": {"ArnLike": {"aws:SourceArn": args[1]}},
                                    }
                                ],
                            }
                        )
                    )

                    topic_policy = sns.TopicPolicy(
                        safe_name(
                            context().prefix(),
                            f"{self.name}-{notification.topic_ref.name}-tp",
                            64,
                        ),
                        arn=notification.topic_ref.arn,
                        policy=policy_document,
                    )
                    topic_policies.append(topic_policy)

                topic_configs.append(
                    pulumi_aws.s3.BucketNotificationTopicArgs(
                        topic_arn=topic_arn,
                        events=notification.events,
                        filter_prefix=notification.filter_prefix,
                        filter_suffix=notification.filter_suffix,
                    )
                )

        # Create single BucketNotification resource with all configurations
        # We need to depend on all permissions/policies being created first
        depends_on: list[pulumi.Resource] = [
            *notification_permissions,
            *queue_policies,
            *topic_policies,
        ]

        bucket_notification = pulumi_aws.s3.BucketNotification(
            context().prefix(f"{self.name}-notifications"),
            bucket=bucket.id,
            lambda_functions=lambda_function_configs if lambda_function_configs else None,
            queues=queue_configs if queue_configs else None,
            topics=topic_configs if topic_configs else None,
            opts=pulumi.ResourceOptions(depends_on=depends_on) if depends_on else None,
        )

        return (
            bucket_notification,
            notification_functions,
            notification_permissions,
            queue_policies,
            topic_policies,
        )

    @staticmethod
    def _resolve_queue(
        queue_ref: Queue | str,
    ) -> tuple[pulumi.Output[str], pulumi.Output[str]]:
        """Resolve queue reference to ARN and URL.

        Args:
            queue_ref: Either a Queue component or a queue ARN string.

        Returns:
            Tuple of (queue_arn, queue_url) as Pulumi Outputs.
        """
        # Import here to avoid circular imports
        from stelvio.aws.queue import Queue

        if isinstance(queue_ref, Queue):
            return queue_ref.arn, queue_ref.url

        # String reference - treat as ARN and derive URL from it
        # ARN format: arn:aws:sqs:<region>:<account-id>:<queue-name>
        # URL format: https://sqs.<region>.amazonaws.com/<account-id>/<queue-name>
        arn = queue_ref
        queue_url = pulumi.Output.from_input(arn).apply(lambda arn_str: _arn_to_sqs_url(arn_str))
        return pulumi.Output.from_input(arn), queue_url

    @staticmethod
    def _resolve_topic(
        topic_ref: Topic | str,
    ) -> pulumi.Output[str]:
        """Resolve topic reference to ARN.

        Args:
            topic_ref: Either a Topic component or a topic ARN string.

        Returns:
            Topic ARN as a Pulumi Output.
        """
        # Import here to avoid circular imports
        from stelvio.aws.topic import Topic

        if isinstance(topic_ref, Topic):
            return topic_ref.arn

        # String reference - treat as ARN
        return pulumi.Output.from_input(topic_ref)

    def notify(  # noqa: PLR0913
        self,
        name: str,
        /,
        *,
        events: list[S3EventType],
        filter_prefix: str | None = None,
        filter_suffix: str | None = None,
        function: str | FunctionConfig | FunctionConfigDict | None = None,
        queue: Queue | str | None = None,
        topic: Topic | str | None = None,
        links: list[Link | Linkable] | None = None,
        **opts: Unpack[FunctionConfigDict],
    ) -> None:
        """Subscribe to event notifications from this bucket.

        You can subscribe to these notifications with a function, a queue, or a topic.

        Args:
            name: Unique name for this notification subscription.
            events: List of S3 event types to subscribe to (required).
            filter_prefix: Filter notifications by object key prefix.
            filter_suffix: Filter notifications by object key suffix.
            function: Lambda function handler to invoke. Can be:
                - str: Handler path (e.g., "functions/handler.process")
                - FunctionConfig: Complete function configuration
                - FunctionConfigDict: Function configuration dictionary
            queue: SQS queue to send notifications to. Can be:
                - Queue: Queue component instance
                - str: Queue ARN (e.g., "arn:aws:sqs:us-east-1:123456789:my-queue")
            topic: SNS topic to send notifications to. Can be:
                - Topic: Topic component instance
                - str: Topic ARN (e.g., "arn:aws:sns:us-east-1:123456789:my-topic")
            links: List of links to grant the notification function access to other
                resources (e.g., DynamoDB tables, S3 buckets, queues). Only valid
                when using function notifications.
            **opts: Additional function configuration options (memory, timeout, etc.)
                when function is specified as a string.

        Raises:
            RuntimeError: If called after bucket resources have been created.
            ValueError: If not exactly one of function, queue, or topic is specified.
            ValueError: If links is specified with queue or topic (they don't execute code).
            ValueError: If events list is empty or contains invalid event types.
            ValueError: If a notification with the same name already exists.
        """
        # Check resources haven't been created yet
        if self._resources is not None:
            raise RuntimeError(
                "Cannot add notifications after Bucket resources have been created."
            )

        # Count how many targets are specified
        targets_specified = sum(x is not None for x in [function, queue, topic])

        if targets_specified == 0:
            raise ValueError(
                "Missing notification target: must specify exactly one of "
                "'function', 'queue', or 'topic'."
            )
        if targets_specified > 1:
            raise ValueError(
                "Invalid configuration: cannot specify multiple notification targets "
                "- provide exactly one of 'function', 'queue', or 'topic'."
            )

        # Validate links is not used with queue or topic (they don't execute code)
        if queue is not None and links:
            raise ValueError(
                "The 'links' parameter cannot be used with 'queue' notifications "
                "- queues do not execute code. Add links when subscribing to the queue instead."
            )
        if topic is not None and links:
            raise ValueError(
                "The 'links' parameter cannot be used with 'topic' notifications "
                "- topics do not execute code. Add links when subscribing to the topic instead."
            )

        # Validate events
        if not events:
            raise ValueError("events list cannot be empty - at least one event type is required.")
        invalid_events = [e for e in events if e not in VALID_S3_EVENTS]
        if invalid_events:
            raise ValueError(
                f"Invalid S3 event type(s): {invalid_events}. "
                f"Valid events are: {sorted(VALID_S3_EVENTS)}"
            )

        # Check for duplicate notification names
        if any(n.name == name for n in self._notifications):
            raise ValueError(f"Notification '{name}' already exists for bucket '{self.name}'.")

        # Resolve function config if provided
        function_config: FunctionConfig | None = None
        if function is not None:
            function_config = parse_handler_config(function, opts)

        # Create internal notification object
        notification = _BucketNotification(
            name=name,
            events=events,
            filter_prefix=filter_prefix,
            filter_suffix=filter_suffix,
            links=links or [],
            function_config=function_config,
            queue_ref=queue,
            topic_ref=topic,
        )

        self._notifications.append(notification)

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
