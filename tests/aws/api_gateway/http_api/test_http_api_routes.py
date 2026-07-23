"""Tests for HttpApi route validation."""

import pytest

from stelvio.aws.api_gateway.http_api import HttpApi
from stelvio.aws.api_gateway.rest_api.constants import ROUTE_MAX_LENGTH, ROUTE_MAX_PARAMS

pytestmark = pytest.mark.usefixtures("project_cwd")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("ANY", "/users"),
        ("*", "/users"),
        ("ANY", "$default"),
    ],
)
def test_http_api_route_accepts_valid_method_and_path(method, path):
    api = HttpApi("my-api")

    api.route(method, path, "functions/simple.handler")


@pytest.mark.parametrize(
    ("method", "path", "expected_error"),
    [
        ("GET", "users", "start with '/'"),
        ("GET", "/users/{}", "Empty path parameters"),
        ("GET", "/users/{id}{name}", "Adjacent"),
        ("GET", "/users/{id}/orders/{id}", "Duplicate"),
        ("GET", "/files/{proxy+}/other", "end of the path"),
        ("GET", "/files/{other+}", r"Only.*proxy"),
        ("GET", "/" + "x" * ROUTE_MAX_LENGTH, "Path too long"),
        (
            "GET",
            "/" + "/".join(f"{{param{index}}}" for index in range(ROUTE_MAX_PARAMS + 1)),
            "Maximum of 10 path parameters",
        ),
        ([], "/users", "Method list cannot be empty"),
        (["GET", "ANY"], "/users", "ANY"),
        (["GET", "*"], "/users", "ANY"),
        ("TRACE", "/users", "Invalid HTTP method"),
        ("GET", "$default", r"\$default"),
        (["GET", "POST"], "$default", r"\$default"),
    ],
)
def test_http_api_route_rejects_invalid_method_or_path(method, path, expected_error):
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match=expected_error):
        api.route(method, path, "functions/simple.handler")


@pytest.mark.parametrize("method", [[123], [[str]]])
def test_http_api_route_rejects_invalid_method_type(method):
    api = HttpApi("my-api")

    with pytest.raises(TypeError, match="Invalid method type in list"):
        api.route(method, "/users", "functions/simple.handler")


def test_http_api_route_accepts_maximum_path_length_and_parameters():
    path = "/" + "/".join(f"{{param{index}}}" for index in range(ROUTE_MAX_PARAMS))
    path += "x" * (ROUTE_MAX_LENGTH - len(path))

    HttpApi("my-api").route("GET", path, "functions/simple.handler")
