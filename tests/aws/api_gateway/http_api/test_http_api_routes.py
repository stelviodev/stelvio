"""Tests for HttpApi route validation."""

import pytest

from stelvio.aws.api_gateway.http_api import HttpApi

pytestmark = pytest.mark.usefixtures("project_cwd")

# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def test_path_must_start_with_slash():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="start with '/'"):
        api.route("GET", "users", "functions/simple.handler")


def test_path_dollar_default_is_valid():
    api = HttpApi("my-api")

    api.route("ANY", "$default", "functions/simple.handler")


def test_path_empty_param_raises():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="Empty path parameters"):
        api.route("GET", "/users/{}", "functions/simple.handler")


def test_path_adjacent_params_raises():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="Adjacent"):
        api.route("GET", "/users/{id}{name}", "functions/simple.handler")


def test_path_duplicate_params_raises():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="Duplicate"):
        api.route("GET", "/users/{id}/orders/{id}", "functions/simple.handler")


def test_path_greedy_only_at_end():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="end of the path"):
        api.route("GET", "/files/{proxy+}/other", "functions/simple.handler")


def test_path_greedy_non_proxy_raises():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match=r"Only.*proxy"):
        api.route("GET", "/files/{other+}", "functions/simple.handler")


# ---------------------------------------------------------------------------
# Method validation
# ---------------------------------------------------------------------------


def test_method_any_is_valid():
    api = HttpApi("my-api")

    api.route("ANY", "/users", "functions/simple.handler")


def test_method_star_is_valid():
    api = HttpApi("my-api")

    api.route("*", "/users", "functions/simple.handler")


def test_method_any_in_list_raises():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="ANY"):
        api.route(["GET", "ANY"], "/users", "functions/simple.handler")


def test_method_star_in_list_raises():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="ANY"):
        api.route(["GET", "*"], "/users", "functions/simple.handler")


def test_invalid_method_raises():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match="Invalid HTTP method"):
        api.route("TRACE", "/users", "functions/simple.handler")


def test_dollar_default_path_requires_any_method():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match=r"\$default"):
        api.route("GET", "$default", "functions/simple.handler")


def test_dollar_default_path_rejects_method_list():
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match=r"\$default"):
        api.route(["GET", "POST"], "$default", "functions/simple.handler")


def test_dollar_default_with_any_is_valid():
    api = HttpApi("my-api")

    api.route("ANY", "$default", "functions/simple.handler")
