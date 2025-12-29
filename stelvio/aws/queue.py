from dataclasses import dataclass
from typing import TypedDict, final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.dns import Dns, DnsProviderNotConfiguredError
from stelvio.link import Link, Linkable, LinkConfig


@final
@dataclass(frozen=True)
class QueueResources:
    queue: pulumi_aws.sqs.Queue


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
        
        self._resources = None

    def _create_resources(self) -> QueueResources:
        queue = pulumi_aws.sqs.Queue(
            resource_name=self.name,
            delay_seconds=self.delay_seconds,
            visibility_timeout_seconds=self.visibility_timeout_seconds,
            fifo_queue=self.fifo,
        )
        return QueueResources(
            queue=queue,
        )

    def link(self) -> Link:
        link_creator_ = ComponentRegistry.get_link_config_creator(type(self))

        link_config = link_creator_(self.resources.queue)
        return Link(self.name, link_config.properties, link_config.permissions)


@link_config_creator(Queue)
def default_queue_link(
        queue: pulumi_aws.sqs.Queue
) -> LinkConfig:
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
