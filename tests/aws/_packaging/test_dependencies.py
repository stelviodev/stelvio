import hashlib
import itertools
import logging
import os
import re
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from stelvio.aws._packaging import dependencies as deps
from stelvio.aws._packaging.dependencies import (
    RequirementsSpec,
    clean_stale_dependency_caches,
    get_or_install_dependencies,
)

EIGHT_DAYS_AGO = time.time() - 8 * 24 * 3600

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
    _lambda_dependencies_root to use it.
    """
    cache_base = tmp_path / "dot_stelvio" / "lambda_dependencies"
    cache_base.mkdir(parents=True)

    monkeypatch.setattr(deps, "_lambda_dependencies_root", lambda: cache_base)
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


def _get_expected_cache_details(  # noqa: PLR0913
    requirements_content: str,
    runtime: str,
    architecture: str,
    dependencies_cache_base: Path,
    cache_subdirectory: str,
    glibc_target: str = "manylinux_2_34",
) -> tuple[str, Path]:
    """
    Calculates expected cache key and directory path by replicating the expected hashing
    logic for inline requirements. The glibc target is hashed in: wheels differ per target.
    """
    py_version = runtime[6:]

    content_hash = hashlib.sha256(f"{glibc_target}\n{requirements_content}".encode()).hexdigest()
    cache_key = f"{architecture}__{py_version}__{content_hash[:16]}"

    return cache_key, dependencies_cache_base / cache_subdirectory / cache_key


def _create_side_effect_simulation(
    packages_to_simulate: list[str],
    fail_with: str | None = None,
    seen_requirements: list[str] | None = None,
):
    """Returns a function that simulates installer file creation.

    `fail_with` is the installer's stderr when the install fails.
    `seen_requirements` gets the `-r` file's text as the installer saw it: an inline list's
    file lives in a temp dir that is gone once the install returns.
    """

    def side_effect_run(*args, **kwargs):
        cmd_list = args[0]
        if seen_requirements is not None:
            seen_requirements.append(Path(cmd_list[cmd_list.index("-r") + 1]).read_text())
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
        if fail_with is not None:
            return subprocess.CompletedProcess(cmd_list, returncode=1, stdout="", stderr=fail_with)

        # Create and return a standard success object directly
        return subprocess.CompletedProcess(
            args=cmd_list, returncode=0, stdout="Simulated Success", stderr=""
        )

    return side_effect_run


# pip takes `--platform` tags literally, so it gets the set uv derives from one target: every
# PEP 600 tag the glibc runs plus the legacy aliases; x86_64 wheels go back to manylinux1.
AMAZON_LINUX_2_X86_64_TAGS = [
    "manylinux_2_17_x86_64",
    "manylinux2014_x86_64",
    "manylinux_2_16_x86_64",
    "manylinux_2_15_x86_64",
    "manylinux_2_14_x86_64",
    "manylinux_2_13_x86_64",
    "manylinux_2_12_x86_64",
    "manylinux2010_x86_64",
    "manylinux_2_11_x86_64",
    "manylinux_2_10_x86_64",
    "manylinux_2_9_x86_64",
    "manylinux_2_8_x86_64",
    "manylinux_2_7_x86_64",
    "manylinux_2_6_x86_64",
    "manylinux_2_5_x86_64",
    "manylinux1_x86_64",
]
AMAZON_LINUX_2_AARCH64_TAGS = ["manylinux_2_17_aarch64", "manylinux2014_aarch64"]
PIP_TAGS = {
    ("manylinux_2_17", "x86_64"): AMAZON_LINUX_2_X86_64_TAGS,
    ("manylinux_2_17", "aarch64"): AMAZON_LINUX_2_AARCH64_TAGS,
    ("manylinux_2_34", "x86_64"): [
        *(f"manylinux_2_{minor}_x86_64" for minor in range(34, 17, -1)),
        *AMAZON_LINUX_2_X86_64_TAGS,
    ],
    ("manylinux_2_34", "aarch64"): [
        *(f"manylinux_2_{minor}_aarch64" for minor in range(34, 17, -1)),
        *AMAZON_LINUX_2_AARCH64_TAGS,
    ],
}


def assert_installer_call(  # noqa: PLR0913
    mock_run: MagicMock,
    expected_installer_path: str,
    expected_cache_dir: Path,
    expected_py_version: str,
    expected_architecture: str,
    expected_r_value: str | None,
    project_root: Path,
    expected_glibc_target: str = "manylinux_2_34",
):
    """Asserts that subprocess.run was called correctly for the installer.

    The target is a temp dir next to the cache dir, renamed into it afterwards.
    `expected_r_value=None` means an inline list, written to a file in that temp dir.
    """
    assert mock_run.call_count == 1, "subprocess.run should be called exactly once"
    args, kwargs = mock_run.call_args
    # utf-8: the stderr lands in the error the user reads, and uv's glyphs are not in cp1252
    assert kwargs == {
        "capture_output": True,
        "check": False,
        "cwd": project_root,
        "encoding": "utf-8",
        "errors": "replace",
    }
    cmd_list = args[0]
    target_dir = Path(cmd_list[cmd_list.index("--target") + 1])
    temp_dir = target_dir.parent
    assert temp_dir.parent == expected_cache_dir.parent
    assert not temp_dir.exists()
    if expected_r_value is None:
        expected_r_value = str(temp_dir / "requirements.txt")
    expected_cmd_list = [
        expected_installer_path,
        "install",
        "-r",
        expected_r_value,
        "--target",
        str(target_dir),
    ]
    platform_arch = "aarch64" if expected_architecture == "arm64" else "x86_64"
    if expected_installer_path.endswith("/uv"):
        # for uv we need to insert pip before install as that's how it works
        expected_cmd_list.insert(1, "pip")
        expected_cmd_list += ["--python-platform", f"{platform_arch}-{expected_glibc_target}"]
    elif expected_installer_path.endswith("/pip"):
        expected_cmd_list += ["--implementation", "cp", "--no-compile"]
        for tag in PIP_TAGS[expected_glibc_target, platform_arch]:
            expected_cmd_list += ["--platform", tag]
    expected_cmd_list += ["--python-version", expected_py_version, "--only-binary=:all:"]
    assert cmd_list == expected_cmd_list


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
    # Amazon Linux 2023's glibc for python3.12+, Amazon Linux 2's closest tag for 3.10/3.11
    glibc_target: str = "manylinux_2_34"
    cache_subdirectory: str = "functions"
    available_installers: dict[str, str] = field(default_factory=dict)
    expected_installer: str | None = None  # Which installer is expected to be used
    should_raise: tuple[type[Exception], str | None] | None = None
    installer_stderr: str | None = None  # Set = the install fails with this output
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
        "'-r', '-c', '--requirement' and '--constraint' references are not allowed when "
        "providing requirements as a list.",
    ),
)
ARCHITECTURES = ["x86_64", "arm64"]
RUNTIMES = ["python3.12", "python3.13"]
CACHE_SUBDIRS = ["functions", "layers"]
UV_INSTALL_ERROR = (
    "  × No solution found when resolving dependencies:\n"  # noqa: RUF001  # uv's glyph
    "  ╰─▶ Because nosuchpkg was not found in the package registry"
)

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
        name="inline_requirements_cache_miss_pip_arm64",
        architecture="arm64",
        available_installers={"pip": "/path/to/pip"},
        expected_installer="pip",
    ),
    DependenciesTestCase(
        name="inline_requirements_cache_miss_uv_python3_11_targets_amazon_linux_2",
        runtime="python3.11",
        glibc_target="manylinux_2_17",
        available_installers={"uv": "/path/to/uv"},
        expected_installer="uv",
    ),
    DependenciesTestCase(
        name="inline_requirements_cache_miss_pip_python3_11_targets_amazon_linux_2",
        runtime="python3.11",
        glibc_target="manylinux_2_17",
        available_installers={"pip": "/path/to/pip"},
        expected_installer="pip",
    ),
    DependenciesTestCase(
        name="inline_requirements_cache_miss_pip_arm64_python3_11_targets_amazon_linux_2",
        runtime="python3.11",
        architecture="arm64",
        glibc_target="manylinux_2_17",
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
    replace(RAISES_WHEN_R_OR_C_TC, requirements_list=["requests", "--requirement file.txt"]),
    replace(RAISES_WHEN_R_OR_C_TC, requirements_list=["requests", "--constraint=file.txt"]),
    DependenciesTestCase(
        # The CLI logs to a file, so the installer's words must be in the exception
        name="inline_requirements_cache_miss_install_fails_with_the_installer_output",
        available_installers={"uv": "/path/to/uv"},
        should_raise=(
            RuntimeError,
            r"\[TestFunction\] Dependency install failed:\n"
            + re.escape(textwrap.indent(UV_INSTALL_ERROR, "  ")),
        ),
        installer_stderr=UV_INSTALL_ERROR,
        expected_run_called_when_raise=True,
        expected_which_calls=[call("uv")],
    ),
    DependenciesTestCase(
        name="runtime_without_a_version_raises",
        runtime="python3",
        should_raise=(
            ValueError,
            r"\[TestFunction\] runtime must look like 'python3.12', got 'python3'\.",
        ),
    ),
    DependenciesTestCase(
        name="runtime_of_another_language_raises",
        runtime="nodejs20.x",
        should_raise=(
            ValueError,
            r"\[TestFunction\] runtime must look like 'python3.12', got 'nodejs20.x'\.",
        ),
    ),
    DependenciesTestCase(
        name="inline_requirements_cache_miss_install_fails_silently_names_the_exit_code",
        available_installers={"uv": "/path/to/uv"},
        should_raise=(RuntimeError, r"\[TestFunction\] Dependency install failed:\nexit code 1"),
        installer_stderr="",
        expected_run_called_when_raise=True,
        expected_which_calls=[call("uv")],
    ),
    *[
        DependenciesTestCase(
            name=f"inline_requirements_cache_hit_{arch}_{runtime}_{subdir}",
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
        name="file_requirements_path_outside_project_raises",
        requirements_file=Path("../requirements.txt"),
        should_raise=(ValueError, " is outside the project root "),
    ),
]


@pytest.mark.parametrize("test_case", TEST_CASES, ids=[tc.name for tc in TEST_CASES])
def test_get_or_install_dependencies__(  # noqa: C901, PLR0912
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
    seen_requirements = []
    mock_subprocess_run.side_effect = _create_side_effect_simulation(
        test_case.requirements_packages,
        fail_with=test_case.installer_stderr,
        seen_requirements=seen_requirements,
    )

    _, expected_cache_dir = _get_expected_cache_details(
        requirements_content="\n".join(sorted(test_case.requirements_list)),
        runtime=test_case.runtime,
        architecture=test_case.architecture,
        dependencies_cache_base=dependencies_cache_base,
        cache_subdirectory=test_case.cache_subdirectory,
        glibc_target=test_case.glibc_target,
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
        if test_case.expected_run_called_when_raise:
            # A failed install leaves nothing: no cache dir the next run would take for a hit,
            # no temp dir.
            assert list(expected_cache_dir.parent.iterdir()) == []
        else:
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
            expected_cache_dir=expected_cache_dir,
            expected_py_version=test_case.runtime[6:],
            expected_architecture=test_case.architecture,
            expected_r_value=str(requirements_file_abs_path)
            if test_case.requirements_file
            else None,
            project_root=project_root,
            expected_glibc_target=test_case.glibc_target,
        )
        assert seen_requirements == [requirements_content]
    else:
        mock_subprocess_run.assert_not_called()
        mock_shutil_which.assert_not_called()


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
        # pip's comment rule: `#` after whitespace, never inside a URL
        NormalizationTestCase(
            name="url_fragment_is_not_a_comment",
            clean_requirements=["pkg @ https://x/p.whl#sha256=aaa"],
            requirements=["pkg @ https://x/p.whl#sha256=aaa  # pinned build"],
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
            # a BOM (Windows editors) must not hide a first-line reference
            name="file_references_after_a_bom",
            clean_requirements=["boto3>=1.20.0", "requests==2.28.1", "numpy"],
            requirements={
                "main.txt": ["﻿-r sub.txt", "requests==2.28.1"],
                "sub.txt": ["boto3>=1.20.0", "numpy"],
            },
            requirements_file="main.txt",
        ),
        NormalizationTestCase(
            name="circular_references_are_expanded_once",
            clean_requirements=["boto3>=1.20.0", "requests==2.28.1"],
            requirements={
                "main.txt": ["requests==2.28.1", "-r sub.txt"],
                "sub.txt": ["boto3>=1.20.0", "-r main.txt"],
            },
            requirements_file="main.txt",
        ),
        NormalizationTestCase(
            name="file_references_long_form",
            clean_requirements=["boto3>=1.20.0", "requests==2.28.1", "numpy"],
            requirements={
                "main.txt": ["requests==2.28.1", "--requirement sub.txt"],
                "sub.txt": ["boto3>=1.20.0", "numpy"],
            },
            requirements_file="main.txt",
        ),
        NormalizationTestCase(
            name="file_references_long_form_with_equals",
            clean_requirements=["boto3>=1.20.0", "requests==2.28.1", "numpy"],
            requirements={
                "main.txt": ["requests==2.28.1", "--requirement=sub.txt"],
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
        # Constraint files: their lines hash apart from `-r` lines (a constraint installs nothing)
        NormalizationTestCase(
            name="constraint_files",
            clean_requirements=["-c boto3>=1.20.0", "-c requests==2.28.1", "boto3", "requests"],
            requirements={
                "main.txt": ["requests", "boto3", "-c constraints.txt"],
                "constraints.txt": ["requests==2.28.1", "boto3>=1.20.0"],
            },
            requirements_file="main.txt",
        ),
        NormalizationTestCase(
            name="constraint_files_long_form",
            clean_requirements=["-c boto3>=1.20.0", "-c requests==2.28.1", "boto3", "requests"],
            requirements={
                "main.txt": ["requests", "boto3", "--constraint constraints.txt"],
                "constraints.txt": ["requests==2.28.1", "boto3>=1.20.0"],
            },
            requirements_file="main.txt",
        ),
        NormalizationTestCase(
            # pins used as constraints AND installed: both expansions, not "seen already"
            name="a_file_used_as_constraint_and_as_requirement_is_expanded_both_ways",
            clean_requirements=[
                "-c boto3>=1.20.0",
                "-c requests==2.28.1",
                "boto3>=1.20.0",
                "requests==2.28.1",
            ],
            requirements={
                "main.txt": ["-c pins.txt", "-r pins.txt"],
                "pins.txt": ["requests==2.28.1", "boto3>=1.20.0"],
            },
            requirements_file="main.txt",
        ),
        NormalizationTestCase(
            name="constraint_file_referencing_another_file_keeps_the_constraint_prefix",
            clean_requirements=["-c boto3>=1.20.0", "-c requests==2.28.1", "requests"],
            requirements={
                "main.txt": ["requests", "-c constraints.txt"],
                "constraints.txt": ["requests==2.28.1", "-r more_pins.txt"],
                "more_pins.txt": ["boto3>=1.20.0"],
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

    clean_key, _ = _get_expected_cache_details(
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


def test_interrupted_install_is_not_a_cache_hit(
    project_root, dependencies_cache_base, patch_installer_calls
):
    mock_run, mock_which = patch_installer_calls
    mock_which.side_effect = lambda cmd: f"/usr/bin/{cmd}"

    def interrupted(cmd, **_):
        (Path(cmd[cmd.index("--target") + 1]) / "half_installed").mkdir()
        raise KeyboardInterrupt

    install = partial(
        get_or_install_dependencies,
        RequirementsSpec(content="requests", path_from_root=None),
        "python3.12",
        "x86_64",
        project_root,
        "functions",
        "fn",
    )
    mock_run.side_effect = interrupted
    with pytest.raises(KeyboardInterrupt):
        install()
    mock_run.side_effect = _create_side_effect_simulation(["requests"])
    cache_dir = install()

    assert mock_run.call_count == 2
    assert sorted(p.name for p in cache_dir.iterdir()) == ["requests"]
    assert [p.name for p in cache_dir.parent.iterdir()] == [cache_dir.name]


def _install_requests(project_root):
    return get_or_install_dependencies(
        RequirementsSpec(content="requests", path_from_root=None),
        "python3.12",
        "x86_64",
        project_root,
        "functions",
        "fn",
    )


def _target(cmd):
    return Path(cmd[cmd.index("--target") + 1])


# Two stlv runs on one project can overlap: `stlv diff` takes no lock, and each env locks on
# its own. They share the dependency caches.
def test_install_uses_the_cache_another_run_filled_first(
    project_root, dependencies_cache_base, patch_installer_calls
):
    mock_run, mock_which = patch_installer_calls
    mock_which.side_effect = lambda cmd: f"/usr/bin/{cmd}"

    def other_run_finishes_first(cmd, **_):
        temp_dir = _target(cmd).parent  # `.{cache_key}-{random}`
        cache_dir = temp_dir.with_name(temp_dir.name[1:].rsplit("-", 1)[0])
        (cache_dir / "requests").mkdir(parents=True)
        (_target(cmd) / "requests").mkdir()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    mock_run.side_effect = other_run_finishes_first

    cache_dir = _install_requests(project_root)

    assert sorted(p.name for p in cache_dir.iterdir()) == ["requests"]


def test_another_runs_cleanup_keeps_an_install_in_progress(
    project_root, dependencies_cache_base, patch_installer_calls
):
    mock_run, mock_which = patch_installer_calls
    mock_which.side_effect = lambda cmd: f"/usr/bin/{cmd}"

    def cleanup_mid_install(cmd, **_):
        (_target(cmd) / "boto3").mkdir()
        clean_stale_dependency_caches()
        (_target(cmd) / "requests").mkdir()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    mock_run.side_effect = cleanup_mid_install

    cache_dir = _install_requests(project_root)

    assert sorted(p.name for p in cache_dir.iterdir()) == ["boto3", "requests"]


@pytest.mark.parametrize(
    "leftover", [pytest.param([], id="empty"), pytest.param([".lock"], id="uv_lock_only")]
)
def test_a_cache_dir_an_older_stelvio_left_half_made_is_not_a_cache_hit(
    project_root, dependencies_cache_base, patch_installer_calls, leftover
):
    mock_run, mock_which = patch_installer_calls
    mock_which.side_effect = lambda cmd: f"/usr/bin/{cmd}"
    mock_run.side_effect = _create_side_effect_simulation(["requests"])
    cache_dir = _install_requests(project_root)
    # Older Stelvio versions installed straight into the cache dir; Ctrl-C left it empty, or
    # with the `.lock` uv writes before resolving.
    shutil.rmtree(cache_dir)
    cache_dir.mkdir()
    for name in leftover:
        (cache_dir / name).touch()

    assert _install_requests(project_root) == cache_dir
    assert mock_run.call_count == 2
    assert sorted(p.name for p in cache_dir.iterdir()) == ["requests"]


def test_a_legacy_empty_dir_that_cannot_be_removed_fails_instead_of_shipping_nothing(
    project_root, dependencies_cache_base, patch_installer_calls, monkeypatch
):
    # Windows: a rename onto an existing dir fails even when it is empty, and an open handle
    # can block the rmdir. The fallback must not take that empty dir for another run's cache.
    mock_run, mock_which = patch_installer_calls
    mock_which.side_effect = lambda cmd: f"/usr/bin/{cmd}"
    mock_run.side_effect = _create_side_effect_simulation(["requests"])
    cache_dir = _install_requests(project_root)
    shutil.rmtree(cache_dir)
    cache_dir.mkdir()
    real_rename = Path.rename

    def rename_like_windows(path, target):
        if Path(target).exists():
            raise FileExistsError(17, "exists", str(target))
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rmdir", lambda _: (_ for _ in ()).throw(PermissionError(13)))
    monkeypatch.setattr(Path, "rename", rename_like_windows)

    with pytest.raises(FileExistsError):
        _install_requests(project_root)


def test_a_cache_dir_removed_between_its_touch_and_its_read_is_a_miss(
    project_root, dependencies_cache_base, patch_installer_calls, monkeypatch
):
    # Another run's cleanup renames a stale dir away right after this run touched it.
    mock_run, mock_which = patch_installer_calls
    mock_which.side_effect = lambda cmd: f"/usr/bin/{cmd}"
    mock_run.side_effect = _create_side_effect_simulation(["requests"])
    cache_dir = _install_requests(project_root)
    real_utime = os.utime

    def touch_then_other_run_renames(path, *args, **kwargs):
        real_utime(path, *args, **kwargs)
        Path(path).rename(Path(path).with_name(f".{Path(path).name}-old"))

    monkeypatch.setattr(os, "utime", touch_then_other_run_renames)

    assert _install_requests(project_root) == cache_dir
    assert mock_run.call_count == 2
    assert sorted(p.name for p in cache_dir.iterdir()) == ["requests"]


def test_cleanup_skips_a_temp_dir_another_run_just_removed(dependencies_cache_base, monkeypatch):
    functions_dir = dependencies_cache_base / "functions"
    temp_dir = functions_dir / ".x86_64__3.12__0123456789abcdef-abc"
    temp_dir.mkdir(parents=True)
    real_is_dir = Path.is_dir

    def is_dir_then_removed(path, **kwargs):
        result = real_is_dir(path, **kwargs)
        if path == temp_dir:
            shutil.rmtree(path)
        return result

    monkeypatch.setattr(Path, "is_dir", is_dir_then_removed)

    clean_stale_dependency_caches()

    assert not temp_dir.exists()


def test_cleanup_removes_an_old_leftover_install_dir(dependencies_cache_base):
    # A hard-killed run never removes its temp dir; a slow install may still be filling its own.
    functions_dir = dependencies_cache_base / "functions"
    old = functions_dir / ".x86_64__3.12__0123456789abcdef-abc"
    slow_install = functions_dir / ".x86_64__3.12__0123456789abcdef-def"
    old.mkdir(parents=True)
    slow_install.mkdir()
    for temp_dir, hours in ((old, 25), (slow_install, 2)):
        started = time.time() - hours * 3600
        os.utime(temp_dir, (started, started))

    clean_stale_dependency_caches()

    assert [p.name for p in functions_dir.iterdir() if p.is_dir()] == [slow_install.name]


def test_cleanup_removes_a_cache_no_run_used_for_a_week(dependencies_cache_base):
    functions_dir = dependencies_cache_base / "functions"
    unused = functions_dir / "x86_64__3.12__0123456789abcdef"
    used = functions_dir / "x86_64__3.12__fedcba9876543210"
    for cache_dir in (unused, used):
        (cache_dir / "requests").mkdir(parents=True)
    os.utime(unused, (EIGHT_DAYS_AGO, EIGHT_DAYS_AGO))

    clean_stale_dependency_caches()

    assert [p.name for p in functions_dir.iterdir()] == [used.name]


def test_a_cache_hit_keeps_the_cache_for_another_week(
    project_root, dependencies_cache_base, patch_installer_calls
):
    # Another env or an overlapping run using a cache must count as use.
    mock_run, mock_which = patch_installer_calls
    mock_which.side_effect = lambda cmd: f"/usr/bin/{cmd}"
    mock_run.side_effect = _create_side_effect_simulation(["requests"])
    cache_dir = _install_requests(project_root)
    os.utime(cache_dir, (EIGHT_DAYS_AGO, EIGHT_DAYS_AGO))

    assert _install_requests(project_root) == cache_dir
    clean_stale_dependency_caches()

    assert mock_run.call_count == 1
    assert sorted(p.name for p in cache_dir.iterdir()) == ["requests"]


def test_a_cache_dir_whose_removal_failed_is_not_a_cache_hit(
    project_root, dependencies_cache_base, patch_installer_calls, monkeypatch
):
    # The cleanup renames the dir out of the way before deleting it, so a delete another run or
    # a kill interrupted leaves a temp dir (removed later), never a half-empty cache dir.
    mock_run, mock_which = patch_installer_calls
    mock_which.side_effect = lambda cmd: f"/usr/bin/{cmd}"
    mock_run.side_effect = _create_side_effect_simulation(["requests"])
    cache_dir = _install_requests(project_root)
    os.utime(cache_dir, (EIGHT_DAYS_AGO, EIGHT_DAYS_AGO))
    with monkeypatch.context() as interrupted_delete:  # tempfile's cleanup uses rmtree too
        interrupted_delete.setattr(shutil, "rmtree", lambda *_, **__: None)
        clean_stale_dependency_caches()

    assert _install_requests(project_root) == cache_dir
    assert mock_run.call_count == 2
    assert sorted(p.name for p in cache_dir.parent.iterdir()) == [
        f".{cache_dir.name}-old",
        cache_dir.name,
    ]


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root writes anywhere")
def test_cleanup_continues_past_a_legacy_list_it_cannot_unlink(dependencies_cache_base):
    # A read-only subdir (a cache baked into a CI image by an older Stelvio) must not stop the
    # cleanup of the other subdirs, and must not raise on every command.
    layers = dependencies_cache_base / "layers"
    layers.mkdir()
    (layers / "active_caches.txt").write_text("x\n")
    stale = dependencies_cache_base / "functions" / "x86_64__3.12__0123456789abcdef"
    (stale / "requests").mkdir(parents=True)
    os.utime(stale, (EIGHT_DAYS_AGO, EIGHT_DAYS_AGO))
    layers.chmod(0o555)
    try:
        clean_stale_dependency_caches()
    finally:
        layers.chmod(0o755)

    assert not stale.exists()


def test_cleanup_leaves_a_symlinked_cache_entry_alone(dependencies_cache_base):
    # A user's symlink (a cache kept elsewhere) is theirs: neither followed nor renamed away.
    target = dependencies_cache_base.parent / "elsewhere"
    (target / "requests").mkdir(parents=True)
    os.utime(target, (EIGHT_DAYS_AGO, EIGHT_DAYS_AGO))
    functions = dependencies_cache_base / "functions"
    functions.mkdir()
    link = functions / "x86_64__3.12__0123456789abcdef"
    link.symlink_to(target)

    clean_stale_dependency_caches()

    assert list(functions.iterdir()) == [link]
    assert (target / "requests").is_dir()


def test_cleanup_removes_the_in_use_list_of_older_stelvio_versions(dependencies_cache_base):
    functions_dir = dependencies_cache_base / "functions"
    functions_dir.mkdir()
    (functions_dir / "active_caches.txt").write_text("x86_64__3.12__0123456789abcdef\n")

    clean_stale_dependency_caches()

    assert list(functions_dir.iterdir()) == []
