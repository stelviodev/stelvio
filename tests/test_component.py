import pytest

from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import LinkConfig


# Mock Pulumi resource for testing
class MockResource:
    def __init__(self, name="test-resource"):
        self.name = name
        self.id = f"{name}-id"


# Concrete implementation of Component for testing
class MockComponent(Component[MockResource]):
    def __init__(self, name: str, resource: MockResource = None):
        super().__init__(name)
        self._mock_resource = resource or MockResource(name)
        # Track if _create_resource was called
        self.create_resource_called = False

    def _create_resource(self) -> MockResource:
        self.create_resource_called = True
        return self._mock_resource


@pytest.fixture
def clear_registry():
    """Clear the component registry before and after tests."""
    # Save old state
    old_instances = ComponentRegistry._instances.copy()
    old_output_pairs = ComponentRegistry._instance_output_pairs.copy()
    old_default_creators = ComponentRegistry._default_link_creators.copy()
    old_user_creators = ComponentRegistry._user_link_creators.copy()

    # Clear registries
    ComponentRegistry._instances = {}
    ComponentRegistry._instance_output_pairs = {}
    ComponentRegistry._default_link_creators = {}
    ComponentRegistry._user_link_creators = {}

    yield
    # We need to do this because otherwise we get:
    # Task was destroyed but it is pending!
    # task: <Task pending name='Task-22672' coro=<Output.__init__.<locals>.
    # is_value_known() running at ~/Library/Caches/pypoetry/virtualenvs/
    # stelvio-wXLVHIoC-py3.12/lib/python3.12/site-packages/pulumi/output.py:127>
    # wait_for=<Future pending cb=[Task.task_wakeup()]>>

    # Restore old state
    ComponentRegistry._instances = old_instances
    ComponentRegistry._instance_output_pairs = old_output_pairs
    ComponentRegistry._default_link_creators = old_default_creators
    ComponentRegistry._user_link_creators = old_user_creators


# Component base class tests


def test_component_initialization(clear_registry):
    """Test that component is initialized and registered correctly."""
    component = MockComponent("test-component")

    # Verify name property
    assert component.name == "test-component"

    # Verify it was added to the registry
    assert type(component) in ComponentRegistry._instances
    assert component in ComponentRegistry._instances[type(component)]


def test_resource_creation_and_caching(clear_registry):
    """Test resource creation and caching behavior."""
    # The component classes don't internally handle caching
    # They rely on ComponentRegistry.get_output
    # So we need to test with add_instance_output after creation

    test_resource = MockResource("test-resource")
    component = MockComponent("test-component", test_resource)

    # First access - creates the resource
    resource1 = component._resource
    assert component.create_resource_called
    assert resource1 is test_resource

    # Manually register the resource output
    ComponentRegistry.add_instance_output(component, test_resource)

    # Reset flag to test caching
    component.create_resource_called = False

    # Second access should use cached resource from registry
    resource2 = component._resource
    assert not component.create_resource_called  # Should not call create again
    assert resource2 is test_resource  # Should get same resource


def test_ensure_resource(clear_registry):
    component = MockComponent("test-component")

    # Initially, create_resource should not be called
    assert not component.create_resource_called

    # Call ensure_resource
    component._ensure_resource()

    # Verify create_resource was called
    assert component.create_resource_called


def test_resource_from_registry(clear_registry):
    """Test retrieving resource from the registry."""
    component = MockComponent("test-component")
    test_resource = MockResource("registry-resource")

    # Register the resource in the registry
    ComponentRegistry.add_instance_output(component, test_resource)

    # Access the resource - should get the one from registry
    resource = component._resource

    # Should not have called create_resource
    assert not component.create_resource_called
    assert resource is test_resource


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


def test_add_and_get_output(clear_registry):
    """Test adding and retrieving component outputs."""
    component = MockComponent("test-component")
    resource = MockResource("test-resource")

    # Add the output
    ComponentRegistry.add_instance_output(component, resource)

    # Get the output
    retrieved = ComponentRegistry.get_output(component)

    assert retrieved is resource


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


def test_link_creator_decorator(clear_registry):
    """Test that the decorator correctly registers and wraps the function."""

    # Define a test function and decorate it
    @link_config_creator(MockComponent)
    def test_creator(resource):
        return LinkConfig(properties={"name": resource.name})

    # Get the registered creator
    creator = ComponentRegistry.get_link_config_creator(MockComponent)

    # Create a mock resource
    resource = MockResource("test")

    # Test the registered function
    config = creator(resource)

    # Verify it returns expected result
    assert isinstance(config, LinkConfig)
    assert config.properties == {"name": "test"}

    # Test that the wrapper preserves function metadata
    assert creator.__name__ == test_creator.__name__
