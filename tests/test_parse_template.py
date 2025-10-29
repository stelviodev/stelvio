import pytest

from stelvio.cli import _parse_template_string


class TestParseTemplateString:
    """Tests for _parse_template_string function."""

    @pytest.mark.parametrize(
        "template, expected",
        [
            ("base", ("stelviodev", "templates", "main", "base")),
            ("example/dir", ("stelviodev", "templates", "main", "example/dir")),
            ("gh:owner/repo@branch/subdirectory", ("owner", "repo", "branch", "subdirectory")),
            ("gh:owner/repo", ("owner", "repo", "main", None)),
            ("gh:owner/repo@branch", ("owner", "repo", "branch", None)),
            ("gh:owner/repo/subdirectory", ("owner", "repo", "main", "subdirectory")),
            ("gh:owner/repo@main/sub/directory", ("owner", "repo", "main", "sub/directory")),
            ("gh:owner/repo/sub/directory", ("owner", "repo", "main", "sub/directory")),
        ],
    )
    def test_valid_templates(self, template, expected):
        owner, repo, branch, subdirectory = _parse_template_string(template)
        assert (owner, repo, branch, subdirectory) == expected

    @pytest.mark.parametrize("template", ["gh:owner", "gh:"])
    def test_invalid_templates(self, template):
        with pytest.raises(ValueError, match="Invalid template format"):
            _parse_template_string(template)
