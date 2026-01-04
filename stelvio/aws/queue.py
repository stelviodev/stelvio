from dataclasses import dataclass
from typing import TypedDict, Unpack, final

import pulumi_aws

from stelvio.aws.function.function import Function
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig



@dataclass(frozen=True, kw_only=True)
class DlqConfig:
    queue: str
    retry: int = 3


@dataclass(frozen=True, kw_only=True)

class DlqConfig:
    """Dead-letter queue configuration."""

    queue: str
    retry: int = 3



@dataclass(frozen=True, kw_only=True)
class QueueConfig:
    fifo: bool = False
    delay: int = 0
    visibility_timeout: int = 30
    dlq: str | DlqConfig | DlqConfig | None = None

class DlqConfigDict(TypedDict, total=False):
    """Configuration for dead-letter queue settings."""

    queue: str
    retry: int = 3

class QueueConfigDict(TypedDict, total=False):
    fifo: bool
    delay: int
    visibility_timeout: int
    dlq: str | DlqConfigDict | DlqConfig | None


@final
@dataclass(frozen=True)
class QueueResources:
    queue: pulumi_aws.sqs.Queue
    # subscriptions: list[pulumi_aws.sqs.QueueEventSubscription] = None


@final
class Queue(Component[QueueResources], Linkable):
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
        self._resources = None


    def _parse_config(
        self, config: QueueConfig | QueueConfigDict | None, opts: QueueConfigDict
    ) -> QueueConfig:
        """Parse configuration from either typed or dict form."""
        if isinstance(config, dict) and opts:
            raise ValueError(
                "Invalid configuration: cannot combine complete handler "
                "configuration with additional options"
            )
        if config is None:
            config = QueueConfig(**opts)
        elif isinstance(config, dict):
            config = QueueConfig(**config)
        
        if isinstance(config.dlq, dict):
            config = QueueConfig(
                fifo=config.fifo,
                delay=config.delay,
                visibility_timeout=config.visibility_timeout,
                dlq=DlqConfig(**config.dlq),
            )
        if isinstance(config.dlq, str):
            config = QueueConfig(
                fifo=config.fifo,
                delay=config.delay,
                visibility_timeout=config.visibility_timeout,
                dlq=DlqConfig(queue=config.dlq),
            )
        return config

    def _create_resources(self) -> QueueResources:
        queue = pulumi_aws.sqs.Queue(
            resource_name=self.config.name,
            delay_seconds=self.config.delay,
            visibility_timeout_seconds=self.config.visibility_timeout,
            fifo_queue=self.config.fifo,
        )

        return QueueResources(
            queue=queue,
        )

    def subscribe(self, function: Function | str) -> None:
        if isinstance(function, str):
            i = len(self.subscriptions)
            function = Function(name=f"{self.name}-function-{i}", handler=function, links=[self])
            return pulumi_aws.sqs.QueueEventSubscription(
                resource_name=f"{self.name}-subscription-{function.name}",
                queue=self.resources.queue.id,
                function=function.arn,
                batch_size=function.batch_size,
                enabled=function.enabled,
            )
        return None

    def link(self) -> Link:
        link_creator_ = ComponentRegistry.get_link_config_creator(type(self))

        link_config = link_creator_(self.resources)
        return Link(self.name, link_config.properties, link_config.permissions)


@link_config_creator(Queue)
def default_queue_link(queue_resources: QueueResources) -> LinkConfig:
    queue = queue_resources.queue
    return LinkConfig(
        properties={
            "queue_url": queue.url,
            "queue_arn": queue.arn,
            "queue_name": queue.name,
        },
        permissions=[
            AwsPermission(
                actions=["sqs:*"],
                resources=[queue.arn],
            ),
        ],
    )
