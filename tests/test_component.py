from dataclasses import dataclass

import pulumi
import pytest
from pulumi.runtime import Mocks, set_mocks

from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import LinkConfig


class _MinimalMocks(Mocks):
    def new_resource(self, args):
        return [args.name + "_id", args.inputs]

    def call(self, args):
        return ({}, [])


# Mock Pulumi resource for testing
class MockResource:
    def __init__(self, name="test-resource"):
        self.name = name
        self.id = f"{name}-id"


@dataclass(frozen=True)
class MockComponentResources:
    mock_resource: MockResource


# Concrete implementation of Component for testing
class MockComponent(Component[MockComponentResources, dict]):
    def __init__(
        self,
        name: str,
        resource: MockResource = None,
        tags: dict[str, str] | None = None,
        customize: dict[str, dict] | None = None,
    ):
        super().__init__("stelvio:test:MockComponent", name, tags=tags, customize=customize)
        self._mock_resource = resource or MockResource(name)
        # Track if _create_resource was called
        self.create_resources_called = False

    def _create_resources(self) -> MockComponentResources:
        self.create_resources_called = True
        return MockComponentResources(self._mock_resource)


@pytest.fixture
def clear_registry():
    """Clear the component registry and set up minimal Pulumi mocks.

    Mocks are needed because Component.__init__ registers a ComponentResource.
    """
    set_mocks(_MinimalMocks())
    # Save old state
    old_instances = ComponentRegistry._instances.copy()
    old_default_creators = ComponentRegistry._default_link_creators.copy()
    old_user_creators = ComponentRegistry._user_link_creators.copy()
    old_names = ComponentRegistry._registered_names.copy()

    # Clear registries
    ComponentRegistry._instances = {}
    ComponentRegistry._default_link_creators = {}
    ComponentRegistry._user_link_creators = {}
    ComponentRegistry._registered_names = set()

    yield
    # We need to do this because otherwise we get:
    # Task was destroyed but it is pending!
    # task: <Task pending name='Task-22672' coro=<Output.__init__.<locals>.
    # is_value_known() running at ~/Library/Caches/pypoetry/virtualenvs/
    # stelvio-wXLVHIoC-py3.12/lib/python3.12/site-packages/pulumi/output.py:127>
    # wait_for=<Future pending cb=[Task.task_wakeup()]>>

    # Restore old state
    ComponentRegistry._instances = old_instances
    ComponentRegistry._default_link_creators = old_default_creators
    ComponentRegistry._user_link_creators = old_user_creators
    ComponentRegistry._registered_names = old_names


# Component base class tests


def test_component_initialization(clear_registry):
    """Test that component is initialized and registered correctly."""
    component = MockComponent("test-component")

    # Verify name property
    assert component.name == "test-component"
    assert component.tags == {}

    # Verify it was added to the registry
    assert type(component) in ComponentRegistry._instances
    assert component in ComponentRegistry._instances[type(component)]


def test_duplicate_component_name_raises(clear_registry):
    """Creating two components with the same name raises ValueError."""
    MockComponent("duplicate-name")
    with pytest.raises(ValueError, match="Duplicate Stelvio component name"):
        MockComponent("duplicate-name")


def test_component_tags_are_stored_and_copied(clear_registry):
    component = MockComponent("tagged", tags={"Team": "platform"})

    assert component.tags == {"Team": "platform"}

    # Returned tags must be a copy so caller mutation doesn't affect component state.
    user_tags = component.tags
    user_tags["Team"] = "mutated"
    assert component.tags == {"Team": "platform"}


def test_component_tags_require_str_keys_and_values(clear_registry):
    with pytest.raises(TypeError, match="Tag key must be str"):
        MockComponent("bad-key", tags={1: "ok"})  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="Tag value for key 'k' must be str"):
        MockComponent("bad-value", tags={"k": 123})  # type: ignore[arg-type]


def test_resources_stores_created_resources(clear_registry):
    test_resource = MockResource("test-resource")
    component = MockComponent("test-component", test_resource)

    # First access - creates the resource
    resources1 = component.resources
    assert component.create_resources_called
    assert resources1.mock_resource is test_resource

    # Reset flag to test caching
    component.create_resources_called = False

    # Second access should use cached resource from registry
    resources2 = component.resources
    assert not component.create_resources_called  # Should not call create again
    assert resources2.mock_resource is test_resource  # Should get same resource


# ComponentRegistry tests


def test_add_and_get_instance(clear_registry):
    """Test adding and retrieving component instances."""

    # Create multiple components of different types
    class ComponentA(MockComponent):
        pass

    class ComponentB(MockComponent):
        pass

    comp_a1 = ComponentA("a1")
    comp_a2 = ComponentA("a2")
    comp_b = ComponentB("b")

    # Verify they're in the registry
    assert ComponentA in ComponentRegistry._instances
    assert len(ComponentRegistry._instances[ComponentA]) == 2
    assert comp_a1 in ComponentRegistry._instances[ComponentA]
    assert comp_a2 in ComponentRegistry._instances[ComponentA]

    assert ComponentB in ComponentRegistry._instances
    assert len(ComponentRegistry._instances[ComponentB]) == 1
    assert comp_b in ComponentRegistry._instances[ComponentB]


def test_all_instances(clear_registry):
    """Test iterating through all component instances."""

    # Create components of different types
    class ComponentA(MockComponent):
        pass

    class ComponentB(MockComponent):
        pass

    comp_a1 = ComponentA("a1")
    comp_a2 = ComponentA("a2")
    comp_b = ComponentB("b")

    # Get all instances
    all_instances = list(ComponentRegistry.all_instances())

    # Verify all components are in the list
    assert len(all_instances) == 3
    assert comp_a1 in all_instances
    assert comp_a2 in all_instances
    assert comp_b in all_instances


def test_instances_of(clear_registry):
    """instances_of returns only components of the requested type."""

    class ComponentA(MockComponent):
        pass

    class ComponentB(MockComponent):
        pass

    comp_a1 = ComponentA("a1")
    comp_a2 = ComponentA("a2")
    ComponentB("b")

    result = list(ComponentRegistry.instances_of(ComponentA))
    assert len(result) == 2
    assert comp_a1 in result
    assert comp_a2 in result


def test_instances_of_empty(clear_registry):
    """instances_of returns empty iterator for unregistered type."""
    result = list(ComponentRegistry.instances_of(MockComponent))
    assert result == []


def test_get_component_by_name(clear_registry):
    """get_component_by_name returns the component with the given name."""
    comp = MockComponent("find-me")
    MockComponent("other")

    assert ComponentRegistry.get_component_by_name("find-me") is comp


def test_get_component_by_name_not_found(clear_registry):
    """get_component_by_name returns None for unknown names."""
    assert ComponentRegistry.get_component_by_name("nonexistent") is None


def test_registry_uses_internal_name_when_public_name_is_overridden(clear_registry):
    class AliasComponent(MockComponent):
        def __init__(self, name: str, public_name: str):
            self._public_name = public_name
            super().__init__(name)

        @property
        def name(self) -> str:
            return self._public_name

    first = AliasComponent("internal-a", "shared")
    second = AliasComponent("internal-b", "shared")

    assert first.name == "shared"
    assert second.name == "shared"
    assert ComponentRegistry.get_component_by_name("internal-a") is first
    assert ComponentRegistry.get_component_by_name("internal-b") is second


def test_link_creator_decorator(clear_registry):
    """Test that the decorator correctly registers and wraps the function."""

    # Define a test function and decorate it
    @link_config_creator(MockComponent)
    def test_creator(r):
        return LinkConfig(properties={"name": r.name})

    # Get the registered creator
    creator = ComponentRegistry.get_link_config_creator(MockComponent)

    # Create a mock resource
    resource = MockResource("test")

    # Test the registered function
    # noinspection PyTypeChecker
    config = creator(resource)

    # Verify it returns expected result
    assert isinstance(config, LinkConfig)
    assert config.properties == {"name": "test"}

    # Test that the wrapper preserves function metadata
    assert creator.__name__ == test_creator.__name__


# Customizer tests


def test_customizer_returns_default_props_when_no_customization(clear_registry):
    """Test that _customizer returns default props when no customization is provided."""
    component = MockComponent("test-component")

    default_props = {"key1": "value1", "key2": "value2"}
    result = component._customizer("some_resource", default_props)

    assert result == default_props


def test_customizer_returns_default_props_when_resource_not_in_customize(clear_registry):
    """Test that _customizer returns default props when resource name is not in customize dict."""
    component = MockComponent(
        "test-component",
        customize={"other_resource": {"key1": "override1"}},
    )

    default_props = {"key1": "value1", "key2": "value2"}
    result = component._customizer("some_resource", default_props)

    assert result == default_props


def test_customizer_merges_customization_with_defaults(clear_registry):
    """Test that _customizer merges customization overrides with default props."""
    component = MockComponent(
        "test-component",
        customize={"bucket": {"key1": "override1", "key3": "new_value"}},
    )

    default_props = {"key1": "value1", "key2": "value2"}
    result = component._customizer("bucket", default_props)

    # Customization should override key1 and add key3
    assert result == {"key1": "override1", "key2": "value2", "key3": "new_value"}


def test_customizer_overrides_take_precedence(clear_registry):
    """Test that customization values take precedence over defaults."""
    component = MockComponent(
        "test-component",
        customize={"resource": {"setting": "custom"}},
    )

    default_props = {"setting": "default"}
    result = component._customizer("resource", default_props)

    assert result["setting"] == "custom"


def test_customizer_with_empty_defaults(clear_registry):
    """Test that _customizer works with empty default props."""
    component = MockComponent(
        "test-component",
        customize={"resource": {"key1": "value1"}},
    )

    result = component._customizer("resource", {})

    assert result == {"key1": "value1"}


def test_customizer_with_empty_customization_for_resource(clear_registry):
    """Test that _customizer handles empty customization for a specific resource."""
    component = MockComponent(
        "test-component",
        customize={"resource": {}},
    )

    default_props = {"key1": "value1"}
    result = component._customizer("resource", default_props)

    assert result == default_props


def test_customizer_injects_tags_when_requested(clear_registry):
    component = MockComponent("tagged-resource", tags={"Team": "platform"})

    result = component._customizer("resource", {"name": "test"}, inject_tags=True)

    assert result["tags"] == {"Team": "platform"}


def test_customizer_does_not_inject_tags_by_default(clear_registry):
    component = MockComponent("tagged-resource", tags={"Team": "platform"})

    result = component._customizer("resource", {"name": "test"})

    assert "tags" not in result


def test_customizer_with_nested_dict_values(clear_registry):
    """Test that _customizer works with nested dictionary values."""
    component = MockComponent(
        "test-component",
        customize={"bucket": {"versioning": {"enabled": False}}},
    )

    default_props = {"bucket": "my-bucket", "versioning": {"enabled": True}}
    result = component._customizer("bucket", default_props)

    # Note: dict merge is shallow, so nested dict is completely replaced
    assert result == {
        "bucket": "my-bucket",
        "versioning": {"enabled": False},
    }


def test_customizer_shallow_merge_nested_dict_completely_replaced(clear_registry):
    """Test that nested dicts are completely replaced, not deep-merged.

    This is a key behavior to document: when defaults have
    {"tags": {"a": 1, "b": 2}} and user provides {"tags": {"c": 3}},
    the entire tags dict gets replaced → {"tags": {"c": 3}}
    (NOT merged to {"a": 1, "b": 2, "c": 3}).
    """
    component = MockComponent(
        "test-component",
        customize={"bucket": {"tags": {"c": 3}}},
    )

    default_props = {"bucket": "my-bucket", "tags": {"a": 1, "b": 2}}
    result = component._customizer("bucket", default_props)

    # Shallow merge: tags is completely replaced, not merged
    # Keys "a" and "b" are lost - this is intentional!
    assert result == {
        "bucket": "my-bucket",
        "tags": {"c": 3},
    }
    # Explicitly verify the old keys are NOT present
    assert "a" not in result["tags"]
    assert "b" not in result["tags"]


def test_customizer_with_multiple_resources(clear_registry):
    """Test that _customizer correctly selects the right resource configuration."""
    component = MockComponent(
        "test-component",
        customize={
            "bucket": {"key": "bucket_value"},
            "policy": {"key": "policy_value"},
            "role": {"key": "role_value"},
        },
    )

    bucket_result = component._customizer("bucket", {"key": "default"})
    policy_result = component._customizer("policy", {"key": "default"})
    role_result = component._customizer("role", {"key": "default"})
    other_result = component._customizer("other", {"key": "default"})

    assert bucket_result == {"key": "bucket_value"}
    assert policy_result == {"key": "policy_value"}
    assert role_result == {"key": "role_value"}
    assert other_result == {"key": "default"}


def test_customize_defaults_to_empty_dict(clear_registry):
    """Test that customize defaults to an empty dict when None is passed."""
    component = MockComponent("test-component", customize=None)

    # Should not raise, and should return defaults
    result = component._customizer("resource", {"key": "value"})
    assert result == {"key": "value"}


def test_customize_initialization_without_parameter(clear_registry):
    """Test that component can be created without customize parameter."""
    component = MockComponent("test-component")

    # Internal _customize should be an empty dict
    assert component._customize == {}


# ComponentResource tests


def test_component_is_pulumi_component_resource(clear_registry):
    """Component instances are Pulumi ComponentResources."""
    component = MockComponent("cr-test")
    assert isinstance(component, pulumi.ComponentResource)


def test_component_is_abstract(clear_registry):
    """Component requires _create_resources to be implemented."""
    from abc import ABC

    assert ABC in Component.__mro__
    with pytest.raises(TypeError):
        Component("stelvio:test:Test", "test")


# _resource_opts tests


def test_resource_opts_parent_is_self(clear_registry):
    """_resource_opts sets parent to the component itself."""
    component = MockComponent("parent-test")
    opts = component._resource_opts()
    assert opts.parent is component


def test_resource_opts_has_root_alias(clear_registry):
    """_resource_opts includes alias from ROOT_STACK_RESOURCE for migration."""
    component = MockComponent("alias-test")
    opts = component._resource_opts()
    assert len(opts.aliases) == 1
    alias = opts.aliases[0]
    assert isinstance(alias, pulumi.Alias)
    assert alias.parent is pulumi.ROOT_STACK_RESOURCE


def test_resource_opts_depends_on(clear_registry):
    """_resource_opts passes through depends_on."""
    comp1 = MockComponent("dep-source")
    comp2 = MockComponent("dep-target")
    opts = comp2._resource_opts(depends_on=[comp1])
    assert opts.depends_on == [comp1]


def test_resource_opts_provider(clear_registry):
    """_resource_opts passes through a custom provider."""
    from stelvio.provider import ProviderStore

    component = MockComponent("provider-test")
    provider = ProviderStore.aws()
    opts = component._resource_opts(provider=provider)
    assert opts.provider is provider


def test_resource_opts_defaults(clear_registry):
    """_resource_opts defaults: no depends_on, no provider."""
    component = MockComponent("defaults-test")
    opts = component._resource_opts()
    assert opts.depends_on is None
    assert opts.provider is None
