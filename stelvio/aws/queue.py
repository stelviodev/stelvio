from dataclasses import dataclass
from typing import final

import pulumi_aws

from stelvio.aws.function.function import Function
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig


@final
@dataclass(frozen=True)
class QueueResources:
    queue: pulumi_aws.sqs.Queue
    subscriptions: list[pulumi_aws.sqs.QueueEventSubscription] = None


@final
class Queue(Component[QueueResources], Linkable):
    def __init__(
        self,
        name: str,
        delay_seconds: int = 0,
        visibility_timeout_seconds: int = 30,
        fifo: bool = False,
    ):
        super().__init__(name)
        self.subscriptions: list[pulumi_aws.sqs.QueueEventSubscription] = []
        self.delay_seconds = delay_seconds
        self.visibility_timeout_seconds = visibility_timeout_seconds
        self.fifo = fifo
        self._resources = None

    def _create_resources(self) -> QueueResources:
        queue = pulumi_aws.sqs.Queue(
            resource_name=self.name,
            delay_seconds=self.delay_seconds,
            visibility_timeout_seconds=self.visibility_timeout_seconds,
            fifo_queue=self.fifo,
        )

        subscription_resources = []
        for function in self.subscriptions:
            subscription_resource = pulumi_aws.sqs.QueueEventSubscription(
                resource_name=f"{self.name}-subscription-{function.name}",
                queue=queue.id,
                function=function.arn,
                batch_size=function.batch_size,
                enabled=function.enabled,
            )
            subscription_resources.append(subscription_resource)

        return QueueResources(
            queue=queue,
            subscriptions=subscription_resources,
        )

    def subscribe(self, function: Function | str) -> None:
        if isinstance(function, str):
            i = len(self.subscriptions)
            function = Function(name=f"{self.name}-function-{i}", handler=function, links=[self])
        self.subscriptions.append(function)

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
