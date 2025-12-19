# Working with Lambda Functions in Stelvio

Stelvio simplifies AWS Lambda function creation and configuration by automating
packaging, dependency management, and secure resource linking, letting you focus
on your function's logic. In this guide, we'll explore how to organize your
Lambda functions and manage their dependencies using Stelvio.

## Understanding Lambda Functions Organization

When creating Lambda functions in Stelvio, you have two main approaches for
organizing
your code:

1. Single-file functions
2. Folder-based functions

Each approach has its own use cases and benefits.

### Single-File Lambda Functions

Single-file functions are perfect for simple, focused tasks that don't require
additional code files. Here's how to create one:

```python
# functions/simple.py
def handler(event, context):
    return {
        "statusCode": 200,
        "body": "Hello from Lambda!"
    }


# In your infrastructure code
fn = Function(handler="functions/simple.handler")
```

Key characteristics of single-file functions:

- One Python file contains all the function code
- **Cannot** import from other files in the same directory
- Perfect for simple, focused tasks
- Automatically packaged by Stelvio

### Folder-Based Lambda Functions

For more complex scenarios where you need to split your code across multiple
files, use folder-based functions:

```python
# functions/
# └── users/
#     ├── handler.py         # Main function code
#     ├── database.py        # Database operations
#     └── validation.py      # Input validation

# In your infrastructure code
fn = Function(
    name="my-user-processor",
    folder="functions/users",  # folder of the function
    handler="handler.process"  # Relative to folder directory
)
```

Key characteristics of folder-based functions:

- Can split code across multiple files (within its folder)
- Can import between files in the folder
- All files in the folder are packaged together
- Perfect for complex functions with shared code

## Function Configuration

You can configure your Lambda functions by specifying different parameters to
Function
class:

```python
from stelvio.aws.function import Function

fn = Function(
    name="user-processor-config",
    folder="users",  # For folder-based Lambda
    handler="handler.process",  # Handler function relative to folder
    memory=512,  # Memory in MB
    timeout=30,  # Timeout in seconds
)
```

For simpler cases, when you're happy with defaults, you can just provide the
handler:

```python
from stelvio.aws.function import Function

fn = Function(handler="simple.handler")
```

## Linking and Environment Variables

When you link other components to your Lambda function, Stelvio automatically:

1. Generates the necessary IAM permissions
2. Creates lambda environment variables for component access
3. Generates a type-safe component access python file

Here's how it works:

```python
# Create component
from stelvio.aws.dynamo import AttributeType, DynamoTable
from stelvio.aws.function import Function

table = DynamoTable(
    name="users",
    fields={
        "user_id": AttributeType.STRING
    },
    partition_key="user_id"
)

# Link to Lambda
fn = Function(
    handler="users/handler.process",
    links=[table]  # Link the table to the function
)
```

Stelvio generates a stlv_resources.py file in your Lambda's directory (when you
deploy or preview):

```python
# Generated stlv_resources.py
import os
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class UsersResource:
    @property
    def table_arn(self) -> str:
        return os.getenv("STLV_USERS_TABLE_ARN")

    @property
    def table_name(self) -> str:
        return os.getenv("STLV_USERS_TABLE_NAME")


@dataclass(frozen=True)
class LinkedResources:
    users: Final[UsersResource] = UsersResource()


Resources: Final = LinkedResources()
```

!!! info "Generation Timing"
    The `stlv_resources.py` file is generated or updated in your function's source
    directory whenever you run `stlv diff` or `stlv deploy`.

You can then use these resources in your Lambda code with full IDE support:

```python
from stlv_resources import Resources


def handler(event, context):
    table_name = Resources.users.table_name
    # Use table_name with boto3...
```

This provides:

- Type-safe access to resource properties
- IDE completion for available resources

## Best Practices

1. Start Simple:
    - Use single-file functions for simple tasks.
    - Move to folder-based organization (using the `folder` parameter) when your
      function grows or
      needs multiple files or specific dependencies.

2. Dependency Management:
    - Use the `requirements` parameter
      to [manage dependencies](lambda.md#managing-dependencies)
    - Leverage `-r` in `requirements.txt` to share common dependencies.
    - Keep dependencies minimal to reduce package size and cold starts.
    - Stelvio handles the complexities of platform-specific installation and
      caching.

3. Resource Access:
    - Use the generated `stlv_resources.Resources` object for type-safe resource
      access.
    - Keep your functions focused on business logic
    - Let Stelvio manage IAM permissions through linking

4. Function Organization:
    - Keep related code together in folder-based functions
    - Use clear file names and structure

## Resource Naming

Each Lambda function creates several AWS resources (the function itself, IAM role, IAM policy). These follow Stelvio's standard naming pattern with automatic truncation for long names.

See [Resource Naming](environments.md#resource-naming) for details on naming patterns and how Stelvio handles AWS length limits.

## Managing Dependencies

Stelvio provides a flexible and automated system for managing Python
dependencies for your Lambda
functions. It handles finding, installing (using platform-specific settings),
caching, and
packaging dependencies, ensuring they work correctly in the AWS Lambda
environment.

You control dependency handling using the `requirements` parameter when defining
a `Function`:

```python hl_lines="6"
from stelvio.aws.function import Function

fn = Function(
    name="my-function",
    handler="functions/users/to/handler.handler_fn",
    requirements=...  # Configure dependencies here
)
```

Let's explore the different ways you can configure the `requirements` parameter.

### Default Behavior

If you don't specify the `requirements` parameter (or set it to `None`), Stelvio
automatically looks for a file named `requirements.txt` relative to your
function's code:

* **For Single-File Functions:** It looks in the same directory as the handler
  file.
  ```text title="Project Structure" hl_lines="4"
  functions/
  ├── users.py
  ├── orders.py
  └── requirements.txt  # <-- Stelvio finds this automatically
  ```
  ```python
  # Stelvio automatically uses functions/requirements.txt
  fn = Function(
      name="users",
      handler="functions/users.get",
      # requirements=None (default)
  )
  ```
  
    !!! warning "requirements.txt is shared among lambda functions within the same folder"
        In the example above function using orders.py and function using 
        users.py would have same dependencies (defined in requirements.txt)
    
* **For Folder-Based Functions:** It looks inside the function's source folder (
  specified by the
  `folder` parameter or derived from the `handler` if using
  `folder::file.handler` syntax).
  ```text title="Project Structure" hl_lines="5"
  functions/
  └── my_complex_function/
      ├── handler.py
      ├── utils.py
      └── requirements.txt  # <-- Stelvio finds this automatically
  ```
  ```python
  # Stelvio automatically uses functions/my_complex_function/requirements.txt
  fn_folder = Function(
      name="complex-fn-folder",
      folder="functions/my_complex_function", # Specify the folder
      handler="handler.process",              # Handler relative to the folder
      # requirements=None (default)
  )
  ```

If no `requirements.txt` file is found in the expected location, Stelvio assumes
there are no
dependencies to install for that function.

### Explicit File Path

You can tell Stelvio to use a specific requirements file by providing its path
as a string. The path should be relative to your project's root directory 
(where your`stlv_app.py`).

```python
# Use a shared requirements file (path relative to project root)
fn = Function(
    name="users",
    handler="functions/users.get",
    requirements="common/base_requirements.txt"
)

```

!!! note "File Not Found"
    If you provide a path string and the file does not exist at that location (
    relative to the project root), Stelvio will raise an error during deployment.

### Inline List

If you prefer, you can provide requirements as a list of strings when defining
a function.
Each string should be a valid requirement specifier, just like a line in a
`requirements.txt` file.

```python title="stlv_app.py"
fn = Function(
    name="users",
    handler="functions/users.get",
    requirements=[
        "requests==2.31.0",  # Specific version
        "boto3",  # Latest compatible version
        "pydantic>=2.0,<3.0"  # Version range
    ]
)
```

You can include any valid pip requirement string, including version specifiers,
VCS URLs (`git+...`), etc.

### Disable Dependency Handling

If you want to explicitly prevent Stelvio from looking for or installing any
dependencies, even if a default `requirements.txt` file exists, set 
`requirements` to `False` (or an empty list `[]`). This is useful if your Lambda
function doesn't need dependencies specified in requirements.txt file shared in
same folder as other functions or if you are managing dependencies
through other means (like Lambda Layers).

```python title="stlv_app.py"
# Disable even if functions/requirements.txt exists
fn = Function(
    name="no-deps-function",
    handler="functions/users.get",
    requirements=False
)

# An empty list also disables dependency handling
fn_other = Function(
    name="other-no-deps",
    handler="functions/users.get",
    requirements=[]
)
```

### How Installation Works

When dependencies need to be installed (i.e., not disabled and requirements are
found/provided),
Stelvio performs the following steps automatically:

1. **Installer Selection:** It prefers `uv` (if installed and found in `PATH`)
   for its speed & global caching, otherwise it falls back to `pip`.
2. **Platform Targeting:** It runs the installer with flags specific to your
   function's configured
   architecture (`x86_64` or `arm64`) and Python runtime (e.g., `3.12`),
   ensuring compatibility
   with the AWS Lambda execution environment. Example flags used internally:
    * `--platform manylinux2014_x86_64` (or `aarch64`)
    * `--python-version 3.12`
    * `--implementation cp` (for pip)
    * `--only-binary=:all:` (to prefer pre-compiled wheels, crucial for Lambda
      compatibility)
3. **Caching:** Dependencies are installed into a local cache directory within
   your project (`.stelvio/lambda_dependencies/`). The cache key is
   intelligently generated based on the requirements content, the target
   architecture, and the target Python version. The "content" part of the key
   is derived from a normalized representation of your requirements: Stelvio
   strips whitespace and comments, sorts the lines, and crucially, resolves any
   paths in `-r` or `-c` flags to be relative to your project root before
   hashing. This ensures that trivial formatting differences or different ways
   of specifying the same relative path don't break the cache. Subsequent
   deployments with identical normalized requirements and configuration will
   reuse the cache, significantly speeding up the deployment process.
4. **Packaging:** The installed dependencies retrieved from the cache are
   packaged alongside your function code into the final deployment archive 
   (`.zip` file) uploaded to AWS Lambda.

### Sharing Dependencies via `-r`

If you are using file-based requirements (`requirements=None` or
`requirements="path/to/file.txt"`), you can leverage pip's standard `-r` flag
within your
`requirements.txt` files to include dependencies from other files. This is
useful for sharing
common dependencies across multiple functions.

```text title="common/base_requirements.txt"
# Common libraries used across the project
boto3>=1.34
pydantic>=2.5
```

```text title="functions/orders/requirements.txt"
# Include common dependencies (path relative to the directory containing this file)
-r ../../common/base_requirements.txt

# Specific dependencies for this function
stripe>=8.0
```

```python
order_fn = Function(
    name="order-processor",
    handler="functions/orders::handler.process",
    # Stelvio will find functions/orders/requirements.txt by default
)
```

Stelvio's caching mechanism is aware of these `-r` (and `-c` for constraints)
references. If the
content of `common/base_requirements.txt` changes in the example above, the
cache key for the
`order-processor` function will also change, correctly triggering a
re-installation of its
dependencies on the next deployment.

### Important Notes

*   **Package Size:** Be mindful of the total size of your dependencies. Large
    dependencies increase the size of your Lambda deployment package, which can
    negatively impact cold start times and potentially hit AWS deployment size
    limits. Keep your requirements lists focused on what's truly needed.
*   **No Usage Analysis:** Stelvio installs *all* packages listed in the resolved
    requirements file(s); it does not analyze your code to determine which
    imports are actually used.
*   **Binary Compatibility:** Stelvio dependencies management works only with
    pre-compiled binary wheels. If a package (or one of its transitive
    dependencies) requires compilation during installation and doesn't offer a
    compatible wheel for the Lambda Linux environment (`manylinux...`), the
    installation step might fail. Support for such packages is planned; please
    raise an issue on the project's repository if this feature is important to you.
*   **Cache Management:** The dependency cache is stored locally in
    `.stelvio/lambda_dependencies/` (with a separate `layers/` subdirectory for layer dependencies).
    While Stelvio automatically reuses cached dependencies, you might want to clear this directory
    (`rm -rf .stelvio`) if you suspect caching issues or want to force a completely clean installation.
    Stelvio also includes logic to automatically clean up stale cache directories
    that haven't been used in the most recent deployment.

## Sharing Code and Dependencies with Lambda Layers

Lambda Layers provide a mechanism to package libraries, custom runtimes, or other dependencies
that you want to share across multiple Lambda functions. Stelvio simplifies the creation and
management of layers through the `Layer` component.

### Creating a Layer

You define a layer using the `stelvio.aws.layer.Layer` component:

```python
from stelvio.aws.layer import Layer
from stelvio.aws.function import Function

# Layer containing shared utility code
utils_layer = Layer(
    name="common-utils",
    code="src/common_utils",  # Path to the directory with your code
)

# Layer containing specific dependencies
libs_layer = Layer(
    name="data-libs",
    requirements="requirements/data_processing.txt" # Path to requirements file
)

# Layer with both code and dependencies
combined_layer = Layer(
    name="shared-logic-and-deps",
    code="src/shared_logic",
    requirements=[  # Or provide requirements inline
        "pandas==2.1.0",
        "numpy>=1.25"
    ],
    runtime="python3.11", # Optional: Specify runtime/architecture if needed
    architecture="arm64"  # Defaults are usually sufficient
)
```

Key `Layer` parameters:

*   `name`: A unique logical name for the layer within your Stelvio application.
*   `code`: (Optional) Path relative to your project root containing the Python code for the layer.
    Stelvio packages the directory specified by the *last part* of this path (e.g., `common_utils`
    from `src/common_utils`) under `python/` in the layer archive (resulting in
    `python/common_utils/...`). This allows standard Python imports like `from common_utils import ...`.
*   `requirements`: (Optional) Specifies Python package dependencies. Accepts:
    *   A path string (relative to project root) to a `requirements.txt` file.
    *   A list of requirement strings (e.g., `["requests", "boto3"]`).
    *   `None` (default): No dependencies are installed for this layer. **Unlike functions, there is no automatic lookup for `requirements.txt` when `requirements` is `None`.**
*   `runtime`, `architecture`: (Optional) Specify the compatible runtime and architecture. Defaults
    to the project-wide defaults (`python3.12`, `x86_64`). Layers must be compatible with the
    functions that use them (Stelvio performs this check).

!!! warning "Layer Content Required"
    A `Layer` must be configured with either the `code` parameter, the `requirements` parameter,
    or both. Defining a layer with neither will result in an error.

### How Packaging Works

Stelvio handles the packaging details according to AWS Lambda Layer standards:

1.  **Code (`code`):** If a `code` path is provided (e.g., `src/common_utils`), Stelvio packages
    the directory specified by the *last part* of the path (e.g., `common_utils`) into a `python/`
    directory within the layer's archive (`python/common_utils/...`). This structure allows
    standard Python imports within your Lambda functions (e.g., `from common_utils import ...`).
2.  **Dependencies (`requirements`):** If `requirements` are specified (as a path string or list),
    Stelvio uses the same dependency resolution and installation logic as for functions
    (preferring `uv`, falling back to `pip`, targeting the specified runtime/architecture).
    Dependencies are installed into `python/lib/pythonX.Y/site-packages/` within the layer archive.
3.  **Caching:** Installed layer dependencies are cached separately in
    `.stelvio/lambda_dependencies/layers/` to avoid conflicts with function caches. The cache
    key considers the requirements content, runtime, and architecture.
4.  **Versioning:** Stelvio creates a Pulumi `AssetArchive` from the packaged code and dependencies.
    Pulumi calculates a hash of this archive. A new AWS `LayerVersion` resource is created only
    if this hash changes (meaning the code or resolved dependencies have changed).

### Using Layers with Functions

To use one or more layers with a function, pass a list of `Layer` component instances to the
`layers` parameter of the `Function`:

```python
# Assume utils_layer and libs_layer are defined as above

data_processor_fn = Function(
    name="data-processor",
    handler="functions/processing.handler",
    layers=[utils_layer, libs_layer] # Attach the layers
)

another_fn = Function(
    name="reporter",
    handler="functions/reporting.handler",
    layers=[utils_layer] # Reuse the utils layer
)
```

Stelvio automatically retrieves the correct `LayerVersion` ARN (Amazon Resource Name) for each
layer and configures the Lambda function to use them.

!!! info "Validation and Compatibility Checks"

    When you define a function with layers, Stelvio performs several validation checks early on (during configuration processing, before deployment):

    *   **Limit Check:** Verifies that no more than 5 layers are attached (the AWS limit).
    *   **Compatibility Check:** Compares the function's effective `runtime` and `architecture` (considering defaults if not explicitly set) against those of *each* attached layer. If any mismatch is found (e.g., attaching an `arm64` layer to an `x86_64` function, or a `python3.13` layer to a `python3.12` function), Stelvio raises a clear `ValueError`, preventing deployment issues.

If a layer's content changes in a subsequent deployment, Stelvio detects the change, creates a new layer version, and updates any functions using that layer to reference the new version.

### Precedence

Dependencies included directly in a function's package take precedence over dependencies in layers.
If multiple layers provide the same package, the standard AWS layer ordering applies (later layers
in the list override earlier ones).

## Function URLs

Function URLs provide a dedicated HTTPS endpoint for your Lambda function.

```python
from stelvio.aws.function import Function

# Public: CORS enabled, no authentication
webhook = Function(
    handler="functions/webhook.handler",
    url="public"
)

# Private: IAM authentication, no CORS
internal = Function(
    handler="functions/internal.handler",
    url="private"
)

# Access the URL
webhook.url  # Output[str]
```

**Configuration**

For more control, use `FunctionUrlConfig`, a dict, or mix both:

```python
from stelvio.aws.function import Function, FunctionUrlConfig
from stelvio.aws.cors import CorsConfig

# Using FunctionUrlConfig
fn = Function(
    handler="functions/api.handler",
    url=FunctionUrlConfig(
        auth=None,
        cors=CorsConfig(
            allow_origins=["https://example.com"],
            allow_methods=["GET", "POST"]
        ),
        streaming=False
    )
)

# Or use a dict
fn = Function(
    handler="functions/api.handler",
    url={
        "auth": None,
        "cors": {
            "allow_origins": ["https://example.com"],
            "allow_methods": ["GET", "POST"]
        },
        "streaming": False
    }
)
```

**Options**:

- `auth` - `None` (public), `"iam"` (requires AWS credentials), or `"default"` (same as `None`)
- `cors` - `True` (permissive), `False`/`None` (disabled), or `CorsConfig` for granular control
- `streaming` - `False` (buffered, max 6 MB), or `True` (streaming, max 200 MB)

```python
# Enable permissive CORS
url={"auth": None, "cors": True}

# Granular CORS control
url={
    "cors": CorsConfig(
        allow_origins=["https://app.example.com"],
        allow_methods=["GET", "POST"],
        allow_credentials=True,
        max_age=3600
    )
}
```

AWS handles CORS preflight (`OPTIONS`) requests automatically.

!!! note "When to Use"
    Use Function URLs for webhooks, simple APIs, or internal endpoints.

    Use API Gateway when you need custom domains, rate limiting, caching, or complex routing.

!!! warning "IAM Auth"
    `auth="iam"` requires AWS Signature Version 4. Use for service-to-service calls or with CloudFront + OAC. Not suitable for browser apps.

### Exposing a function along with other resources

If you want to expose a function along with other resources, such as an API Gateway, you can use the [`Router` component](/guides/cloudfront-router/).

## Next Steps

Now that you understand Lambda functions and layers in Stelvio, you might want to explore:

- [Working with API Gateway](api-gateway.md) - Learn how to create APIs
- [Working with DynamoDB](dynamo-db.md) - Learn how to create DynamoDB tables
- [Linking](linking.md) - Learn how linking automates IAM, permissions, envars
  and more
- [Project Structure](project-structure.md) - Discover patterns for organizing
  your Stelvio
  applications
