# Stelvio

_**AWS for Python devs - made simple.**_

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

- [AWS Lambda](https://docs.stelvio.dev/guides/lambda/)
- [Amazon DynamoDB](https://docs.stelvio.dev/guides/dynamo-db/)
- [API Gateway](https://docs.stelvio.dev/guides/api-gateway/)
- [Linking - automated IAM](https://docs.stelvio.dev/guides/linking/)

Support for additional AWS services is planned. See [**Roadmap**](https://github.com/michal-stlv/stelvio/wiki/Roadmap).

## Quick Start

```bash
# Initialize a new project
stlv init

# Deploy to your personal environment
stlv deploy

# Deploy to production
stlv deploy prod
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

Stelvio is currently in early (alpha) but active development as a side project. 

## Contributing

Best way to contribute now is to play with it and report any issues.

I'm also happy to gather any feedback or feature requests.

Use GitHub Issues or email me directly at michal@stelvio.dev

I'm focused on building a solid foundation before accepting code contributions. 
This will make future collaboration easier and more enjoyable. 
But don't hesitate to email me if you want to contribute before everything is ready.

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.