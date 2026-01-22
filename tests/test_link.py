import pytest
from pulumi.runtime import set_mocks

from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig, Permission

from .aws.pulumi_mocks import PulumiTestMocks


class MockPermission(Permission):
    def __init__(self, id_, actions=None, resources=None):
        self.id = id_
        self.actions = actions or []
        self.resources = resources or []

    def to_provider_format(self):
        return {"id": self.id, "actions": self.actions, "resources": self.resources}

    def __eq__(self, other):
        if not isinstance(other, MockPermission):
            return False
        return self.id == other.id

    def __hash__(self):
        # Make hashable for use in sets
        return hash(self.id)


class MockResource:
    def __init__(self, name="test-resource"):
        self.name = name
        self.arn = f"arn:aws:mock:::{name}"


class MockComponent(Component[MockResource, dict], Linkable):
    def __init__(self, name):
        super().__init__(name)
        self._mock_resource = MockResource(name)

    def _create_resource(self) -> MockResource:
        return self._mock_resource

    @property
    def _resource(self) -> MockResource:
        return self._mock_resource

    def link(self) -> Link:
        """Implementation of Linkable protocol."""
        link_creator = ComponentRegistry.get_link_config_creator(type(self))
        if not link_creator:
            return Link(self.name, {}, [])

        link_config = link_creator(self._resource)
        return Link(self.name, link_config.properties, link_config.permissions)


@pytest.fixture
def clear_registry():
    """Clear the component registry before and after tests."""
    # Clear before test
    ComponentRegistry._default_link_creators = {}
    ComponentRegistry._user_link_creators = {}

    yield

    # Clear after test
    ComponentRegistry._default_link_creators = {}
    ComponentRegistry._user_link_creators = {}


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


# Link class tests


def test_link_properties():
    properties = {"key1": "value1", "key2": "value2"}
    permissions = [MockPermission("test-perm")]

    link = Link("test-link", properties, permissions)

    assert link.name == "test-link"
    assert link.properties == properties
    assert link.permissions == permissions


def test_link_method():
    link = Link("test-link", {}, [])
    assert link.link() is link


def test_with_config():
    link = Link("test-link", {"old": "value"}, [MockPermission("old")])

    new_props = {"new": "value"}
    new_perms = [MockPermission("new")]

    new_link = link.with_config(properties=new_props, permissions=new_perms)

    # Original should be unchanged
    assert link.properties == {"old": "value"}
    assert len(link.permissions) == 1
    assert link.permissions[0].id == "old"

    # New link should have new values
    assert new_link.name == "test-link"  # Name stays the same
    assert new_link.properties == new_props
    assert new_link.permissions == new_perms


def test_with_properties():
    link = Link("test-link", {"old": "value"}, [MockPermission("perm")])

    new_link = link.with_properties(new="value", another="prop")

    # Original should be unchanged
    assert link.properties == {"old": "value"}

    # New link should have new properties
    assert new_link.properties == {"new": "value", "another": "prop"}
    assert new_link.permissions == link.permissions  # Permissions unchanged


def test_with_permissions():
    perm1 = MockPermission("perm1")
    link = Link("test-link", {"prop": "value"}, [perm1])

    perm2 = MockPermission("perm2")
    perm3 = MockPermission("perm3")
    new_link = link.with_permissions(perm2, perm3)

    # Original should be unchanged
    assert len(link.permissions) == 1
    assert link.permissions[0].id == "perm1"

    # New link should have new permissions
    assert len(new_link.permissions) == 2
    assert new_link.permissions[0].id == "perm2"
    assert new_link.permissions[1].id == "perm3"
    assert new_link.properties == link.properties  # Properties unchanged


def test_add_properties():
    link = Link("test-link", {"existing": "value"}, [])

    new_link = link.add_properties(new="value", another="prop")

    # Original should be unchanged
    assert link.properties == {"existing": "value"}

    # New link should have combined properties
    assert new_link.properties == {"existing": "value", "new": "value", "another": "prop"}

    # Test with None properties
    link_none = Link("test-link", None, [])
    new_link_none = link_none.add_properties(new="value")
    assert new_link_none.properties == {"new": "value"}


def test_add_permissions():
    perm1 = MockPermission("perm1")
    link = Link("test-link", {}, [perm1])

    perm2 = MockPermission("perm2")
    perm3 = MockPermission("perm3")
    new_link = link.add_permissions(perm2, perm3)

    # Original should be unchanged
    assert len(link.permissions) == 1
    assert link.permissions[0].id == "perm1"

    # New link should have combined permissions
    assert len(new_link.permissions) == 3
    assert new_link.permissions[0].id == "perm1"
    assert new_link.permissions[1].id == "perm2"
    assert new_link.permissions[2].id == "perm3"

    # Test with None permissions
    link_none = Link("test-link", {}, None)
    new_link_none = link_none.add_permissions(perm1)
    assert len(new_link_none.permissions) == 1
    assert new_link_none.permissions[0].id == "perm1"


def test_remove_properties():
    link = Link("test-link", {"keep": "value", "remove1": "value", "remove2": "value"}, [])

    new_link = link.remove_properties("remove1", "remove2")

    # Original should be unchanged
    assert link.properties == {"keep": "value", "remove1": "value", "remove2": "value"}

    # New link should have filtered properties
    assert new_link.properties == {"keep": "value"}

    # Test with None properties
    link_none = Link("test-link", None, [])
    new_link_none = link_none.remove_properties("anything")
    assert new_link_none.properties is None

    # Test removing non-existent properties
    link_no_match = Link("test-link", {"keep": "value"}, [])
    new_link_no_match = link_no_match.remove_properties("not-there")
    assert new_link_no_match.properties == {"keep": "value"}


# Link registry tests


def test_default_link_creator(clear_registry):
    # Define a default link creator
    @link_config_creator(MockComponent)
    def default_link_creator(resource):
        return LinkConfig(
            properties={"name": resource.name, "arn": resource.arn},
            permissions=[MockPermission("default")],
        )

    # Get the registered creator
    creator = ComponentRegistry.get_link_config_creator(MockComponent)

    # Cannot check identity because decorator returns wrapped function
    # Instead test the behavior is as expected
    assert creator is not None

    # Test creating a link config
    mock_resource = MockResource("test-component")
    config = creator(mock_resource)

    assert config.properties == {"name": "test-component", "arn": "arn:aws:mock:::test-component"}
    assert len(config.permissions) == 1
    assert config.permissions[0].id == "default"


def test_user_link_creator_override(clear_registry):
    # Define a default link creator
    @link_config_creator(MockComponent)
    def default_link_creator(resource):
        return LinkConfig(properties={"default": "value"}, permissions=[MockPermission("default")])

    # Define a user link creator
    def user_link_creator(resource):
        return LinkConfig(properties={"user": "value"}, permissions=[MockPermission("user")])

    # Register the user creator
    ComponentRegistry.register_user_link_creator(MockComponent, user_link_creator)

    # Get the registered creator - should be the user one
    creator = ComponentRegistry.get_link_config_creator(MockComponent)

    # Verify it's the user function
    assert creator is user_link_creator

    # Test creating a link config
    mock_resource = MockResource()
    config = creator(mock_resource)

    assert config.properties == {"user": "value"}
    assert len(config.permissions) == 1
    assert config.permissions[0].id == "user"
