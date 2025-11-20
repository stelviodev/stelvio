import re

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.cors import CorsConfig
from stelvio.aws.function import Function

from ..pulumi_mocks import PulumiTestMocks

TP = "test-test-"

PERMISSIVE_CORS_ORIGINS = ["*"]
PERMISSIVE_CORS_METHODS = ["*"]
PERMISSIVE_CORS_HEADERS = ["*"]


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


@pytest.fixture
def project_cwd(monkeypatch, pytestconfig, tmp_path):
    from stelvio.project import get_project_root

    get_project_root.cache_clear()
    rootpath = pytestconfig.rootpath
    source_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    temp_project_dir = tmp_path / "sample_project_copy"

    import shutil

    shutil.copytree(source_project_dir, temp_project_dir, dirs_exist_ok=True)
    monkeypatch.chdir(temp_project_dir)

    return temp_project_dir


def _get_function_url(pulumi_mocks, expected_name):
    function_urls = pulumi_mocks.created_function_urls()
    assert len(function_urls) == 1
    url_args = function_urls[0]
    assert url_args.name == expected_name
    return url_args


def _assert_auth(url_args, expected_auth):
    assert url_args.inputs["authorizationType"] == expected_auth


def _assert_invoke_mode(url_args, expected_mode):
    assert url_args.inputs["invokeMode"] == expected_mode


def _assert_cors(  # noqa: PLR0913
    url_args,
    origins,
    methods,
    headers,
    credentials=False,
    max_age=None,
    expose_headers=None,
):
    cors = url_args.inputs["cors"]
    assert cors is not None

    expected = {
        "allowOrigins": origins,
        "allowMethods": methods,
        "allowHeaders": headers,
        "allowCredentials": credentials,
    }
    if max_age is not None:
        expected["maxAge"] = max_age
    if expose_headers is not None:
        expected["exposeHeaders"] = expose_headers

    assert cors == expected


@pulumi.runtime.test
def test_function_url_shortcut_public(pulumi_mocks, project_cwd):
    function = Function("public-fn", handler="functions/simple.handler", url="public")

    def check_resources(_):
        url_args = _get_function_url(pulumi_mocks, f"{TP}public-fn-url")
        _assert_auth(url_args, "NONE")
        _assert_invoke_mode(url_args, "BUFFERED")
        _assert_cors(
            url_args, PERMISSIVE_CORS_ORIGINS, PERMISSIVE_CORS_METHODS, PERMISSIVE_CORS_HEADERS
        )
        assert function.resources.function_url is not None

    function.url.apply(check_resources)


@pulumi.runtime.test
def test_function_url_shortcut_private(pulumi_mocks, project_cwd):
    function = Function("private-fn", handler="functions/simple.handler", url="private")

    def check_resources(_):
        url_args = _get_function_url(pulumi_mocks, f"{TP}private-fn-url")
        _assert_auth(url_args, "AWS_IAM")
        _assert_invoke_mode(url_args, "BUFFERED")
        assert url_args.inputs.get("cors") is None
        assert function.resources.function_url is not None

    function.url.apply(check_resources)


@pulumi.runtime.test
def test_function_url_dict_full_config(pulumi_mocks, project_cwd):
    function = Function(
        "full-config-fn",
        handler="functions/simple.handler",
        url={
            "auth": "iam",
            "cors": CorsConfig(
                allow_origins="https://example.com",
                allow_methods=["GET", "POST"],
                allow_headers=["Content-Type", "Authorization"],
                allow_credentials=True,
                max_age=3600,
                expose_headers=["X-Custom-Header"],
            ),
            "streaming": True,
        },
    )

    def check_resources(_):
        url_args = _get_function_url(pulumi_mocks, f"{TP}full-config-fn-url")
        _assert_auth(url_args, "AWS_IAM")
        _assert_invoke_mode(url_args, "RESPONSE_STREAM")
        _assert_cors(
            url_args,
            ["https://example.com"],
            ["GET", "POST"],
            ["Content-Type", "Authorization"],
            credentials=True,
            max_age=3600,
            expose_headers=["X-Custom-Header"],
        )

    function.url.apply(check_resources)


@pytest.mark.parametrize(
    ("streaming", "expected_mode", "fn_suffix"),
    [
        (False, "BUFFERED", "buffered"),
        (True, "RESPONSE_STREAM", "streaming"),
    ],
    ids=["streaming_false_buffered", "streaming_true_response_stream"],
)
@pulumi.runtime.test
def test_function_url_streaming_modes(
    pulumi_mocks, project_cwd, streaming, expected_mode, fn_suffix
):
    fn_name = f"{fn_suffix}-fn"
    function = Function(
        fn_name,
        handler="functions/simple.handler",
        url={"auth": None, "streaming": streaming},
    )

    def check_resources(_):
        url_args = _get_function_url(pulumi_mocks, f"{TP}{fn_name}-url")
        _assert_invoke_mode(url_args, expected_mode)

    function.url.apply(check_resources)


@pytest.mark.parametrize(
    (
        "test_id",
        "cors_config",
        "expected_origins",
        "expected_methods",
        "expected_headers",
        "expected_max_age",
    ),
    [
        (
            "string-to-list",
            CorsConfig(
                allow_origins="https://single-origin.com",
                allow_methods="GET",
                allow_headers="Content-Type",
            ),
            ["https://single-origin.com"],
            ["GET"],
            ["Content-Type"],
            None,
        ),
        (
            "list-unchanged",
            CorsConfig(
                allow_origins=["https://origin1.com", "https://origin2.com"],
                allow_methods=["GET", "POST", "PUT"],
                allow_headers=["Content-Type", "Authorization"],
            ),
            ["https://origin1.com", "https://origin2.com"],
            ["GET", "POST", "PUT"],
            ["Content-Type", "Authorization"],
            None,
        ),
        (
            "dict-config",
            {
                "allow_origins": "https://dict-origin.com",
                "allow_methods": ["GET", "POST"],
                "allow_headers": "Authorization",
                "max_age": 7200,
            },
            ["https://dict-origin.com"],
            ["GET", "POST"],
            ["Authorization"],
            7200,
        ),
    ],
    ids=["string_to_list_conversion", "list_unchanged", "dict_config"],
)
@pulumi.runtime.test
def test_function_url_cors_normalization(  # noqa: PLR0913
    pulumi_mocks,
    project_cwd,
    test_id,
    cors_config,
    expected_origins,
    expected_methods,
    expected_headers,
    expected_max_age,
):
    fn_name = f"cors-{test_id}-fn"
    function = Function(
        fn_name,
        handler="functions/simple.handler",
        url={"auth": None, "cors": cors_config},
    )

    def check_resources(_):
        url_args = _get_function_url(pulumi_mocks, f"{TP}{fn_name}-url")
        _assert_cors(
            url_args,
            expected_origins,
            expected_methods,
            expected_headers,
            max_age=expected_max_age,
        )

    function.url.apply(check_resources)


@pulumi.runtime.test
def test_function_url_property_returns_output(pulumi_mocks, project_cwd):
    function = Function("url-property-fn", handler="functions/simple.handler", url="public")

    def check_url_property(url_value):
        pattern = r"^https://[a-z0-9-]+\.lambda-url\.[a-z0-9-]+\.on\.aws/$"
        assert re.match(pattern, url_value), (
            f"URL doesn't match expected Lambda URL format. Got: {url_value}"
        )

    function.url.apply(check_url_property)


@pulumi.runtime.test
def test_function_url_property_none_when_not_configured(pulumi_mocks, project_cwd):
    function = Function("no-url-fn", handler="functions/simple.handler")

    def check_resources(_):
        function_urls = pulumi_mocks.created_function_urls()
        assert len(function_urls) == 0
        assert function.resources.function_url is None

    def check_url_property(url_value):
        assert url_value is None

    function.url.apply(check_resources)
    function.url.apply(check_url_property)


@pulumi.runtime.test
def test_function_url_cors_true_uses_permissive_defaults(pulumi_mocks, project_cwd):
    function = Function(
        "cors-true-fn",
        handler="functions/simple.handler",
        url={"auth": None, "cors": True},
    )

    def check_resources(_):
        url_args = _get_function_url(pulumi_mocks, f"{TP}cors-true-fn-url")
        _assert_cors(
            url_args,
            PERMISSIVE_CORS_ORIGINS,
            PERMISSIVE_CORS_METHODS,
            PERMISSIVE_CORS_HEADERS,
        )

    function.url.apply(check_resources)


@pulumi.runtime.test
def test_function_url_auth_none_has_no_cors_by_default(pulumi_mocks, project_cwd):
    function = Function(
        "auth-none-fn",
        handler="functions/simple.handler",
        url={"auth": None},
    )

    def check_resources(_):
        url_args = _get_function_url(pulumi_mocks, f"{TP}auth-none-fn-url")
        _assert_auth(url_args, "NONE")
        assert url_args.inputs.get("cors") is None

    function.url.apply(check_resources)


@pulumi.runtime.test
def test_function_url_auth_default_becomes_none_for_standalone(pulumi_mocks, project_cwd):
    function = Function(
        "auth-default-fn",
        handler="functions/simple.handler",
        url={"auth": "default"},
    )

    def check_resources(_):
        url_args = _get_function_url(pulumi_mocks, f"{TP}auth-default-fn-url")
        _assert_auth(url_args, "NONE")

    function.url.apply(check_resources)


@pulumi.runtime.test
def test_function_url_function_name_reference(pulumi_mocks, project_cwd):
    function = Function("ref-test-fn", handler="functions/simple.handler", url="public")

    def check_resources(_):
        url_args = _get_function_url(pulumi_mocks, f"{TP}ref-test-fn-url")
        # Verify the Function URL references the correct Lambda function
        expected_function_name = f"{TP}ref-test-fn-test-name"
        assert url_args.inputs["functionName"] == expected_function_name

    function.url.apply(check_resources)


@pulumi.runtime.test
def test_function_resources_signature_with_function_url(pulumi_mocks, project_cwd):
    from pulumi_aws.lambda_ import FunctionUrl

    function_with_url = Function("with-url-fn", handler="functions/simple.handler", url="public")
    function_without_url = Function("without-url-fn", handler="functions/simple.handler")

    def check_resources(_):
        assert function_with_url.resources.function_url is not None
        assert isinstance(function_with_url.resources.function_url, FunctionUrl)
        assert function_without_url.resources.function_url is None

    function_with_url.invoke_arn.apply(check_resources)
