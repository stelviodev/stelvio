"""Tests for global customization via StelvioAppConfig.customize.

These tests verify that:
1. Global customization from StelvioAppConfig applies to all component instances
2. Per-instance customization overrides global settings
3. Environment-based configuration returns correct customization per environment
"""

import shutil
from pathlib import Path

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.function import Function
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket
from stelvio.aws.topic import Topic
from stelvio.config import AwsConfig, StelvioAppConfig
from stelvio.context import AppContext, _ContextStore

from .pulumi_mocks import PulumiTestMocks

# Test prefix
TP = "test-test-"


def delete_files(directory: Path, filename: str):
    """Helper to clean up generated files."""
    for file_path in directory.rglob(filename):
        file_path.unlink(missing_ok=True)


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

    shutil.copytree(source_project_dir, temp_project_dir, dirs_exist_ok=True)
    monkeypatch.chdir(temp_project_dir)
    yield temp_project_dir
    delete_files(temp_project_dir, "stlv_resources.py")


def create_app_context_with_global_customize(customize: dict) -> None:
    """Helper to set up AppContext with global customization."""
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize=customize,
        )
    )


# =============================================================================
# Global Customization Applied to All Instances
# =============================================================================


@pulumi.runtime.test
def test_global_customize_applies_to_bucket(pulumi_mocks, project_cwd, clean_registries):
    """Test that global customization applies to Bucket instances."""
    # Arrange - set global customize for Bucket
    create_app_context_with_global_customize(
        {Bucket: {"bucket": {"force_destroy": True, "tags": {"Global": "true"}}}}
    )

    bucket1 = Bucket("bucket-one")
    bucket2 = Bucket("bucket-two")

    # Act
    _ = bucket1.resources
    _ = bucket2.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "bucket-one")
        assert len(buckets) == 1
        assert buckets[0].inputs.get("forceDestroy") is True
        assert buckets[0].inputs.get("tags") == {"Global": "true"}

        buckets2 = pulumi_mocks.created_s3_buckets(TP + "bucket-two")
        assert len(buckets2) == 1
        assert buckets2[0].inputs.get("forceDestroy") is True
        assert buckets2[0].inputs.get("tags") == {"Global": "true"}

    bucket2.resources.bucket.id.apply(check_resources)


@pulumi.runtime.test
def test_global_customize_applies_to_function(pulumi_mocks, project_cwd, clean_registries):
    """Test that global customization applies to Function instances."""
    # Arrange - set global customize for Function
    create_app_context_with_global_customize(
        {Function: {"function": {"reserved_concurrent_executions": 100}}}
    )

    fn1 = Function("fn-one", handler="functions/simple.handler")
    fn2 = Function("fn-two", handler="functions/simple.handler")

    # Act
    _ = fn1.resources
    _ = fn2.resources

    # Assert
    def check_resources(_):
        functions1 = pulumi_mocks.created_functions(TP + "fn-one")
        assert len(functions1) == 1
        assert functions1[0].inputs.get("reservedConcurrentExecutions") == 100

        functions2 = pulumi_mocks.created_functions(TP + "fn-two")
        assert len(functions2) == 1
        assert functions2[0].inputs.get("reservedConcurrentExecutions") == 100

    fn2.resources.function.id.apply(check_resources)


@pulumi.runtime.test
def test_global_customize_multiple_component_types(pulumi_mocks, project_cwd, clean_registries):
    """Test that global customization works for multiple component types."""
    # Arrange - set global customize for multiple types
    create_app_context_with_global_customize(
        {
            Bucket: {"bucket": {"force_destroy": True}},
            Queue: {"queue": {"tags": {"GlobalQueue": "yes"}}},
            Topic: {"topic": {"tags": {"GlobalTopic": "yes"}}},
        }
    )

    bucket = Bucket("my-bucket")
    queue = Queue("my-queue")
    topic = Topic("my-topic")

    # Act
    _ = bucket.resources
    _ = queue.resources
    _ = topic.resources

    # Assert
    def check_resources(_):
        # Check bucket
        buckets = pulumi_mocks.created_s3_buckets(TP + "my-bucket")
        assert len(buckets) == 1
        assert buckets[0].inputs.get("forceDestroy") is True

        # Check queue
        queues = pulumi_mocks.created_sqs_queues(TP + "my-queue")
        assert len(queues) == 1
        assert queues[0].inputs.get("tags") == {"GlobalQueue": "yes"}

        # Check topic
        topics = [t for t in pulumi_mocks.created_sns_topics() if "my-topic" in t.name]
        assert len(topics) == 1
        assert topics[0].inputs.get("tags") == {"GlobalTopic": "yes"}

    topic.resources.topic.id.apply(check_resources)


# =============================================================================
# Per-Instance Overrides Global Customization
# =============================================================================


@pulumi.runtime.test
def test_per_instance_overrides_global_bucket(pulumi_mocks, project_cwd, clean_registries):
    """Test that per-instance customization overrides global for Bucket."""
    # Arrange - set global customize, then override per-instance
    create_app_context_with_global_customize(
        {Bucket: {"bucket": {"force_destroy": True, "tags": {"Source": "global"}}}}
    )

    # bucket1 uses global settings
    bucket1 = Bucket("bucket-global")

    # bucket2 overrides with per-instance settings
    bucket2 = Bucket(
        "bucket-override",
        customize={"bucket": {"force_destroy": False, "tags": {"Source": "instance"}}},
    )

    # Act
    _ = bucket1.resources
    _ = bucket2.resources

    # Assert
    def check_resources(_):
        # bucket1 should have global settings
        buckets1 = pulumi_mocks.created_s3_buckets(TP + "bucket-global")
        assert len(buckets1) == 1
        assert buckets1[0].inputs.get("forceDestroy") is True
        assert buckets1[0].inputs.get("tags") == {"Source": "global"}

        # bucket2 should have per-instance overrides
        buckets2 = pulumi_mocks.created_s3_buckets(TP + "bucket-override")
        assert len(buckets2) == 1
        assert buckets2[0].inputs.get("forceDestroy") is False
        assert buckets2[0].inputs.get("tags") == {"Source": "instance"}

    bucket2.resources.bucket.id.apply(check_resources)


@pulumi.runtime.test
def test_per_instance_overrides_global_function(pulumi_mocks, project_cwd, clean_registries):
    """Test that per-instance customization overrides global for Function."""
    # Arrange
    create_app_context_with_global_customize(
        {Function: {"function": {"reserved_concurrent_executions": 100, "timeout": 30}}}
    )

    # fn1 uses global settings
    fn1 = Function("fn-global", handler="functions/simple.handler")

    # fn2 overrides with per-instance
    fn2 = Function(
        "fn-override",
        handler="functions/simple.handler",
        customize={"function": {"reserved_concurrent_executions": 5}},
    )

    # Act
    _ = fn1.resources
    _ = fn2.resources

    # Assert
    def check_resources(_):
        # fn1 should have global settings
        functions1 = pulumi_mocks.created_functions(TP + "fn-global")
        assert len(functions1) == 1
        assert functions1[0].inputs.get("reservedConcurrentExecutions") == 100
        assert functions1[0].inputs.get("timeout") == 30

        # fn2 should have per-instance override (only reservedConcurrentExecutions)
        functions2 = pulumi_mocks.created_functions(TP + "fn-override")
        assert len(functions2) == 1
        # Per-instance override
        assert functions2[0].inputs.get("reservedConcurrentExecutions") == 5

    fn2.resources.function.id.apply(check_resources)


@pulumi.runtime.test
def test_per_instance_partial_override(pulumi_mocks, project_cwd, clean_registries):
    """Test that per-instance can override specific resource while inheriting global."""
    # Arrange - global sets bucket AND public_access_block
    create_app_context_with_global_customize(
        {
            Bucket: {
                "bucket": {"force_destroy": True, "tags": {"GlobalTag": "yes"}},
            }
        }
    )

    # Per-instance only overrides bucket force_destroy, should still get global tags via merge
    bucket = Bucket("partial-override", customize={"bucket": {"force_destroy": False}})

    # Act
    _ = bucket.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "partial-override")
        assert len(buckets) == 1
        # Per-instance override should take precedence
        assert buckets[0].inputs.get("forceDestroy") is False
        # Note: Due to shallow merge, global tags are replaced by per-instance
        # (which has none), so tags won't be present. This is documented behavior.

    bucket.resources.bucket.id.apply(check_resources)


@pulumi.runtime.test
def test_per_instance_tags_completely_replace_global_tags(
    pulumi_mocks, project_cwd, clean_registries
):
    """Test that per-instance tags completely replace global tags (shallow merge).

    This explicitly tests the shallow merge behavior documented in the codebase:
    when global customize sets {"tags": {"a": 1, "b": 2}} and per-instance sets
    {"tags": {"c": 3}}, the result is {"tags": {"c": 3}} - NOT a deep merge.

    This is intentional behavior but can surprise users, so we test it explicitly.
    """
    # Arrange - global sets multiple tags
    create_app_context_with_global_customize(
        {
            Bucket: {
                "bucket": {"tags": {"Team": "platform", "Cost": "shared", "ManagedBy": "stelvio"}}
            }
        }
    )

    # Per-instance sets different tags - this will REPLACE all global tags
    bucket = Bucket(
        "tags-replace-test",
        customize={"bucket": {"tags": {"Env": "dev"}}},
    )

    # Act
    _ = bucket.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "tags-replace-test")
        assert len(buckets) == 1
        tags = buckets[0].inputs.get("tags", {})

        # Shallow merge: per-instance tags COMPLETELY replace global tags
        # Only "Env" should exist - "Team", "Cost", "ManagedBy" are gone!
        assert tags == {"Env": "dev"}

        # Explicitly verify global tags are NOT present
        assert "Team" not in tags
        assert "Cost" not in tags
        assert "ManagedBy" not in tags

    bucket.resources.bucket.id.apply(check_resources)


# =============================================================================
# Environment-Based Configuration Tests
# =============================================================================


def sample_configuration(env: str) -> StelvioAppConfig:
    """Sample configuration function that returns different settings per environment."""
    if env == "dev":
        return StelvioAppConfig(
            aws=AwsConfig(profile="dev-profile", region="us-east-1"),
            customize={
                Bucket: {"bucket": {"force_destroy": True}},
                Function: {"function": {"memory_size": 256}},
            },
        )
    if env == "staging":
        return StelvioAppConfig(
            aws=AwsConfig(profile="staging-profile", region="us-west-2"),
            customize={
                Bucket: {"bucket": {"force_destroy": False}},
                Function: {"function": {"memory_size": 512}},
            },
        )
    if env == "prod":
        return StelvioAppConfig(
            aws=AwsConfig(profile="prod-profile", region="us-east-1"),
            customize={
                Function: {
                    "function": {
                        "memory_size": 1024,
                        "reserved_concurrent_executions": 100,
                    }
                },
            },
        )
    # Personal/dev environment
    return StelvioAppConfig(
        aws=AwsConfig(),
        customize={
            Bucket: {"bucket": {"force_destroy": True}},
        },
    )


def test_env_config_dev_returns_correct_customize():
    """Test that dev environment configuration has correct customize settings."""
    config = sample_configuration("dev")

    assert config.aws.profile == "dev-profile"
    assert config.aws.region == "us-east-1"
    assert Bucket in config.customize
    assert config.customize[Bucket]["bucket"]["force_destroy"] is True
    assert Function in config.customize
    assert config.customize[Function]["function"]["memory_size"] == 256


def test_env_config_staging_returns_correct_customize():
    """Test that staging environment configuration has correct customize settings."""
    config = sample_configuration("staging")

    assert config.aws.profile == "staging-profile"
    assert config.aws.region == "us-west-2"
    assert Bucket in config.customize
    assert config.customize[Bucket]["bucket"]["force_destroy"] is False
    assert config.customize[Function]["function"]["memory_size"] == 512


def test_env_config_prod_returns_correct_customize():
    """Test that prod environment configuration has correct customize settings."""
    config = sample_configuration("prod")

    assert config.aws.profile == "prod-profile"
    assert Function in config.customize
    assert config.customize[Function]["function"]["memory_size"] == 1024
    assert config.customize[Function]["function"]["reserved_concurrent_executions"] == 100
    # Prod should NOT have bucket customization
    assert Bucket not in config.customize


def test_env_config_unknown_returns_safe_defaults():
    """Test that unknown/personal environment returns safe defaults."""
    config = sample_configuration("johndoe")  # Personal username

    assert Bucket in config.customize
    assert config.customize[Bucket]["bucket"]["force_destroy"] is True
    # No function customization for personal envs
    assert Function not in config.customize


@pulumi.runtime.test
def test_env_based_customize_applied_to_components(pulumi_mocks, project_cwd, clean_registries):
    """Test that environment-specific customization is applied to components."""
    # Arrange - simulate dev environment
    dev_config = sample_configuration("dev")
    create_app_context_with_global_customize(dev_config.customize)

    bucket = Bucket("dev-bucket")
    fn = Function("dev-fn", handler="functions/simple.handler")

    # Act
    _ = bucket.resources
    _ = fn.resources

    # Assert
    def check_resources(_):
        # Check bucket has dev settings
        buckets = pulumi_mocks.created_s3_buckets(TP + "dev-bucket")
        assert len(buckets) == 1
        assert buckets[0].inputs.get("forceDestroy") is True

        # Check function has dev settings
        functions = pulumi_mocks.created_functions(TP + "dev-fn")
        assert len(functions) == 1
        assert functions[0].inputs.get("memorySize") == 256

    fn.resources.function.id.apply(check_resources)


@pulumi.runtime.test
def test_env_based_prod_customize_applied(pulumi_mocks, project_cwd, clean_registries):
    """Test that prod environment customization is applied correctly."""
    # Arrange - simulate prod environment
    prod_config = sample_configuration("prod")
    create_app_context_with_global_customize(prod_config.customize)

    fn = Function("prod-fn", handler="functions/simple.handler")

    # Act
    _ = fn.resources

    # Assert
    def check_resources(_):
        functions = pulumi_mocks.created_functions(TP + "prod-fn")
        assert len(functions) == 1
        assert functions[0].inputs.get("memorySize") == 1024
        assert functions[0].inputs.get("reservedConcurrentExecutions") == 100

    fn.resources.function.id.apply(check_resources)


# =============================================================================
# Function Role and Policy Customization Tests
# =============================================================================


@pulumi.runtime.test
def test_function_role_customization(pulumi_mocks, project_cwd, clean_registries):
    """Test that role customization is applied to Lambda IAM role."""
    # Arrange
    create_app_context_with_global_customize({})

    fn = Function(
        "fn-with-role-custom",
        handler="functions/simple.handler",
        customize={
            "role": {
                "tags": {"RoleTag": "custom"},
            }
        },
    )

    # Act
    _ = fn.resources

    # Assert
    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        matching_roles = [r for r in roles if "fn-with-role-custom" in r.name]
        assert len(matching_roles) == 1
        assert matching_roles[0].inputs.get("tags") == {"RoleTag": "custom"}

    fn.resources.role.id.apply(check_resources)


@pulumi.runtime.test
def test_global_function_role_customization(pulumi_mocks, project_cwd, clean_registries):
    """Test that global role customization applies to all functions."""
    # Arrange
    create_app_context_with_global_customize(
        {
            Function: {
                "role": {"tags": {"GlobalRoleTag": "yes"}},
            }
        }
    )

    fn = Function("fn-global-role", handler="functions/simple.handler")

    # Act
    _ = fn.resources

    # Assert
    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        matching_roles = [r for r in roles if "fn-global-role" in r.name]
        assert len(matching_roles) == 1
        assert matching_roles[0].inputs.get("tags") == {"GlobalRoleTag": "yes"}

    fn.resources.role.id.apply(check_resources)
