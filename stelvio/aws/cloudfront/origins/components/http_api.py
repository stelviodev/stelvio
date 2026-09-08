import pulumi
import pulumi_aws

from stelvio.aws.api_gateway.http_api import HttpApi
from stelvio.aws.cloudfront.dtos import Route, RouteOriginConfig
from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter
from stelvio.aws.cloudfront.origins.decorators import register_adapter
from stelvio.provider import aws_region_of


@register_adapter(HttpApi)
class HttpApiCloudfrontAdapter(ComponentCloudfrontAdapter):
    def __init__(
        self, idx: int, route: Route, resource_opts: pulumi.ResourceOptions | None = None
    ) -> None:
        super().__init__(idx, route, resource_opts)
        self.api = route.component

    def get_origin_config(self) -> RouteOriginConfig:
        region = aws_region_of(self.api)
        stage_name = self.api.config.stage_name
        custom_domain_name = self.api.domain_name
        origin_path = self._origin_path(custom_domain_name, stage_name)
        origin_args = pulumi_aws.cloudfront.DistributionOriginArgs(
            origin_id=self.api.resources.api.id,
            domain_name=(
                custom_domain_name
                if custom_domain_name is not None
                else self.api.resources.api.id.apply(
                    lambda api_id: f"{api_id}.execute-api.{region}.amazonaws.com"
                )
            ),
            origin_path=origin_path,
        )
        origin_dict = self._api_origin_dict(origin_args)
        cf_function = self._api_uri_rewrite_function(
            component_name=self.api.name,
            depends_on=[self.api.resources.api, self.api.resources.stage],
        )
        cache_behavior = self._api_cache_behavior(
            origin_id=origin_dict["origin_id"],
            cf_function=cf_function,
            forwarded_headers=["*"],
        )

        return RouteOriginConfig(
            origin_access_controls=None,
            origins=origin_dict,
            ordered_cache_behaviors=cache_behavior,
            cloudfront_functions=cf_function,
        )

    def _origin_path(self, custom_domain_name: str | None, stage_name: str) -> str | None:
        if custom_domain_name is not None:
            if self.api.config.api_mapping_key is not None:
                return f"/{self.api.config.api_mapping_key}"
            return None
        if stage_name == "$default":
            return None
        return f"/{stage_name}"

    def get_access_policy(self, distribution: pulumi_aws.cloudfront.Distribution) -> None:  # noqa: ARG002
        return None
