# Contributing to Stelvio

First off, thank you for considering contributing to Stelvio! Contributions of all kinds are welcome and valued, from code improvements to documentation updates. Every contribution, no matter how small, helps make Stelvio better for everyone.

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md). It's very short and simple - basically just be nice and stay on topic.

## Contributor License Agreement

Stelvio uses a Contributor License Agreement (CLA) to ensure that the project has the necessary rights to use your contributions. When you submit your first PR, our CLA Assistant bot will guide you through the signing process. This is a one-time process for all your future contributions.

The CLA protects both contributors and the project by clearly defining the terms under which code is contributed.

## Getting Started

The quickest way to get started with development is to:

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/stelvio.git`
3. Add the original repo as upstream: `git remote add upstream https://github.com/michal-stlv/stelvio.git`
4. Install UV if not already installed (see [UV installation docs](https://github.com/astral-sh/uv?tab=readme-ov-file#installation))
5. Set up the development environment: `uv sync`
6. Run tests to make sure everything works: `uv run pytest`
7. Run docs locally with: `uv run mkdocs serve`
8. Format code with Ruff: `uv run ruff format`
9. Check code with Ruff: `uv run ruff check`

For a more detailed guide on using Stelvio, please refer to our [Quick Start Guide](https://docs.stelvio.dev/getting-started/quickstart/).

## Contribution Process

1. Create a branch for your work: `git checkout -b feature/your-feature-name`
2. Make your changes
3. Write tests for any code you add or modify
4. Update documentation if needed
5. Ensure all tests pass: `uv run pytest`
6. Commit your changes with descriptive messages
7. Push to your fork: `git push origin feature/your-feature-name`
8. Create a Pull Request to the `main` branch of the original repository

## Pull Request Guidelines

- Every code change should include appropriate tests
- Update documentation for any user-facing changes
- Keep PRs focused on a single change or feature
- Follow the existing code style
- Format your code with Ruff: `uv run ruff format`
- Ensure all linting checks pass: `uv run ruff check`
- Ensure all tests pass before submitting: `uv run pytest`
- Provide a clear description of the changes in your PR

## Communication

Have questions or suggestions? Here are the best ways to reach out:

- [GitHub Issues](https://github.com/michal-stlv/stelvio/issues) for bug reports and feature requests
- [GitHub Discussions](https://github.com/michal-stlv/stelvio/discussions) for general questions and discussions
- Email: michal@stelvio.dev
- Twitter: [@michal_stlv](https://twitter.com/michal_stlv)

## Issue Reporting

When reporting issues, please include:

- A clear and descriptive title
- A detailed description of the issue
- Steps to reproduce the behavior
- What you expected to happen
- What actually happened
- Your environment (OS, Python version, Stelvio version)

## Thank You!

Your contributions to open source, no matter how small, are greatly appreciated. Even if it's just fixing a typo in the documentation, it helps make Stelvio better for everyone.
