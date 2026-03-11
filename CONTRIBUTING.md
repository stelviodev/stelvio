# Contributing to Stelvio

Thank you for considering contributing to Stelvio! Contributions of all kinds are welcome — code, tests, documentation, bug reports.

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md). It's very short and simple — basically just be nice and stay on topic.

## Contributor License Agreement

When you submit your first PR, our CLA Assistant bot will guide you through signing the Contributor License Agreement. This is a one-time process for all your future contributions.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/stelvio.git`
3. Add the original repo as upstream: `git remote add upstream https://github.com/stelviodev/stelvio.git`
4. Install [uv](https://github.com/astral-sh/uv?tab=readme-ov-file#installation) if not already installed (Python 3.12+ required)
5. Set up the development environment: `uv sync`
6. Run tests to make sure everything works: `uv run pytest`

For a guide on using Stelvio itself, see the [Quick Start Guide](https://stelvio.dev/docs/getting-started/quickstart/).

## Project Structure

Stelvio is a Python library that wraps Pulumi to simplify AWS infrastructure deployment. Users define components like `Function`, `DynamoTable`, `Api` in a `stlv_app.py` file, and Stelvio handles resource creation, IAM permissions, and packaging.

Key areas of the codebase:

- **`stelvio/`** — library source
  - `component.py` — `Component` base class and `ComponentRegistry`
  - `link.py` — link system (automatic IAM permissions between components)
  - `app.py` — `StelvioApp` singleton, orchestrates deployment
  - `context.py` — `AppContext` with app name, environment, AWS config
  - `aws/` — all AWS components (`function/`, `api_gateway/`, `dynamo_db.py`, `queue.py`, `topic.py`, `s3/`, `cloudfront/`, `email.py`, `cron.py`, `layer.py`, `acm.py`)
  - `cli/` — `stlv` CLI commands (deploy, destroy, dev, diff, etc.)
- **`tests/`** — unit tests (Pulumi mocks, no AWS credentials needed)
- **`tests/integration/`** — integration tests (deploy real AWS resources)
- **`docs/`** — MkDocs Material documentation site

## Development

### Tests

Stelvio has unit tests and integration tests.

**Unit tests** use Pulumi mocks and don't need AWS credentials:

```bash
uv run pytest                  # run all unit tests
uv run pytest --cov            # with coverage
```

**Integration tests** deploy real AWS resources. Most contributors won't need to run these — unit tests are sufficient for the majority of changes. Integration tests are primarily for verifying infrastructure behavior and are split into two tiers:

*Standard tier* — tests core components (DynamoDB, Lambda, SQS, SNS, S3, API Gateway, CloudFront, etc.). Requires an AWS profile with permissions to create these resources:

```bash
STELVIO_TEST_AWS_PROFILE=<profile> uv run pytest tests/integration/ --integration -v -n 8
```

*DNS tier* — tests that need a Route 53 hosted zone for DNS validation (ACM certificates, CloudFront custom domains, SES domain identities). Slower due to DNS/certificate propagation:

```bash
STELVIO_TEST_AWS_PROFILE=<profile> \
  STELVIO_TEST_DNS_DOMAIN=<domain> \
  STELVIO_TEST_DNS_ZONE_ID=<zone-id> \
  uv run pytest tests/integration/test_dns_*.py --integration-dns -v -n 3
```

Both tiers run in parallel. Use `-k` to filter by component, e.g. `pytest -k "dynamo"`.

### Linting and formatting

```bash
uv run ruff format             # format code
uv run ruff check --fix        # lint and auto-fix
```

### Documentation

```bash
uv run mkdocs serve            # preview docs locally at http://127.0.0.1:8000
```

## Contribution Process

1. Create a branch for your work: `git checkout -b feature/your-feature-name`
2. Make your changes
3. Write tests for any code you add or modify
4. Update documentation if needed
5. Ensure tests pass and linting is clean (see [Development](#development))
6. Commit your changes with descriptive messages
7. Push to your fork: `git push origin feature/your-feature-name`
8. Create a Pull Request to the `main` branch of the original repository

## Pull Request Guidelines

- Every code change should include appropriate tests
- Update documentation for any user-facing changes
- Keep PRs focused on a single change or feature
- Follow the existing code style
- Ensure all tests pass and linting is clean before submitting
- Provide a clear description of the changes in your PR

## Communication

- [GitHub Issues](https://github.com/stelviodev/stelvio/issues) for bug reports and feature requests
- [GitHub Discussions](https://github.com/stelviodev/stelvio/discussions) for general questions and discussions
- Email: team@stelvio.dev
- Twitter: [@stelviodev](https://twitter.com/stelviodev)

## Issue Reporting

When reporting issues, please include:

- A clear and descriptive title
- Steps to reproduce the behavior
- What you expected vs. what actually happened
- Your environment (OS, Python version, Stelvio version)

## Thank You!

Your contributions to open source, no matter how small, are greatly appreciated. Even if it's just fixing a typo in the documentation, it helps make Stelvio better for everyone.
