import pytest

from stelvio.aws.api_gateway import Api
from stelvio.aws.cloudfront.router import Router
from stelvio.aws.s3 import Bucket

from .assert_helpers import assert_cloudfront_distribution, assert_s3_bucket
from .conftest import NO_WAIT_DEPLOY

pytestmark = pytest.mark.integration


# --- S3 origin ---


def test_router_s3_root(stelvio_env):
    def infra():
        bucket = Bucket("site")
        router = Router("web", customize=NO_WAIT_DEPLOY)
        router.route("/", bucket)

    outputs = stelvio_env.deploy(infra)

    assert_s3_bucket(outputs["s3bucket_site_name"])
    assert_cloudfront_distribution(
        outputs["router_web_distribution_id"],
        enabled=True,
        origins_count=1,
        default_certificate=True,
    )
    assert outputs["router_web_num_origins"] == 1


def test_router_s3_path(stelvio_env):
    def infra():
        bucket = Bucket("docs")
        router = Router("cdn", customize=NO_WAIT_DEPLOY)
        router.route("/docs", bucket)

    outputs = stelvio_env.deploy(infra)

    # No root path → default 404 function created (CloudFront Function, not an origin)
    assert_cloudfront_distribution(
        outputs["router_cdn_distribution_id"],
        enabled=True,
        origins_count=1,
    )
    assert outputs["router_cdn_num_origins"] == 1


# --- Multiple origins ---


def test_router_multiple_s3_origins(stelvio_env):
    def infra():
        site = Bucket("site")
        assets = Bucket("assets")
        router = Router("multi", customize=NO_WAIT_DEPLOY)
        router.route("/", site)
        router.route("/assets", assets)

    outputs = stelvio_env.deploy(infra)

    assert_cloudfront_distribution(
        outputs["router_multi_distribution_id"],
        origins_count=2,
    )
    assert outputs["router_multi_num_origins"] == 2


# --- API origin ---


def test_router_api_origin(stelvio_env, project_dir):
    def infra():
        api = Api("backend")
        api.route("GET", "/hello", "handlers/echo.main")
        router = Router("apirouter", customize=NO_WAIT_DEPLOY)
        router.route("/api", api)

    outputs = stelvio_env.deploy(infra)

    assert_cloudfront_distribution(
        outputs["router_apirouter_distribution_id"],
        enabled=True,
        origins_count=1,
    )
    assert outputs["router_apirouter_num_origins"] == 1


# --- Mixed origins ---


def test_router_mixed_s3_and_api(stelvio_env, project_dir):
    def infra():
        bucket = Bucket("frontend")
        api = Api("api")
        api.route("GET", "/hello", "handlers/echo.main")
        router = Router("app", customize=NO_WAIT_DEPLOY)
        router.route("/", bucket)
        router.route("/api", api)

    outputs = stelvio_env.deploy(infra)

    assert_cloudfront_distribution(
        outputs["router_app_distribution_id"],
        origins_count=2,
    )
    assert outputs["router_app_num_origins"] == 2


# --- Exports ---


def test_router_exports(stelvio_env):
    def infra():
        bucket = Bucket("content")
        router = Router("edge", customize=NO_WAIT_DEPLOY)
        router.route("/", bucket)

    outputs = stelvio_env.deploy(infra)

    assert outputs["router_edge_domain_name"].endswith(".cloudfront.net")
    assert outputs["router_edge_distribution_id"]
    assert outputs["router_edge_num_origins"] == 1
