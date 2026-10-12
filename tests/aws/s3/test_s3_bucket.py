import json

import pulumi
import pytest

from stelvio.aws.permission import AwsPermission
from stelvio.aws.s3 import Bucket

from ...conftest import TP
from ..conftest import assert_hash_truncated, spy_old_names
from ..pulumi_mocks import R, tid, tn

BUCKET_ARN_TEMPLATE = "arn:aws:s3:::{name}"


@pulumi.runtime.test
def test_bucket_properties(pulumi_mocks):
    # Arrange
    bucket = Bucket("my-bucket")
    # Act
    _ = bucket.resources

    # Assert
    def check_resources(args):
        bucket_id, arn = args
        assert bucket_id == TP + "my-bucket-test-id"
        assert arn == BUCKET_ARN_TEMPLATE.format(name=tn(TP + "my-bucket"))

    pulumi.Output.all(bucket.resources.bucket.id, bucket.arn).apply(check_resources)


def test_s3_bucket_basic(pulumi_mocks):
    # Arrange
    bucket = Bucket("my-bucket")

    # Act
    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res("my-bucket", R.BUCKET, {})
    pulumi_mocks.assert_res(
        "my-bucket-pab",
        R.BUCKET_PUBLIC_ACCESS_BLOCK,
        {
            "bucket": tid(TP + "my-bucket"),
            "blockPublicAcls": True,
            "blockPublicPolicy": True,
            "ignorePublicAcls": True,
            "restrictPublicBuckets": True,
        },
    )
    pulumi_mocks.assert_res_counts({R.BUCKET: 1, R.BUCKET_PUBLIC_ACCESS_BLOCK: 1})
    assert bucket.resources.bucket_policy is None


@pulumi.runtime.test
def test_s3_bucket_link(pulumi_mocks):
    # Arrange
    bucket = Bucket("my-bucket")

    # Create the resource so we have the bucket output
    _ = bucket.resources

    # Act - Get the link from the bucket
    link = bucket.link()

    # Assert - Check link properties and permissions
    def check_link(args):
        properties, permissions = args

        expected_properties = {
            "bucket_name": tn(TP + "my-bucket"),
            "bucket_arn": BUCKET_ARN_TEMPLATE.format(name=tn(TP + "my-bucket")),
        }
        assert properties == expected_properties

        assert len(permissions) == 2
        assert all(isinstance(perm, AwsPermission) for perm in permissions)

        # Check ListBucket permission (first permission)
        list_bucket_perm = permissions[0]
        assert list_bucket_perm.actions == ["s3:ListBucket"]

        def check_list_bucket_resource(resource):
            assert resource == BUCKET_ARN_TEMPLATE.format(name=tn(TP + "my-bucket"))

        assert len(list_bucket_perm.resources) == 1
        list_bucket_perm.resources[0].apply(check_list_bucket_resource)

        # Check object operations permission (second permission)
        object_ops_perm = permissions[1]
        expected_object_actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        assert sorted(object_ops_perm.actions) == sorted(expected_object_actions)

        def check_object_resource(resource):
            expected_object_arn = BUCKET_ARN_TEMPLATE.format(name=tn(TP + "my-bucket")) + "/*"
            assert resource == expected_object_arn

        assert len(object_ops_perm.resources) == 1
        object_ops_perm.resources[0].apply(check_object_resource)

    # We use Output.all and .apply because Link properties and permissions contain
    # Pulumi Output objects (like bucket.arn)
    pulumi.Output.all(link.properties, link.permissions).apply(check_link)


@pulumi.runtime.test
def test_s3_bucket_link_permissions_structure(pulumi_mocks):
    # Arrange
    bucket = Bucket("test-bucket")
    _ = bucket.resources

    # Act
    link = bucket.link()

    # Assert - Verify the permissions have correct actions for the right resources
    def check_permissions_structure(bucket_arn):
        permissions = link.permissions
        assert len(permissions) == 2

        # First permission: ListBucket on bucket itself
        list_bucket_perm = permissions[0]
        assert list_bucket_perm.actions == ["s3:ListBucket"]

        def check_list_bucket_resource(resource):
            assert resource == bucket_arn

        list_bucket_perm.resources[0].apply(check_list_bucket_resource)

        # Second permission: Object operations on bucket contents
        object_ops_perm = permissions[1]
        expected_object_actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        assert sorted(object_ops_perm.actions) == sorted(expected_object_actions)

        def check_object_resource(resource):
            assert resource == f"{bucket_arn}/*"

        object_ops_perm.resources[0].apply(check_object_resource)

    # Use bucket.arn to trigger the check after resources are created
    bucket.arn.apply(check_permissions_structure)


def test_multiple_s3_buckets(pulumi_mocks):
    # Arrange
    bucket1 = Bucket("bucket-one")
    bucket2 = Bucket("bucket-two")

    # Act
    @pulumi.runtime.test
    def deploy():
        return [bucket1.resources, bucket2.resources]

    deploy()

    # Assert
    pulumi_mocks.assert_res("bucket-one", R.BUCKET)
    pulumi_mocks.assert_res("bucket-two", R.BUCKET)


@pytest.mark.parametrize("versioning_enabled", [True, False])
def test_s3_bucket_versioning(pulumi_mocks, versioning_enabled):
    @pulumi.runtime.test
    def deploy():
        return Bucket("test-bucket", versioning=versioning_enabled).resources

    deploy()

    # The deprecated inline property must not come back
    bucket = pulumi_mocks.assert_res("test-bucket", R.BUCKET)
    assert "versioning" not in bucket.inputs

    if versioning_enabled:
        pulumi_mocks.assert_res(
            "test-bucket-versioning",
            R.BUCKET_VERSIONING,
            {
                "bucket": tid(TP + "test-bucket"),
                "versioningConfiguration": {"status": "Enabled"},
            },
        )
    else:
        # Type-scoped, not name-scoped: catches a stray versioning resource under any name
        pulumi_mocks.assert_no_res(R.BUCKET_VERSIONING)


def test_s3_bucket_access_public_pab_created_with_false_flags_and_policy(pulumi_mocks):
    # Test that when access="public", PAB is created with all blocking flags set to False
    # and a bucket policy is created with proper values

    # Arrange
    bucket = Bucket("test-bucket", access="public")

    # Act
    @pulumi.runtime.test
    def deploy():
        return bucket.resources

    deploy()

    # Assert
    pulumi_mocks.assert_res(
        "test-bucket-pab",
        R.BUCKET_PUBLIC_ACCESS_BLOCK,
        {
            "bucket": tid(TP + "test-bucket"),
            "blockPublicAcls": False,
            "blockPublicPolicy": False,
            "ignorePublicAcls": False,
            "restrictPublicBuckets": False,
        },
    )
    policy = pulumi_mocks.assert_res(
        "test-bucket-policy", R.BUCKET_POLICY, {"bucket": tid(TP + "test-bucket")}, partial=True
    )
    assert bucket.resources.bucket_policy is not None
    assert json.loads(policy.inputs["policy"]) == [
        {
            "effect": "Allow",
            "principals": [{"type": "*", "identifiers": ["*"]}],
            "actions": ["s3:GetObject"],
            "resources": [f"arn:aws:s3:::{tn(TP + 'test-bucket')}/*"],
        }
    ]


def test_bucket_lets_pulumi_name_it(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return Bucket("uploads").resources

    deploy()

    [bucket] = pulumi_mocks.created(R.BUCKET, f"{TP}uploads")
    assert "bucket" not in bucket.inputs


def test_bucket_long_name_truncates_logical_name(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return Bucket("b" * 100).resources

    deploy()

    [bucket] = pulumi_mocks.created(R.BUCKET)
    assert_hash_truncated(bucket.name, 55)  # S3 63 minus the 8-char Pulumi suffix


def test_bucket_long_name_aliases_unguarded_logical_name(pulumi_mocks, monkeypatch):
    """A bucket deployed before the length guard keeps its URN, and with it its data."""
    old_names = spy_old_names(monkeypatch, Bucket)

    @pulumi.runtime.test
    def deploy():
        return Bucket("b" * 100).resources

    deploy()

    assert old_names == [f"{TP}{'b' * 100}"]
