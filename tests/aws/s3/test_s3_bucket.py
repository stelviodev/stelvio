import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.permission import AwsPermission
from stelvio.aws.s3 import Bucket

from ..pulumi_mocks import PulumiTestMocks, tid, tn

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


@pytest.mark.parametrize("versioning_enabled", [True, False])
@pulumi.runtime.test
def test_s3_bucket_versioning(pulumi_mocks, versioning_enabled):
    # Test that versioning is configured correctly based on the versioning parameter

    # Arrange
    bucket = Bucket("test-bucket", versioning=versioning_enabled)

    # Act
    _ = bucket.resources

    # Assert
    def check_versioning(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "test-bucket")
        assert len(buckets) == 1
        created_bucket = buckets[0]

        # Check that versioning is explicitly set to the expected value
        versioning_config = created_bucket.inputs.get("versioning")
        assert versioning_config is not None
        assert versioning_config["enabled"] is versioning_enabled

    bucket.resources.bucket.id.apply(check_versioning)


@pulumi.runtime.test
def test_s3_bucket_access_none_pab_created_with_true_flags(pulumi_mocks):
    # Test that when access=None, PAB is created with all blocking flags set to True
    # and no bucket policy is created

    # Arrange
    bucket = Bucket("test-bucket", access=None)

    # Act
    _ = bucket.resources

    # Assert
    def check_access_configuration(_):
        # Check Public Access Block configuration
        pabs = pulumi_mocks.created_s3_public_access_blocks(TP + "test-bucket-pab")
        assert len(pabs) == 1
        pab = pabs[0]

        # All blocking flags should be True (using camelCase keys)
        assert pab.inputs["blockPublicAcls"] is True
        assert pab.inputs["blockPublicPolicy"] is True
        assert pab.inputs["ignorePublicAcls"] is True
        assert pab.inputs["restrictPublicBuckets"] is True

        # No bucket policy should be created
        policies = pulumi_mocks.created_bucket_policies(TP + "test-bucket-policy")
        assert len(policies) == 0

        # Verify that bucket_policy in resources is None
        assert bucket.resources.bucket_policy is None

    # Use the PAB resource ID to trigger the check after all resources are created
    bucket.resources.public_access_block.id.apply(check_access_configuration)


@pulumi.runtime.test
def test_s3_bucket_access_public_pab_created_with_false_flags_and_policy(pulumi_mocks):
    # Test that when access="public", PAB is created with all blocking flags set to False
    # and a bucket policy is created with proper values

    # Arrange
    bucket = Bucket("test-bucket", access="public")

    # Act
    _ = bucket.resources

    # Assert
    def check_access_configuration(_):
        # Check Public Access Block configuration
        pabs = pulumi_mocks.created_s3_public_access_blocks(TP + "test-bucket-pab")
        assert len(pabs) == 1
        pab = pabs[0]

        # All blocking flags should be False for public access (using camelCase keys)
        assert pab.inputs["blockPublicAcls"] is False
        assert pab.inputs["blockPublicPolicy"] is False
        assert pab.inputs["ignorePublicAcls"] is False
        assert pab.inputs["restrictPublicBuckets"] is False

        # Bucket policy should be created
        policies = pulumi_mocks.created_bucket_policies(TP + "test-bucket-policy")
        assert len(policies) == 1
        policy = policies[0]

        # Verify that bucket_policy in resources is not None
        assert bucket.resources.bucket_policy is not None

        # Check that policy is attached to the correct bucket
        # The bucket ID from the policy should match the generated test ID
        bucket_id_from_policy = policy.inputs["bucket"]
        # In the mock environment, bucket ID should be the test ID
        expected_bucket_id = tid(TP + "test-bucket")
        assert bucket_id_from_policy == expected_bucket_id

        # Policy should contain the correct JSON structure for public read access
        import json

        policy_json = policy.inputs["policy"]
        policy_doc = json.loads(policy_json)

        assert len(policy_doc) == 1  # Should have one statement
        statement = policy_doc[0]

        assert statement["effect"] == "Allow"
        assert statement["principals"] == [{"type": "*", "identifiers": ["*"]}]
        assert statement["actions"] == ["s3:GetObject"]

        # Resource should reference the bucket ARN with /*
        expected_bucket_arn = f"arn:aws:s3:::{tn(TP + 'test-bucket')}"
        expected_resource = f"{expected_bucket_arn}/*"
        assert statement["resources"] == [expected_resource]

    # Use the bucket policy ID to trigger the check after all resources are created
    bucket.resources.bucket_policy.id.apply(check_access_configuration)
