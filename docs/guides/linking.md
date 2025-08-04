# Linking in Stelvio

Linking in Stelvio is a powerful concept that automates IAM permission management and environment variable configuration between AWS resources.

## What is a Link?

A Link represents a connection between two resources in your infrastructure. It defines:

1. **Permissions**: The IAM policies that allow one resource to interact with another (such as a Lambda function accessing a DynamoDB table)
2. **Properties**: Key-value pairs that one resource shares with another (like environment variables passed to a Lambda function)

Links make it easy to establish secure, properly configured connections between resources without manually setting up complex IAM policies or environment configurations.

## How Linking Works

Resources that can be linked implement the `Linkable` protocol, which requires a `link()` method that returns a `Link` object. This object contains default permissions and properties appropriate for that resource type.

## Using Links

### Basic Linking

```python
from stelvio.aws.dynamo_db import AttributeType, DynamoTable
from stelvio.aws.function import Function

# Create a DynamoDB table
table = DynamoTable(
    name="todos",
    fields={"username": AttributeType.STRING},
    partition_key="username"
)

# Link the table to a Lambda function
fn = Function(
   handler="users/handler.process",
   links=[table]  # Link the table to the function
)
```

When you link a DynamoDB table to a Lambda function, Stelvio automatically:
1. Creates IAM permissions allowing the Lambda to perform operations on the table
2. Passes the table ARN and name as environment variables to the Lambda

### Linking with API Routes

You can also link resources to API routes:

```python
from stelvio.aws.api_gateway import Api

api = Api("todo-api")
api.route("POST", "/todos", handler="functions/todos.post", links=[table])
```

### Creating Custom Links

You can also create standalone links with custom properties and permissions:

```python
from stelvio.link import Link
from stelvio.aws.permission import AwsPermission

# Create a custom link with specific properties
custom_link = Link(
    name="custom-config",
    properties={"api_url": "https://example.com/api", "timeout": "30"},
    permissions=None
)

# Link with specific permissions to an S3 bucket
s3_link = Link(
    name="logs-bucket",
    properties={"bucket_name": "my-logs-bucket"},
    permissions=[
        AwsPermission(
            actions=["s3:GetObject", "s3:PutObject"],
            resources=["arn:aws:s3:::my-logs-bucket/*"]
        )
    ]
)

# Use these custom links with a function
fn = Function(
    handler="functions/process.handler",
    links=[custom_link, s3_link]
)
```

### Customizing Links

You can customize links using various methods which all return a new Link instance (the original link remains unchanged):

- `with_properties()` - Replace all properties
- `with_permissions()` - Replace all permissions  
- `add_properties()` - Add to existing properties
- `add_permissions()` - Add to existing permissions
- `remove_properties()` - Remove specific properties

Example:

```python
# Create a read-only link to the table (creates a new Link object)
read_only_link = table.link().with_permissions(
    AwsPermission(
        actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"],
        resources=[table.arn]
    )
)

# Link with custom properties (creates a new Link object)
fn = Function(
   handler="users/handler.process",
   links=[table.link().add_properties(table_region="us-west-2")]
)
```

## Overriding Default Link Creation

You can override how links are created for specific component types by providing custom link configurations to your Stelvio application:

```python
from stelvio.app import StelvioApp
from pulumi_aws.dynamodb import Table
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.link import LinkConfig
from stelvio.aws.permission import AwsPermission

# Define a custom link creation function
def read_only_dynamo_link(table: Table) -> LinkConfig:
    return LinkConfig(
        properties={"table_arn": table.arn, "table_name": table.name},
        permissions=[
            AwsPermission(
                actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"],
                resources=[table.arn]
            )
        ]
    )

# Initialize app with custom link configs
app = StelvioApp(
    name="my-app",
    link_configs={
        DynamoTable: read_only_dynamo_link  # Override default DynamoTable link creation
    }
)
```

## Next Steps

Now that you understand linking, you might want to explore:

- [Working with Lambda Functions](lambda.md) - Learn more about how to work with Lambda functions
- [Working with API Gateway](api-gateway.md) - Learn how to create APIs
- [Working with DynamoDB](dynamo-db.md) - Learn how to create DynamoDB tables
- [Project Structure](project-structure.md) - Discover patterns for organizing your Stelvio applications
