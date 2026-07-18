import pytest

from stelvio.aws.api_gateway.http_api import HttpApiConfig, HttpApiConfigDict

from ....test_utils import assert_config_dict_matches_dataclass


def test_http_api_config_dict_matches_http_api_config():
    assert_config_dict_matches_dataclass(HttpApiConfig, HttpApiConfigDict)


@pytest.mark.parametrize(
    ("config_kwargs", "expected_error"),
    [
        ({"domain_name": ""}, "Domain name cannot be empty"),
        ({"domain_name": "   "}, "Domain name cannot be empty"),
        ({"stage_name": "$invalid"}, "Stage name starting with '\\$'"),
        ({"stage_name": "with spaces"}, "Stage name must contain only"),
        ({"stage_name": "x" * 129}, "Stage name must be at most 128 characters"),
        ({"domain_name": "api.example.com", "domain": object()}, "Cannot specify both"),
        ({"api_mapping_key": "v1"}, "api_mapping_key requires"),
        ({"disable_execute_api_endpoint": True}, "disable_execute_api_endpoint=True requires"),
    ],
)
def test_http_api_config_validation_errors(config_kwargs, expected_error):
    with pytest.raises(ValueError, match=expected_error):
        HttpApiConfig(**config_kwargs)


@pytest.mark.parametrize("domain_name", [123, [], {}, True])
def test_http_api_config_domain_name_type_error(domain_name):
    with pytest.raises(TypeError, match="Domain name must be a string"):
        HttpApiConfig(domain_name=domain_name)  # type: ignore[arg-type]


@pytest.mark.parametrize("stage_name", ["$default", "v1", "prod", "test-env", "api_v2"])
def test_http_api_config_valid_stage_names(stage_name):
    config = HttpApiConfig(stage_name=stage_name)

    assert config.stage_name == stage_name


def test_http_api_config_defaults():
    config = HttpApiConfig()

    assert config.domain_name is None
    assert config.domain is None
    assert config.stage_name == "$default"
    assert config.cors is None
    assert config.disable_execute_api_endpoint is False
    assert config.api_mapping_key is None
    assert config.access_log_retention_days == 30
