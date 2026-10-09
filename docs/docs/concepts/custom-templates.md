# Custom templates

A template is a starter kit for a new Stelvio application. It supplies an application
blueprint: infrastructure definitions, handler code, dependencies, tests, and instructions.
Teams copy it when starting a project, then adapt the resulting files to their application.

## When to create a template

Use a template when teams repeatedly start with the same application structure. For example,
an HTTP service might need a health endpoint, a worker application might need a queue and
handler, and an internal tool might need a standard deployment workflow. A template gives
each project a working starting point without asking its developers to assemble those
pieces from scratch.

A template is a starting point you copy once. It is not a dependency that existing
applications upgrade.

For an internal developer platform, a platform team can maintain a small catalog of
templates for supported application types. Application teams choose a blueprint and own
the generated project. The platform team maintains the starter. Updates to a template
apply to newly initialized projects. Existing applications do not automatically receive
those changes.

## How Stelvio uses templates

Run `stlv init --template` in the directory where the new application should live. A custom
template comes from a GitHub repository, optionally selecting a subdirectory and a branch
or tag:

```bash
mkdir orders-service
cd orders-service
uvx --from stelvio==0.10.0b6 stlv init --template gh:YOUR_ORG/app-templates@v1.0.0/http-service
```

This example uses uv to run Stelvio without first creating project files. If you already
have the CLI installed, use `stlv init --template` with the same selector. Replace
`YOUR_ORG`, the repository, and the release tag with your own published template.

Stelvio copies the contents of `http-service` into the current directory. The template's
Git history is removed, and initialization offers to create a new Git repository when
appropriate. The files are copied as they are: Stelvio does not replace placeholders,
rename the application, install its dependencies, or run template setup hooks.

Unlike plain `stlv init`, which generates an application name from the current directory,
`stlv init --template` keeps the name written in the template's `stlv_app.py`. Include
explicit renaming instructions for consumers.

The command copies nothing and exits with a non-zero status when Git is missing, the
clone fails, the branch, tag, or subdirectory does not exist, or the selected directory
has no `stlv_app.py`.

!!! important "Start in an empty directory"
    If `stlv_app.py` already exists, initialization stops without applying the template.
    If any other file or directory from the template is already present, initialization
    stops before copying and leaves the directory unchanged.

### Template selectors

| Selector | Source |
|----------|--------|
| `base` | The `base` directory in `stelviodev/templates`, on `main`. |
| `gh:YOUR_ORG/http-service-template` | The repository root on its default branch. |
| `gh:YOUR_ORG/app-templates/http-service` | The `http-service` directory on the repository's default branch. |
| `gh:YOUR_ORG/http-service-template@v1.0.0` | The repository root at tag `v1.0.0`. |
| `gh:YOUR_ORG/app-templates@v1.0.0/http-service` | The `http-service` directory at tag `v1.0.0`. |

Omit `@` to use the repository's default branch, whether that is `main`, `master`, or
another name. Put a branch or tag after `@` to select that ref. Named branches and tags
work because Stelvio passes them to
[git clone --branch](https://git-scm.com/docs/git-clone#Documentation/git-clone.txt---branchltnamegt).
An arbitrary commit SHA is not a valid selector. Use a release tag for a repeatable
starting point and keep published tags unchanged.

Use branch and tag names without `/`. The selector treats the first slash after `@` as the
start of the subdirectory. Nested subdirectories such as `python/http-service` are
supported. The `gh:` form targets GitHub.com. Local filesystem paths, full repository
URLs, and other Git hosts are not template selectors.

## Create an application blueprint

A template needs no manifest or special template engine. Put the files of a working
Stelvio application in a repository root or subdirectory. The selected directory should
contain `stlv_app.py` directly, so it lands at the new application's root. Initialization
fails if that file is missing.

For a repository with several blueprints, use this layout:

```text
app-templates/
├── README.md
└── http-service/
    ├── README.md
    ├── pyproject.toml
    ├── uv.lock
    ├── .gitignore
    ├── stlv_app.py
    ├── functions/
    │   ├── __init__.py
    │   └── health.py
    └── tests/
        └── test_health.py
```

The repository-level README can describe the catalog. The README inside `http-service`
is copied into every generated application and should explain how to use that blueprint.
Files outside the selected directory are not copied, so each blueprint must include its
own dependencies, handlers, and instructions.

### Define the infrastructure

Create `http-service/stlv_app.py`:

```python
from stelvio.app import StelvioApp
from stelvio.aws.api_gateway import HttpApi

app = StelvioApp("http-service-starter")


@app.run
def run() -> None:
    api = HttpApi("service")
    api.route("GET", "/health", "functions/health.handler")
```

This blueprint creates an HTTP API with a Lambda-backed health endpoint. The endpoint is
public and returns no application data. Consumers can add routes and authentication as
their service grows; see the [HTTP API guide](../components/aws/http-api.md).

Keep infrastructure construction inside `@app.run`. Use portable defaults, and explain any
AWS region, profile, domain, or account-specific configuration the consumer must supply.
The minimal example uses the consumer's AWS CLI configuration and environment variables.

### Include runnable application code

Create an empty `http-service/functions/__init__.py` and add
`http-service/functions/health.py`:

```python
import json


def handler(event, context):
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"status": "ok"}),
    }
```

Include a test in `http-service/tests/test_health.py`:

```python
import json

from functions.health import handler


def test_health():
    response = handler({}, None)

    assert response["statusCode"] == 200
    assert response["headers"] == {"content-type": "application/json"}
    assert json.loads(response["body"]) == {"status": "ok"}
```

The test demonstrates expected behavior and gives consumers a working test command from
their first day. For larger blueprints, include tests for the application behavior they
are expected to extend.

### Declare dependencies and generated files

Use a `http-service/pyproject.toml` such as:

```toml
[project]
name = "http-service-starter"
version = "0.1.0"
description = "A Stelvio HTTP service starter"
readme = "README.md"
requires-python = ">=3.12"
dependencies = ["stelvio==0.10.0b6"]

[dependency-groups]
dev = ["pytest>=8,<10"]
```

This is an application, so it does not need a wheel build configuration. Choose a Stelvio
version you have tested, generate `uv.lock` by running `uv lock` inside `http-service`,
and commit it with the template. Update the version and lockfile together when maintaining
the blueprint. If the template imports another package, declare it here too and document
how to authenticate to its repository or package index.

Include a `.gitignore` inside the blueprint:

```gitignore
__pycache__/
*.pyc
.venv/
.pytest_cache/
.stelvio/
.pulumi/
dist/
*.egg-info/
```

Stelvio copies committed files from the selected template directory, including dotfiles.
A `.gitignore` prevents accidental additions to your repository. It does not remove files
already tracked there. Keep credentials, local environments, deployment state, and build
artifacts out of the template. Include workflow files only when you intend each generated
application to receive them, and document any repository secrets or permissions they need.

### Write the consumer's setup instructions

The blueprint's README should give an ordered path from initialization to a working app:

1. Rename `StelvioApp("http-service-starter")` and the project name in `pyproject.toml` to
   the new application's name. Choose the Stelvio app name before the first deployment.
   It is part of the application's infrastructure identity.
2. Configure AWS access and the intended region using the [quick start](../intro/quickstart.md).
3. Install dependencies and run tests from the generated application's root.
4. Review and deploy the infrastructure, then call the health endpoint.

After renaming and configuring AWS access, the commands are:

```bash
uv sync
uv run python -m pytest
uv run stlv diff
uv run stlv deploy
```

Use the API URL shown after deployment and request `/health`. Expect HTTP 200 and the JSON
body `{"status": "ok"}`. The commands above use a personal environment. If the blueprint
supports shared environments or CI deployment, document the configuration and explicit
environment arguments; see [environments](environments.md).

Explain the resources created, relevant costs, cleanup, and who maintains the blueprint.
Record the template repository and release tag in the generated README so teams can find
later fixes. Avoid placeholder expressions such as `{{ project_name }}` in runnable code:
Stelvio copies them literally.

## Distribute and maintain templates

Publish the repository on GitHub and give consumers the exact `stlv init --template`
command. A normal repository is sufficient. Marking it as a GitHub template repository is
not required by Stelvio. A dedicated repository works for one blueprint, while a catalog
repository can provide separate directories for several application types.

### Private repositories

Private repositories use the same selector. Stelvio invokes Git with an HTTPS GitHub URL,
so developers and CI need Git credentials that can read that repository over HTTPS.
Configure those credentials through the environment's Git credential helper before running
initialization. Do not put a token in the selector or commit it into template files.

Access to the template repository and access to private Python dependencies are separate:
copying the starter can succeed even if installing its dependencies later fails.
Check both paths from the environment your consumers will use.

### Changes after an application is created

Release templates with named tags and describe changes between releases. A template update
changes the starting point for future applications. Stelvio does not merge it into projects
already created. Give existing teams explicit migration instructions for fixes they need
to adopt. Re-running initialization in an existing application is not an update mechanism.

Shared libraries belong in the blueprint's `pyproject.toml` as ordinary dependencies.
Each generated application upgrades them on its own.

## Test a template

Test the generated application as the deliverable, not only the source blueprint directory.
The copy step, dependency installation, imports, and first deployment are all part of the
experience you are promising to consumers.

### Check the starter without deploying

For each supported Python version, initialize into a fresh directory from the exact branch
or tag and subdirectory you intend to distribute. Verify that `stlv_app.py`, handlers,
dependency files, tests, and intended dotfiles are present. Confirm that sibling blueprints
and catalog-only files are absent.

Run the README's renaming and installation steps, then run its tests. For the example above,
the handler test checks the health response.

### Verify the deployed blueprint

Before releasing, deploy a generated application into a dedicated test environment using
the README's instructions. For this example, call the deployed `/health` endpoint and assert
HTTP 200 and the parsed JSON response. A handler unit test cannot catch a missing route,
incorrect handler packaging, or broken API integration.

Give concurrent test runs unique application or environment names. Always arrange teardown
and check for leftover resources after failures. Use `stlv destroy` for the test application's
environment when finished. Include any additional preparation needed to delete resources
that hold data.

When updating the template, test initialization from the new release and check the migration
instructions for existing consumers separately. If the blueprint depends on other packages,
test the exact dependency versions recorded in the template's lockfile.
