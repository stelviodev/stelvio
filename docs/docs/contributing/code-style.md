# Code style

Ruff owns formatting and linting: line length 99, Python 3.12 syntax, rule set in
`pyproject.toml`. CI runs `ruff format --check` and `ruff check`, so run both first:

```bash
uv run ruff format
uv run ruff check --fix
```

## House rules

- Full type annotations. `str | None`, not `Optional[str]`.
- Value types (Resources, configs) are frozen dataclasses; components and Resources are
  `@final`.
- Validate early. Raise `ValueError` or `TypeError` in `__init__` naming the bad value and
  the accepted shapes, not halfway through a deploy.
- Module-level pure functions over methods when the logic doesn't need `self`.
- Keep backwards compatibility unless the change is meant to break it.
- `# noqa` is a smell. Only when the fix is worse than the suppression, with the reason on
  the same line or the line above: `# noqa: X  # why`.

## Comments say why, not what

Well-named code already says what it does. A comment earns its place when it records
something the code can't: a decision, a rejected alternative, an AWS quirk, a bug it
guards against, a link to the source. Comments that group a file into sections are fine
too, and common in tests.

```python
# Glob pattern
files = project_root.rglob(pattern)
```

The reader can see it's a glob. Delete the comment. Compare:

```python
# Always execute-api HTTPS, never the custom-domain or wss client URL.
return self._execute_api_url("https")
```

That one stops the next person from "fixing" it. Same rule for docstrings: a class
docstring that restates the class name is noise; one that says what the class is for and
what it creates is documentation.

Component conventions: [Writing components](components.md). Test patterns:
[Writing unit tests](unit-tests.md), [Writing integration tests](integration-tests.md).
