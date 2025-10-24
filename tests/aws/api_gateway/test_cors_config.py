import pytest

from stelvio.aws.api_gateway.config import ApiConfig, CorsConfig, CorsConfigDict

from ...test_utils import assert_config_dict_matches_dataclass


def test_cors_config_dict_has_same_fields_as_cors_config():
    assert_config_dict_matches_dataclass(CorsConfig, CorsConfigDict)


def test_cors_config_defaults():
    config = CorsConfig()
    assert config.allow_origins == "*"
    assert config.allow_methods == "*"
    assert config.allow_headers == "*"
    assert config.allow_credentials is False
    assert config.max_age is None
    assert config.expose_headers is None


@pytest.mark.parametrize(
    ("field", "value", "error_pattern"),
    [
        ("allow_origins", "", "allow_origins string cannot be empty"),
        ("allow_methods", "", "allow_methods string cannot be empty"),
        ("allow_headers", "", "allow_headers string cannot be empty"),
        ("allow_origins", [], "allow_origins list cannot be empty"),
        ("allow_methods", [], "allow_methods list cannot be empty"),
        ("allow_headers", [], "allow_headers list cannot be empty"),
        ("allow_origins", ["*"], "Wildcard '\\*' must be a string"),
        ("allow_methods", ["*"], "Wildcard '\\*' must be a string"),
        ("allow_headers", ["*"], "Wildcard '\\*' must be a string"),
        ("allow_origins", ["https://a.com", None], "Each allow_origins value must be"),
        ("allow_methods", ["GET", ""], "Each allow_methods value must be"),
        ("allow_headers", ["Content-Type", 123], "Each allow_headers value must be"),
    ],
)
def test_cors_config_validation_errors(field, value, error_pattern):
    with pytest.raises(ValueError, match=error_pattern):
        CorsConfig(**{field: value})


@pytest.mark.parametrize(
    ("field", "value", "error_pattern"),
    [
        ("allow_origins", 123, "allow_origins must be a string or list of strings"),
        ("allow_methods", None, "allow_methods must be a string or list of strings"),
        ("allow_headers", {}, "allow_headers must be a string or list of strings"),
    ],
)
def test_cors_config_type_errors(field, value, error_pattern):
    with pytest.raises(TypeError, match=error_pattern):
        CorsConfig(**{field: value})


def test_cors_config_credentials_with_wildcard_origin_rejected():
    with pytest.raises(ValueError, match="allow_credentials=True requires specific origins"):
        CorsConfig(allow_credentials=True)


def test_cors_config_invalid_http_method_rejected():
    with pytest.raises(ValueError, match="Invalid HTTP method"):
        CorsConfig(allow_methods="INVALID")


@pytest.mark.parametrize(
    "methods",
    ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "*", ["GET", "POST"]],
)
def test_cors_config_valid_http_methods_accepted(methods):
    config = CorsConfig(allow_methods=methods)
    assert config.allow_methods == methods


def test_cors_config_http_methods_case_insensitive():
    config = CorsConfig(allow_methods="get")
    assert config.allow_methods == "get"


@pytest.mark.parametrize(
    ("expose_headers", "error_pattern"),
    [
        (["X-Custom", ""], "Each expose_headers value must be"),
        (["X-Custom", None], "Each expose_headers value must be"),
    ],
)
def test_cors_config_expose_headers_with_invalid_items_rejected(expose_headers, error_pattern):
    with pytest.raises(ValueError, match=error_pattern):
        CorsConfig(expose_headers=expose_headers)


def test_cors_config_negative_max_age_rejected():
    with pytest.raises(ValueError, match="max_age must be non-negative"):
        CorsConfig(max_age=-1)


def test_cors_config_empty_expose_headers_rejected():
    with pytest.raises(ValueError, match="expose_headers list cannot be empty"):
        CorsConfig(expose_headers=[])


def test_cors_config_full_custom_configuration():
    config = CorsConfig(
        allow_origins="https://example.com",
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
        allow_credentials=True,
        max_age=3600,
        expose_headers=["X-Request-Id"],
    )
    assert config.allow_origins == "https://example.com"
    assert config.allow_methods == ["GET", "POST"]
    assert config.allow_headers == ["Content-Type", "Authorization"]
    assert config.allow_credentials is True
    assert config.max_age == 3600
    assert config.expose_headers == ["X-Request-Id"]


@pytest.mark.parametrize("cors_value", [False, None])
def test_api_config_cors_disabled_normalizes_to_none(cors_value):
    config = ApiConfig(cors=cors_value)
    assert config.normalized_cors is None


def test_api_config_cors_true_normalizes_to_permissive_defaults():
    config = ApiConfig(cors=True)
    cors = config.normalized_cors
    assert cors is not None
    assert cors.allow_origins == "*"
    assert cors.allow_methods == "*"
    assert cors.allow_headers == "*"


def test_api_config_cors_config_instance_returned_as_is():
    cors_config = CorsConfig(allow_origins="https://example.com")
    config = ApiConfig(cors=cors_config)
    assert config.normalized_cors is cors_config


def test_api_config_cors_dict_converted_to_cors_config():
    config = ApiConfig(cors={"allow_origins": "https://example.com", "allow_credentials": True})
    cors = config.normalized_cors
    assert cors is not None
    assert cors.allow_origins == "https://example.com"
    assert cors.allow_credentials is True
    assert cors.allow_methods == "*"
    assert cors.allow_headers == "*"
