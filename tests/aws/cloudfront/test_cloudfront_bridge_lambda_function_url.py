from unittest.mock import Mock, patch

import pytest

from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.origins.components.lambda_function import (
    LambdaFunctionCloudfrontBridge,
    _normalize_function_url_config,
)
from stelvio.aws.function import Function, FunctionUrlConfig


def test_normalize_function_url_config_none():
    """Test normalization when config is None."""
    config = _normalize_function_url_config(None)
    assert isinstance(config, FunctionUrlConfig)
    assert config.auth == "default"
    assert config.cors is None
    assert config.streaming is False


def test_normalize_function_url_config_dict():
    """Test normalization when config is a dictionary."""
    config_dict = {"auth": "iam", "cors": True, "streaming": True}
    config = _normalize_function_url_config(config_dict)
    assert isinstance(config, FunctionUrlConfig)
    assert config.auth == "iam"
    assert config.cors is True
    assert config.streaming is True


def test_normalize_function_url_config_object():
    """Test normalization when config is already a FunctionUrlConfig object."""
    original_config = FunctionUrlConfig(auth=None, cors=None, streaming=False)
    config = _normalize_function_url_config(original_config)
    assert config is original_config


def test_normalize_function_url_config_invalid():
    """Test normalization with invalid type."""
    with pytest.raises(TypeError):
        _normalize_function_url_config("invalid")


@patch("stelvio.aws.cloudfront.origins.components.lambda_function._create_function_url")
@patch("stelvio.aws.cloudfront.origins.components.lambda_function.pulumi_aws")
@patch("stelvio.aws.cloudfront.origins.components.lambda_function.pulumi")
@patch("stelvio.aws.cloudfront.origins.components.lambda_function.context")
def test_get_origin_config_default_auth(mock_context, mock_pulumi, mock_pulumi_aws, mock_create_url):
    """Test that default auth is converted to IAM."""
    mock_function = Mock(spec=Function)
    mock_function.name = "test-func"
    mock_function.resources.function = Mock()
    
    # Mock context().prefix()
    mock_context.return_value.prefix.side_effect = lambda x: f"prefix-{x}"

    # Mock function URL creation
    mock_url = Mock()
    mock_url.function_url = Mock()
    mock_url.function_url.apply.return_value = "test-domain"
    mock_create_url.return_value = mock_url

    # Route with no function_url_config (defaults to None -> auth="default")
    route = Route(path_pattern="/api", component_or_url=mock_function, function_url_config=None)
    bridge = LambdaFunctionCloudfrontBridge(idx=0, route=route)

    bridge.get_origin_config()

    # Verify _create_function_url was called with auth="iam"
    args, _ = mock_create_url.call_args
    # args[2] is url_config
    assert args[2].auth == "iam"


@patch("stelvio.aws.cloudfront.origins.components.lambda_function._create_function_url")
@patch("stelvio.aws.cloudfront.origins.components.lambda_function.pulumi_aws")
@patch("stelvio.aws.cloudfront.origins.components.lambda_function.pulumi")
@patch("stelvio.aws.cloudfront.origins.components.lambda_function.context")
def test_get_origin_config_explicit_iam_auth(mock_context, mock_pulumi, mock_pulumi_aws, mock_create_url):
    """Test that explicit IAM auth is preserved."""
    mock_function = Mock(spec=Function)
    mock_function.name = "test-func"
    mock_function.resources.function = Mock()
    
    mock_context.return_value.prefix.side_effect = lambda x: f"prefix-{x}"

    mock_url = Mock()
    mock_url.function_url = Mock()
    mock_url.function_url.apply.return_value = "test-domain"
    mock_create_url.return_value = mock_url

    # Route with explicit IAM auth
    route = Route(
        path_pattern="/api", 
        component_or_url=mock_function, 
        function_url_config={"auth": "iam"}
    )
    bridge = LambdaFunctionCloudfrontBridge(idx=0, route=route)

    bridge.get_origin_config()

    args, _ = mock_create_url.call_args
    assert args[2].auth == "iam"


@patch("stelvio.aws.cloudfront.origins.components.lambda_function._create_function_url")
@patch("stelvio.aws.cloudfront.origins.components.lambda_function.pulumi_aws")
@patch("stelvio.aws.cloudfront.origins.components.lambda_function.pulumi")
@patch("stelvio.aws.cloudfront.origins.components.lambda_function.context")
def test_get_origin_config_none_auth(mock_context, mock_pulumi, mock_pulumi_aws, mock_create_url):
    """Test that explicit None auth is preserved."""
    mock_function = Mock(spec=Function)
    mock_function.name = "test-func"
    mock_function.resources.function = Mock()
    
    mock_context.return_value.prefix.side_effect = lambda x: f"prefix-{x}"

    mock_url = Mock()
    mock_url.function_url = Mock()
    mock_url.function_url.apply.return_value = "test-domain"
    mock_create_url.return_value = mock_url

    # Route with explicit None auth (public)
    route = Route(
        path_pattern="/api", 
        component_or_url=mock_function, 
        function_url_config={"auth": None}
    )
    bridge = LambdaFunctionCloudfrontBridge(idx=0, route=route)

    bridge.get_origin_config()

    args, _ = mock_create_url.call_args
    assert args[2].auth is None


@patch("stelvio.aws.cloudfront.origins.components.lambda_function.pulumi_aws")
@patch("stelvio.aws.cloudfront.origins.components.lambda_function.context")
def test_get_access_policy_iam_auth(mock_context, mock_pulumi_aws):
    """Test that get_access_policy returns a Permission when auth is IAM."""
    mock_function = Mock(spec=Function)
    mock_function.name = "test-func"
    mock_function.resources.function.name = "test-func-name"
    
    mock_context.return_value.prefix.side_effect = lambda x: f"prefix-{x}"
    
    mock_distribution = Mock()
    mock_distribution.arn = "arn:aws:cloudfront::123456789012:distribution/ABCDEF"

    # Route with IAM auth (default)
    route = Route(path_pattern="/api", component_or_url=mock_function, function_url_config=None)
    bridge = LambdaFunctionCloudfrontBridge(idx=0, route=route)

    bridge.get_access_policy(mock_distribution)

    # Verify Permission was created
    mock_pulumi_aws.lambda_.Permission.assert_called_once()
    _, kwargs = mock_pulumi_aws.lambda_.Permission.call_args
    assert kwargs["action"] == "lambda:InvokeFunctionUrl"
    assert kwargs["principal"] == "cloudfront.amazonaws.com"


@patch("stelvio.aws.cloudfront.origins.components.lambda_function.pulumi_aws")
def test_get_access_policy_none_auth(mock_pulumi_aws):
    """Test that get_access_policy returns None when auth is not IAM."""
    mock_function = Mock(spec=Function)
    mock_function.name = "test-func"

    # Route with None auth
    route = Route(
        path_pattern="/api", 
        component_or_url=mock_function, 
        function_url_config={"auth": None}
    )
    bridge = LambdaFunctionCloudfrontBridge(idx=0, route=route)

    policy = bridge.get_access_policy(Mock())

    assert policy is None
    mock_pulumi_aws.lambda_.Permission.assert_not_called()
