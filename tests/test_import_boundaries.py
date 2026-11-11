"""Keep deployment code and laptop networking imports directed through shared data."""

import ast
from importlib.util import resolve_name
from pathlib import Path

from pytest import mark


def _imports(path, source):
    package = ".".join(path.parent.parts)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            yield node.lineno, module
            yield from ((node.lineno, f"{module}.{alias.name}") for alias in node.names)


def _violations(path, source):
    forbidden = ()
    if path == Path("stelvio/network.py"):
        forbidden = ("stelvio",)
    elif path.parts[:2] == ("stelvio", "aws"):
        forbidden = ("stelvio.tunnel",)
    elif path.parts[:2] == ("stelvio", "tunnel"):
        forbidden = ("stelvio.aws", "stelvio.component", "stelvio.command_run", "stelvio.cli")
    elif path in (Path("stelvio/command_run.py"), Path("stelvio/stack_outputs.py")):
        forbidden = ("stelvio.tunnel", "stelvio.bridge")
    return [
        f"{path}:{line}: forbidden import {module}"
        for line, module in _imports(path, source)
        if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden)
    ]


def test_import_boundaries():
    root = Path(__file__).resolve().parents[1]
    violations = [
        violation
        for path in sorted((root / "stelvio").rglob("*.py"))
        for violation in _violations(path.relative_to(root), path.read_text())
    ]
    assert not violations, "\n".join(violations)


@mark.parametrize(
    ("path", "source", "module", "line"),
    [
        ("stelvio/network.py", "from . import context", "stelvio.context", 1),
        ("stelvio/aws/vpc/vpc.py", "import stelvio.tunnel as t", "stelvio.tunnel", 1),
        ("stelvio/aws/vpc/vpc.py", "from stelvio import tunnel as t", "stelvio.tunnel", 1),
        ("stelvio/aws/vpc/vpc.py", "from ...tunnel import transport", "stelvio.tunnel", 1),
        (
            "stelvio/tunnel/transport.py",
            "def connect():\n    from ..aws import vpc",
            "stelvio.aws",
            2,
        ),
        ("stelvio/tunnel/transport.py", "from .. import component", "stelvio.component", 1),
        ("stelvio/tunnel/transport.py", "import stelvio.command_run", "stelvio.command_run", 1),
        ("stelvio/tunnel/transport.py", "from stelvio import cli", "stelvio.cli", 1),
        ("stelvio/command_run.py", "from .bridge import local", "stelvio.bridge", 1),
        ("stelvio/stack_outputs.py", "from .tunnel import transport", "stelvio.tunnel", 1),
    ],
)
def test_boundary_scan_resolves_imports_and_reports_location(path, source, module, line):
    assert f"{path}:{line}: forbidden import {module}" in _violations(Path(path), source)


@mark.parametrize(
    ("path", "source"),
    [
        ("stelvio/network.py", "from dataclasses import dataclass"),
        ("stelvio/aws/vpc/vpc.py", "from stelvio.network import SSH_USER"),
        ("stelvio/tunnel/transport.py", "from ..network import SSH_USER"),
        ("stelvio/command_run.py", "from .stack_outputs import read_network_manifest"),
        ("stelvio/stack_outputs.py", "from .network import NetworkManifest"),
        ("stelvio/tunnel/transport.py", "import stelvio.aws_inventory"),
    ],
)
def test_boundary_scan_allows_shared_data_and_unrelated_prefixes(path, source):
    assert _violations(Path(path), source) == []
