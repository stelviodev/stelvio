
from stelvio.aws.cloudfront.origins.s3 import S3BucketCloudfrontBridge


class CFBridgeRegistry:
    def __init__(self):
        self.classes = [
            S3BucketCloudfrontBridge,   
        ]

    def get_bridge_for_component(self, component) -> S3BucketCloudfrontBridge:
        for cls in self.classes:
            if cls.match(component):
                return cls
        raise ValueError(f"No bridge found for component: {component}")