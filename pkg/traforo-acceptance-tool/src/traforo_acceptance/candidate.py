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


def validate(source: Path) -> None:
    required = {
        "tests/integration/cli_session.py": {"StandaloneCliSession", "deploy", "destroy"},
        "tests/integration/macos_host.py": {"MacOSHost", "prepare", "save", "restore"},
        "pkg/traforo-client-go/scripts/build.py": {"build", "goreleaser_version"},
    }
    for relative, names in required.items():
        path = source / relative
        if not path.is_file():
            raise RuntimeError(f"Adapter {ADAPTER}: missing {relative}")
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
