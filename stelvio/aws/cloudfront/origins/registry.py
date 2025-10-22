from stelvio.aws.cloudfront.origins.api_gateway import ApiGatewayCloudfrontBridge
from stelvio.aws.cloudfront.origins.s3 import S3BucketCloudfrontBridge
from stelvio.component import Component


class CFBridgeRegistry:
    def __init__(self) -> None:
        self.classes = [
            S3BucketCloudfrontBridge,
            ApiGatewayCloudfrontBridge,
        ]

    def get_bridge_for_component(self, component: Component) -> any:
        for cls in self.classes:
            if cls.match(component):
                return cls
        raise ValueError(f"No bridge found for component: {component}")
