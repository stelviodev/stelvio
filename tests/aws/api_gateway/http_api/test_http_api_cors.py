import pulumi
import pytest

from stelvio.aws.api_gateway.http_api import HttpApi
from stelvio.aws.cors import CorsConfig

from .conftest import TP

pytestmark = pytest.mark.usefixtures("project_cwd")


@pulumi.runtime.test
def test_http_api_cors_configures_all_api_level_fields(pulumi_mocks):
    api = HttpApi(
        "my-api",
        cors=CorsConfig(
            allow_origins=["https://app.example.com", "https://admin.example.com"],
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "Authorization"],
            allow_credentials=True,
            max_age=3600,
            expose_headers=["X-Request-Id"],
        ),
    )
    api.route("GET", "/users", "functions/simple.handler")

    def check(_):
        apis = pulumi_mocks.created_http_apis()

        assert len(apis) == 1
        assert apis[0].typ == "aws:apigatewayv2/api:Api"
        assert apis[0].inputs["corsConfiguration"] == {
            "allowOrigins": ["https://app.example.com", "https://admin.example.com"],
            "allowMethods": ["GET", "POST"],
            "allowHeaders": ["Content-Type", "Authorization"],
            "allowCredentials": True,
            "maxAge": 3600,
            "exposeHeaders": ["X-Request-Id"],
        }

    api.resources.api.id.apply(check)


@pulumi.runtime.test
def test_http_api_cors_dict_is_normalized_to_lists(pulumi_mocks):
    api = HttpApi(
        "my-api",
        cors={
            "allow_origins": "https://app.example.com",
            "allow_methods": "GET",
            "allow_headers": "Authorization",
        },
    )
    api.route("GET", "/users", "functions/simple.handler")

    def check(_):
        apis = pulumi_mocks.created_http_apis()

        assert len(apis) == 1
        assert apis[0].inputs["corsConfiguration"] == {
            "allowOrigins": ["https://app.example.com"],
            "allowMethods": ["GET"],
            "allowHeaders": ["Authorization"],
        }

    api.resources.api.id.apply(check)


@pulumi.runtime.test
def test_multiple_apis_with_cors_create_uniquely_named_resources(pulumi_mocks):
    """Two CORS-enabled HTTP APIs coexist with distinct API resources and unique names."""
    api1 = HttpApi("api-one", cors=True)
    api1.route("GET", "/users", "functions/simple.handler")
    api2 = HttpApi("api-two", cors=True)
    api2.route("GET", "/users", "functions/simple.handler")

    def check(_):
        # Two distinct API resources, each with CORS configuration
        apis = pulumi_mocks.created_http_apis()
        assert len(apis) == 2
        assert {a.name for a in apis} == {TP + "api-one", TP + "api-two"}
        for api_res in apis:
            assert api_res.inputs["corsConfiguration"] == {
                "allowOrigins": ["*"],
                "allowMethods": ["*"],
                "allowHeaders": ["*"],
            }

        # No resource-name collisions across the two APIs
        all_names = [r.name for r in pulumi_mocks.created_resources]
        assert len(all_names) == len(set(all_names)), "Resource names collide across APIs"

    # Wait on routes/permissions for both so deferred resources register before assertions
    wait_outputs = [api1.resources.stage.id, api2.resources.stage.id]
    wait_outputs.extend(r.id for r in api1.resources.routes)
    wait_outputs.extend(r.id for r in api2.resources.routes)
    wait_outputs.extend(p.id for p in api1.resources.permissions)
    wait_outputs.extend(p.id for p in api2.resources.permissions)
    pulumi.Output.all(*wait_outputs).apply(check)
