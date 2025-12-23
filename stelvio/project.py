import logging
from functools import cache
from pathlib import Path

logger = logging.getLogger(__name__)


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


@cache
def get_stelvio_lib_root() -> Path:
    """Get and cache the Stelvio library root directory."""
    stelvio_file = Path(__file__).resolve()
    return stelvio_file.parent


def get_dot_stelvio_dir() -> Path:
    return get_project_root() / ".stelvio"


def _read_metadata_file(filename: str) -> str | None:
    file_path = get_dot_stelvio_dir() / filename
    if file_path.exists() and file_path.is_file():
        return file_path.read_text().strip()
    return None


def _write_metadata_file(filename: str, content: str) -> None:
    stelvio_dir = get_dot_stelvio_dir()
    stelvio_dir.mkdir(exist_ok=True, parents=True)

    file_path = stelvio_dir / filename
    try:
        file_path.write_text(content)
        logger.debug("Saved %s: %s", filename, content)
    except Exception:
        logger.exception("Failed to write .stelvio/%s", filename)


def get_user_env() -> str | None:
    return _read_metadata_file("userenv")


def save_user_env(env: str) -> None:
    _write_metadata_file("userenv", env)
