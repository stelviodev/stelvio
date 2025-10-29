import pytest

from stelvio.cli import _parse_template_string


class TestParseTemplateString:
    """Tests for _parse_template_string function."""

    def test_base_template(self):
        """Test simple template name resolves to stelviodev/templates/base."""
        owner, repo, branch, subdirectory = _parse_template_string("base")
        assert owner == "stelviodev"
        assert repo == "templates"
        assert branch == "main"
        assert subdirectory == "base"

    def test_example_dir_template(self):
        """Test template with nested directory."""
        owner, repo, branch, subdirectory = _parse_template_string("example/dir")
        assert owner == "stelviodev"
        assert repo == "templates"
        assert branch == "main"
        assert subdirectory == "example/dir"

    def test_gh_owner_repo_branch_subdirectory(self):
        """Test full format with branch and subdirectory."""
        owner, repo, branch, subdirectory = _parse_template_string(
            "gh:owner/repo@branch/subdirectory"
        )
        assert owner == "owner"
        assert repo == "repo"
        assert branch == "branch"
        assert subdirectory == "subdirectory"

    def test_gh_owner_repo(self):
        """Test gh:owner/repo format."""
        owner, repo, branch, subdirectory = _parse_template_string("gh:owner/repo")
        assert owner == "owner"
        assert repo == "repo"
        assert branch == "main"
        assert subdirectory is None

    def test_gh_owner_repo_branch(self):
        """Test gh:owner/repo@branch format."""
        owner, repo, branch, subdirectory = _parse_template_string("gh:owner/repo@branch")
        assert owner == "owner"
        assert repo == "repo"
        assert branch == "branch"
        assert subdirectory is None

    def test_gh_owner_repo_subdirectory(self):
        """Test gh:owner/repo/subdirectory format."""
        owner, repo, branch, subdirectory = _parse_template_string("gh:owner/repo/subdirectory")
        assert owner == "owner"
        assert repo == "repo"
        assert branch == "main"
        assert subdirectory == "subdirectory"

    def test_gh_owner_repo_branch_nested_subdirectory(self):
        """Test gh:owner/repo@branch/sub/directory format."""
        owner, repo, branch, subdirectory = _parse_template_string(
            "gh:owner/repo@main/sub/directory"
        )
        assert owner == "owner"
        assert repo == "repo"
        assert branch == "main"
        assert subdirectory == "sub/directory"

    def test_gh_owner_repo_nested_subdirectory(self):
        """Test gh:owner/repo/sub/directory format."""
        owner, repo, branch, subdirectory = _parse_template_string("gh:owner/repo/sub/directory")
        assert owner == "owner"
        assert repo == "repo"
        assert branch == "main"
        assert subdirectory == "sub/directory"

    def test_invalid_format_missing_repo(self):
        """Test that invalid format without repo raises ValueError."""
        with pytest.raises(ValueError, match="Invalid template format"):
            _parse_template_string("gh:owner")

    def test_invalid_format_empty_gh_prefix(self):
        """Test that gh: prefix without content raises ValueError."""
        with pytest.raises(ValueError, match="Invalid template format"):
            _parse_template_string("gh:")
