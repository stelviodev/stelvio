import pulumi_aws

from stelvio.aws.cloudfront.dtos import CloudfrontRouterRouteOriginConfig
from stelvio.component import Component


class ComponentCloudfrontBridge:
    def __init__(self, idx: int, route: any) -> None:
        self.idx = idx
        self.route = route

    @classmethod
    def match(cls, stlv_component: Component) -> bool:
        return isinstance(stlv_component, cls.component_class)

    def get_origin_config(self) -> CloudfrontRouterRouteOriginConfig:
        raise NotImplementedError("This method should be implemented by subclasses.")

    def get_access_policy(self, distribution: pulumi_aws.cloudfront.Distribution) -> any:
        raise NotImplementedError("This method should be implemented by subclasses.")
