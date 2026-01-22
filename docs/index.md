# Welcome to Stelvio

Stelvio is a Python framework that makes AWS development simple for Python devs. 

It lets you build and deploy AWS applications using pure Python code and a simple CLI, without dealing with complex infrastructure tools.

**Head over to the _[Quick Start](getting-started/quickstart.md)_ guide to get started.**

!!! note "Approaching Beta"
    Stelvio is actively developed and approaching beta. The core features are stable and ready for real projects, though some APIs may evolve as we refine the developer experience.

    Supports Lambda, API Gateway, DynamoDB, S3, SQS, SNS, SES, custom domains, CloudFront routing, Cron, and more coming soon.

## Why We're Built This

As a Python developers working with AWS, I got tired of:

- Switching between YAML, JSON, and other config formats
- Figuring out IAM roles and permissions
- Managing infrastructure separately from my code
- Clicking through endless AWS console screens
- Writing and maintaining complex infrastructure code

We wanted to focus on building applications, not fighting with infrastructure. That's 
why we created Stelvio.

## How It Works

Here's how simple it is to create and deploy an API with Stelvio:

```py
from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig

app = StelvioApp("my-api")

@app.config
def config(env: str) -> StelvioAppConfig:
    return StelvioAppConfig()

@app.run
def run() -> None:
    from stelvio.aws.api_gateway import Api
    
    api = Api('my-api')
    api.route('GET', '/users', 'users/handler.get')
    api.route('POST', '/users', 'users/handler.create')
```

Then deploy with one command:
```bash
stlv deploy
```

Stelvio takes care of everything else:

- Creates Lambda functions automatically
- Sets up API Gateway with routing
- Handles IAM roles and permissions
- Manages environment variables
- Deploys everything to AWS

## What Makes It Different

### Zero-Setup CLI
Get started in seconds with `stlv init`. No complex configuration, no manual tool installation, no YAML files. Just install Stelvio and start deploying.

### Just Python
Write everything in Python. No new tools or languages to learn. If you know Python, you know how to use Stelvio.

### Environments Built-In
Deploy to your personal environment by default, or share staging/production environments with your team. All resources are automatically isolated and named.

### Smart Defaults That Make Sense
Start simple with sensible defaults. Add configuration only when you need it. Simple things stay simple, but you still have full control when you need it.

### Type Safety That Actually Helps
Get IDE support and type checking for all your AWS resources. No more guessing about environment variables or resource configurations.

## Ready to Try It?

Head over to the [Quick Start](getting-started/quickstart.md) guide to get started.

## What I Believe In

I built Stelvio believing that:

1. Infrastructure should feel natural in your Python code
2. You shouldn't need to become an AWS expert
3. Simple things should be simple
4. Your IDE should help you catch problems early
5. Good defaults beat endless options
6. Developer experience matters as much as functionality

## Let's Talk

- Found a bug or want a feature? [Open an issue](https://github.com/stelviodev/stelvio/issues)
- Have questions? [Join the discussion](https://github.com/stelviodev/stelvio/discussions)
- Want updates and tips? [Follow us on X](https://x.com/stelviodev)

## License

Stelvio is released under the Apache 2.0 License. See the LICENSE file for details.

## Where to go from here

### Getting Started

- [Quick Start](getting-started/quickstart.md) - Deploy your first app in minutes
- [StelvioApp Basics](guides/stelvio-app.md) - Understanding the core concepts
- [Environments](guides/environments.md) - Personal and team environments

### Guides

- [`stlv dev`](guides/stlv-dev.md) - Execute Lambda Functions locally
- [Using CLI](guides/using-cli.md) - The `stlv` CLI
- [API Gateway](guides/api-gateway.md) - Build REST APIs
- [Lambda Functions](guides/lambda.md) - Serverless functions with Python
- [Queues](guides/queues.md) - Serverless queues with SQS
- [SNS Topics](guides/topics.md) - Pub/sub messaging with SNS
- [Dynamo DB](guides/dynamo-db.md) - Serverless NoSQL Database
- [S3 Buckets](guides/s3.md) - AWS S3 (Object Storage)
- [Cron](guides/cron.md) - Scheduled tasks with EventBridge
- [Linking](guides/linking.md) - Automatic IAM permissions
- [DNS](guides/dns.md) - Custom domains and TLS certificates
- [Router](guides/cloudfront-router.md) - Routing components on one domain
- [Email](guides/email.md) - Send emails using SES
- [Project Structure](guides/project-structure.md) - Organizing your code
- [State Management](guides/state.md) - Understand Deployment State
- [Parameter Customization](guides/customization.md) - Customize internals of cloud primitives
- [Troubleshooting](guides/troubleshooting.md) - Common misconceptions

### Reference

- [CLI Commands](guides/using-cli.md) - All stlv commands
- [State Management](guides/state.md) - How Stelvio manages state
- [Troubleshooting](guides/troubleshooting.md) - Debug common issues
