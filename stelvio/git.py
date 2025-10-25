import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def _get_git_executable() -> str:
    """Get the full path to the git executable."""
    git_path = shutil.which("git")
    if git_path is None:
        raise RuntimeError("Git is not installed or not found in PATH.")
    return git_path


def _validate_github_identifier(value: str, name: str) -> None:
    """Validate GitHub identifiers (owner, repo, branch) using strict regex patterns."""
    # GitHub usernames/org names: alphanumeric, hyphens, max 39 chars
    if name == "owner":
        if not re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$", value):
            msg = f"Invalid GitHub {name}: must be alphanumeric with hyphens, 1-39 chars"
            raise ValueError(msg)
    # Repository names: alphanumeric, hyphens, underscores, dots, max 100 chars
    elif name == "repo":
        if not re.match(r"^[a-zA-Z0-9._-]{1,100}$", value):
            msg = f"Invalid GitHub {name}: must be alphanumeric with ._-, max 100 chars"
            raise ValueError(msg)
    # Branch names: more flexible but still constrained
    elif name == "branch" and (not re.match(r"^[a-zA-Z0-9._/-]{1,250}$", value) or ".." in value):
        raise ValueError(f"Invalid {name}: contains prohibited characters or patterns")


def _validate_subdirectory(subdirectory: str) -> None:
    """Validate subdirectory path to prevent traversal and injection."""
    if not re.match(r"^[a-zA-Z0-9._/-]+$", subdirectory):
        raise ValueError("Subdirectory contains invalid characters")
    if ".." in subdirectory or subdirectory.startswith("/"):
        raise ValueError("Subdirectory cannot contain '..' or start with '/'")


def _run_git_command(git_executable: str, args: list[str], cwd: Path | None = None) -> None:
    """Safely run a git command with validated arguments."""
    # Ensure git_executable is the expected git binary
    if not git_executable.endswith(("git", "git.exe")):
        raise ValueError("Invalid git executable path")

    # Additional validation: ensure the executable is actually git
    if not Path(git_executable).exists():
        raise ValueError("Git executable does not exist")

    # Allow-list of safe git commands and arguments
    safe_commands = {
        "clone",
        "sparse-checkout",
        "init",
        "add",
        "--branch",
        "--single-branch",
        "--depth",
        "--filter=blob:none",
        "--sparse",
        "--cone",
    }

    # Validate first argument is a safe git command
    if (
        args
        and args[0] not in safe_commands
        and not (
            args[0].startswith("https://github.com/")
            or args[0].startswith("/")
            or Path(args[0]).is_absolute()
            or re.match(r"^[a-zA-Z0-9._/-]+$", args[0])
        )
    ):
        raise ValueError(f"Disallowed git command or argument: {args[0]}")

    # Validate that all arguments are safe strings
    for arg in args:
        if not isinstance(arg, str):
            raise TypeError("All git arguments must be strings")
        # Prevent command injection through arguments
        dangerous_chars = [";", "&", "|", "`", "$", "$(", "${", ">", "<", "\n", "\r"]
        if any(char in arg for char in dangerous_chars):
            raise ValueError(f"Unsafe characters in git argument: {arg}")

    # Build command list efficiently
    cmd_list = [git_executable, *args]

    # Use subprocess.run with explicit safety parameters
    # S603 is a false positive here: we validate the git executable path,
    # use an allow-list for commands/args, sanitize all inputs, and use shell=False
    try:
        subprocess.run(  # noqa: S603
            cmd_list,
            check=True,
            shell=False,  # Never use shell=True
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Git command failed: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Git command timed out: {e}") from e


def _checkout_from_github(
    owner: str,
    repo: str,
    branch: str = "main",
    subdirectory: str | None = None,
    destination: Path | str = ".",
) -> Path:
    git_executable = _get_git_executable()

    if not isinstance(destination, Path):
        destination = Path(destination)

    # Validate all inputs with strict patterns
    _validate_github_identifier(owner, "owner")
    _validate_github_identifier(repo, "repo")
    _validate_github_identifier(branch, "branch")

    if subdirectory:
        _validate_subdirectory(subdirectory)

    repo_url = f"https://github.com/{owner}/{repo}.git"
    clone_args = ["clone", "--branch", branch, repo_url, destination.as_posix()]

    if subdirectory:
        clone_args += ["--single-branch", "--depth", "1", "--filter=blob:none", "--sparse"]

        # Run git clone
        _run_git_command(git_executable, clone_args)

        # Configure sparse checkout
        _run_git_command(git_executable, ["sparse-checkout", "init", "--cone"], cwd=destination)
        _run_git_command(git_executable, ["sparse-checkout", "add", subdirectory], cwd=destination)
    else:
        _run_git_command(git_executable, clone_args)

    # Remove .git directory to detach from git history
    git_dir = destination / ".git"
    if git_dir.exists() and git_dir.is_dir():
        shutil.rmtree(git_dir)

    return destination / (subdirectory if subdirectory else "")


def copy_from_github(
    owner: str,
    repo: str,
    branch: str = "main",
    subdirectory: str | None = None,
    destination: Path | str = ".",
) -> Path:
    with tempfile.TemporaryDirectory() as tmpdirname:
        temp_path = Path(tmpdirname)
        _checkout_from_github(owner, repo, branch, subdirectory, temp_path)
        src_path = temp_path / (subdirectory if subdirectory else "")
        dest_path = Path(destination)
        if not dest_path.exists():
            dest_path.mkdir(parents=True)
        for item in src_path.iterdir():
            dest_item = dest_path / item.name
            if item.is_dir():
                shutil.copytree(item, dest_item)
            else:
                shutil.copy2(item, dest_item)
    return dest_path
