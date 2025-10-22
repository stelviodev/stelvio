
from stelvio.aws.cloudfront.origins.api_gateway import ApiGatewayCloudfrontBridge
from stelvio.aws.cloudfront.origins.s3 import S3BucketCloudfrontBridge


class CFBridgeRegistry:
    def __init__(self):
        self.classes = [
            S3BucketCloudfrontBridge,
            ApiGatewayCloudfrontBridge,
        ]

    def get_bridge_for_component(self, component) -> any:
        for cls in self.classes:
            if cls.match(component):
                return cls
        raise ValueError(f"No bridge found for component: {component}")