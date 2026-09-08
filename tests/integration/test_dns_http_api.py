from pytest import mark

from stelvio.aws.api_gateway import ApiDomain, HttpApi
from stelvio.aws.dns import Route53Dns

from .assert_helpers import (
    assert_apigatewayv2_execute_endpoint,
    assert_apigatewayv2_mapping,
    assert_http_api_routes,
)
from .export_helpers import export_http_api, export_http_api_domain

pytestmark = mark.integration_dns


def test_http_api_custom_domain_mapping_and_disabled_execute_endpoint(
    stelvio_env, project_dir, dns_domain, dns_zone_id
):
    dns = Route53Dns(zone_id=dns_zone_id)
    subdomain = f"http-api-{stelvio_env.run_id}.{dns_domain}"

    def infra():
        domain = ApiDomain("customhttp-domain", domain_name=subdomain)
        api = HttpApi(
            "customhttp",
            domain=domain,
            api_mapping_key="v1",
            disable_execute_api_endpoint=True,
        )
        api.route("GET", "/hello", "handlers/echo.main")
        export_http_api_domain(domain)
        export_http_api(api)

    outputs = stelvio_env.deploy(infra, dns=dns)

    assert outputs["http_api_customhttp_url"] == f"https://{subdomain}/v1"
    assert_http_api_routes(outputs["http_api_customhttp_id"], expected_route_keys={"GET /hello"})
    assert_apigatewayv2_execute_endpoint(outputs["http_api_customhttp_id"], disabled=True)
    assert_apigatewayv2_mapping(
        subdomain,
        expected_api_id=outputs["http_api_customhttp_id"],
        expected_mapping_key="v1",
    )
    assert outputs["http_api_domain_customhttp-domain_domain_name"] == subdomain
    assert outputs["http_api_domain_customhttp-domain_target_domain_name"].endswith(
        f".execute-api.{stelvio_env.aws_region}.amazonaws.com"
    )
