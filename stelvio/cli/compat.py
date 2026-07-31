"""Compatibility CLI commands for Stelvio upgrades."""

import click


@click.group()
def compat() -> None:
    """One-shot helpers for breaking upgrades."""
