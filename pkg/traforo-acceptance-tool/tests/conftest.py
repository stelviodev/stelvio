from pathlib import Path

import pytest

from traforo_acceptance.store import Store


@pytest.fixture
def run_root(tmp_path):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    Store(root).save(
        {
            "schema": 1,
            "selected": ["prepare"],
            "configuration": {
                "go": "1.27.3",
                "profile": "default",
                "region": "us-east-1",
                "timeouts": {},
            },
            "results": {},
            "artifacts": {},
        }
    )
    return root


@pytest.fixture
def repo(tmp_path):
    import subprocess

    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    (path / "file").write_text("base\n")
    commit(path, "base")
    return path


def commit(repo: Path, message):
    import subprocess

    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True)
