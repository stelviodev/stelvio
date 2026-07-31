import pulumi
import pulumi_aws

from stelvio.aws.api_gateway import RestApi
from stelvio.aws.cloudfront.dtos import Route, RouteOriginConfig
from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter
from stelvio.aws.cloudfront.origins.decorators import register_adapter


@register_adapter(RestApi)
class ApiGatewayCloudfrontAdapter(ComponentCloudfrontAdapter):
    def __init__(
        self, idx: int, route: Route, resource_opts: pulumi.ResourceOptions | None = None
    ) -> None:
        super().__init__(idx, route, resource_opts)
        self.api = route.component

    def get_origin_config(self) -> RouteOriginConfig:
        region = pulumi_aws.get_region().region
        origin_args = pulumi_aws.cloudfront.DistributionOriginArgs(
            origin_id=self.api.resources.rest_api.id,
            domain_name=self.api.resources.rest_api.id.apply(
                lambda api_id: f"{api_id}.execute-api.{region}.amazonaws.com"
            ),
            origin_path=self.api.resources.stage.stage_name.apply(lambda stage: f"/{stage}"),
        )
        origin_dict = self._api_origin_dict(origin_args)
        cf_function = self._api_uri_rewrite_function(
            component_name=self.api.name,
            depends_on=[self.api.resources.rest_api],
        )
        cache_behavior = self._api_cache_behavior(
            origin_id=origin_dict["origin_id"],
            cf_function=cf_function,
        )

        return RouteOriginConfig(
            origin_access_controls=None,
            origins=origin_dict,
            ordered_cache_behaviors=cache_behavior,
            cloudfront_functions=cf_function,
        )

    def get_access_policy(self, distribution: pulumi_aws.cloudfront.Distribution) -> None:  # noqa: ARG002
        return None
