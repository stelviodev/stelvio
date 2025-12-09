from abc import ABC, abstractmethod

import pulumi
import pulumi_aws

from stelvio.aws.cloudfront.dtos import Route, RouteOriginConfig
from stelvio.component import Component


class ComponentCloudfrontAdapter(ABC):
    component_class: type[Component] | None = None

    def __init__(self, idx: int, route: Route) -> None:
        self.idx = idx
        self.route = route

    @classmethod
    def match(cls, stlv_component: Component) -> bool:
        return isinstance(stlv_component, cls.component_class)

    @abstractmethod
    def get_origin_config(self) -> RouteOriginConfig:
        pass

    @abstractmethod
    def get_access_policy(
        self, distribution: pulumi_aws.cloudfront.Distribution
    ) -> pulumi.Resource | None:
        pass
