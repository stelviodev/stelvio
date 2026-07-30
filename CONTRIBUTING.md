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
- **`docs/`** — Zensical documentation site

## Development

### Writing code and docs

The contributor guides on stelvio.dev cover the patterns we follow:

- [Writing components](https://stelvio.dev/docs/contributing/components/)
- [Writing unit tests](https://stelvio.dev/docs/contributing/unit-tests/)
- [Writing integration tests](https://stelvio.dev/docs/contributing/integration-tests/)
- [Writing documentation](https://stelvio.dev/docs/contributing/docs/)

### Tests

Stelvio has unit tests and integration tests.

**Unit tests** use Pulumi mocks and don't need AWS credentials:

```bash
uv run pytest                  # run all unit tests
uv run pytest --cov            # with coverage
```

**Integration tests** deploy real AWS resources, assert against them with boto3, then destroy them. They're the release gate: if you add or change infrastructure behavior, add or update integration tests too (see [Writing integration tests](https://stelvio.dev/docs/contributing/integration-tests/)). Running them is a separate question — they need an AWS account and take minutes, and CI runs them only on manual dispatch. Run them if you can; say so in your PR if you can't.

There are three tiers — standard, CloudFront (slow teardown), and DNS (needs a Route 53 hosted zone). `tests/integration/run_all.sh` is the single source of truth for tiers and worker counts and runs them all in parallel:

```bash
STLV_TEST_AWS_PROFILE=<profile> ./tests/integration/run_all.sh
```

The DNS tier runs only when `STLV_TEST_DNS_DOMAIN` and `STLV_TEST_DNS_ZONE_ID` are also set. To run a single tier or component, take the pytest command for that tier from `run_all.sh`; use `-k` to filter by component, e.g. `-k "dynamo"`.

### Linting and formatting

```bash
uv run ruff format             # format code
uv run ruff check --fix        # lint and auto-fix
```

### Documentation

```bash
uv run zensical serve            # preview docs locally at http://127.0.0.1:8000
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
