from dataclasses import dataclass
from typing import final

import pulumi_aws

from stelvio.aws.function.function import Function
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig


@dataclass(frozen=True)
class FifoConfig:
    content_based_deduplication: bool = False

@dataclass(frozen=True)
class DlqConfig:
    queue: str
    retry: int = 3

@dataclass(frozen=True)
class QueueConfig:
    fifo: bool | FifoConfig = False
    delay: int = 0
    visibility_timeout: int = 30
    dlq: DlqConfig | None = None

class QueueConfigDict(TypedDict):
    fifo: bool | FifoConfigDict
    delay: int
    visibility_timeout: int
    dlq: str | DlqConfigDict


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
        self._config = _parse_queue_config(config, opts)
        self._subscriber: QueueSubscriber | None = None

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

        link_config = link_creator_(self.resources.queue)
        return Link(self.name, link_config.properties, link_config.permissions)


@link_config_creator(Queue)
def default_queue_link(queue: pulumi_aws.sqs.Queue) -> LinkConfig:
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
