from dataclasses import dataclass, replace

import pulumi
import pulumi_aws
import pytest
from pulumi.runtime import Mocks, set_mocks

from stelvio import context
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.context import _ContextStore
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
        parent: pulumi.Resource | None = None,
    ):
        super().__init__(
            "stelvio:test:MockComponent", name, tags=tags, customize=customize, parent=parent
        )
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


def test_component_without_parent_has_no_aliases(clear_registry):
    component = MockComponent("top-level")
    assert component._aliases == []


def test_component_with_parent_has_migration_alias(clear_registry):
    parent = MockComponent("parent")
    child = MockComponent("child", parent=parent)

    # Parented components should carry one alias for previous stack-root parentage.
    assert len(child._aliases) == 1


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
        "test-component", customize={"other_resource": {"key1": "override1"}}
    )

    default_props = {"key1": "value1", "key2": "value2"}
    result = component._customizer("some_resource", default_props)

    assert result == default_props


def test_customizer_merges_customization_with_defaults(clear_registry):
    """Test that _customizer merges customization overrides with default props."""
    component = MockComponent(
        "test-component", customize={"bucket": {"key1": "override1", "key3": "new_value"}}
    )

    default_props = {"key1": "value1", "key2": "value2"}
    result = component._customizer("bucket", default_props)

    # Customization should override key1 and add key3
    assert result == {"key1": "override1", "key2": "value2", "key3": "new_value"}


def test_customizer_overrides_take_precedence(clear_registry):
    """Test that customization values take precedence over defaults."""
    component = MockComponent("test-component", customize={"resource": {"setting": "custom"}})

    default_props = {"setting": "default"}
    result = component._customizer("resource", default_props)

    assert result["setting"] == "custom"


def test_customizer_with_empty_defaults(clear_registry):
    """Test that _customizer works with empty default props."""
    component = MockComponent("test-component", customize={"resource": {"key1": "value1"}})

    result = component._customizer("resource", {})

    assert result == {"key1": "value1"}


def test_customizer_with_empty_customization_for_resource(clear_registry):
    """Test that _customizer handles empty customization for a specific resource."""
    component = MockComponent("test-component", customize={"resource": {}})

    default_props = {"key1": "value1"}
    result = component._customizer("resource", default_props)

    assert result == default_props


def test_customizer_applies_global_resource_callable_customization(clear_registry):
    calls = []

    def global_customize(computed_props):
        calls.append(computed_props)
        return computed_props | {"memory": 512}

    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(current_ctx, customize={MockComponent: {"function": global_customize}})
    )

    component = MockComponent("test-component")
    default_props = {"memory": 128}
    computed_props = {"name": "fn", "memory": None}

    result = component._customizer("function", computed_props, default_props)

    assert result == {"name": "fn", "memory": 512}
    assert calls == [computed_props]


def test_customizer_applies_local_callable_customization(clear_registry):
    calls = []

    def local_customize(default_props):
        calls.append(default_props)
        return {"memory": default_props["memory"] * 2, "timeout": 30}

    component = MockComponent("test-component", customize={"function": local_customize})
    default_props = {"memory": 128}

    result = component._customizer("function", default_props)

    assert result == {"memory": 256, "timeout": 30}
    assert calls == [default_props]


def test_customizer_callable_can_return_pulumi_args(clear_registry):
    def local_customize(props):
        return pulumi_aws.s3.BucketArgs(bucket=f"{props['bucket']}-custom")

    component = MockComponent("test-component", customize={"bucket": local_customize})

    result = component._customizer("bucket", {"bucket": "my-bucket", "acl": "private"})

    assert result == {"bucket": "my-bucket-custom"}


def test_customizer_callable_returning_empty_dict_replaces_all_props(clear_registry):
    def local_customize(_props):
        return {}

    component = MockComponent("test-component", customize={"resource": local_customize})

    result = component._customizer("resource", {"key1": "value1", "key2": "value2"})

    assert result == {}


def test_customizer_global_callable_not_applied_to_other_resources(clear_registry):
    calls: list[dict[str, str]] = []

    def global_customize(props: dict[str, str]) -> dict[str, str]:
        calls.append(props)
        return {"key1": "override"}

    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(current_ctx, customize={MockComponent: {"other_resource": global_customize}})
    )

    component = MockComponent("test-component")
    default_props = {"key1": "value1", "key2": "value2"}

    result = component._customizer("some_resource", default_props)

    assert result == default_props
    assert calls == []


def test_customizer_local_callable_takes_precedence_over_global_dict(clear_registry):
    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(current_ctx, customize={MockComponent: {"function": {"memory": 256}}})
    )

    calls: list[dict[str, int]] = []

    def local_customize(props: dict[str, int]) -> dict[str, int]:
        calls.append(props)
        return {"timeout": props["timeout"] + 5}

    component = MockComponent(
        "test-component",
        customize={"function": local_customize},
    )

    result = component._customizer("function", {}, {"timeout": 25, "memory": 128})

    assert result == {"timeout": 30}
    assert calls == [{"timeout": 25, "memory": 256}]


def test_customizer_local_callable_receives_global_customized_props(clear_registry):
    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(current_ctx, customize={MockComponent: {"function": {"timeout": 30}}})
    )

    def local_customize(props):
        return {"timeout": props["timeout"] + 5}

    component = MockComponent("test-component", customize={"function": local_customize})

    result = component._customizer("function", {}, {"timeout": 25})

    # Local callable should receive final_props (after global customization), not defaults.
    assert result == {"timeout": 35}


def test_customizer_global_and_local_resource_callables_are_both_invoked(clear_registry):
    call_order: list[str] = []

    def global_customize(default_props):
        call_order.append("global")
        return default_props | {"global": True}

    def local_customize(default_props):
        call_order.append("local")
        return default_props | {"local": True}

    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(current_ctx, customize={MockComponent: {"bucket": global_customize}})
    )

    component = MockComponent("test-component", customize={"bucket": local_customize})

    result = component._customizer("bucket", {"name": "test"}, {})

    assert result == {"name": "test", "local": True, "global": True}
    assert call_order == ["global", "local"]


def test_customizer_defaults_mode_global_customize_is_default_not_override(clear_registry):
    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(current_ctx, customize={MockComponent: {"function": {"memory": 512}}})
    )

    component = MockComponent("test-component")

    result = component._customizer(
        "function",
        computed_props={"memory": 1024, "timeout": None},
        default_props={"memory": 128, "timeout": 10},
    )

    # Global customize acts as a default, while explicit computed props win.
    assert result == {"memory": 1024, "timeout": 10}


def test_customizer_explicit_computed_value_overrides_global_callable_default(clear_registry):
    calls = []

    def global_customize(computed_props):
        calls.append(computed_props)
        memory = 512 if computed_props.get("memory") is None else computed_props["memory"]
        timeout = 30 if computed_props.get("timeout") is None else computed_props["timeout"]
        return {"memory": memory, "timeout": timeout}

    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(current_ctx, customize={MockComponent: {"function": global_customize}})
    )

    component = MockComponent("test-component")

    result = component._customizer(
        "function",
        computed_props={"memory": 1024, "timeout": None},
        default_props={"memory": 128, "timeout": 10},
    )

    # Explicit non-None computed props should override global callable defaults.
    assert result == {"memory": 1024, "timeout": 30}
    assert calls == [{"memory": 1024, "timeout": None}]


def test_customizer_injects_tags_when_requested(clear_registry):
    component = MockComponent("tagged-resource", tags={"Team": "platform"})

    result = component._customizer("resource", {"name": "test"}, inject_tags=True)

    assert result["tags"] == {"Team": "platform"}


def test_customizer_injects_tags_before_callable_and_keeps_tags_if_returned(clear_registry):
    seen_props = []

    def local_customize(props):
        seen_props.append(props)
        return {
            "name": props["name"],
            "tags": {**props["tags"], "Service": "api"},
        }

    component = MockComponent(
        "tagged-resource",
        tags={"Team": "platform"},
        customize={"resource": local_customize},
    )

    result = component._customizer("resource", {"name": "test"}, inject_tags=True)

    assert seen_props == [{"name": "test", "tags": {"Team": "platform"}}]
    assert result == {"name": "test", "tags": {"Team": "platform", "Service": "api"}}


def test_customizer_callable_can_drop_injected_tags_if_omitted(clear_registry):
    def local_customize(props):
        return {"name": props["name"]}

    component = MockComponent(
        "tagged-resource",
        tags={"Team": "platform"},
        customize={"resource": local_customize},
    )

    result = component._customizer("resource", {"name": "test"}, inject_tags=True)

    assert result == {"name": "test"}
    assert "tags" not in result


def test_customizer_inject_tags_with_computed_and_default_props(clear_registry):
    component = MockComponent("tagged-resource", tags={"Team": "platform"})

    result = component._customizer(
        "resource",
        computed_props={"name": "test", "memory": None, "tags": {"Env": "dev"}},
        default_props={"memory": 128, "timeout": 30, "tags": {"Base": "yes"}},
        inject_tags=True,
    )

    assert result == {
        "name": "test",
        "memory": 128,
        "timeout": 30,
        "tags": {"Env": "dev", "Team": "platform"},
    }


def test_customizer_does_not_inject_tags_by_default(clear_registry):
    component = MockComponent("tagged-resource", tags={"Team": "platform"})

    result = component._customizer("resource", {"name": "test"})

    assert "tags" not in result


def test_customizer_with_nested_dict_values(clear_registry):
    """Test that _customizer works with nested dictionary values."""
    component = MockComponent(
        "test-component", customize={"bucket": {"versioning": {"enabled": False}}}
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
    component = MockComponent("test-component", customize={"bucket": {"tags": {"c": 3}}})

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


# Customizer tests with computed_props + default_props combinations
# These test real-world scenarios like in the Function component


def test_customizer_explicit_values_in_computed_props_override_defaults(clear_registry):
    """Explicit values in computed_props override default_props.

    Simulates: user explicitly sets memory=1024, so computed_props has that value.
    """
    component = MockComponent("test-component")

    result = component._customizer(
        "function",
        computed_props={"memory": 1024, "timeout": None},
        default_props={"memory": 128, "timeout": 30},
    )

    # Explicit 1024 wins, but None defaults to 30
    assert result == {"memory": 1024, "timeout": 30}


def test_customizer_none_values_in_computed_props_use_defaults(clear_registry):
    """None values in computed_props fall back to default_props.

    Simulates: user didn't set timeout, so computed_props has None.
    Default should be used.
    """
    component = MockComponent("test-component")

    result = component._customizer(
        "function",
        computed_props={"memory": None, "timeout": None, "runtime": None},
        default_props={"memory": 128, "timeout": 30, "runtime": "python3.12"},
    )

    # All None values use defaults
    assert result == {"memory": 128, "timeout": 30, "runtime": "python3.12"}


def test_customizer_explicit_values_override_global_customize(clear_registry):
    """Explicit values in computed_props override global customize.

    The precedence is: computed_props (explicit) > global customize > defaults.
    """
    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(current_ctx, customize={MockComponent: {"function": {"memory": 512}}})
    )

    component = MockComponent("test-component")

    result = component._customizer(
        "function",
        computed_props={"memory": 1024, "timeout": None},
        default_props={"memory": 128, "timeout": 30},
    )

    # Explicit 1024 overrides global customize's 512
    assert result == {"memory": 1024, "timeout": 30}


def test_customizer_none_values_use_global_customize_as_default(clear_registry):
    """None values in computed_props use global customize as defaults.

    When user doesn't set a value (None in computed_props):
    1. Global customize provides a default
    2. If global customize doesn't have it, use default_props
    """
    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(current_ctx, customize={MockComponent: {"function": {"memory": 512}}})
    )

    component = MockComponent("test-component")

    result = component._customizer(
        "function",
        computed_props={"memory": None, "timeout": None},
        default_props={"memory": 128, "timeout": 30},
    )

    # None uses global customize for memory (512), and Stelvio default for timeout
    assert result == {"memory": 512, "timeout": 30}


def test_customizer_mixed_explicit_and_none_with_global_customize(clear_registry):
    """Complex mix: explicit values, None values, global customize, and defaults.

    This simulates real-world Function component usage where:
    - User explicitly sets memory to 1024
    - User doesn't set timeout (None)
    - Global customize sets layers to [layer-arn]
    - Defaults provide other values
    """
    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(
            current_ctx,
            customize={
                MockComponent: {
                    "function": {"layers": ["arn:aws:lambda:layers"], "runtime": "python3.11"}
                }
            },
        )
    )

    component = MockComponent("test-component")

    result = component._customizer(
        "function",
        computed_props={
            "memory": 1024,  # Explicit
            "timeout": None,  # Not set
            "layers": None,  # Not set (but global has it)
            "runtime": None,  # Not set (but global has it)
            "handler": "index.main",  # Explicit
        },
        default_props={
            "memory": 128,
            "timeout": 30,
            "layers": None,
            "runtime": "python3.12",
            "handler": "unknown",
        },
    )

    # Explicit values win, None values use global customize or defaults
    assert result == {
        "memory": 1024,  # Explicit
        "timeout": 30,  # None → default
        "layers": ["arn:aws:lambda:layers"],  # None → global customize
        "runtime": "python3.11",  # None → global customize
        "handler": "index.main",  # Explicit
    }


def test_customizer_per_instance_customize_overrides_all(clear_registry):
    """Per-instance customize takes precedence over everything.

    Precedence: per-instance > computed_props > global customize > defaults
    """
    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(current_ctx, customize={MockComponent: {"function": {"memory": 512}}})
    )

    component = MockComponent("test-component", customize={"function": {"memory": 2048}})

    result = component._customizer(
        "function",
        computed_props={"memory": 1024, "timeout": None},
        default_props={"memory": 128, "timeout": 30},
    )

    # Per-instance customize wins: 2048 > 1024 > 512 > 128
    assert result == {"memory": 2048, "timeout": 30}


def test_customizer_global_callable_customizes_defaults(clear_registry):
    """Global callable customize receives defaults and can modify them.

    The callable is applied to effective_defaults (default_props + global customize).
    """
    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(
            current_ctx,
            customize={
                MockComponent: {
                    "function": lambda props: {
                        **props,
                        # Double the memory if it's None, otherwise double the provided default
                        "memory": (128 if props.get("memory") is None else props.get("memory"))
                        * 2,
                    }
                }
            },
        )
    )

    component = MockComponent("test-component")

    result = component._customizer(
        "function",
        computed_props={"memory": None, "timeout": None},
        default_props={"memory": 128, "timeout": 30},
    )

    # Global callable doubles the default memory: 128 * 2 = 256
    assert result == {"memory": 256, "timeout": 30}


def test_customizer_local_callable_overrides_global_for_explicit_values(clear_registry):
    """Local callable receives computed_props + defaults + global customize.

    Local callable can override computed_props (explicit values).
    """

    def local_customize(props):
        # Local callable completely replaces props
        return {"memory": 4096, "timeout": props.get("timeout", 60)}

    current_ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        replace(current_ctx, customize={MockComponent: {"function": {"memory": 512}}})
    )

    component = MockComponent("test-component", customize={"function": local_customize})

    result = component._customizer(
        "function",
        computed_props={"memory": 1024, "timeout": None},
        default_props={"memory": 128, "timeout": 30},
    )

    # Local callable output: 4096 is highest precedence
    assert result == {"memory": 4096, "timeout": 30}


def test_customizer_multiple_resources_independent_customization(clear_registry):
    """Each resource in computed_props/default_props is customized independently.

    Simulates Function component creating multiple resources:
    function, role, policy, function_url.
    """
    component = MockComponent("test-component")

    # Function resource
    function_result = component._customizer(
        "function",
        computed_props={"memory": 512, "timeout": None},
        default_props={"memory": 128, "timeout": 30},
    )

    # Role resource
    role_result = component._customizer(
        "role",
        computed_props={"path": "/", "assume_role_policy": "policy-doc"},
        default_props={"path": "/service-role/"},
    )

    # Function result
    assert function_result == {"memory": 512, "timeout": 30}

    # Role result (all explicit)
    assert role_result == {"path": "/", "assume_role_policy": "policy-doc"}


def test_customizer_empty_computed_props_all_defaults(clear_registry):
    """Empty computed_props dict means all values should come from defaults.

    This happens when no explicit values are set in component constructor.
    """
    component = MockComponent("test-component")

    result = component._customizer(
        "function",
        computed_props={},  # Empty - no explicit values
        default_props={"memory": 128, "timeout": 30, "runtime": "python3.12"},
    )

    # All from defaults
    assert result == {"memory": 128, "timeout": 30, "runtime": "python3.12"}


def test_customizer_computed_props_all_explicit_no_defaults(clear_registry):
    """When all computed_props are explicit, defaults are ignored.

    This happens when component is fully configured by the user.
    """
    component = MockComponent("test-component")

    result = component._customizer(
        "function",
        computed_props={"memory": 512, "timeout": 60, "runtime": "python3.11"},
        default_props={"memory": 128, "timeout": 30, "runtime": "python3.12"},
    )

    # All from computed_props
    assert result == {"memory": 512, "timeout": 60, "runtime": "python3.11"}
