# Quick Start Guide

Welcome to Stelvio! 

Thank you for trying it! 

While it's still in early development (alpha), I hope this guide shows how much it simplifies AWS infrastructure for Python developers.

In this guide, we'll go through the basics so you can see how Stelvio makes AWS easy. 


## Prerequisites

Before we begin, you'll need:

- Python 3.12 or newer installed
- An AWS account where you can deploy infrastructure
- AWS credentials configured (via AWS CLI or environment variables)
- Basic familiarity with Python and AWS concepts

## Setting up a project

### 1. AWS Credentials

First, make sure you have an AWS account with programmatic access and rights to deploy infrastructure.

You can configure credentials in several ways:

**Option 1: AWS CLI profiles**

```bash
aws configure --profile YOUR_PROFILE_NAME
```

Then either specify the profile name during `stlv init` or use:

```bash
export AWS_PROFILE=YOUR_PROFILE_NAME
```

**Option 2: Environment variables**

```bash
export AWS_ACCESS_KEY_ID="<YOUR_ACCESS_KEY_ID>"
export AWS_SECRET_ACCESS_KEY="<YOUR_SECRET_ACCESS_KEY>"
```

If using environment variables (Option 2), just press Enter when `stlv init` asks for profile name.

### 2. Create Project

If you can use `uv` but you can use anything.

=== "uv"

    ```bash
    # Create a new project
    uv init my-todo-api && cd my-todo-api
    
    # Install Stelvio
    uv add stelvio
    
    # Initialize Stelvio project
    uv run stlv init
    ```

=== "poetry"

    ```bash
    # Create a new project
    poetry new my-todo-api && cd my-todo-api
    
    # Install Stelvio
    poetry add stelvio
    
    # Initialize Stelvio project
    poetry run stlv init
    ```

=== "pip"

    ```bash
    # Create a new project
    mkdir my-todo-api && cd my-todo-api
    python -m venv .venv && source .venv/bin/activate
    
    # Install Stelvio
    pip install stelvio
    
    # Initialize Stelvio project
    stlv init
    ```

The `stlv init` command will:

- Ask for your AWS profile name (or press Enter to use default credentials)
- Ask for your AWS region
- Create `stlv_app.py` with your project configuration 

## Simple project using Stelvio

### Project structure

For this quickstart guide, we'll keep things simple and put our infrastructure 
definitions in the main `stlv_app.py` file so our project structure will look 
like this:

```
my-todo-api/
├── stlv_app.py      # Infrastructure configuration
└── functions/       # Lambda functions
    └── todos.py     # Our function code
```

??? note "Project structure" 
    In Stelvio, you have complete flexibility in 
    [how you organize your project](../guides/project-structure.md) and where your infrastructure files 
    are located.

Open `stlv_app.py`, it will look like this:

```python title="stlv_app.py"
from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig, AwsConfig

app = StelvioApp("my-todo-api")

@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(
            region="us-east-1",
            profile="your-profile",  # or None if using env vars
        ),
    )

@app.run
def run() -> None:
    # Create your infra here
    pass
```

### Define our infrastructure

We need to put our infrastructure definitions inside the `@app.run` function.

Let's create a simple API to create and list todos.

First, let's add the imports we need at the top of the file:

```python title="stlv_app.py" hl_lines="3 4"
from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig, AwsConfig
from stelvio.aws.dynamo_db import AttributeType, DynamoTable
from stelvio.aws.api_gateway import Api
```

Now let's update our `@app.run` function to create a DynamoDB table:

```python title="stlv_app.py" hl_lines="3-11"
@app.run
def run() -> None:
    table = DynamoTable(
        name="todos",
        fields={
            "username": AttributeType.STRING,
            "created": AttributeType.STRING,
        },
        partition_key="username",
        sort_key='created'
    )
```

The above will create a 
[DynamoDB table](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithTables.Basics.html) 
with partition key `username`, sort key `created` and billing mode `PAY_PER_REQUEST`.

Now let's add an API and routes to the same function:

```python title="stlv_app.py" hl_lines="13-16"
@app.run
def run() -> None:
    table = DynamoTable(
        name="todos",
        fields={
            "username": AttributeType.STRING,
            "created": AttributeType.STRING,
        },
        partition_key="username",
        sort_key='created'
    )
    
    api = Api("todo-api")
    api.route("POST", "/todos", handler="functions/todos.post", links=[table])
    api.route("GET", "/todos/{username}", handler="functions/todos.get")
```

The above will create:

- An API Gateway REST API
- API resources (e.g., `/todos`, `/todos/{username}`)
- API methods (GET and POST)
- A Lambda function with code from `functions/todos.py` file with:
  - properly configured env vars containing table name and arn
  - generated routing code to properly route requests to proper functions
- lambda integration between methods and lambda 
- IAM (roles, policies, etc.) 
- stage
- deployment
- log groups

So our complete `stlv_app.py` now looks like this:

```python title="stlv_app.py"
from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig, AwsConfig
from stelvio.aws.dynamo_db import AttributeType, DynamoTable
from stelvio.aws.api_gateway import Api

app = StelvioApp("my-todo-api")

@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(
            region="us-east-1",
            profile="your-profile",  # or None if using env vars
        ),
    )

@app.run
def run() -> None:
    table = DynamoTable(
        name="todos",
        fields={
            "username": AttributeType.STRING,
            "created": AttributeType.STRING,
        },
        partition_key="username",
        sort_key='created'
    )
    
    api = Api("todo-api")
    api.route("POST", "/todos", handler="functions/todos.post", links=[table])
    api.route("GET", "/todos/{username}", handler="functions/todos.get")
```

### Lambda code

Now we can write code for our `functions/todos.py`:

```python title="functions/todos.py" 
import json
from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Key

from stlv_resources import Resources

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(Resources.todos.table_name)


def post(event, context):
    # Parse the request body
    body = json.loads(event.get('body', '{}'))

    # Create item
    item = {
        'username': body.get('username'),
        'created': datetime.utcnow().isoformat(),
        'title': body.get('title'),
        'done': False
    }
    # Save to DynamoDB
    table.put_item(Item=item)
    return {
        'statusCode': 201,
        'body': json.dumps(item)
    }

def get(event, context):
    # Get username from query parameters
    username = event.get('pathParameters', {}).get('username')

    # Query DynamoDB
    response = table.query(
        KeyConditionExpression=Key('username').eq(username)
    )

    return {
        'statusCode': 200,
        'body': json.dumps({
            'todos': response['Items']
        })
    }
```


### Preview our infrastructure

Now we're ready to deploy. First let's create our `functions/todos.py` file, then preview what will be created.

Create the `functions` directory and `todos.py` file with the Lambda code shown above.

Now let's preview what will be deployed:

=== "uv"
    ```bash
    uv run stlv diff
    ```

=== "poetry"
    ```bash
    poetry run stlv diff
    ```

=== "pip"
    ```bash
    stlv diff
    ```

It will show a nice preview like this:
```bash
Previewing update (dev):
     Type                             Name                                                Plan       Info
 +   pulumi:pulumi:Stack              stelvio-app-dev                                            create     5 messages
 +   ├─ aws:apigateway:RestApi        todos-api                                           create     
 +   ├─ aws:dynamodb:Table            todos                                               create     
 +   ├─ aws:iam:Role                  api-gateway-role                                    create     
 +   ├─ aws:apigateway:Deployment     todos-api-deployment                                create     
 +   ├─ aws:apigateway:Resource       resource-todos                                      create     
 +   ├─ aws:iam:RolePolicyAttachment  api-gateway-role-logs-policy-attachment             create     
 +   ├─ aws:apigateway:Account        api-gateway-account                                 create     
 +   ├─ aws:iam:Role                  functions-todos-Role                                create     
 +   ├─ aws:iam:Policy                functions-todos-Policy                              create     
 +   ├─ aws:apigateway:Integration    integration-POST-/todos                             create     
 +   ├─ aws:apigateway:Integration    integration-GET-/todos/{username}                   create     
 +   ├─ aws:apigateway:Resource       resource-todos-username                             create     
 +   ├─ aws:lambda:Function           functions-todos                                     create     
 +   ├─ aws:iam:RolePolicyAttachment  functions-todos-DefaultRolePolicyAttachment         create     
 +   ├─ aws:iam:RolePolicyAttachment  functions-todos-BasicExecutionRolePolicyAttachment  create     
 +   ├─ aws:apigateway:Method         method-POST-todos                                   create     
 +   ├─ aws:apigateway:Method         method-GET-todos-username                           create     
 +   ├─ aws:lambda:Permission         todos-api-functions-todos-policy-statement          create     
 +   └─ aws:apigateway:Stage          todos-api-v1                                        create     

Diagnostics:
  pulumi:pulumi:Stack (stelvio-app-dev):
    todos
    todos-api
    todos
    todos-api
    functions-todos

Outputs:
    dynamo_todos_arn                : output<string>
    invoke_url_for_restapi_todos-api: output<string>
    lambda_functions-todos_arn      : output<string>
    restapi_todos-api_arn           : output<string>

Resources:
    + 20 to create
```

It shows you all resources that will be created. But it has one side effect - when you run
preview or deploy Stelvio will create `stlv_resources.py` which contains type safe 
definitions of our lambda environment variables which we an use in our lambda code. 

You can see it above in our lambda code:
```python
from stlv_resources import Resources # <--- importing Resources class from stlv_resources.py
...
table = dynamodb.Table(Resources.todos.table_name) ## <--- getting our table's name
```
### Deploy

Now let's deploy our infrastructure:

=== "uv"
    ```bash
    uv run stlv deploy
    ```

=== "poetry"
    ```bash
    poetry run stlv deploy
    ```

=== "pip"
    ```bash
    stlv deploy
    ```

Stelvio will create all your infrastructure with real-time progress indicators.

When deployment finishes, you'll see the outputs:
```bash
Outputs:
    dynamo_todos_arn                : "arn:aws:dynamodb:us-east-1:482403859050:table/todos-4442577"
    invoke_url_for_restapi_todos-api: "https://somerandomstring.execute-api.us-east-1.amazonaws.com/v1"
    lambda_functions-todos_arn      : "arn:aws:lambda:us-east-1:482403859050:function:functions-todos-fbe96ae"
    restapi_todos-api_arn           : "arn:aws:apigateway:us-east-1::/restapis/en4kl5pn23"

Resources:
    + 20 created

Duration: 57s
```

In the outputs, look for `invoke_url_for_restapi_todos-api` - this contains the URL of your todos API.
Copy this URL to test your API.

!!! note "Environment Management"
    By default, Stelvio deployed to your personal environment (using your username). All resources are automatically prefixed with your app name and environment, so you can safely deploy multiple projects and environments without naming conflicts.

## Testing Your API

We'll use curl to create a todo item:

```bash
curl -X POST https://YOUR_API_URL/todos/ \
  -d '{"username": "john",  "title": "Buy milk"}'
```

And now we can list todos:

```bash
curl https://YOUR_API_URL/todos/john
```

### Understanding What We've Built

Let's take a moment to appreciate what we've accomplished with just a few commands:

- **Set up a complete project** with `stlv init`
- **Created a database** (DynamoDB table)
- **Built serverless functions** (AWS Lambda)
- **Deployed a REST API** (API Gateway)
- **Deployed everything to AWS** with `stlv deploy`

Most importantly, we did this while writing clean, maintainable Python code. No YAML files, no complex setup, no clicking through AWS consoles, and no infrastructure expertise required.

Stelvio handled all the complex AWS configuration automatically - IAM roles, permissions, networking, environment variables, and more.

That's it for this quickstart! We hope Stelvio makes your AWS development much simpler. Try building something and let us know your feedback on GitHub or michal@stelvio.dev


## Next Steps

- [Using Stelvio CLI](../guides/using-cli.md) - Learn all CLI commands and environment management
- [Working with Lambda Functions](../guides/lambda.md) - Learn more about how to work with Lambda functions
- [Working with API Gateway](../guides/api-gateway.md) - Learn how to create APIs
- [Working with DynamoDB](../guides/dynamo-db.md) - Learn how to create DynamoDB tables
- [Linking](../guides/linking.md) - Learn how linking automates IAM, permissions, envars and more
- [Project Structure](../guides/project-structure.md) - Discover patterns for organizing your Stelvio applications
