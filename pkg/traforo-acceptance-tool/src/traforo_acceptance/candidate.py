"""Versioned adapter for the stack's build and acceptance interfaces."""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
from pathlib import Path

ADAPTER = 1


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, timeout=60).strip()


def fingerprint(source: Path) -> str:
    names = subprocess.check_output(
        ["git", "-C", str(source), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        timeout=60,
    ).split(b"\0")
    result = hashlib.sha256()
    for name in sorted(set(names)):
        if not name:
            continue
        path = source / os.fsdecode(name)
        result.update(name + b"\0")
        if path.is_symlink():
            result.update(os.readlink(path).encode())
        elif path.is_file():
            result.update(path.read_bytes())
        else:
            result.update(b"<deleted>")
    result.update(git(source, "diff", "HEAD", "--binary").encode())
    return result.hexdigest()


def checkout(repo: Path, target: str, patches: tuple[str, ...], destination: Path) -> dict:
    repo = Path(git(repo, "rev-parse", "--show-toplevel"))
    commit = git(repo, "rev-parse", "--verify", f"{target}^{{commit}}")
    resolved = []
    for patch in patches:
        revision = git(repo, "rev-parse", "--verify", f"{patch}^{{commit}}")
        parents = git(repo, "rev-list", "--parents", "-n", "1", revision).split()
        if len(parents) != 2:
            raise ValueError("Patch commits must have exactly one parent")
        resolved.append(revision)
    subprocess.run(
        ["git", "clone", "--no-hardlinks", "--no-checkout", str(repo), str(destination)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    subprocess.run(
        ["git", "-C", str(destination), "checkout", "--detach", commit],
        check=True,
        capture_output=True,
        timeout=60,
    )
    for index, revision in enumerate(resolved):
        patch = subprocess.check_output(
            ["git", "-C", str(repo), "diff", f"{revision}^", revision, "--binary"], timeout=60
        )
        (destination.parent / f"patch-{index:02d}-{revision}.diff").write_bytes(patch)
        if not patch:
            raise ValueError(f"Empty patch: {revision}")
        subprocess.run(
            ["git", "-C", str(destination), "apply", "--check", "-"],
            input=patch,
            check=True,
            capture_output=True,
            timeout=60,
        )
        subprocess.run(
            ["git", "-C", str(destination), "apply", "-"],
            input=patch,
            check=True,
            capture_output=True,
            timeout=60,
        )
    diff = git(destination, "diff", "HEAD", "--binary")
    (destination.parent / "candidate.diff").write_text(diff)
    return {
        "commit": commit,
        "patches": resolved,
        "fingerprint": fingerprint(destination),
        "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
        "adapter": ADAPTER,
    }


def validate(source: Path, selected=()) -> None:
    required = {
        "tests/integration/cli_session.py": {"StandaloneCliSession", "deploy", "destroy"},
        "tests/integration/macos_host.py": {"MacOSHost", "prepare", "save", "restore"},
        "pkg/traforo-client-go/scripts/build.py": {"build", "goreleaser_version"},
    }
    if "aws-lambda-vpc" in selected:
        required["tests/integration/lambda_tunnel_session.py"] = {
            "LambdaTunnelSession",
            "PrivateApi",
            "create",
            "destroy",
            "deploy",
            "retry_api_throttling",
            "stopped_bastion",
            "write_evidence",
            "check_plain_clients",
        }
        required["tests/integration/test_tunnel_lambda.py"] = set()
    for relative, names in required.items():
        path = source / relative
        if not path.is_file():
            raise RuntimeError(
                f"Adapter {ADAPTER}: missing {relative}. For aws-lambda-vpc, select a candidate "
                "containing PR #411 at 49eafd2a, or explicitly apply "
                "ea1d9144, e196a31d, and 49eafd2a "
                "with --patch. No patches are fetched or applied automatically."
            )
        symbols = {
            node.name
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        }
        if not names <= symbols:
            raise RuntimeError(
                f"Adapter {ADAPTER}: unsupported interface in {relative}: {names - symbols}"
            )
    for name in ("test_tunnel_host.py", "test_tunnel_dns.py", "test_tunnel.py"):
        if not (source / "tests/integration" / name).is_file():
            raise RuntimeError(f"Missing acceptance suite {name}")
    if "aws-lambda-vpc" in selected:
        from .stages import LAMBDA_TESTS

        suite = ast.parse((source / "tests/integration/test_tunnel_lambda.py").read_text())
        actual = {
            n.name
            for n in suite.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
        }
        if actual != LAMBDA_TESTS:
            raise RuntimeError(f"Unsupported Lambda acceptance cases: {sorted(actual)}")
        fixture = ast.parse((source / "tests/integration/lambda_tunnel_session.py").read_text())
        classes = {n.name: n for n in fixture.body if isinstance(n, ast.ClassDef)}
        for name, methods in {
            "LambdaTunnelSession": {
                "deploy",
                "destroy",
                "stopped_bastion",
                "check_plain_clients",
                "write_evidence",
            },
            "PrivateApi": {"create", "destroy"},
        }.items():
            if name not in classes or not methods <= {
                n.name for n in classes[name].body if isinstance(n, ast.FunctionDef)
            }:
                raise RuntimeError(f"Unsupported Lambda adapter class: {name}")
        for relative in (
            "tests/integration/handlers/tunnel_lambda_app.py.txt",
            "tests/integration/handlers/tunnel_lambda_probe.py",
        ):
            if not (source / relative).is_file():
                raise RuntimeError(f"Missing Lambda fixture input: {relative}")
        base = ast.parse((source / "tests/integration/cli_session.py").read_text())
        base_class = next(
            (
                n
                for n in base.body
                if isinstance(n, ast.ClassDef) and n.name == "StandaloneCliSession"
            ),
            None,
        )
        if base_class is None or "wait_host_key_refusal" not in {
            n.name for n in base_class.body if isinstance(n, ast.FunctionDef)
        }:
            raise RuntimeError("Unsupported Lambda adapter: missing wait_host_key_refusal")
    workflow = (source / "pkg/traforo-client-go/.github/workflows/release.yml").read_text()
    if not re.search(r"version:\s*[\"']?v?2\.18\.3", workflow):
        raise RuntimeError("Adapter requires review for this GoReleaser workflow")


def tool_fingerprint(directory: Path) -> str:
    value = hashlib.sha256()
    for path in sorted(directory.glob("*.py")):
        value.update(path.name.encode() + b"\0" + path.read_bytes())
    return value.hexdigest()


def freeze_tool(root: Path) -> str:
    import shutil

    original = Path(__file__).parent
    archived = root / "observer/traforo_acceptance"
    archived.mkdir(parents=True)
    for path in original.glob("*.py"):
        shutil.copyfile(path, archived / path.name)
    return tool_fingerprint(archived)
