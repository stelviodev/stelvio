# Welcome to Stelvio

Stelvio is a Python library that makes AWS development simple for for Python devs. 

It lets you build and deploy AWS applications using pure Python code, without dealing 
with complex infrastructure tools.

**Head over to the _[Quick Start](getting-started/quickstart.md)_ guide to get started.**

!!! warning "Stelvio is in Early alpha state - Not production ready - Only for experimentation - API unstable"
    Stelvio is currently in active development as a side project. 
    
    It supports basic Lambda, Dynamo DB and API Gateway setup.

## Why I Built This

As a Python developer working with AWS, I got tired of:

- Switching between YAML, JSON, and other config formats
- Figuring out IAM roles and permissions
- Managing infrastructure separately from my code
- Clicking through endless AWS console screens
- Writing and maintaining complex infrastructure code

I wanted to focus on building applications, not fighting with infrastructure. That's 
why I created Stelvio.

## How It Works

Here's how simple it can be to create an API (Gateway) with Stelvio:

```py
from stelvio import Api

api = Api('my-api')
api.route('GET', '/users', 'users/handler.list')
api.route('POST', '/users', 'users/handler.create')
api.deploy()
```

Stelvio takes care of everything else:

- Creates Lambda functions
- Sets up API Gateway
- Handles IAM roles and permissions
- Makes sure your resources can talk to each other
- Deploys everything properly

## What Makes It Different

### Just Python
Write everything in Python. No new tools or languages to learn. If you know Python, 
you know how to use Stelvio.

### Smart Defaults That Make Sense
Start simple with sensible defaults. Add configuration only when you need it. Simple 
things stay simple, but you still have full control when you need it.

### Type Safety That Actually Helps
Get IDE support and type checking for all your AWS resources. No more guessing about 
environment variables or resource configurations.

### Works Your Way
Keep your infrastructure code wherever makes sense:

- Next to your application code
- In a separate folder
- Or mix both approaches

## Ready to Try It?

Head over to the [Quick Start](getting-started/quickstart.md) guide to get started.

## What I Believe In

I built Stelvio believing that:

1. Infrastructure should feel natural in your Python code
2. You shouldn't need to become an AWS expert
3. Simple things should be simple
4. Your IDE should help you catch problems early
5. Good defaults beat endless options

## Let's Talk

- Found a bug or want a feature? [Open an issue](https://github.com/michal-stlv/stelvio/issues)
- Have questions? [Join the discussion](https://github.com/michal-stlv/stelvio/discussions)
- Want updates and tips? [Follow me on Twitter](https://twitter.com/michal_stlv)

## License

Stelvio is released under the Apache 2.0 License. See the LICENSE file for details.
