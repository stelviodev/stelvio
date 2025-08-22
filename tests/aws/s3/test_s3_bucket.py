import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.permission import AwsPermission
from stelvio.aws.s3 import Bucket

from ..pulumi_mocks import PulumiTestMocks, tn

BUCKET_ARN_TEMPLATE = "arn:aws:s3:::{name}"

# Test prefix
TP = "test-test-"


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


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


@pulumi.runtime.test
def test_s3_bucket_basic(pulumi_mocks):
    # Arrange
    bucket = Bucket("my-bucket")

    # Act
    _ = bucket.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "my-bucket")
        assert len(buckets) == 1
        created_bucket = buckets[0]
        # S3 buckets have minimal required configuration by default
        assert created_bucket.name == TP + "my-bucket"

    bucket.resources.bucket.id.apply(check_resources)


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

    # Assert - Verify the permissions are structured correctly for IAM policies
    def check_permissions_structure(_):
        permissions = link.permissions
        assert len(permissions) == 2

        # Check that we have the right permission types
        list_bucket_perm = next((p for p in permissions if "s3:ListBucket" in p.actions), None)
        object_ops_perm = next((p for p in permissions if "s3:GetObject" in p.actions), None)

        assert list_bucket_perm is not None, "Should have ListBucket permission"
        assert object_ops_perm is not None, "Should have object operations permission"

        # Check that permissions can be converted to provider format
        list_bucket_format = list_bucket_perm.to_provider_format()
        object_ops_format = object_ops_perm.to_provider_format()

        assert "actions" in list_bucket_format
        assert "resources" in list_bucket_format
        assert "actions" in object_ops_format
        assert "resources" in object_ops_format

    # Use bucket.arn to trigger the check after resources are created
    bucket.arn.apply(check_permissions_structure)


@pulumi.runtime.test
def test_multiple_s3_buckets(pulumi_mocks):
    # Arrange
    bucket1 = Bucket("bucket-one")
    bucket2 = Bucket("bucket-two")

    # Act
    _ = bucket1.resources
    _ = bucket2.resources

    # Assert
    def check_multiple_buckets(_):
        bucket1_resources = pulumi_mocks.created_s3_buckets(TP + "bucket-one")
        bucket2_resources = pulumi_mocks.created_s3_buckets(TP + "bucket-two")

        assert len(bucket1_resources) == 1
        assert len(bucket2_resources) == 1

        assert bucket1_resources[0].name == TP + "bucket-one"
        assert bucket2_resources[0].name == TP + "bucket-two"

    pulumi.Output.all(bucket1.resources.bucket.id, bucket2.resources.bucket.id).apply(
        check_multiple_buckets
    )


@pulumi.runtime.test
def test_s3_bucket_name_validation(pulumi_mocks):
    # Test that bucket names are handled correctly
    # Note: AWS S3 has strict naming rules, but we're testing Stelvio's handling

    # Arrange
    bucket = Bucket("valid-bucket-name")

    # Act
    _ = bucket.resources

    # Assert
    def check_bucket_name(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "valid-bucket-name")
        assert len(buckets) == 1
        created_bucket = buckets[0]

        # The bucket name should be prefixed by the context
        assert created_bucket.name == TP + "valid-bucket-name"

    bucket.resources.bucket.id.apply(check_bucket_name)
