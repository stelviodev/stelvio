# Working with Lambda Functions in Stelvio

Stelvio provides an easy way to create and configure AWS Lambda functions. In this 
guide, we'll explore how to organize your Lambda functions and manage their 
dependencies.

## Understanding Lambda Organization

When creating Lambda functions in Stelvio, you have two main approaches for organizing 
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

For more complex scenarios where you need to split your code across multiple files, 
use folder-based functions:

```python
# functions/
# └── users/
#     ├── handler.py         # Main function code
#     ├── database.py        # Database operations
#     └── validation.py      # Input validation

# In your infrastructure code
fn = Function(
    folder="functions/users",     # folder of the function
    handler="handler.process"  # Relative to src directory
)
```

Key characteristics of folder-based functions:

- Can split code across multiple files
- Can import between files in the folder
- All files in the folder are packaged together
- Perfect for complex functions with shared code

## Function Configuration

You can configure your Lambda functions by specifying different parameters to Function 
class:

```python
from  stelvio.aws.function import Function

fn = Function(
    folder="users",               # For folder-based Lambda
    handler="handler.process", # Handler function
    memory=512,          # Memory in MB
    timeout=30,               # Timeout in seconds
)
```

For simpler cases, when you're happy with defaults, you can just provide the handler:
```python
from  stelvio.aws.function import Function

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
from  stelvio.aws.function import Function

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

Stelvio generates a stlv_resources.py file in your Lambda's directory:

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
     - Use single-file functions for simple tasks 
     - Move to folder-based organization when your function gets bigger or needs special dependencies.

2. ~~Dependency Management:~~
     - ~~Keep requirements.txt files focused and minimal~~
     - ~~Use layers for shared dependencies~~
     - ~~Let Stelvio handle platform-specific installations~~

3. Resource Access:
     - Use the generated Resources object for type-safe resource access
     - Keep your functions focused on business logic
     - Let Stelvio manage IAM permissions through linking

4. Function Organization:
     - Keep related code together in folder-based functions
     - Use clear file names and structure

## Managing Dependencies

!!! warning "NOT IMPLEMENTED"
    While dependency management is designed in Stelvio it has not been implemented yet.
    It's one of the top priorities. This section is here for you to know what to expect
    in the future releases and I'm happy to take any suggestions.

Stelvio automatically handles Python dependencies for your Lambda functions. Let's 
explore how this works.

### Dependencies for Single-File Functions

When you write single-file Lambda functions, Stelvio looks for a requirements.txt file 
in the same directory as your functions. Here's an example of several single-file 
functions with their dependencies:

```
functions/
├── login.py             # Handles user login
├── register.py          # Handles user registration
├── process_order.py     # Processes new orders
└── requirements.txt     # Dependencies shared by these functions
```

### Dependencies for Folder-Based Functions

For folder-based functions, place your requirements.txt inside the function folder:

```
functions/
├── users/
│   ├── handler.py          # Main handler with multiple functions
│   ├── database.py        # Database operations
│   ├── validation.py      # Input validation
│   └── requirements.txt   # Dependencies for this Lambda
└── orders/
    ├── handler.py         # Order processing handlers
    ├── stripe.py         # Payment processing
    └── requirements.txt   # Dependencies for this Lambda
```


### How Dependency Installation Works

When Stelvio packages your Lambda function, it:

1. Detects the relevant requirements.txt file
2. Downloads dependencies using platform-specific settings for AWS Lambda e.g.:
   ```bash
   pip install -r requirements.txt \
       --platform manylinux2014_x86_64 \
       --implementation cp \
       --python-version 3.12 \
       --only-binary=:all:
   ```
3. Packages the installed dependencies with your function code

This ensures your dependencies work correctly in the Lambda environment.

### Sharing Dependencies

You can share common dependencies using pip's `-r` flag in your requirements.txt files:

```
functions/
├── base_requirements.txt   # Common dependencies
├── login.py
├── requirements.txt        # Can include: -r ../base_requirements.txt
└── orders/
    ├── handler.py          # Order processing handlers
    ├── stripe.py           # Payment processing
    └── requirements.txt    # Can include: -r ../base_requirements.txt  or -r ../requirements.txt
```

### Important Notes

1. Stelvio installs all packages listed in `requirements.txt` - it doesn't analyze which 
   imports are actually used
2. Consider package sizes as they affect your Lambda deployment package size and 
   consequently also lambda cold start.

Stelvio handles all the complexity of dependency installation and packaging 
automatically, letting you focus on writing your Lambda function code.

## Lambda Layers

!!! warning "NOT IMPLEMENTED"
    Lambda layers suppport is not implemented yet. It's one of the top priorities. 
    This section is here for you to know what to expect in the future releases and I'm 
    happy to take any suggestions.

For sharing code between functions, you can create Lambda layers:

```python
from  stelvio.aws.function import Function, Layer

my_layer = Layer(
    name="my_layer",
    folder="layers/utils",        # Layer source directory
)

fn = Function(
    "my-function",
    handler="handler.process",
    layers=[my_layer]
)
```

Each layer is in it's own folder layer directory should follow the following structure:
```
layers
└── my_layer/
    ├──my_utils/         # Your code in layer needs its own folder. Then you can impor it like from my_utils.db import something
    │  └──db.py
    │  └──helpers.py
    └──requirements.txt  # Layer dependencies
```

## Next Steps

Now that you understand Lambda functions in Stelvio, you might want to explore:

- [Working with API Gateway](api-gateway.md) - Learn how to create APIs
- [Working with DynamoDB](dynamo-db.md) - Learn how to create DynamoDB tables
- [Linking](linking.md) - Learn how linking automates IAM, permissions, envars and more
- [Project Structure](project-structure.md) - Discover patterns for organizing your Stelvio applications