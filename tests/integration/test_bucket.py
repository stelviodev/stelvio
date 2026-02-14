import pytest

from stelvio.aws.s3 import Bucket

from .assert_helpers import assert_s3_bucket


@pytest.mark.integration
def test_bucket_basic(stelvio_env):
    def infra():
        Bucket("files")

    outputs = stelvio_env.deploy(infra)

    assert_s3_bucket(outputs["s3bucket_files_name"], public_access_blocked=True)
