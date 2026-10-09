"""Docs code examples import names that exist. Nothing in the docs runs; a rename that
forgets the docs fails here."""

import importlib
import re
from collections.abc import Iterator
from pathlib import Path

from pytest import mark, param

DOCS = Path(__file__).parent.parent / "docs" / "docs"
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# Indented fences sit in admonitions and content tabs.
PYTHON_FENCE = re.compile(
    r"^[ \t]*```(?:python|py)\b[^\n]*\n(.*?)^[ \t]*```", re.DOTALL | re.MULTILINE
)
STELVIO_IMPORT = re.compile(r"^[ \t]*from (stelvio[\w.]*) import (\([^)]*\)|[^\n]+)", re.MULTILINE)


def _page_text(page: Path) -> str:
    # Commented-out drafts (a component not shipped yet) aren't rendered; blank them but keep
    # their newlines so line numbers still match the file.
    text = page.read_text()
    return HTML_COMMENT.sub(lambda m: "\n" * m.group().count("\n"), text)


def _imports() -> Iterator:
    # The changelog keeps old names on purpose.
    for page in sorted(p for p in DOCS.rglob("*.md") if p.name != "changelog.md"):
        text = _page_text(page)
        for fence in PYTHON_FENCE.finditer(text):
            # Comments go first: a `)` in one would end a parenthesised import early.
            code = re.sub(r"#[^\n]*", "", fence.group(1))
            for imp in STELVIO_IMPORT.finditer(code):
                line = text.count("\n", 0, fence.start(1)) + code.count("\n", 0, imp.start()) + 1
                for entry in imp.group(2).strip("()").split(","):
                    name = entry.split(" as ")[0].strip()
                    if name:
                        location = f"{page.relative_to(DOCS)}:{line}"
                        yield param(imp.group(1), name, id=f"{location}:{name}")


@mark.parametrize(("module", "name"), list(_imports()))
def test_docs_import_resolves(module, name):
    imported = importlib.import_module(module)
    if not hasattr(imported, name):
        importlib.import_module(f"{module}.{name}")  # a submodule, `from stelvio.aws import s3`
