# Stelvio

[![PyPI](https://img.shields.io/pypi/v/stelvio.svg)](https://pypi.org/project/stelvio/)
[![Python Version](https://img.shields.io/pypi/pyversions/stelvio.svg)](https://pypi.org/project/stelvio/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

## AWS for Python devs - made simple

[**Documentation**](https://docs.stelvio.dev/getting-started/quickstart/) - 
[**Why I'm building Stelvio**](https://blog.stelvio.dev/why-i-am-building-stelvio/) - [**Intro article with quickstart**](https://blog.stelvio.dev/introducing-stelvio/)

## What is Stelvio?

Stelvio is a Python framework that simplifies AWS cloud infrastructure management and deployment. It lets you define your cloud infrastructure using pure Python, with smart defaults that handle complex configuration automatically.

With the `stlv` CLI, you can deploy AWS infrastructure in seconds without complex setup or configuration.

### Key Features

- **Developer-First**: Built specifically for Python developers, not infrastructure experts
- **Zero-Setup CLI**: Just run `stlv init` and start deploying - no complex configuration
- **Python-Native Infrastructure**: Define your cloud resources using familiar Python code
- **Environment Management**: Personal and shared environments with automatic resource isolation
- **Smart Defaults**: Automatic configuration of IAM roles, networking, and security

### Currently Supported

- [AWS Lambda & Layers](https://docs.stelvio.dev/guides/lambda/)
- [Amazon DynamoDB](https://docs.stelvio.dev/guides/dynamo-db/)
- [API Gateway](https://docs.stelvio.dev/guides/api-gateway/)
- [Linking - automated IAM](https://docs.stelvio.dev/guides/linking/)

Support for additional AWS services is planned. See [**Roadmap**](https://github.com/michal-stlv/stelvio/wiki/Roadmap).

## Example

Define AWS infrastructure in pure Python:

```python
@app.run
def run() -> None:
    # Create a DynamoDB table
    table = DynamoTable(
        name="todos",
        partition_key="username",
        sort_key="created"
    )
    
    # Create an API with Lambda functions
    api = Api("todos-api")
    api.route("POST", "/todos", handler="functions/todos.post", links=[table])
    api.route("GET", "/todos/{username}", handler="functions/todos.get")
```

See the [intro article](https://blog.stelvio.dev/introducing-stelvio/) for a complete working example.

## Quick Start

```bash
# Create a new project
uv init my-todo-api && cd my-todo-api

# Install Stelvio
uv add stelvio

# Initialize Stelvio project
uv run stlv init

# Edit stlv_app.py file to define your infra

# Deploy
uv run stlv deploy
```

Go to our [Quick Start Guide](https://docs.stelvio.dev/getting-started/quickstart/) for the full tutorial. 

## Why Stelvio?

Unlike generic infrastructure tools like Terraform, AWS CDK or Pulumi Stelvio is:

- Built specifically for Python developers
- Focused on developer productivity, not infrastructure complexity
- Designed to minimize boilerplate through intelligent defaults
- Maintained in pure Python without mixing application and infrastructure code

For detailed explanation see [Why I'm building Stelvio](https://blog.stelvio.dev/why-i-am-building-stelvio/) blog post.

## Project Status

Stelvio is currently in early but active development. 

## Contributing

Best way to contribute now is to play with it and report any issues.

I'm also happy to gather any feedback or feature requests.

Use GitHub Issues or email me directly at michal@stelvio.dev

If you want to contribute code you can open a PR. If you need any help I'm happy to talk.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.