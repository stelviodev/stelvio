import pytest

from stelvio.aws.s3 import S3StaticWebsite

from .assert_helpers import assert_cloudfront_tags, assert_s3_bucket_tags
from .conftest import NO_WAIT_DEPLOY
from .export_helpers import export_s3_static_website

pytestmark = pytest.mark.integration_cf


def test_s3_static_website_tags(stelvio_env, tmp_path):
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("<h1>hello</h1>")

    def infra():
        site = S3StaticWebsite(
            "site",
            directory=site_dir,
            tags={"Team": "platform"},
            customize={"cloudfront_distribution": NO_WAIT_DEPLOY},
        )
        export_s3_static_website(site)

    outputs = stelvio_env.deploy(infra)
    assert_s3_bucket_tags(outputs["s3_static_website_site_bucket_name"], {"Team": "platform"})
    assert_cloudfront_tags(outputs["cloudfront_site-cloudfront_arn"], {"Team": "platform"})
