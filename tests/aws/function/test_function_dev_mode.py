"""`stlv dev` runs every handler in one interpreter through `Function.handle_bridge_event`.
These tests drive that seam with handler files written into the project copy, the way the
dev server does after `stack.up`."""

import json
import os
import re
import sys
from pathlib import Path

import pulumi
from pytest import fixture, mark, param

from stelvio.aws.api_gateway import RestApi
from stelvio.aws.api_gateway.rest_api.config import CorsConfig
from stelvio.aws.function import Function
from stelvio.link import Link

pytestmark = mark.usefixtures("project_cwd", "pulumi_mocks", "dev_mode_context")


@fixture(autouse=True)
def _forget_handler_modules(project_cwd):
    """Handlers import by bare name (`handler`, `utils`). Dev evicts only modules under the
    current project root, and every test here has its own root, so drop this test's."""
    yield
    root = str(project_cwd)
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None) or next(
            iter(getattr(module, "__path__", None) or []), None
        )
        if name == "stlv_resources" or (path and path.startswith(root)):
            del sys.modules[name]


HANDLER_OK = "def main(event, context):\n    return {}\n"
LAMBDA_CONTEXT = {
    "invoke_id": "invoke-1",
    "client_context": None,
    "cognito_identity": None,
    "epoch_deadline_time_in_ms": 0,
}


def write_files(project: Path, files: dict[str, str]) -> None:
    for relative_path, source in files.items():
        path = project / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)


async def invoke(function: Function) -> object:
    event = {
        "endpointId": function._dev_endpoint_id,
        "event": {"path": "/", "httpMethod": "GET"},
        "context": LAMBDA_CONTEXT,
    }
    return await function.handle_bridge_event({"type": "data", "event": json.dumps(event)})


async def invoke_ok(function: Function) -> dict:
    result = await invoke(function)
    assert result.error_result is None
    return result.success_result


def table_link(name: str) -> Link:
    return Link("table", properties={"table_name": name}, permissions=[])


@pulumi.runtime.test
def test_dev_mode_each_call_sees_its_own_function(project_cwd):
    """Two folders with the same link name, CORS origin and a same-named `utils` module:
    the second function called must not get the first one's cached modules, and an edited
    helper must load on the next call."""
    handler = (
        "import utils\n"
        "from stlv_resources import Resources\n"
        "def main(event, context):\n"
        "    return {'statusCode': 200, 'body': {'table': Resources.table.table_name,"
        " 'origin': Resources.cors.allow_origin, 'utils': utils.WHO}}\n"
    )
    write_files(
        project_cwd,
        {
            "functions/a/handler.py": handler,
            "functions/a/utils.py": "WHO = 'a-v1'\n",
            "functions/b/handler.py": handler,
            "functions/b/utils.py": "WHO = 'b-v1'\n",
        },
    )
    functions = {}
    for folder in ("a", "b"):
        fn = Function(
            f"fn-{folder}",
            handler=f"functions/{folder}::handler.main",
            links=[table_link(f"table-{folder}")],
        )
        RestApi(f"api-{folder}", cors=CorsConfig(allow_origins=f"https://{folder}.example")).route(
            "GET", "/", fn
        )
        _ = fn.resources
        functions[folder] = fn

    async def check():
        result_a = await invoke_ok(functions["a"])
        result_b = await invoke_ok(functions["b"])
        assert result_a["body"] == {
            "table": "table-a",
            "origin": "https://a.example",
            "utils": "a-v1",
        }
        assert result_b["body"] == {
            "table": "table-b",
            "origin": "https://b.example",
            "utils": "b-v1",
        }
        # same size and mtime as b-v1: a pyc would pass its mtime-and-size check, only a
        # load from source can tell
        helper = project_cwd / "functions/b/utils.py"
        mtime_ns = helper.stat().st_mtime_ns
        helper.write_text("WHO = 'b-v2'\n")
        os.utime(helper, ns=(mtime_ns, mtime_ns))
        edited = await invoke_ok(functions["b"])
        assert edited["body"]["utils"] == "b-v2"

    return check()


@pulumi.runtime.test
def test_dev_mode_evicts_project_modules_under_an_interpreter_above_the_project(
    project_cwd, monkeypatch
):
    """A system interpreter (`/usr` over `/usr/src/app`) puts the whole project under
    `sys.prefix`; that must not shield the project's own modules from eviction."""
    monkeypatch.setattr(sys, "prefix", str(project_cwd.parent))
    write_files(
        project_cwd,
        {
            "functions/app/handler.py": (
                "import utils\n"
                "def main(event, context):\n"
                "    return {'statusCode': 200, 'body': utils.WHO}\n"
            ),
            "functions/app/utils.py": "WHO = 'v1'\n",
        },
    )
    fn = Function("fn", handler="functions/app::handler.main")
    _ = fn.resources

    async def check():
        assert (await invoke_ok(fn))["body"] == "v1"
        (project_cwd / "functions/app/utils.py").write_text("WHO = 'v2-edited'\n")
        assert (await invoke_ok(fn))["body"] == "v2-edited"

    return check()


@pulumi.runtime.test
def test_dev_mode_nested_handler_imports_from_the_folder_root(project_cwd):
    """Lambda imports `sub.handler` from the zip root, so the handler finds stlv_resources
    and the folder's packages from there, not from its own directory."""
    write_files(
        project_cwd,
        {
            "functions/nested/sub/handler.py": (
                "from stlv_resources import Resources\n"
                "from sub import helper\n"
                "def main(event, context):\n"
                "    return {'statusCode': 200,"
                " 'body': [Resources.table.table_name, helper.WHO, __name__]}\n"
            ),
            "functions/nested/sub/helper.py": "WHO = 'helper'\n",
        },
    )
    fn = Function("fn", handler="functions/nested::sub/handler.main", links=[table_link("t")])
    _ = fn.resources

    async def check():
        result = await invoke_ok(fn)
        assert result["body"] == ["t", "helper", "sub.handler"]

    return check()


@pulumi.runtime.test
def test_dev_mode_keeps_installed_packages_between_calls(project_cwd, monkeypatch):
    """The venv usually sits inside the project; its modules survive the per-call eviction
    (numpy cannot load twice), so module state persists across calls like on a warm Lambda."""
    write_files(
        project_cwd,
        {
            ".venv/pkg.py": "import itertools\nCALLS = itertools.count()\n",
            "functions/h.py": "import pkg\n" + HANDLER_OK.replace("{}", "{'n': next(pkg.CALLS)}"),
        },
    )
    monkeypatch.setattr(sys, "prefix", str(project_cwd / ".venv"))
    monkeypatch.syspath_prepend(str(project_cwd / ".venv"))
    fn = Function("fn", handler="functions/h.main")
    _ = fn.resources

    async def check():
        assert await invoke_ok(fn) == {"n": 0}
        assert await invoke_ok(fn) == {"n": 1}

    return check()


@pulumi.runtime.test
def test_dev_mode_single_file_handler_dataclass_under_future_annotations(project_cwd):
    """dataclasses resolve string annotations through `sys.modules[cls.__module__]`, so the
    single-file handler module must be registered under its name before it runs."""
    write_files(
        project_cwd,
        {
            "functions/dc.py": (
                "from __future__ import annotations\n"
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Item:\n    a: int\n"
                "def main(event, context):\n    return {'a': Item(1).a}\n"
            )
        },
    )
    fn = Function("fn", handler="functions/dc.main")
    _ = fn.resources

    async def check():
        assert await invoke_ok(fn) == {"a": 1}

    return check()


@pulumi.runtime.test
def test_dev_mode_function_without_links_has_no_resources_module(project_cwd):
    """The on-disk stlv_resources.py is the folder's union for the IDE. A Lambda without
    links or CORS ships no copy, so its import fails there; dev fails the same way instead
    of serving the sibling's file."""
    write_files(
        project_cwd,
        {
            "functions/shared/linked.py": "def main(event, context):\n    return {}\n",
            "functions/shared/plain.py": (
                "from stlv_resources import Resources\n"
                "def main(event, context):\n    return {'statusCode': 200}\n"
            ),
        },
    )
    linked = Function("linked", handler="functions/shared::linked.main", links=[table_link("t")])
    plain = Function("plain", handler="functions/shared::plain.main")
    _ = linked.resources, plain.resources
    assert (project_cwd / "functions/shared/stlv_resources.py").exists()

    async def check():
        result = await invoke(plain)
        assert type(result.error_result) is ModuleNotFoundError
        assert result.error_result.name == "stlv_resources"
        assert result.success_result is None

    return check()


@mark.parametrize(
    ("handler", "source", "error", "match"),
    [
        param(
            "functions/err::handler.main",
            "def main(:\n",
            SyntaxError,
            "invalid syntax",
            id="syntax_error",
        ),
        param(
            "functions/err::handler.main",
            "import sys\nsys.exit(3)\n",
            SystemExit,
            r"^3$",
            id="exit_at_import",
        ),
        param(
            "functions/err::handler.main",
            "import sys\ndef main(event, context):\n    sys.exit(3)\n",
            SystemExit,
            r"^3$",
            id="exit_in_handler",
        ),
        param(
            "functions/solo.main",
            "import simple2\n" + HANDLER_OK,
            ModuleNotFoundError,
            "No module named 'simple2'",
            id="single_file_sibling_import",
        ),
        param(
            "functions/json.main",
            HANDLER_OK,
            ImportError,
            "already imported module 'json'",
            id="single_file_named_like_an_imported_module",
        ),
        param(
            "functions/err::json.main",
            HANDLER_OK,
            AttributeError,
            "Handler function 'main' not found",
            id="folder_file_named_like_an_imported_module",
        ),
    ],
)
@pulumi.runtime.test
def test_dev_mode_handler_failures_become_error_results(
    project_cwd, handler, source, error, match
):
    """An import-time error or `sys.exit()` is reported to the caller like any handler
    exception; the dev server keeps running. A single-file handler has nothing on sys.path,
    so importing a sibling file fails as it does on Lambda. A handler file named like a
    module the process already imported (`json.py`): the single-file one is refused, the
    folder one resolves to the stdlib module and reports the handler missing, as on Lambda."""
    fn = Function("fn", handler=handler)
    write_files(project_cwd, {fn.config.full_handler_python_path: source})
    _ = fn.resources

    async def check():
        result = await invoke(fn)
        assert type(result.error_result) is error
        assert re.search(match, str(result.error_result))
        assert result.success_result is None

    return check()
