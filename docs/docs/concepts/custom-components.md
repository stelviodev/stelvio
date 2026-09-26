# Custom components

Stelvio's built-in components cover common application infrastructure. Your application
may need another AWS service, or your organization may want several teams to use the same
infrastructure pattern. A custom component lets you put that infrastructure behind a
Python API that you own, version, and use alongside Stelvio's built-in components.

## When to create a custom component

Custom components are useful in two common situations:

- **A required resource is missing from Stelvio.** Wrap resources from `pulumi_aws` to add
  the capability to your application without waiting for a built-in component. For
  example, you could provide a Parameter Store component or a service-specific database
  component with its networking and access configuration.
- **Several applications need the same infrastructure decisions.** Package a queue with
  a worker and dead-letter queue, or a database with your team's backup defaults. Teams
  choose the settings relevant to their applications while the package supplies the
  shared configuration and wiring.

Choose the smallest abstraction that solves the problem:

| Need | Approach |
|------|----------|
| Change a setting on a resource a built-in component already creates | Use [customization](customization.md). |
| Reuse a short combination of existing components | Start with a Python factory function that constructs and returns them. |
| Add a resource or offer a reusable unit with its own outputs, customization, and links | Write a custom component. |

You can keep a component in one application's repository initially. Move it to a shared
package when other applications need the same contract.

### Components as part of an internal developer platform

An [internal developer platform (IDP)](https://internaldeveloperplatform.org/what-is-an-internal-developer-platform/)
gives developers self-service access to the capabilities they need to deliver applications.
A shared component library can provide the infrastructure part of that experience.

For example, a platform team could maintain a `BackgroundWorker` component that creates
a queue, a worker function, and failure handling. Application teams provide a handler and
capacity settings. The platform team owns the implementation, tests, documentation, and
upgrade path. Each application deploys its own resources using a chosen package version;
sharing the Python package does not mean sharing a queue or database.

Treat the library as a product for its consuming teams: document supported use cases,
provide working examples, and identify who supports it. A component library is one part
of a platform; teams still need deployment workflows, access management, and operational
support. Defaults and customization options also do not enforce organizational policy:
requirements that must hold for every deployment need enforcement beyond library defaults.

## Write a component

A component defines its inputs in `__init__` and creates its cloud resources in
`_create_resources()`. Stelvio calls the latter when resources are needed during deployment.
The returned resource object makes useful outputs available to callers.

The example below wraps an AWS Systems Manager Parameter Store parameter for **non-secret
configuration**. It creates a standard string parameter and supports tags and resource
customization. Start with `config_parameter.py` in your application; the packaging section
shows how to move it into a shared library.

```python
from dataclasses import dataclass
from typing import TypedDict, final

import pulumi
from pulumi_aws import ssm

from stelvio.component import Component, resource_name
from stelvio.customize import Customization
from stelvio.provider import ProviderStore


@final
@dataclass(frozen=True, kw_only=True)
class ConfigParameterResources:
    parameter: ssm.Parameter


class ConfigParameterCustomizationDict(TypedDict, total=False):
    parameter: Customization[ssm.ParameterArgs]


@final
class ConfigParameter(Component[ConfigParameterResources, ConfigParameterCustomizationDict]):
    def __init__(
        self,
        name: str,
        value: str,
        *,
        tags: dict[str, str] | None = None,
        customize: ConfigParameterCustomizationDict | None = None,
        parent: pulumi.Resource | None = None,
    ):
        if not isinstance(value, str):
            raise TypeError("value must be a string")
        if not value:
            raise ValueError("value must be a non-empty string")

        super().__init__(
            ProviderStore.aws(),
            "acme:aws:ConfigParameter",
            name,
            tags=tags,
            customize=customize,
            parent=parent,
        )
        self._value = value

    def _create_resources(self) -> ConfigParameterResources:
        parameter = ssm.Parameter(
            resource_name(self.name, limit=128),
            **self._customizer(
                "parameter",
                {"type": "String", "value": self._value},
                {"tier": "Standard"},
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )
        return ConfigParameterResources(parameter=parameter)

    @property
    def parameter_name(self) -> pulumi.Output[str]:
        return self.resources.parameter.name
```

The small amount of Stelvio-specific code gives your component the same deployment
behavior as built-in components:

- `Component` supplies resource creation and the `.resources` property. Validate and
  normalize inputs in the constructor, store them there, and return the resources from
  `_create_resources()` without changing configuration on the component.
- `ProviderStore.aws()` uses the application's AWS region, credentials configuration,
  and default tags. `opts=self._resource_opts()` makes each Pulumi resource a child of
  your component so it inherits that provider and appears beneath it in infrastructure
  state. Use it on every resource you create.
- `resource_name()` includes the application and environment prefix and handles long
  names. Here, `128` is a deliberately conservative naming budget, not Parameter Store's
  maximum. Pulumi derives the physical name and adds its suffix. For other resources,
  choose a limit appropriate to that resource and its provider. See [naming](naming.md).
- `"acme:aws:ConfigParameter"` identifies the component type. Use your own namespace and
  keep the type string stable across releases, since it participates in resource identity.
- `_customizer()` applies the declared `parameter` overrides and, with `inject_tags=True`,
  component tags. It accepts computed arguments and optional defaults separately; this
  lets application-wide customization change defaults such as `tier`. The customization
  key uses Pulumi's [ParameterArgs](https://www.pulumi.com/registry/packages/aws/api-docs/ssm/parameter/#inputs)
  inputs. Overrides are shallow; see [customization](customization.md) for precedence.

The frozen resources dataclass exposes the parameter because callers may need its name,
ARN, or resource object. Keep this surface small: consumers rarely need every supporting
permission or attachment. Output properties such as `parameter_name` are conveniences for
values people frequently use. They return Pulumi `Output` values, which you can pass into
other resources; they are not plain strings available during construction.

This example deliberately accepts a plain string. A component that accepts another
resource's output should use the corresponding `pulumi.Input[...]` type and avoid trying
to validate its unresolved value as an ordinary Python value.

### Use it in an application

Create the component inside your application's `@app.run` function:

```python
import pulumi

from config_parameter import ConfigParameter
from stelvio.app import StelvioApp

app = StelvioApp("orders")


@app.run
def run() -> None:
    endpoint = ConfigParameter(
        "catalog-endpoint",
        "https://catalog.example.com",
        tags={"Team": "commerce"},
        customize={"parameter": {"description": "Catalog service endpoint"}},
    )
    pulumi.export("catalog_parameter_name", endpoint.parameter_name)
```

The normal `stlv diff` and `stlv deploy` workflow now includes your custom component.
Component names must be unique within the application, including names of components
created by a factory or another component.

### Compose existing components

For a larger abstraction, reuse built-in components where they already provide the
behavior you need. A factory can construct a `Queue`, a dead-letter `Queue`, and call
`queue.subscribe()` to attach a worker. Give each a name derived from the factory's name
so two uses of the factory do not collide.

If you wrap that combination in a `Component` class, create the children in
`_create_resources()`, return the objects consumers need, and forward tags and documented
customization options. For child constructors that support `parent`, pass `parent=self`.
Check each child's API rather than assuming every component accepts a parent argument.
Keep the wrapper's inputs focused on choices its consumers need to make.

### Make the component linkable

Add linking when a function needs to discover or access the resource. For the parameter
example, add these imports to `config_parameter.py`:

```python
from stelvio.aws.permission import AwsPermission
from stelvio.component import link_config_creator
from stelvio.link import LinkableMixin, LinkConfig
```

Add `LinkableMixin` to the existing class's bases, retaining its implementation:

```python
class ConfigParameter(
    Component[ConfigParameterResources, ConfigParameterCustomizationDict],
    LinkableMixin,
):
    ...  # Keep the constructor, resource creation, and property above.
```

Then add the default link creator below the class in the same module:

```python
@link_config_creator(ConfigParameter)
def config_parameter_link(parameter: ConfigParameter) -> LinkConfig:
    resource = parameter.resources.parameter
    return LinkConfig(
        properties={"parameter_name": resource.name},
        permissions=[
            AwsPermission(actions=["ssm:GetParameter"], resources=[resource.arn])
        ],
    )
```

Consumers can now pass the component to `Function(..., links=[endpoint])`. Stelvio supplies
the parameter name through generated resource accessors and grants the function permission
to read that parameter. The function still calls SSM to fetch its value; linking does not
fetch the value for it. Keep default permissions limited to the intended operation and
resource. A component intended for secrets needs a separate design for secret inputs,
encryption, and any required KMS permissions.

See [linking](linking.md) for using generated resource accessors and overriding permissions.

## Package and share components

A custom component is ordinary Python code. To share it, put it in a Python package in a
separate repository, which can be private:

```text
acme-infra/
├── pyproject.toml
├── README.md
├── src/
│   └── acme_infra/
│       ├── __init__.py
│       ├── py.typed
│       └── config_parameter.py
├── tests/
└── examples/
    └── parameter-app/
        └── stlv_app.py
```

Move the implementation into `src/acme_infra/config_parameter.py`. Export its public types
from `src/acme_infra/__init__.py`:

```python
from .config_parameter import (
    ConfigParameter,
    ConfigParameterCustomizationDict,
    ConfigParameterResources,
)

__all__ = [
    "ConfigParameter",
    "ConfigParameterCustomizationDict",
    "ConfigParameterResources",
]
```

An empty `py.typed` file lets consumers' type checkers use the package's annotations. A
minimal `pyproject.toml` using Hatchling is:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "acme-infra"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "stelvio==0.10.0b6",
    "pulumi==3.263.0",
    "pulumi-aws==7.47.0",
]

[tool.hatch.build.targets.wheel]
packages = ["src/acme_infra"]
```

These pins match the Stelvio version used by this example. Choose versions you have tested
together, and update them deliberately. Declare packages you import directly, with
constraints compatible with Stelvio's dependencies. Broaden supported version ranges only
after testing them, especially while Stelvio is in beta.

From a consuming application's environment, install a tagged release over SSH:

```bash
python -m pip install "acme-infra @ git+ssh://git@github.com/YOUR_ORG/acme-infra.git@v0.1.0"
```

Replace `YOUR_ORG` and create the release tag in your repository. Record the dependency in
the application's dependency configuration and lockfile; a commit SHA provides an immutable
Git reference. Alternatively, build wheels and publish versioned releases to your private
Python package index. Developers and CI need read access through their SSH configuration
or package-index credentials. Keep credentials out of dependency URLs committed to Git.

The application then imports `ConfigParameter` from `acme_infra` in place of
`config_parameter`; the rest of the application example stays the same. Installing and
importing the package needs no Stelvio plugin registration. Define classes at import time
and construct components inside the consuming application's infrastructure definition.

### Maintain a contract for consuming teams

Document inputs, defaults, created resources, outputs, link permissions, customization
keys, and relevant costs. Explain who owns deployed resources and how teams get support.

Treat changes to resource names, the component type string, parent relationships, and
storage configuration as infrastructure migrations, even when the Python API stays the
same. A package upgrade can otherwise replace a database or another stateful resource.
Publish release notes, test upgrades from the previous release, and have consumers review
`stlv diff` before deploying. Keep application upgrades explicit so teams can adopt and
validate a new release on their own schedule.

## Test custom components

Test the contract consumers rely on: construct the component, read its resources and
outputs, and exercise it from an application. Keep tests in your component package so they
can run before you publish a release.

### Fast tests without AWS

Use pytest and [Pulumi runtime mocks](https://www.pulumi.com/docs/iac/guides/testing/unit/)
to capture resource declarations without deploying. Set up mocks before constructing
components, use a fixed application name, environment, and region, and isolate tests from
local AWS credentials. Resource creation is deferred, so read `.resources` or an output
property to exercise it.

Use `@pulumi.runtime.test` to wait for resource registrations and outputs. Assertions on
resolved output values belong in an `Output.apply()` callback. If checking the inputs
recorded by your mocks, run construction inside a decorated helper and inspect the records
after that helper returns; checking earlier can miss resources still being registered.

For `ConfigParameter`, useful tests check:

- The default creates exactly one `aws:ssm/parameter:Parameter` with the supplied value,
  `String` type, and `Standard` tier. Account separately for the provider and component
  registrations in your mock recorder.
- The exported parameter name resolves to the name of that resource, and names include
  the application/environment prefix, including with long component names.
- Tags reach the parameter, documented customization changes its inputs, and misspelled
  customization keys fail clearly.
- Empty values and values of the wrong type fail at construction with a useful error.
- A linked function receives the parameter name and an IAM statement granting exactly
  `ssm:GetParameter` on its ARN. Parse policy JSON and compare actions and resources.

For components with several resources, also check their relationships: for example, that
an event source points to the intended queue and worker. Assert resource types and relevant
inputs, not just counts or the presence of some text in serialized JSON. Exercise validation
through the component constructor rather than testing private validation helpers.

Stelvio's [unit-testing guide](../contributing/unit-tests.md) explains these patterns in
more depth. Its fixtures and mock helpers belong to Stelvio's own test suite; they are not
an installed testing SDK. A separate package needs its own fixtures for application context
and test isolation. Keep any version-specific setup in one test support module, or run
isolated test processes, so application context and registered component names do not leak
between tests. Keep that setup out of your library's production code.

### Tests against real AWS

Mocks verify what your component declares. They cannot establish that AWS accepts the
configuration or that a deployed function has working access. Maintain a small example
Stelvio app that installs your package and deploy it into a dedicated test environment.

For the parameter example, deploy the parameter and a linked function, invoke the function
to read it through SSM, and assert the exact returned value. This tests naming, deployment,
link properties, and permissions together. For an event-driven component, send an event
and poll for its expected result with a bounded timeout. A successful deployment alone
does not verify behavior.

Use unique environment names for concurrent runs. Arrange cleanup even after test failure,
and check for leftover resources. Read resource configuration through AWS APIs when the
property has no directly observable behavior. The [integration-testing guide](../contributing/integration-tests.md)
has further examples; its deployment harness also belongs to Stelvio's repository, so a
separate package should own its test-app deployment and cleanup workflow.

### Test the release, including upgrades

Run fast tests on each change. Before releasing, build the package, install the wheel in a
clean example application, and run the AWS scenario tests using that installed artifact.
This catches missing modules, missing exports, and dependency conflicts that tests against
the source checkout can hide.

Also deploy the previous package version, upgrade it, review the infrastructure diff, and
deploy again. Verify that existing data and resource identities survive changes intended
to be non-destructive. Test the Python and Stelvio versions you claim to support, and keep
an intentional behavior change in the tests and release notes together.
