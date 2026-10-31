import pulumi_aws

from stelvio.aws.api_gateway import RestApi
from stelvio.aws.api_gateway.domain import execute_api_host
from stelvio.aws.cloudfront.dtos import RouteOriginConfig
from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter
from stelvio.aws.cloudfront.origins.registry import register_adapter
from stelvio.provider import aws_region_of


@register_adapter(RestApi)
class ApiGatewayCloudfrontAdapter(ComponentCloudfrontAdapter):
    @property
    def api(self) -> RestApi:
        return self.route.component

    def get_origin_config(self) -> RouteOriginConfig:
        region = aws_region_of(self.api)
        origin_args = pulumi_aws.cloudfront.DistributionOriginArgs(
            origin_id=self.api.resources.rest_api.id,
            domain_name=self.api.resources.rest_api.id.apply(
                lambda api_id: execute_api_host(api_id, region)
            ),
            origin_path=self.api.resources.stage.stage_name.apply(lambda stage: f"/{stage}"),
        )
        origin_dict = self._api_origin_dict(origin_args)
        cf_function = self._uri_rewrite_function(depends_on=[self.api.resources.rest_api])
        cache_behaviors = self._api_cache_behaviors(
            origin_id=origin_dict["origin_id"],
            cf_function=cf_function,
        )

        return RouteOriginConfig(
            origin_access_controls=None,
            origins=origin_dict,
            ordered_cache_behaviors=cache_behaviors,
            cloudfront_functions=cf_function,
        )

    def get_access_policy(self, distribution: pulumi_aws.cloudfront.Distribution) -> None:  # noqa: ARG002
        return None
