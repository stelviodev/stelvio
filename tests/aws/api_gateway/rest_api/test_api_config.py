import pytest

from stelvio.aws.api_gateway.rest_api.config import RestApiConfig, RestApiConfigDict

from ....test_utils import assert_config_dict_matches_dataclass


def test_api_config_dict_has_same_fields_as_api_config():
    """Tests that the RestApiConfigDict matches the RestApiConfig dataclass."""
    assert_config_dict_matches_dataclass(RestApiConfig, RestApiConfigDict)


@pytest.mark.parametrize(
    ("config_kwargs", "expected_error"),
    [
        ({"domain_name": ""}, "Domain name cannot be empty"),
        ({"domain_name": "   "}, "Domain name cannot be empty"),
        ({"stage_name": ""}, "Stage name cannot be empty"),
        (
            {"stage_name": "invalid_chars!"},
            "Stage name can only contain alphanumeric characters, hyphens, and underscores",
        ),
        (
            {"stage_name": "with spaces"},
            "Stage name can only contain alphanumeric characters, hyphens, and underscores",
        ),
        (
            {"endpoint_type": "invalid"},
            "Invalid endpoint type: invalid. Only 'regional' and 'edge' are supported.",
        ),
        (
            {"endpoint_type": "private"},
            "Invalid endpoint type: private. Only 'regional' and 'edge' are supported.",
        ),
    ],
)
def test_api_config_validation_errors(config_kwargs, expected_error):
    with pytest.raises(ValueError, match=expected_error):
        RestApiConfig(**config_kwargs)


@pytest.mark.parametrize(
    "domain_name",
    [123, [], {}, True],
)
def test_api_config_domain_name_type_error(domain_name):
    with pytest.raises(TypeError, match="Domain name must be a string"):
        RestApiConfig(domain_name=domain_name)


@pytest.mark.parametrize(
    "stage_name",
    [
        "v1",
        "prod",
        "staging",
        "test-env",
        "api_v2",
        "stage-123",
        "a",  # single character
        "very-long-stage-name-with-many-chars",
    ],
)
def test_api_config_valid_stage_names(stage_name):
    config = RestApiConfig(stage_name=stage_name)
    assert config.stage_name == stage_name


@pytest.mark.parametrize(
    "endpoint_type",
    ["regional", "edge"],
)
def test_api_config_valid_endpoint_types(endpoint_type):
    config = RestApiConfig(endpoint_type=endpoint_type)
    assert config.endpoint_type == endpoint_type


def test_api_config_all_none():
    config = RestApiConfig()
    assert config.domain_name is None
    assert config.stage_name is None
    assert config.endpoint_type is None


def test_api_config_valid_full_config():
    config = RestApiConfig(domain_name="api.example.com", stage_name="prod", endpoint_type="edge")
    assert config.domain_name == "api.example.com"
    assert config.stage_name == "prod"
    assert config.endpoint_type == "edge"
