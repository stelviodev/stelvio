# Quick Start Guide

Welcome to Stelvio! 

First of all, thank you for taking a time to try it. 

While Stelvio is in **very early development stage (alpha - Developer Preview)** I hope 
it shows how it simplifies AWS infrastructure for Python devs. 

In this guide, we'll go through basics so you can see how Stelvio makes AWS easy. 


## Prerequisites

Before we begin, you'll need:

- Python 3.12 or newer installed
- An AWS account where you can deploy with credentials configured
- Basic familiarity with Python and AWS concepts
- _Pulumi CLI_ - [installation instructions here](https://www.pulumi.com/docs/iac/download-install/) 

!!! note "This way of setting up a project and the need to manually install Pulumi CLI is temporary"
    I understand that manually installing another tool and setting up  project in this
    specific way is far from ideal. I assure you this is temporary and Stelvio will have
    its own CLI which will make things much easier. However to gather early feedback 
    I had to cut many features from Stelvio's early releases and this is one of them.

## Setting up a project
    
### 1. AWS keys and envars
First we need to make sure you configure your AWS credentials correctly.

Make sure you have configured AWS account that has programmatic access with rights to 
deploy infrastructure. If you have installed and configured the AWS CLI before Stelvio 
will use those configs. If not you can configure it by setting AWS keys with envars:
    
```bash
export AWS_ACCESS_KEY_ID="<YOUR_ACCESS_KEY_ID>"
export AWS_SECRET_ACCESS_KEY="<YOUR_SECRET_ACCESS_KEY>"
```
or AWS profile:

```bash
export AWS_PROFILE="<YOUR_PROFILE_NAME>"
```

### 2. Project

First let's create a project folder and go inside it.

```bash
mkdir stelvio-app
cd stelvio-app
```

We need to tell Pulumi to use local backend to make sure that Pulumi will keep state of 
our infrastructure on our computer rather then sending it to their cloud or S3 bucket ([see docs on this](https://www.pulumi.com/docs/iac/concepts/state-and-backends/)).

```bash
pulumi login --local 
```
Ok now that we're inside our folder we can init new project.

When you run the command below it will ask you to create a passphrase. Put any
passphrase you like but _remember it_ because you'll be asked for it every time you'll
deploy.  
You can also set it to `PULUMI_CONFIG_PASSPHRASE` envar and then you'll not be asked 
for it. 

We use name `stelvio-app` but you can choose whatever name you like.  
It will also use a default region which is `us-east-1`. If you want to use other 
region you can specify it by adding `-c aws:region=YOUR_REGION` to the command.  

```bash
pulumi new https://github.com/michal-stlv/stelvio/tree/main/pulumi-tmpl \
    --name stelvio-app \
    --stack dev \
    --force \
    --yes
```

By running this command Pulumi CLI has created a Pulumi project for us using Stelvio 
template.

It did a few things for us: 

- created a Pulumi project `stelvio-app`.
- created a stack named `dev` ([stack](https://www.pulumi.com/docs/iac/concepts/stacks/) 
  is an instance of Pulumi program).
- created Python virtual env named `venv` inside our project folder.
- created requirements.txt file which has only one thing in it - `stelvio`. 
- ran `pip install requirements.txt` to install it. `stelvio` has dependency on `pulumi` 
  and `pulumi-aw`s so those were installed as well.
- created `stlv_app.py` - main Stelvio file which contains StelvioApp
- created `__main__.py` which Pulumi runs - this file just imports our Stelvio file `stlv_app.py`
- created Pulumi.yaml - root file that Pulumi requires
- created Pulumi.dev.yaml - Pulumi's config file for stack dev

You can run `pulumi preview` to check all is working. 

When you run `pulumi preview` or `pulumi up` Pulumi is automatically activating virtual 
env it created for us. 

!!! note
    All of this setup will go away once Stelvio has its own CLI. Then we'll not have
    to have Pulumi yaml files nor `__main__.py` file. 

## Simple project using Stelvio


### Project structure

In Stelvio, you have complete flexibility in 
[how you organize your project](../guides/project-structure.md) and where are
your Stelvio infrastructure files located. But for this quickstart guide, we'll 
keep things super simple and keep our infra definitions only in the main `stvl_app.py` 
file so our project structure will look like this:

```
stelvio-app/
├── stlv_app.py      # Infrastructure configuration
└── functions/       # Lambda functions
    └── todos.py     # Our function code
```


Open `stlv_app.py`, it will look like this:

```python
from stelvio.app import StelvioApp

app = StelvioApp(
    name="Stelvio app",
    modules=[
        # Need these in specific order? Just list them
        # "infra.base",
        # Don't care about the rest? Glob it!
        "*/infra/*.py",
        "**/*stlv.py",
    ],
)

app.run()
```

### Define our infrastructure
We need to put any infrastructure definitions before `app.run()` but after `app = StelvioApp(...)`

Let's create a simple API to create and list todos.

First create a DynamoDB table:

```python
from stelvio.aws.dynamo_db import AttributeType, DynamoTable

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

The above will create 
[DynamoDB table](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithTables.Basics.html) 
with partition key `username`, sort key `created` and billing mode `PAY_PER_REQUEST`.

Now lets create API Gateway and routes:

```python
from stelvio.aws.api_gateway import Api

api = Api("todo-api")
api.route("POST", "/todos", handler="functions/todos.post", links=[table])
api.route("GET", "/todos/{username}", handler="functions/todos.get")

```

The above will create:
- API Gateway
- resource (todos)
- methods (GET and POST)
- one lambda with code from `functions/todos.py` file with:
  - properly configured env vars containing table name and arn
  - generated routing code to properly route requests to proper functions
- lambda integration between methods and lambda 
- IAM (roles, policies, etc.) 
- stage
- deployment
- log groups

So our complete `app_stlv.py` now looks like this:

```python
from stelvio.app import StelvioApp
from stelvio.aws.api_gateway import Api
from stelvio.aws.dynamo_db import AttributeType, DynamoTable

app = StelvioApp(
    name="Stelvio app",
    modules=[
        # Need these in specific order? Just list them
        # "infra.base",
        # Don't care about the rest? Glob it!
        "*/infra/*.py",
        "**/*stlv.py",
    ],
)

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

app.run()
```

### Lambda code

Now we can write code for our `functions/todos.py`:

```python
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


### Preview our infra

Now we're ready to deploy. First let's try to preview what we've got - run `pulumi preview`.

It should print something like this:
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

Now to deploy we run need to `pulumi up`. It will ask you to confirm deployment.  
Select _yes_ and it will create our infrastructure. 

During deployment it will print what resources it creates. But when it finishes it should
print something like this at the end:
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

In Outputs there is one called `invoke_url_for_restapi_todos-api`. This contains URL of our todos API.
Copy it and we can test our API.

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

Let's take a moment to appreciate what we've accomplished with just a few files:

- Set up a database
- Created lambda function
- Created a serverless API
- Deployed everything to AWS

Most importantly, we did this while writing clean, maintainable Python code. No YAML 
files, no clicking through consoles, and no complex configuration.

That's it for this quickstart. Hope you give Stelvio a chance. I encourage you to play
around and let me know any feedback on GitHub or michal@stelvio.dev


## Next Steps

- [Working with Lambda Functions](lambda.md) - Learn more about how to work with Lambda functions
- [Working with API Gateway](api-gateway.md) - Learn how to create APIs
- [Working with DynamoDB](dynamo-db.md) - Learn how to create DynamoDB tables
- [Linking](linking.md) - Learn how linking automates IAM, permissions, envars and more
- [Project Structure](project-structure.md) - Discover patterns for organizing your Stelvio applications
