# Stelvio

[![PyPI](https://img.shields.io/pypi/v/stelvio.svg)](https://pypi.org/project/stelvio/)
[![Python Version](https://img.shields.io/pypi/pyversions/stelvio.svg)](https://pypi.org/project/stelvio/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)


## 🎁 Merry Christmas 🎄

[![Merry Christmas](https://media0.giphy.com/media/v1.Y2lkPTc5MGI3NjExdGVhYmNqdzg4NnhoNWM3ZW1naGkyd21pMnM1djk4OXcya2l5cmcyMiZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/PRK7GwyAaWnEA/giphy.gif)](https://github.com/stelviodev/stelvio/releases/tag/v0.6.1a9)

**Stelvio 0.6.1** (Codename X-Mas) was [just released](https://github.com/stelviodev/stelvio/releases/tag/v0.6.1a9)!

## AWS for Python devs - made simple

[**Documentation**](https://stelvio.dev/docs/getting-started/quickstart/) - [**Stelvio Manifesto**](https://stelvio.dev/manifesto/) - [**Intro article with quickstart**](https://stelvio.dev/blog/introducing-stelvio/)

## What is Stelvio?

Stelvio is a Python framework that simplifies AWS cloud infrastructure management and deployment. It lets you define your cloud infrastructure using pure Python, with smart defaults that handle complex configuration automatically.

With the `stlv` CLI, you can deploy AWS infrastructure in seconds without complex setup or configuration.

### Key Features

- **Developer-First**: Built specifically for Python developers, not infrastructure experts
- **Zero-Setup CLI**: Just run `stlv init` and start deploying - no complex configuration
- **Python-Native Infrastructure**: Define your cloud resources using familiar Python code
- **Environments**: Personal and shared environments with automatic resource isolation
- **Smart Defaults**: Automatic configuration of IAM roles, networking, and security

### Currently Supported

- [AWS Lambda & Layers](https://stelvio.dev/docs/guides/lambda/)
- [Amazon DynamoDB](https://stelvio.dev/docs/guides/dynamo-db/)
- [API Gateway](https://stelvio.dev/docs/guides/api-gateway/)
- [Linking - automated IAM](https://stelvio.dev/docs/guides/linking/)
- [S3 Buckets](https://stelvio.dev/docs/guides/s3/)
- [Custom Domains](https://stelvio.dev/docs/guides/dns)

Support for additional AWS services is coming. See [**Roadmap**](https://github.com/stelviodev/stelvio/wiki/Roadmap).

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
    api = Api("todos-api", domain_name="api.example.com")
    api.route("POST", "/todos", handler="functions/todos.post", links=[table])
    api.route("GET", "/todos/{username}", handler="functions/todos.get")
```

See the [intro article](https://stelvio.dev/blog/introducing-stelvio/) for a complete working example.

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

Go to our [Quick Start Guide](https://stelvio.dev/docs/getting-started/quickstart/) for the full tutorial. 

## Why Stelvio?

Unlike generic infrastructure tools like Terraform, AWS CDK or Pulumi Stelvio is:

- Built specifically for Python developers
- Focused on developer productivity, not infrastructure complexity
- Designed to minimize boilerplate through intelligent defaults
- Maintained in pure Python without mixing application and infrastructure code

For detailed explanation see [Stelvio Manifesto](https://stelvio.dev/manifesto/) blog post.

## Project Status

Stelvio is currently in early but active development. 

## Contributing

Best way to contribute now is to play with it and report any issues.

I'm also happy to gather any feedback or feature requests.

Use GitHub Issues or email us directly at team@stelvio.dev

If you want to contribute code you can open a PR. If you need any help I'm happy to talk.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.