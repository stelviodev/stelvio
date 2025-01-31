# Stelvio

Cloud infrastructure made simple for Python developers.

## What is Stelvio?

Stelvio is a Python library that simplifies cloud infrastructure management and deployment. It lets you define your cloud infrastructure using pure Python, with smart defaults that handle complex configuration automatically.

### Key Features

- **Python-Native Infrastructure**: Define your cloud resources using familiar Python code
- **Smart Defaults**: Automatic configuration of IAM roles, networking, and security
- **Clean Separation**: Keep your infrastructure code separate from application code
- **Developer-First**: Built specifically for Python developers, not infrastructure experts

### Currently Supported

- AWS Lambda
- Amazon DynamoDB
- API Gateway
- Linking - automated IAM

*Support for additional AWS services and other cloud providers (Cloudflare) is planned.*

## Quick Start

Go to our [Quick Start Guide](docs/getting-started/quickstart.md) to start

## Why Stelvio?

Unlike generic infrastructure tools like Terraform, Pulumi, or AWS CDK, Stelvio is:

- Built specifically for Python developers
- Focused on developer productivity, not infrastructure complexity
- Designed to minimize boilerplate through intelligent defaults
- Maintained in pure Python without mixing application and infrastructure code

## Project Status

Stelvio is currently in active development as a side project. 

⚠️ It is in Early alpha state - Not production ready - Only for experimentation - API unstable"

It supports basic Lambda, Dynamo DB and API Gateway setup.

## Contributing

Best way to contribute now is to play with it and report any issues.

I'm also happy to gather any feedback or feature requests.

Use GitHub Issues or email me directly at michal@stelvio.dev

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.