import hashlib
import itertools
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from stelvio.aws._packaging import dependencies as deps
from stelvio.aws._packaging.dependencies import (
    _ACTIVE_CACHE_FILENAME,
    RequirementsSpec,
    get_or_install_dependencies,
)

logger = logging.getLogger(__name__)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Creates a temporary directory simulating a project root."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return project_dir


@pytest.fixture
def dependencies_cache_base(tmp_path: Path, monkeypatch) -> Path:
    """
    Creates a temporary directory for dependency caches and patches
    _get_lambda_dependencies_dir to use it.
    """
    cache_base = tmp_path / "dot_stelvio" / "lambda_dependencies"
    cache_base.mkdir(parents=True)

    monkeypatch.setattr(deps, "_get_lambda_dependencies_dir", lambda subdir: cache_base / subdir)
    return cache_base


@pytest.fixture
def patch_installer_calls(monkeypatch):
    """Patches subprocess.run and shutil.which, returning the mocks."""
    mock_run = MagicMock(spec=subprocess.run)
    mock_which = MagicMock(spec=shutil.which)

    # Default success return value for subprocess.run
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Success", stderr=""
    )

    # Use monkeypatch for reliable patching within fixtures/tests
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr(shutil, "which", mock_which)

    return mock_run, mock_which


def _get_expected_cache_details(
    requirements_content: str,
    runtime: str,
    architecture: str,
    dependencies_cache_base: Path,
    cache_subdirectory: str,
) -> tuple[str, Path, Path]:
    """
    Calculates expected cache key, directory path, and active file path
    by replicating the expected hashing logic for inline requirements.
    """
    py_version = runtime[6:]

    content_hash = hashlib.sha256(requirements_content.encode("utf-8")).hexdigest()
    cache_key = f"{architecture}__{py_version}__{content_hash[:16]}"

    cache_dir = dependencies_cache_base / cache_subdirectory / cache_key
    active_file = dependencies_cache_base / cache_subdirectory / _ACTIVE_CACHE_FILENAME
    return cache_key, cache_dir, active_file


def _create_side_effect_simulation(packages_to_simulate: list[str], raise_: bool = False):
    """Returns a function that simulates installer file creation."""

    def side_effect_run(*args, **kwargs):
        cmd_list = args[0]
        target_path_str = None
        if "--target" in cmd_list:
            try:
                target_index = cmd_list.index("--target")
                if target_index + 1 < len(cmd_list):
                    target_path_str = cmd_list[target_index + 1]
            except ValueError:
                pass  # Let main assertions catch missing target

        if target_path_str:
            target_path = Path(target_path_str)
            target_path.mkdir(parents=True, exist_ok=True)  # Ensure base dir exists
            for pkg_name in packages_to_simulate:
                pkg_dir = target_path / pkg_name
                pkg_dir.mkdir(exist_ok=True)
                (pkg_dir / "__init__.py").touch()  # Simple simulation
        if raise_:
            raise subprocess.CalledProcessError(2, [])

        # Create and return a standard success object directly
        return subprocess.CompletedProcess(
            args=cmd_list, returncode=0, stdout="Simulated Success", stderr=""
        )

    return side_effect_run


def assert_installer_call(  # noqa: PLR0913
    mock_run: MagicMock,
    expected_installer_path: str,
    expected_target_dir: Path,
    expected_py_version: str,
    expected_architecture: str,
    expected_r_value: str,
    expected_input: str | None = None,
):
    """Asserts that subprocess.run was called correctly for the installer."""
    assert mock_run.call_count == 1, "subprocess.run should be called exactly once"
    args, kwargs = mock_run.call_args
    if expected_input is not None:
        assert kwargs["input"] == expected_input
    cmd_list = args[0]
    expected_cmd_list = [
        expected_installer_path,
        "install",
        "-r",
        expected_r_value,
        "--target",
        str(expected_target_dir),
    ]
    platform_arch = "aarch64" if expected_architecture == "arm64" else "x86_64"
    if expected_installer_path.endswith("/uv"):
        # for uv we need to insert pip before install as that's how it works
        expected_cmd_list.insert(1, "pip")
        expected_cmd_list += [
            "--python-platform",
            f"{platform_arch}-manylinux2014",
        ]

    elif expected_installer_path.endswith("/pip"):
        expected_cmd_list += [
            "--implementation",
            "cp",
            "--platform",
            f"manylinux2014_{platform_arch}",
        ]
    expected_cmd_list += ["--python-version", expected_py_version, "--only-binary=:all:"]
    assert cmd_list == expected_cmd_list


def assert_active_cache_file(expected_active_file_path: Path, expected_cache_key: str):
    assert expected_active_file_path.is_file(), "Active cache file not found"
    active_keys = set(expected_active_file_path.read_text().splitlines())
    assert len(active_keys) == 1
    assert expected_cache_key in active_keys


@dataclass
class DependenciesTestCase:
    name: str
    requirements_list: list[str] = field(
        default_factory=lambda: ["requests==2.28.1", "boto3>=1.20.0"]
    )
    requirements_file: Path | None = None
    requirements_file_exists: bool = True
    requirements_file_dir: bool = False
    runtime: str = "python3.12"
    architecture: str = "x86_64"
    cache_subdirectory: str = "functions"
    available_installers: dict[str, str] = field(default_factory=dict)
    expected_installer: str | None = None  # Which installer is expected to be used
    should_raise: tuple[type[Exception], str | None] | None = None
    simulate_installer_error: bool = False
    expected_which_calls: list | None = None
    expected_run_called_when_raise: bool = False
    pre_create_cache: bool = False  # Whether to pre-create the cache directory

    @property
    def requirements_packages(self) -> list[str]:
        return [
            match.group(1) if (match := re.match(r"^([A-Za-z0-9_.-]+)", stripped)) else stripped
            for requirement in self.requirements_list
            if (stripped := requirement.strip())
        ]


RAISES_WHEN_R_OR_C_TC = DependenciesTestCase(
    name="inline_requirements_raises_when_r_or_c",
    should_raise=(
        ValueError,
        "'-r' or '-c' references are not allowed  when providing requirements as list. ",
    ),
)
ARCHITECTURES = ["x86_64", "arm64"]
RUNTIMES = ["python3.12", "python3.13"]
CACHE_SUBDIRS = ["functions", "layers"]

TEST_CASES = [
    DependenciesTestCase(
        name="inline_requirements_cache_miss_uv",
        available_installers={"uv": "/path/to/uv"},
        expected_installer="uv",
    ),
    DependenciesTestCase(
        # We want to test that if both uv and pip are available we use uv
        name="inline_requirements_cache_miss_uses_uv_if_both_uv_and_pip_available",
        available_installers={"uv": "/path/to/uv", "pip": "/path/to/pip"},
        expected_installer="uv",
    ),
    DependenciesTestCase(
        name="inline_requirements_cache_miss_pip",
        available_installers={"pip": "/path/to/pip"},
        expected_installer="pip",
    ),
    DependenciesTestCase(
        name="inline_requirements_cache_miss_no_uv_or_pip_found_raises",
        expected_installer="pip",
        should_raise=(
            RuntimeError,
            "Could not find 'pip' or 'uv'. Please ensure one is installed and in your PATH.",
        ),
        expected_which_calls=[call("uv"), call("pip")],
    ),
    # TODO: Maybe we should validate these on layer/function level before we get to get or install?
    replace(RAISES_WHEN_R_OR_C_TC, requirements_list=["requests", "-r file.txt", "boto3>=1.20.0"]),
    replace(
        RAISES_WHEN_R_OR_C_TC, requirements_list=["requests", " -r file.txt", "boto3>=1.20.0"]
    ),
    replace(RAISES_WHEN_R_OR_C_TC, requirements_list=["requests", "-c file.txt", "boto3>=1.20.0"]),
    replace(
        RAISES_WHEN_R_OR_C_TC, requirements_list=["requests", " -c file.txt", "boto3>=1.20.0"]
    ),
    replace(
        RAISES_WHEN_R_OR_C_TC,
        requirements_list=["requests", " -c file.txt # comment", "-r file.txt", "boto3>=1.20.0"],
    ),
    # TODO: Test manually to see what's printed in the terminal
    DependenciesTestCase(
        name="inline_requirements_cache_miss_install_fails",
        available_installers={"uv": "/path/to/uv"},
        should_raise=(
            RuntimeError,
            r"Stelvio: \[TestFunction\] Failed to install dependencies. Check logs for details.",
        ),
        simulate_installer_error=True,
        expected_run_called_when_raise=True,
        expected_which_calls=[call("uv")],
    ),
    *[
        DependenciesTestCase(
            name="inline_requirements_cache_hit",
            pre_create_cache=True,
            architecture=arch,
            runtime=runtime,
            cache_subdirectory=subdir,
        )
        for arch, runtime, subdir in itertools.product(ARCHITECTURES, RUNTIMES, CACHE_SUBDIRS)
    ],
    DependenciesTestCase(
        name="file_requirements_cache_miss_uv",
        requirements_file=Path("requirements.txt"),
        available_installers={"uv": "/path/to/uv"},
        expected_installer="uv",
    ),
    DependenciesTestCase(
        name="file_requirements_cache_miss_pip",
        requirements_file=Path("requirements.txt"),
        available_installers={"pip": "/path/to/pip"},
        expected_installer="pip",
    ),
    DependenciesTestCase(
        name="file_requirements_cache_hit",
        requirements_file=Path("requirements.txt"),
        pre_create_cache=True,
    ),
    DependenciesTestCase(
        name="file_requirements_file_not_found_raises",
        requirements_file=Path("requirements.txt"),
        requirements_file_exists=False,
        should_raise=(FileNotFoundError, "Requirements file not found: "),
    ),
    DependenciesTestCase(
        name="file_requirements_path_is_folder_raises",
        requirements_file=Path("requirements.txt"),
        requirements_file_dir=True,
        should_raise=(ValueError, "Requirements path is not a file: "),
    ),
    DependenciesTestCase(
        name="file_requirements_path_is_folder_raises",
        requirements_file=Path("../requirements.txt"),
        should_raise=(ValueError, " is outside the project root "),
    ),
]


@pytest.mark.parametrize("test_case", TEST_CASES, ids=[tc.name for tc in TEST_CASES])
def test_get_or_install_dependencies__(
    test_case: DependenciesTestCase,
    project_root: Path,
    dependencies_cache_base: Path,
    patch_installer_calls,
):
    # Arrange
    mock_subprocess_run, mock_shutil_which = patch_installer_calls

    requirements_content = "\n".join(test_case.requirements_list)

    if test_case.requirements_file:
        if test_case.requirements_file_exists:
            requirements_file_abs_path = project_root / test_case.requirements_file
            if test_case.requirements_file_dir:
                requirements_file_abs_path.mkdir(parents=True)
            else:
                requirements_file_abs_path.write_text(requirements_content, encoding="utf-8")
        source = RequirementsSpec(content=None, path_from_root=test_case.requirements_file)
    else:
        source = RequirementsSpec(content=requirements_content, path_from_root=None)

    # Configure mocks provided by the fixture
    mock_shutil_which.side_effect = lambda cmd: test_case.available_installers.get(cmd)
    mock_subprocess_run.side_effect = _create_side_effect_simulation(
        test_case.requirements_packages, raise_=test_case.simulate_installer_error
    )

    expected_cache_key, expected_cache_dir, expected_active_file = _get_expected_cache_details(
        requirements_content="\n".join(sorted(test_case.requirements_list)),
        runtime=test_case.runtime,
        architecture=test_case.architecture,
        dependencies_cache_base=dependencies_cache_base,
        cache_subdirectory=test_case.cache_subdirectory,
    )

    if test_case.pre_create_cache:
        expected_cache_dir.mkdir(parents=True)
        # Add a dummy file to simulate existing installed packages
        dummy_package_file = expected_cache_dir / "dummy_package.py"
        dummy_package_file.touch()

    get_or_install_dependencies_params = partial(
        get_or_install_dependencies,
        requirements_source=source,
        runtime=test_case.runtime,
        architecture=test_case.architecture,
        project_root=project_root,
        cache_subdirectory=test_case.cache_subdirectory,
        log_context="TestFunction",
    )
    if test_case.should_raise:
        with pytest.raises(test_case.should_raise[0], match=test_case.should_raise[1]):
            get_or_install_dependencies_params()
        if not test_case.expected_run_called_when_raise:
            mock_subprocess_run.assert_not_called()

        if test_case.expected_which_calls:
            mock_shutil_which.assert_has_calls(test_case.expected_which_calls)
        else:
            mock_shutil_which.assert_not_called()
        # We return here so code below which is for non raise scenarios is not executed
        return

    # Act
    result_cache_dir = get_or_install_dependencies_params()

    # Assert
    assert result_cache_dir == expected_cache_dir
    assert expected_cache_dir.is_dir()

    if test_case.pre_create_cache:
        assert dummy_package_file.is_file()

    if test_case.expected_installer:
        for pkg in test_case.requirements_packages:
            assert (expected_cache_dir / pkg / "__init__.py").is_file()

        assert_installer_call(
            mock_subprocess_run,
            expected_installer_path=test_case.available_installers.get(
                test_case.expected_installer
            ),
            expected_target_dir=expected_cache_dir,
            expected_py_version=test_case.runtime[6:],
            expected_architecture=test_case.architecture,
            expected_r_value=str(requirements_file_abs_path)
            if test_case.requirements_file
            else "-",
            expected_input=None if test_case.requirements_file else requirements_content,
        )
    else:
        mock_subprocess_run.assert_not_called()
        mock_shutil_which.assert_not_called()

    assert_active_cache_file(expected_active_file, expected_cache_key)


@dataclass
class NormalizationTestCase:
    """Test case for requirements normalization and reference resolution."""

    name: str
    clean_requirements: list[str]
    # The messy/complex input(s) that should normalize to the clean form
    # Either direct content or file_path -> content mapping
    requirements: list[str] | dict[str, list[str]]
    # Which file is used as main requirements file
    requirements_file: str | None = None
    file_as_dir: str | None = False
    should_raise: tuple[type[Exception], str | None] | None = None


@pytest.mark.parametrize(
    "test_case",
    [
        # Simple whitespace and comment normalization
        NormalizationTestCase(
            name="whitespace_and_comments",
            clean_requirements=["boto3>=1.20.0", "requests==2.28.1"],
            requirements=[
                "  boto3>=1.20.0  # Comment",
                "",
                "",
                "  requests==2.28.1  ",
                "#Commented req  requests==2.28.1  ",
                "#Comment",
            ],
        ),
        # Order normalization
        NormalizationTestCase(
            name="ordering",
            clean_requirements=["boto3>=1.20.0", "requests==2.28.1"],
            requirements=["requests==2.28.1", "boto3>=1.20.0"],
        ),
        # File references
        NormalizationTestCase(
            name="file_references_simple",
            clean_requirements=["boto3>=1.20.0", "requests==2.28.1", "numpy"],
            requirements={
                "main.txt": ["requests==2.28.1", "-r sub.txt"],
                "sub.txt": ["boto3>=1.20.0", "numpy"],
            },
            requirements_file="main.txt",
        ),
        NormalizationTestCase(
            name="file_references_nested",
            clean_requirements=["boto3>=1.20.0", "requests==2.28.1", "numpy"],
            requirements={
                "f/a/main.txt": ["requests==2.28.1", "-r ../sub.txt"],
                "f/sub.txt": ["boto3>=1.20.0", "numpy"],
            },
            requirements_file="f/a/main.txt",
        ),
        NormalizationTestCase(
            name="file_references_multiple_nested",
            clean_requirements=[
                "boto3>=1.20.0",
                "numpy",
                "pelican",
                "pydantic=2.11.0",
                "requests==2.28.1",
            ],
            requirements={
                "f/a/main.txt": ["-r ../sub_a.txt", "requests==2.28.1", "-r ../sub_b.txt"],
                "f/sub_a.txt": ["boto3>=1.20.0", "numpy"],
                "f/sub_b.txt": ["pelican", "pydantic=2.11.0"],
            },
            requirements_file="f/a/main.txt",
        ),
        NormalizationTestCase(
            name="file_references_simple_whitespace_comment_order",
            clean_requirements=["boto3>=1.20.0", "requests==2.28.1", "numpy"],
            requirements={
                "main.txt": ["requests==2.28.1", "-r sub.txt"],
                "sub.txt": ["\n", "numpy", "boto3>=1.20.0", "# Comment"],
            },
            requirements_file="main.txt",
        ),
        # Nested references
        NormalizationTestCase(
            name="file_references_multiple",
            clean_requirements=["boto3>=1.20.0", "numpy", "pandas", "requests==2.28.1"],
            requirements={
                "main.txt": ["requests==2.28.1", "-r level1.txt"],
                "level1.txt": ["boto3>=1.20.0", "-r level2.txt"],
                "level2.txt": ["numpy", "pandas"],
            },
            requirements_file="main.txt",
        ),
        NormalizationTestCase(
            name="file_references_multiple_deep",
            clean_requirements=[
                "boto3>=1.20.0",
                "django~=5.4.2",
                "numpy",
                "pandas",
                "fastapi",
                "pelican",
                "stelvio==0.1.0a3",
                "requests==2.28.1",
            ],
            requirements={
                "src/f/a/main.txt": ["requests==2.28.1", "boto3>=1.20.0", "-r ../level1.txt"],
                "src/f/level1.txt": ["-r ../level2_b.txt", "django~=5.4.2", "-r ../level2_a.txt"],
                "src/level2_a.txt": ["numpy", "-r ../level3_a.txt"],
                "src/level2_b.txt": ["pandas", "-r ../level3_b.txt"],
                "level3_a.txt": ["fastapi"],
                "level3_b.txt": ["pelican", "-r src/what/level4.txt"],
                "src/what/level4.txt": ["stelvio==0.1.0a3"],
            },
            requirements_file="src/f/a/main.txt",
        ),
        # Constraint files
        NormalizationTestCase(
            name="constraint_files",
            clean_requirements=["boto3>=1.20.0", "boto3", "requests==2.28.1", "requests"],
            requirements={
                "main.txt": ["requests", "boto3", "-c constraints.txt"],
                "constraints.txt": ["requests==2.28.1", "boto3>=1.20.0"],
            },
            requirements_file="main.txt",
        ),
        NormalizationTestCase(
            name="raise_if_referenced_file_outside_project",
            clean_requirements=["requests"],
            requirements={
                "f/a/main.txt": ["requests", "boto3", "-r ../../../base.txt"],
                "../base.txt": ["requests==2.28.1"],
            },
            requirements_file="f/a/main.txt",
            should_raise=(ValueError, "is outside the project root"),
        ),
        NormalizationTestCase(
            name="raise_if_referenced_file_does_not_exists",
            clean_requirements=["requests"],
            requirements={
                "f/a/main.txt": ["requests", "boto3", "-r ../base.txt"],
            },
            requirements_file="f/a/main.txt",
            should_raise=(FileNotFoundError, "Requirements file not found:"),
        ),
        NormalizationTestCase(
            name="raise_if_referenced_file_is_dir",
            clean_requirements=["requests"],
            requirements={
                "f/a/main.txt": ["requests", "boto3", "-r ../base.txt"],
            },
            requirements_file="f/a/main.txt",
            should_raise=(ValueError, "Requirements path is not a file: "),
            file_as_dir="f/base.txt",
        ),
    ],
    ids=lambda tc: tc.name,
)
def test_requirements_normalization(
    test_case: NormalizationTestCase,
    project_root: Path,
    dependencies_cache_base: Path,
    patch_installer_calls,
):
    _, mock_shutil_which = patch_installer_calls
    runtime = "python3.12"
    architecture = "x86_64"
    cache_subdirectory = "functions"

    clean_key, _, _ = _get_expected_cache_details(
        requirements_content="\n".join(sorted(test_case.clean_requirements)),
        runtime=runtime,
        architecture=architecture,
        dependencies_cache_base=dependencies_cache_base,
        cache_subdirectory=cache_subdirectory,
    )

    if isinstance(test_case.requirements, dict):
        # Create the files from the dictionary
        for relative_path, content in test_case.requirements.items():
            abs_path = project_root / relative_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text("\n".join(content))

        messy_source = RequirementsSpec(
            content=None, path_from_root=Path(test_case.requirements_file)
        )
    else:
        messy_source = RequirementsSpec(
            content="\n".join(test_case.requirements), path_from_root=None
        )
    if test_case.file_as_dir:
        (project_root / test_case.file_as_dir).mkdir(parents=True)

    mock_shutil_which.return_value = "/path/to/pip"

    if test_case.should_raise:
        with pytest.raises(test_case.should_raise[0], match=test_case.should_raise[1]):
            get_or_install_dependencies(
                requirements_source=messy_source,
                runtime=runtime,
                architecture=architecture,
                project_root=project_root,
                cache_subdirectory=cache_subdirectory,
                log_context="TestNormalization",
            )
    else:
        result_dir = get_or_install_dependencies(
            requirements_source=messy_source,
            runtime=runtime,
            architecture=architecture,
            project_root=project_root,
            cache_subdirectory=cache_subdirectory,
            log_context="TestNormalization",
        )
        assert result_dir.name == clean_key
