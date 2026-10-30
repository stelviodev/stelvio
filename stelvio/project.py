import logging
import re
import unicodedata
from functools import cache
from pathlib import Path

from stelvio.exceptions import StelvioProjectError

logger = logging.getLogger(__name__)


@cache
def get_project_root() -> Path:
    """Find and cache the project root by looking for stlv_app.py.
    Raises StelvioProjectError if not found: the CLI shows it as the error, not a traceback.
    """
    start_path = Path.cwd().resolve()

    current = start_path
    while current != current.parent:
        if (current / "stlv_app.py").exists():
            return current
        current = current.parent

    raise StelvioProjectError(
        "No Stelvio project found. Run 'stlv init' to create a new project in this directory."
    )


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


# Letters NFKD can't split into base + accent: the common European ones, mapped by hand.
_UNSPLITTABLE = str.maketrans(
    {
        "Ł": "L",
        "ł": "l",
        "Ø": "O",
        "ø": "o",
        "Đ": "D",
        "đ": "d",
        "Æ": "AE",
        "æ": "ae",
        "Œ": "OE",
        "œ": "oe",
        "ß": "ss",
    }
)


def clean_name(raw: str) -> str:
    """Lowercase, and each run of chars other than letters and digits becomes one '-'. For
    names the user never typed: the default env from the OS username, the app name `stlv init`
    takes from the folder. A saved `.stelvio/userenv` is read as is, so a user who deployed
    before the cleaning keeps their stack; a fresh clone saves the cleaned name.

    The app and env lead every AWS name, and S3, Lambda and DocumentDB each reject a char the
    others accept, so only letters, digits and '-' are safe everywhere. NFKD first splits an
    accent off its letter: `José` becomes `jose`, not `jos`; `_UNSPLITTABLE` covers the letters
    it can't (`Łukasz` becomes `lukasz`). Any other char with no ASCII form is dropped.
    """
    folded = unicodedata.normalize("NFKD", raw.translate(_UNSPLITTABLE))
    ascii_name = folded.encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", ascii_name).strip("-").lower()
