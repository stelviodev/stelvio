# Stelvio

[![PyPI](https://img.shields.io/pypi/v/stelvio.svg)](https://pypi.org/project/stelvio/)
[![Python Version](https://img.shields.io/pypi/pyversions/stelvio.svg)](https://pypi.org/project/stelvio/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)


## Ship Python to AWS in minutes, not days.

[**Documentation**](https://stelvio.dev/docs/getting-started/quickstart/) - [**Stelvio Manifesto**](https://stelvio.dev/manifesto/) - [**Intro Article**](https://stelvio.dev/blog/introducing-stelvio/) - [**Roadmap**](https://github.com/stelviodev/stelvio/wiki/Roadmap)

Stelvio is an **open-source** framework that lets you build and deploy modern AWS applications using **pure Python**. Forget YAML, complex configuration, or learning new DSLs.

With the `stlv` CLI, you focus on your code, and Stelvio handles the infrastructure.

[![stlv dev](https://img.youtube.com/vi/4TdA8XY3Dcw/maxresdefault.jpg)](https://youtu.be/4TdA8XY3Dcw)

## Why Stelvio?

- 🐍 **Pure Python**: Define your infrastructure with standard Python code. Use your favorite IDE, linter, and type checker.
- 🧠 **Smart Defaults**: We handle the complex IAM roles, networking, and configuration so you don't have to.
- 🔗 **Automatic Linking**: Simply pass resources to your functions. Stelvio automatically configures permissions and environment variables.
- ⚡ **Live Dev Mode**: Run `stlv dev` to sync your code changes instantly. No waiting for deployments.
- 🔧 **Full Control**: Logic and infrastructure in one place, with escape hatches to the underlying Pulumi resources.
- 📖 **Open Source**: Built by developers for developers. Apache 2.0 licensed.

## Example

Define your infrastructure and application logic in one file. Stelvio handles the wiring.

```python
from stelvio.app import StelvioApp
from stelvio.aws import DynamoTable, Api

@app.run
def run() -> None:
    # 1. Create a DynamoDB table
    table = DynamoTable(
        name="todos",
        partition_key="username",
        sort_key="created"
    )
    
    # 2. Create an API
    api = Api("todos-api")
    
    # 3. Add a route and link it to the table
    # Stelvio automatically grants the Lambda function permission 
    # to access the table and injects the table name as an env var.
    api.route(
        "POST", "/todos", 
        handler="functions/todos.post", 
        links=[table]
    )
```

## Supported Components

Stelvio provides high-level components for the most common AWS services:

- **[Function](https://stelvio.dev/docs/guides/lambda/)** (AWS Lambda)
- **[Public API](https://stelvio.dev/docs/guides/api-gateway/)** (API Gateway)
- **[Scheduled Tasks](https://stelvio.dev/docs/guides/cron/)** (EventBridge Cron)
- **[Object Storage](https://stelvio.dev/docs/guides/s3/)** (S3)
- **[NoSQL Database](https://stelvio.dev/docs/guides/dynamo-db/)** (DynamoDB)
- **[Message Queues](https://stelvio.dev/docs/guides/queues/)** (SQS)
- **[Pub/Sub Topics](https://stelvio.dev/docs/guides/topics/)** (SNS)
- **[Email](https://stelvio.dev/docs/guides/email/)** (SES)

## Give it a try

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://github.com/codespaces/new?hide_repo_select=true&ref=main&repo=1084563664)

## Quick Start

You can get up and running in less than 2 minutes.

```bash
# 1. Create a new project
uv init my-todo-api && cd my-todo-api

# 2. Add Stelvio
uv add stelvio

# 3. Initialize project structure
uv run stlv init

# 4. Deploy to AWS
uv run stlv deploy
```

See the [Quick Start Guide](https://stelvio.dev/docs/getting-started/quickstart/) for a full walkthrough.

## Community & Contributing

Stelvio is open source and we welcome contributions!

- Check out our [Roadmap](https://github.com/stelviodev/stelvio/wiki/Roadmap) to see what's coming.
- Read the [Stelvio Manifesto](https://stelvio.dev/manifesto/) to understand our philosophy.
- Found a bug? Open an [Issue](https://github.com/stelviodev/stelvio/issues).

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.