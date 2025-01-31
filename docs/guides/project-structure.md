# Project Structure

This guide explains how to structure your Stelvio project and how Stelvio finds and loads your infrastructure code.

## Important Considerations

### Installation
Since Stelvio (and its dependency Pulumi) are used for infrastructure deployment rather than application runtime, you might want to install it as a development or CI dependency:

```toml
# pyproject.toml
[tool.poetry.group.dev.dependencies]
stelvio = "^0.1.0a1"

# or in Pipfile
[dev-packages]
stelvio = ">=0.1.0a1"
```

### Entry Point
Currently, Stelvio requires a `__main__.py` file in your project root due to Pulumi CLI requirements. This will be removed once Stelvio has its own CLI:

```python
# __main__.py
from importlib import import_module

import_module("stlv_app")  # Loads stlv_app.py which contains StelvioApp configuration
```

### Critical: StelvioApp Initialization

StelvioApp must be created before any Stelvio resources are imported. You have two options:

#### Option 1: Direct Imports

```python
# stlv_app.py
from stelvio import StelvioApp

app = StelvioApp(
    name="my-project",
    stage="dev"
)

# Now you can import and use resources
from my_project.infra.tables import users_table
from my_project.infra.api import api_gateway

# Run your infrastructure code
app.run()
```

#### Option 2: Using Modules List

If you prefer not to manage imports manually, you can let Stelvio find and load your resources:

```python
# stlv_app.py
from stelvio import StelvioApp

app = StelvioApp(
    name="my-project",
    modules=[
        # Explicit modules first (if order matters)
        "infra.base",
        "infra.auth",
        # Then patterns to find the rest
        "*/infra/*.py",
        "**/*_stlv.py"
    ]
)

# Run your infrastructure code
app.run()
```

❌ In either case, don't import resources before StelvioApp:

```python
# stlv_app.py
from my_project.infra.tables import users_table  # Don't import resources before StelvioApp!
from stelvio import StelvioApp

app = StelvioApp(
    name="my-project",
)
```

### Importing Between Infrastructure Files

Of course, you can and import between your infrastructure files:

```python
# infra/functions.py
from stelvio.aws.function import Function
from infra.storage.users import users_table  # Importing from other infrastructure files

users_func = Function(
    name="process-users",
    handler='functions/users.process',
    links=[users_table]
)
```

This allows you to organize your infrastructure in different files.

## Project Organization

Stelvio is flexible about how you organize your code. Here are some common patterns:

### Separate Infrastructure Folder
```
my-project/
├── __main__.py
├── stlv_app.py
├── infrastructure/
│   ├── base.py
│   ├── storage.py
│   └── api.py
└── app/
    └── *.py
```

### Co-located with Features
```
my-project/
├── __main__.py
├── stlv_app.py
└── services/
    ├── users/
    │   ├── infra/
    │   │   ├── tables.py
    │   │   └── api.py
    │   └── handler.py
    └── orders/
        ├── infra/
        │   └── queues.py
        └── handler.py
```

### Using File Patterns
```
my-project/
├── __main__.py
├── stlv_app.py
└── services/
    ├── users/
    │   ├── stlv.py     # Any file names works as far as it's defined in modules
    │   └── handler.py
    └── orders/
        └── stlv.py
        └── handler.py
```

## Project Organization Tips

To avoid conflicts with your application code and frameworks:

1. Keep infrastructure code separate from application code
2. Be mindful of framework auto-loaders that might scan all .py files
3. Consider adding infrastructure paths to framework exclude lists


## Next Steps

Now that you understand project structure in Stelvio, you might want to explore:

- [Working with Lambda Functions](lambda.md) - Learn more about how to work with Lambda functions
- [Working with API Gateway](api-gateway.md) - Learn how to create APIs
- [Working with DynamoDB](dynamo-db.md) - Learn how to create DynamoDB table
- [Linking](linking.md) - Learn how linking automates IAM, permissions, envars and more