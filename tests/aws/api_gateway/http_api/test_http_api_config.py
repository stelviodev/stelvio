from stelvio.aws.api_gateway.http_api import HttpApiConfig, HttpApiConfigDict
from tests.test_utils import assert_config_dict_matches_dataclass


def test_http_api_config_dict_matches_http_api_config():
    assert_config_dict_matches_dataclass(HttpApiConfig, HttpApiConfigDict)
