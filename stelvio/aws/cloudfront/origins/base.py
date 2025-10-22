import pulumi_aws

from stelvio.aws.cloudfront.dtos import CloudflareRouterRouteOriginConfig
from stelvio.component import Component


class ComponentCloudfrontBridge:
    def __init__(self, idx: int, route: any) -> None:
        self.idx = idx
        self.route = route

    @staticmethod
    def match(stlv_component: Component) -> bool:
        return isinstance(stlv_component, Component)

    def get_origin_config(self) -> CloudflareRouterRouteOriginConfig:
        raise NotImplementedError("This method should be implemented by subclasses.")

    def get_access_policy(self, distribution: pulumi_aws.cloudfront.Distribution) -> any:
        raise NotImplementedError("This method should be implemented by subclasses.")
