import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, final

from stelvio.project import get_dot_stelvio_dir

# A temp dir's mtime stays at the install's start, so the limit must outlast a slow install.
_IN_PROGRESS_MAX_AGE_SECONDS: Final[int] = 24 * 3600
_UNUSED_CACHE_MAX_AGE_SECONDS: Final[int] = 7 * 24 * 3600
_FILE_REFERENCE_PATTERN: Final[re.Pattern] = re.compile(
    r"^\s*(-r|-c|--requirement|--constraint)(?:\s+|=)(\S+)", re.MULTILINE
)
_CONSTRAINT_FLAGS: Final[frozenset[str]] = frozenset({"-c", "--constraint"})
_LEGACY_MANYLINUX_ALIASES: Final[dict[int, str]] = {
    5: "manylinux1",
    12: "manylinux2010",
    17: "manylinux2014",
}
# pip's rule: `#` starts a comment at the line start or after whitespace, so a URL fragment
# (`pkg @ https://x/p.whl#sha256=...`) stays.
_COMMENT_PATTERN: Final[re.Pattern] = re.compile(r"(^|\s+)#.*$")

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True)
class RequirementsSpec:
    content: str | None = None
    path_from_root: Path | None = None


def get_or_install_dependencies(  # noqa: PLR0913
    requirements_source: RequirementsSpec,
    runtime: str,
    architecture: str,
    project_root: Path,
    cache_subdirectory: str,
    log_context: str,
) -> Path:
    """
    Checks cache, installs dependencies if needed using uv or pip, and returns
    the absolute path to the populated cache directory.

    Args:
        requirements_source: The source of requirements (content and path_from_root).
        runtime: The target Python runtime (e.g., "python3.12").
        architecture: The target architecture (e.g., "x86_64").
        project_root: Absolute path to the project root.
        cache_subdirectory: Subdirectory within .stelvio/lambda_dependencies
                            (e.g., "functions", "layers").
        log_context: A string identifier for logging (e.g., function or layer name).

    Returns:
        Absolute path to the cache directory containing installed dependencies.

    Raises:
        RuntimeError: If installation fails or required tools (uv/pip) are missing.
        FileNotFoundError: If a referenced requirements file cannot be found.
        ValueError: If requirements paths resolve outside the project root.
    """
    # A shape check only, no allow-list: the version is parsed below, and a typo must not
    # surface as an unpacking error.
    if not re.fullmatch(r"python\d+\.\d+", runtime):
        raise ValueError(f"[{log_context}] runtime must look like 'python3.12', got {runtime!r}.")
    py_version = runtime[6:]

    cache_key = _calculate_cache_key(requirements_source, architecture, py_version, project_root)

    dependencies_dir = _get_lambda_dependencies_dir(cache_subdirectory)
    cache_dir = dependencies_dir / cache_key

    # The dir's mtime is its last use; clean_stale_dependency_caches() reads it. A missing dir
    # is a miss below, and a read-only tree must not fail the install.
    with contextlib.suppress(OSError):
        os.utime(cache_dir)

    if _is_filled(cache_dir):
        logger.info("[%s] Cache hit for key '%s'.", log_context, cache_key)
        return cache_dir
    # Older Stelvio versions installed straight into the cache dir, so an interrupted install
    # left it empty, or with uv's `.lock` alone. Removed, because a rename onto it fails on
    # Windows.
    with contextlib.suppress(OSError):  # another stlv run may be filling it right now
        (cache_dir / ".lock").unlink(missing_ok=True)
        cache_dir.rmdir()

    logger.info("[%s] Cache miss for key '%s'. Installing dependencies.", log_context, cache_key)

    installer_cmd, install_flags = _get_installer_command(architecture, py_version)
    dependencies_dir.mkdir(parents=True, exist_ok=True)
    # Installed into a temp dir and renamed into place on success, so an interrupted install
    # (Ctrl-C) leaves no directory that the next run takes for a cache hit.
    with tempfile.TemporaryDirectory(dir=dependencies_dir, prefix=f".{cache_key}-") as tmp:
        if requirements_source.path_from_root is None:
            # pip 26 refuses `-r -` (stdin), so inline requirements go through a file.
            requirements_file = Path(tmp, "requirements.txt")
            requirements_file.write_text(requirements_source.content, encoding="utf-8")
        else:
            requirements_file = (project_root / requirements_source.path_from_root).resolve()
        target_dir = Path(tmp, "packages")
        target_dir.mkdir()
        cmd = [
            *installer_cmd,
            "install",
            "-r",
            str(requirements_file),
            "--target",
            str(target_dir),
            *install_flags,
        ]
        logger.info("[%s] Running dependency installation command: %s", log_context, " ".join(cmd))
        # From the project root, so `--find-links ./wheels` or `./dist/x.whl` in a requirements
        # file mean the same wherever `stlv` is run from.
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            check=False,
            cwd=project_root,
            # The stderr goes into the error the user reads; uv's glyphs are UTF-8 on every OS.
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            logger.error(
                "[%s] Installation command failed.\nCommand: %s\nStderr:\n%s",
                log_context,
                " ".join(cmd),
                result.stderr,
            )
            # The CLI logs to a file, so the installer's own words have to be in the exception.
            # Indented: the error block shows everything after the program's traceback, and
            # pip's own crash output carries a traceback header of its own at column 0. Blank
            # lines only are stripped: uv indents its whole message.
            stderr = textwrap.indent(result.stderr.strip("\n"), "  ")
            raise RuntimeError(
                f"[{log_context}] Dependency install failed:\n"
                f"{stderr or f'exit code {result.returncode}'}"
            )
        logger.debug("[%s] Installation successful. Stdout:\n%s", log_context, result.stdout)
        try:
            target_dir.rename(cache_dir)
        except OSError:
            # Another stlv run on this project installed the same key first.
            if not _is_filled(cache_dir):
                raise
    logger.info("[%s] Dependencies installed into %s.", log_context, cache_dir)
    return cache_dir


def requirements_spec(requirements: str | list[str] | bool | None) -> RequirementsSpec | None:
    """A requirements file path or an inline list as a RequirementsSpec; None when there is
    nothing to install (False, None, an empty or blank-only list)."""
    if isinstance(requirements, str):
        return RequirementsSpec(path_from_root=Path(requirements))
    if isinstance(requirements, list):
        lines = [line for line in requirements if line and line.strip()]
        return RequirementsSpec(content="\n".join(lines)) if lines else None
    return None


def _is_filled(cache_dir: Path) -> bool:
    """A cache dir with packages in it. uv writes `.lock` into the target before resolving, so an
    install interrupted under an older Stelvio version left `.lock` alone behind."""
    try:
        return any(entry.name != ".lock" for entry in cache_dir.iterdir())
    except OSError:  # missing, or removed by another run's cleanup between a check and this read
        return False


def _normalize_requirements(
    content: str,
    current_file_path_relative: Path | None = None,
    project_root: Path | None = None,
    visited: set[tuple[Path | None, str]] | None = None,
    prefix: str = "",
) -> Generator[str, None, None]:
    """
    Normalizes requirements content for consistent hashing.
    - Strips whitespace and comments, drops empty lines.
    - If current_file_path_relative is provided, resolves paths in `-r`/`-c` lines (long forms
      too) relative to the project root and recursively expands their content.
    - Lines reached through a constraint file get a `-c ` prefix: a constraint installs nothing
      by itself, so the same file must hash apart as `-c` and as `-r`. A file reached both ways
      is expanded both ways, so the visited set is keyed by (file, prefix).
    The caller sorts.
    """
    if visited is None:
        visited = set()

    if (current_file_path_relative, prefix) in visited:
        logger.warning(
            "Circular dependency detected: skipping already visited %s", current_file_path_relative
        )
        return

    visited.add((current_file_path_relative, prefix))

    # Determine the directory context for resolving relative paths within this content
    current_dir_abs = None
    if current_file_path_relative and project_root:
        current_dir_abs = (project_root / current_file_path_relative).parent

    for raw_line in content.splitlines():
        line = _COMMENT_PATTERN.sub("", raw_line).strip()
        if not line:
            continue

        match = _FILE_REFERENCE_PATTERN.match(line)
        # Only attempt to resolve if we work with requirements file
        if match and current_dir_abs:
            flag, reference_path_str = match.groups()
            # Resolve the referenced path relative to the current file's directory
            reference_path_abs = (current_dir_abs / reference_path_str).resolve()
            try:
                reference_path_project_relative = reference_path_abs.relative_to(project_root)
            except ValueError:
                raise ValueError(
                    f"Requirements file '{reference_path_str}' "
                    f"referenced from {current_file_path_relative} "
                    f"resolves to '{reference_path_abs}', "
                    f"which is outside the project root '{project_root}'."
                ) from None
            reference_content = _get_requirements_content(
                reference_path_project_relative, project_root
            )
            yield from _normalize_requirements(
                reference_content,
                reference_path_project_relative,
                project_root,
                visited,
                "-c " if prefix or flag in _CONSTRAINT_FLAGS else "",
            )
        else:
            yield prefix + line


def _get_requirements_content(relative_path: Path, project_root: Path) -> str:
    abs_path = _get_abs_requirements_path(relative_path, project_root)
    # utf-8-sig: a BOM (Windows editors) would hide a `-r` on the first line from the regex
    return abs_path.read_text(encoding="utf-8-sig")


def _get_abs_requirements_path(relative_path: Path, project_root: Path) -> Path:
    abs_path = (project_root / relative_path).resolve()

    _validate_is_file_exists(abs_path)

    try:
        abs_path.relative_to(project_root)
    except ValueError:
        raise ValueError(
            f"Requirements file '{relative_path}' resolves to '{abs_path}', "
            f"which is outside the project root '{project_root}'."
        ) from None
    return abs_path


def _validate_is_file_exists(abs_path: Path) -> None:
    if not abs_path.is_file():
        if not abs_path.exists():
            raise FileNotFoundError(f"Requirements file not found: {abs_path}")
        raise ValueError(f"Requirements path is not a file: {abs_path}")


def _calculate_cache_key(
    source: RequirementsSpec, architecture: str, py_version: str, project_root: Path
) -> str:
    """
    Calculates a unique cache key based on requirements content, architecture, Python version
    and the manylinux target the wheels are resolved for.
    """
    if source.path_from_root is None:
        if _FILE_REFERENCE_PATTERN.search(source.content):
            raise ValueError(
                "'-r', '-c', '--requirement' and '--constraint' references are not allowed "
                "when providing requirements as a list."
            )
        normalized_requirements = _normalize_requirements(source.content)
    else:
        content = _get_requirements_content(source.path_from_root, project_root)
        normalized_requirements = _normalize_requirements(
            content, source.path_from_root, project_root
        )
    # The target picks the wheels, so a target change (a Stelvio upgrade) must be a miss.
    hashed = f"manylinux_2_{_glibc_minor(py_version)}\n" + "\n".join(
        sorted(normalized_requirements)
    )
    final_hash = hashlib.sha256(hashed.encode("utf-8")).hexdigest()

    return f"{architecture}__{py_version}__{final_hash[:16]}"


def _glibc_minor(py_version: str) -> int:
    """glibc 2.x of the runtime's OS: python3.12 and later run on Amazon Linux 2023 (2.34),
    python3.10 and 3.11 on Amazon Linux 2 (2.26, where 2.17 is the closest tag uv accepts)."""
    major, minor = (int(part) for part in py_version.split("."))
    return 34 if (major, minor) >= (3, 12) else 17


def _manylinux_tags(glibc_minor: int, platform_arch: str) -> list[str]:
    """The wheel tags a glibc runs, newest first: the set uv derives from one `--python-platform`
    (PEP 600 tags plus the legacy aliases; x86_64 goes back to manylinux1, aarch64 starts at
    manylinux2014)."""
    oldest = 17 if platform_arch == "aarch64" else 5
    tags = []
    for minor in range(glibc_minor, oldest - 1, -1):
        tags.append(f"manylinux_2_{minor}_{platform_arch}")
        if minor in _LEGACY_MANYLINUX_ALIASES:
            tags.append(f"{_LEGACY_MANYLINUX_ALIASES[minor]}_{platform_arch}")
    return tags


def _get_installer_command(architecture: str, py_version: str) -> tuple[list[str], list[str]]:
    """Determines the installer command (uv or pip) and necessary flags."""
    uv_path = shutil.which("uv")
    platform_arch = "aarch64" if architecture == "arm64" else "x86_64"
    glibc_minor = _glibc_minor(py_version)

    if uv_path:
        logger.info("Using 'uv' for dependency installation.")
        installer_cmd = [uv_path, "pip"]
        platform_flags = ["--python-platform", f"{platform_arch}-manylinux_2_{glibc_minor}"]
        pip_flags = []
    else:
        pip_path = shutil.which("pip")
        if not pip_path:
            raise RuntimeError(
                "Could not find 'pip' or 'uv'. Please ensure one is installed and in your PATH."
            )
        logger.info("Using 'pip' for dependency installation.")
        installer_cmd = [pip_path]
        # pip matches `--platform` tags literally, so each one is listed. `--no-compile`:
        # `--target` otherwise writes the host Python's .pyc files into the package.
        platform_flags = [
            flag
            for tag in _manylinux_tags(glibc_minor, platform_arch)
            for flag in ("--platform", tag)
        ]
        pip_flags = ["--implementation", "cp", "--no-compile"]

    install_flags = [
        *pip_flags,
        *platform_flags,
        "--python-version",
        py_version,
        "--only-binary=:all:",
    ]
    return installer_cmd, install_flags


def _lambda_dependencies_root() -> Path:
    return get_dot_stelvio_dir() / "lambda_dependencies"


def _get_lambda_dependencies_dir(cache_subdirectory: str) -> Path:
    return _lambda_dependencies_root() / cache_subdirectory


def clean_stale_dependency_caches() -> None:
    """Removes dependency caches no stlv run has used for a week, and install temp dirs a hard
    kill left behind. Use is the dir's mtime, set on every cache hit, so the caches of other
    envs and of overlapping runs stay."""
    root = _lambda_dependencies_root()
    if not root.is_dir():
        return
    now = time.time()
    for subdir in (path for path in root.iterdir() if path.is_dir()):
        # Earlier versions kept an in-use list per subdir. A read-only subdir (a cache baked
        # into a CI image) must not stop the cleanup of the others.
        with contextlib.suppress(OSError):
            (subdir / "active_caches.txt").unlink(missing_ok=True)
        for item in subdir.iterdir():
            # A `.`-prefixed temp install dir may belong to another stlv run that is still
            # installing; one left by a hard kill goes once it is old enough.
            in_progress = item.name.startswith(".")
            max_age = (
                _IN_PROGRESS_MAX_AGE_SECONDS if in_progress else _UNUSED_CACHE_MAX_AGE_SECONDS
            )
            try:
                # A symlink is the user's own arrangement: neither followed nor renamed away.
                if item.is_symlink() or not item.is_dir() or now - item.stat().st_mtime < max_age:
                    continue
                # A cache dir is renamed first: half-removed it must not be a cache hit, and as
                # a temp dir the next run's cleanup finishes it.
                doomed = item if in_progress else item.rename(item.with_name(f".{item.name}-old"))
            except OSError:  # another stlv run removed or renamed it in between
                continue
            # Another stlv run may remove it at the same time, and a failed cleanup must not
            # fail a deploy that worked.
            shutil.rmtree(doomed, ignore_errors=True)
