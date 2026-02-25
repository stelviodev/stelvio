"""Tests for schema/code file reading helpers."""

from unittest.mock import patch

import pytest

from stelvio.aws.appsync.appsync import _read_schema_or_inline
from stelvio.aws.appsync.resolver import _read_code_or_inline


@pytest.fixture
def project_root(tmp_path):
    with (
        patch("stelvio.aws.appsync.appsync.get_project_root", return_value=tmp_path),
        patch("stelvio.aws.appsync.resolver.get_project_root", return_value=tmp_path),
    ):
        yield tmp_path


class TestReadSchemaOrInline:
    def test_inline_sdl_returned_as_is(self, project_root):
        sdl = "type Query { hello: String }"
        assert _read_schema_or_inline(sdl) == sdl

    def test_multiline_inline_sdl_returned_as_is(self, project_root):
        sdl = "type Query {\n  hello: String\n}"
        assert _read_schema_or_inline(sdl) == sdl

    def test_graphql_extension_reads_file(self, project_root):
        schema_file = project_root / "schema.graphql"
        schema_file.write_text("type Query { hello: String }")
        assert _read_schema_or_inline("schema.graphql") == "type Query { hello: String }"

    def test_gql_extension_reads_file(self, project_root):
        schema_file = project_root / "schema.gql"
        schema_file.write_text("type Query { hi: String }")
        assert _read_schema_or_inline("schema.gql") == "type Query { hi: String }"

    def test_graphql_file_not_found_raises(self, project_root):
        with pytest.raises(FileNotFoundError, match=r"missing\.graphql"):
            _read_schema_or_inline("missing.graphql")

    def test_gql_file_not_found_raises(self, project_root):
        with pytest.raises(FileNotFoundError, match=r"missing\.gql"):
            _read_schema_or_inline("missing.gql")

    def test_nested_path_reads_file(self, project_root):
        subdir = project_root / "schemas"
        subdir.mkdir()
        schema_file = subdir / "api.graphql"
        schema_file.write_text("type Mutation { create: ID }")
        assert _read_schema_or_inline("schemas/api.graphql") == "type Mutation { create: ID }"


class TestReadCodeOrInline:
    def test_inline_code_returned_as_is(self, project_root):
        code = "export function request(ctx) { return {}; }"
        assert _read_code_or_inline(code) == code

    def test_multiline_inline_code_returned_as_is(self, project_root):
        code = "export function request(ctx) {\n  return {};\n}"
        assert _read_code_or_inline(code) == code

    def test_js_extension_reads_file(self, project_root):
        code_file = project_root / "resolver.js"
        code_file.write_text("export function request(ctx) { return {}; }")
        assert _read_code_or_inline("resolver.js") == "export function request(ctx) { return {}; }"

    def test_js_file_not_found_raises(self, project_root):
        with pytest.raises(FileNotFoundError, match=r"missing\.js"):
            _read_code_or_inline("missing.js")

    def test_nested_path_reads_file(self, project_root):
        subdir = project_root / "resolvers"
        subdir.mkdir()
        code_file = subdir / "query.js"
        code_file.write_text("// resolver code")
        assert _read_code_or_inline("resolvers/query.js") == "// resolver code"

    def test_codegen_output_treated_as_inline(self, project_root):
        """Code from codegen helpers (which contain newlines) should be treated as inline."""
        from stelvio.aws.appsync.codegen import dynamo_get

        code = dynamo_get("id")
        assert _read_code_or_inline(code) == code
