from functools import cache
from pathlib import Path


@cache
def get_project_root() -> Path:
    """Find and cache the project root by looking for stlv_app.py.
    Raises ValueError if not found.
    """
    start_path = Path.cwd().resolve()

    current = start_path
    while current != current.parent:
        if (current / "stlv_app.py").exists():
            return current
        current = current.parent

    raise ValueError("Could not find project root: no stlv_app.py found in parent directories")


def get_dot_stelvio_dir() -> Path:
    return get_project_root() / ".stelvio"
