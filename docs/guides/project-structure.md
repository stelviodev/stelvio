# Project Structure

This guide explains how to structure your Stelvio project and how Stelvio finds and loads your infrastructure code.

## Important Considerations

### Installation

Since Stelvio is used for infrastructure deployment rather than application runtime, you might want to install it as a development or CI dependency:

=== "uv"

    ```bash
    # As regular dependency
    uv add stelvio
    
    # As dev dependency
    uv add --dev stelvio
    ```

=== "poetry"

    ```bash
    # As regular dependency
    poetry add stelvio
    
    # As dev dependency
    poetry add --group dev stelvio
    ```

=== "pip"

    ```bash
    # As regular dependency
    pip install stelvio
    
    # In requirements-dev.txt
    echo "stelvio" >> requirements-dev.txt
    pip install -r requirements-dev.txt
    ```

### Critical: Component Creation Order

**The Rule**: Stelvio components can only be created after the `@app.config` function runs. This happens automatically when the CLI loads your project.

#### Import Stelvio Classes Anywhere

```python
# Always fine to import Stelvio classes
from stelvio.aws.function import Function
from stelvio.aws.dynamo_db import DynamoTable
```

#### Don't Import Files with Top-Level Components

```python
# infra/tables.py - This will cause an error if imported in stlv_app.py
from stelvio.aws.dynamo_db import DynamoTable

# This creates a component at import time, before config is loaded
users_table = DynamoTable(name="users", ...)  # Error: "Stelvio context not initialized"

# stlv_app.py - This will fail
from infra.tables import users_table  # Imports file that creates components
```

You have two good solutions:

#### Solution 1: Import Functions and Call from @app.run

```python title="infra/tables.py"
from stelvio.aws.dynamo_db import DynamoTable

def create_tables():
    users_table = DynamoTable(name="users", ...)  # Works inside function
    return users_table
```

```python title="stlv_app.py"
from infra.tables import create_tables  # Fine to import function

@app.run
def run() -> None:
    users_table = create_tables()  # Works when called in run
```

#### Solution 2: Use Module Auto-Discovery

```python title="stlv_app.py"
# Using glob patterns
app = StelvioApp("my-project", modules=["infra/**/*.py"])

# Or explicit module names
app = StelvioApp("my-project", modules=["infra.tables", "infra.api", "infra.functions"])
```

```python title="infra/tables.py"
from stelvio.aws.dynamo_db import DynamoTable

users_table = DynamoTable(name="users", ...)  # Works at module level with auto-discovery
```

!!! important "Auto-Discovery Requirements"
    With auto-discovery, components **must** be created at module level (top of file) because Stelvio only imports the modules - it doesn't call any functions inside them. The timing works because Stelvio imports these files after the config is loaded.

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
- [Working with DynamoDB](dynamo-db.md) - Learn how to create DynamoDB tables
- [Linking](linking.md) - Learn how linking automates IAM, permissions, envars and more
